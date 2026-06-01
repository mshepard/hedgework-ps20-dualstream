"use strict";

// DualStream web UI (Phase 1).
// - Token is stored in localStorage and sent as Bearer on /api/* calls.
// - WebRTC: one RTCPeerConnection with two recvonly video transceivers,
//   one per camera. Receiver tracks are routed to the two <video> elements
//   in the order their associated transceivers were added.

const els = {
  token: document.getElementById("token-input"),
  start: document.getElementById("start-btn"),
  stop: document.getElementById("stop-btn"),
  snapshot: document.getElementById("snapshot-btn"),
  mode: document.getElementById("mode-badge"),
  viewers: document.getElementById("viewer-count"),
  video0: document.getElementById("video-0"),
  video1: document.getElementById("video-1"),
  cam0Meta: document.getElementById("cam0-meta"),
  cam1Meta: document.getElementById("cam1-meta"),
  strip: document.getElementById("snapshot-strip"),
  log: document.getElementById("log"),
};

const state = {
  pc: null,
  cameras: [0, 1],
  // Stable mapping from track receiver to <video> element by transceiver index.
  videoSlots: [els.video0, els.video1],
  statusTimer: null,
  snapshotTimer: null,
};

const TOKEN_KEY = "dualstream.token";

function log(msg, level = "info") {
  const ts = new Date().toLocaleTimeString();
  const entry = document.createElement("div");
  entry.className = `entry ${level === "info" ? "" : level}`;
  entry.textContent = `[${ts}] ${msg}`;
  els.log.prepend(entry);
  while (els.log.childElementCount > 80) {
    els.log.removeChild(els.log.lastChild);
  }
}

function getToken() {
  return els.token.value.trim();
}

function setToken(value) {
  els.token.value = value;
  if (value) {
    localStorage.setItem(TOKEN_KEY, value);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, { ...options, headers });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  const ct = resp.headers.get("Content-Type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const mode = (data.mode || "UNKNOWN").toString();
    els.mode.textContent = mode;
    els.mode.className = `mode-badge mode-${mode.toLowerCase()}`;
    els.viewers.textContent = `${data.viewers || 0} viewer${data.viewers === 1 ? "" : "s"}`;
    if (Array.isArray(data.cameras)) {
      for (const cam of data.cameras) {
        const meta = cam.camera_num === 0 ? els.cam0Meta : els.cam1Meta;
        if (!meta) continue;
        const res = cam.resolution ? `${cam.resolution[0]}x${cam.resolution[1]}` : "--";
        const running = cam.running ? "running" : "idle";
        meta.textContent = `${res}@${cam.framerate}fps · ${cam.bitrate_kbps}kbps · ${running}`;
      }
    }
  } catch (err) {
    log(`status: ${err.message}`, "warn");
  }
}

async function refreshSnapshots() {
  try {
    const data = await api("/api/snapshots?limit=24");
    renderSnapshots(data.snapshots || []);
  } catch (err) {
    // Don't spam the log if auth isn't set yet.
    if (!String(err.message).startsWith("401")) {
      log(`snapshots: ${err.message}`, "warn");
    }
  }
}

function renderSnapshots(items) {
  els.strip.innerHTML = "";
  if (items.length === 0) {
    const empty = document.createElement("span");
    empty.className = "empty";
    empty.textContent = "No snapshots yet.";
    els.strip.appendChild(empty);
    return;
  }
  for (const item of items) {
    const a = document.createElement("a");
    a.href = item.url;
    a.target = "_blank";
    a.rel = "noopener";
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = item.url;
    img.alt = `camera ${item.camera}`;
    const cap = document.createElement("span");
    cap.className = "caption";
    const ts = new Date(item.timestamp * 1000);
    cap.textContent = `c${item.camera} ${ts.toLocaleTimeString()}`;
    a.appendChild(img);
    a.appendChild(cap);
    els.strip.appendChild(a);
  }
}

async function startStreaming() {
  if (state.pc) return;
  if (!getToken()) {
    log("Set a bearer token first.", "err");
    return;
  }
  els.start.disabled = true;
  log("Starting WebRTC session…");

  const pc = new RTCPeerConnection({ iceServers: [] });
  state.pc = pc;
  // Reset video sinks
  els.video0.srcObject = null;
  els.video1.srcObject = null;

  // We add two recvonly transceivers in the order [camera 0, camera 1].
  // The server adds tracks in the order it receives them in body.cameras.
  // Track events fire in transceiver order, so we route by index.
  let trackIndex = 0;
  pc.addEventListener("track", (event) => {
    const slot = state.videoSlots[trackIndex];
    if (slot) {
      slot.srcObject = event.streams[0] || new MediaStream([event.track]);
    }
    trackIndex += 1;
  });

  pc.addEventListener("iceconnectionstatechange", () => {
    log(`ICE: ${pc.iceConnectionState}`);
    if (["failed", "closed", "disconnected"].includes(pc.iceConnectionState)) {
      teardown();
    }
  });
  pc.addEventListener("connectionstatechange", () => {
    log(`PC: ${pc.connectionState}`);
    if (["failed", "closed"].includes(pc.connectionState)) {
      teardown();
    }
  });

  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("video", { direction: "recvonly" });

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    // Wait for ICE gathering to settle (host candidates only, fast).
    await iceGatheringComplete(pc);

    const answer = await api("/api/offer", {
      method: "POST",
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
        cameras: state.cameras,
      }),
    });
    await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    log(`Session established (pc_id=${answer.pc_id})`, "ok");
    els.stop.disabled = false;
  } catch (err) {
    log(`Failed to start: ${err.message}`, "err");
    teardown();
    els.start.disabled = false;
  }
}

function iceGatheringComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", check);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", check);
    // Hard timeout: don't block forever on a stuck local candidate gather.
    setTimeout(() => resolve(), 2000);
  });
}

function teardown() {
  if (state.pc) {
    try { state.pc.close(); } catch (_) { /* noop */ }
    state.pc = null;
  }
  els.video0.srcObject = null;
  els.video1.srcObject = null;
  els.start.disabled = false;
  els.stop.disabled = true;
}

async function manualSnapshot() {
  els.snapshot.disabled = true;
  try {
    await api("/api/snapshot", { method: "POST" });
    log("Snapshot captured", "ok");
    await refreshSnapshots();
  } catch (err) {
    log(`snapshot: ${err.message}`, "err");
  } finally {
    els.snapshot.disabled = false;
  }
}

function init() {
  const stored = localStorage.getItem(TOKEN_KEY);
  if (stored) els.token.value = stored;
  els.token.addEventListener("change", () => setToken(getToken()));
  els.token.addEventListener("blur", () => setToken(getToken()));

  els.start.addEventListener("click", startStreaming);
  els.stop.addEventListener("click", teardown);
  els.snapshot.addEventListener("click", manualSnapshot);

  refreshStatus();
  refreshSnapshots();
  state.statusTimer = setInterval(refreshStatus, 5000);
  state.snapshotTimer = setInterval(refreshSnapshots, 30000);
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("beforeunload", teardown);
