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
  brandName: document.getElementById("brand-name"),
  video0: document.getElementById("video-0"),
  video1: document.getElementById("video-1"),
  cam0Name: document.getElementById("cam0-name"),
  cam1Name: document.getElementById("cam1-name"),
  cam0Meta: document.getElementById("cam0-meta"),
  cam1Meta: document.getElementById("cam1-meta"),
  cam0Share: document.getElementById("cam0-share"),
  cam1Share: document.getElementById("cam1-share"),
  strip: document.getElementById("snapshot-strip"),
  log: document.getElementById("log"),
};

const DEFAULT_SITE_NAME = "HEDGEWORK @ PS 20";

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
    applyBranding(data.site_name);
    if (Array.isArray(data.cameras)) {
      for (const cam of data.cameras) {
        const nameSlot = cam.camera_num === 0 ? els.cam0Name : els.cam1Name;
        const meta = cam.camera_num === 0 ? els.cam0Meta : els.cam1Meta;
        if (nameSlot && cam.display_name) {
          nameSlot.textContent = cam.display_name;
        }
        if (!meta) continue;
        const res = cam.resolution ? `${cam.resolution[0]}x${cam.resolution[1]}` : "--";
        const running = cam.running ? "running" : "idle";
        meta.textContent = `${res}@${cam.framerate}fps · ${cam.bitrate_kbps}kbps · ${running}`;
      }
    }
    updateShareLinks(data.viewer_share_urls || [], data.viewer_token_set);
  } catch (err) {
    log(`status: ${err.message}`, "warn");
  }
}

function applyBranding(siteName) {
  const name = (siteName || "").trim() || DEFAULT_SITE_NAME;
  if (els.brandName && els.brandName.textContent !== name) {
    els.brandName.textContent = name;
  }
  const desiredTitle = `${name} · Admin`;
  if (document.title !== desiredTitle) {
    document.title = desiredTitle;
  }
}

function updateShareLinks(urls, tokenSet) {
  const slots = { 0: els.cam0Share, 1: els.cam1Share };
  // Default state: everything disabled.
  for (const el of Object.values(slots)) {
    if (!el) continue;
    el.classList.add("disabled");
    el.removeAttribute("href");
    el.title = tokenSet === false
      ? "viewer_token not set in /etc/dualstream/dualstream.toml"
      : "Open single-camera view in a new tab";
  }
  for (const item of urls) {
    const el = slots[item.camera_num];
    if (!el || !item.path) continue;
    el.href = item.path;
    el.classList.remove("disabled");
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

  // Route each incoming track to a <video> by its transceiver's position in
  // pc.getTransceivers(), which matches the order we added them in below
  // ([camera 0, camera 1]). This is more reliable than incrementing an
  // index on track-event arrival, which the WebRTC spec doesn't strictly
  // order across implementations.
  //
  // We deliberately wrap each track in a *fresh* MediaStream rather than
  // reusing event.streams[0]. aiortc emits a single msid for the whole PC,
  // so both incoming tracks belong to the same MediaStream object; if we
  // assigned that shared stream to both <video> elements, each <video>
  // would only play its first video track and both tiles would show the
  // same camera. A unique MediaStream per <video> guarantees independent
  // playback.
  pc.addEventListener("track", (event) => {
    const transceivers = pc.getTransceivers();
    const idx = transceivers.indexOf(event.transceiver);
    const slot = idx >= 0 ? state.videoSlots[idx] : null;
    if (slot) {
      slot.srcObject = new MediaStream([event.track]);
      log(`Track bound to slot ${idx} (mid=${event.transceiver.mid || "?"})`);
    } else {
      log(`Track had no slot (idx=${idx})`, "warn");
    }
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
    if (Array.isArray(answer.tracks)) {
      const mapping = answer.tracks
        .map((t) => `mid=${t.mid} -> cam${t.camera_num}`)
        .join(", ");
      log(`Session established (pc_id=${answer.pc_id}) [${mapping}]`, "ok");
    } else {
      log(`Session established (pc_id=${answer.pc_id})`, "ok");
    }
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
