"use strict";

// DualStream public per-camera viewer.
//
// Detects which camera to show from the URL path (/cam0 or /cam1) and
// negotiates a one-track WebRTC session against /api/public/offer. The
// viewer access key is read from "?key=<token>" once on page load; the
// page does not store it in localStorage (the shareable URL is the
// canonical form) but does keep it in memory across Start/Stop cycles.

const els = {
  start: document.getElementById("start-btn"),
  stop: document.getElementById("stop-btn"),
  fullscreen: document.getElementById("fullscreen-btn"),
  brand: document.getElementById("brand-name"),
  label: document.getElementById("cam-label"),
  snapshotMeta: document.getElementById("snapshot-meta"),
  video: document.getElementById("video"),
  stage: document.getElementById("cam-stage"),
  overlay: document.getElementById("overlay"),
  overlayMsg: document.getElementById("overlay-message"),
};

const state = {
  cameraNum: null,
  key: null,
  pc: null,
  siteName: "HEDGEWORK @ PS 20",
  cameraName: null,
  // Holds {url, timestamp, filename} of the most recent snapshot we've
  // fetched. Used to drive the <video> poster so the page shows a still
  // image when the live stream isn't running.
  lastSnapshot: null,
  snapshotTimer: null,
};

// Poll interval for the latest-snapshot endpoint. 30 s comfortably exceeds
// the typical snapshot cadence (default 300 s) so we'll always be fresh
// without hammering the server.
const SNAPSHOT_POLL_MS = 30_000;

function parseCameraNum() {
  const m = location.pathname.match(/\/cam(\d+)/);
  return m ? parseInt(m[1], 10) : null;
}

function parseKey() {
  const params = new URLSearchParams(location.search);
  const k = (params.get("key") || "").trim();
  return k || null;
}

function showOverlay(message, kind = "info") {
  els.overlay.classList.remove("hidden", "info", "warn", "error");
  els.overlay.classList.add(kind);
  els.overlayMsg.textContent = message;
}

function hideOverlay() {
  els.overlay.classList.add("hidden");
}

async function startStreaming() {
  if (state.pc) return;
  if (state.cameraNum == null) {
    showOverlay("Unknown camera in URL.", "error");
    return;
  }
  if (!state.key) {
    showOverlay("Access key required. Open the shareable URL.", "error");
    return;
  }
  els.start.disabled = true;
  showOverlay("Connecting…", "info");

  const pc = new RTCPeerConnection({ iceServers: [] });
  state.pc = pc;
  els.video.srcObject = null;

  pc.addEventListener("track", (event) => {
    // Wrap in a fresh MediaStream for the same reason as the dual-tile
    // UI: aiortc reuses one msid across all tracks on a PC.
    els.video.srcObject = new MediaStream([event.track]);
  });

  pc.addEventListener("iceconnectionstatechange", () => {
    if (["failed", "disconnected", "closed"].includes(pc.iceConnectionState)) {
      showOverlay(`Disconnected (${pc.iceConnectionState}).`, "warn");
      teardown();
    }
  });
  pc.addEventListener("connectionstatechange", () => {
    if (pc.connectionState === "connected") {
      hideOverlay();
    } else if (["failed", "closed"].includes(pc.connectionState)) {
      showOverlay(`Connection ${pc.connectionState}.`, "warn");
      teardown();
    }
  });

  pc.addTransceiver("video", { direction: "recvonly" });

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await iceGatheringComplete(pc);

    const resp = await fetch(`/api/public/offer?key=${encodeURIComponent(state.key)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
        camera: state.cameraNum,
      }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`HTTP ${resp.status}: ${text}`);
    }
    const answer = await resp.json();
    await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    els.stop.disabled = false;
  } catch (err) {
    showOverlay(`Failed to start: ${err.message}`, "error");
    teardown();
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
    setTimeout(() => resolve(), 2000);
  });
}

function teardown() {
  if (state.pc) {
    try { state.pc.close(); } catch (_) { /* noop */ }
    state.pc = null;
  }
  els.video.srcObject = null;
  // Re-evaluate the poster so the still image reappears after a session.
  // Some browsers don't auto-show the poster after a video element has
  // played media and had its srcObject cleared; load() forces it.
  try { els.video.load(); } catch (_) { /* noop */ }
  els.start.disabled = false;
  els.stop.disabled = true;
}

async function fetchBranding() {
  if (!state.key) return;
  try {
    const resp = await fetch(
      `/api/public/info?key=${encodeURIComponent(state.key)}`,
      { cache: "no-store" },
    );
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.site_name) state.siteName = data.site_name;
    const cam = (data.cameras || []).find(
      (c) => c.camera_num === state.cameraNum,
    );
    if (cam && cam.display_name) {
      state.cameraName = cam.display_name;
    }
    applyBranding();
  } catch (_) {
    // Best-effort; default branding stays in place.
  }
}

function applyBranding() {
  if (state.cameraName == null) {
    state.cameraName = `Camera ${state.cameraNum}`;
  }
  els.brand.textContent = state.siteName;
  els.label.textContent = state.cameraName;
  document.title = `${state.siteName} · ${state.cameraName}`;
}

async function refreshLatestSnapshot() {
  if (!state.key || state.cameraNum == null) return;
  try {
    const url =
      `/api/public/snapshots/latest` +
      `?camera=${state.cameraNum}&key=${encodeURIComponent(state.key)}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();
    state.lastSnapshot = data;
    applySnapshotPoster();
    if (data.url && data.timestamp) {
      const date = new Date(data.timestamp * 1000);
      els.snapshotMeta.textContent = `· last snapshot ${formatTime(date)}`;
      els.snapshotMeta.title = date.toLocaleString();
    } else {
      els.snapshotMeta.textContent = "· no snapshots yet";
      els.snapshotMeta.title = "";
    }
  } catch (_) {
    // Best-effort. Failing snapshot poll shouldn't disturb the page.
  }
}

function applySnapshotPoster() {
  if (state.lastSnapshot && state.lastSnapshot.url) {
    // Snapshot URLs include a timestamped path, so the URL itself changes
    // when a new snapshot arrives; no cache-busting needed.
    if (els.video.poster !== state.lastSnapshot.url) {
      els.video.poster = state.lastSnapshot.url;
    }
  } else {
    els.video.removeAttribute("poster");
  }
}

function formatTime(date) {
  return date.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function stop() {
  showOverlay("Stopped. Press Start to resume.", "info");
  teardown();
}

function toggleFullscreen() {
  const target = els.stage;
  if (!document.fullscreenElement) {
    if (target.requestFullscreen) target.requestFullscreen();
  } else if (document.exitFullscreen) {
    document.exitFullscreen();
  }
}

function init() {
  state.cameraNum = parseCameraNum();
  state.key = parseKey();

  if (state.cameraNum == null) {
    els.label.textContent = "Camera ?";
    showOverlay("This URL does not specify a camera.", "error");
    els.start.disabled = true;
    return;
  }
  // Apply provisional branding immediately so the page doesn't flash
  // "DualStream · Camera ?" before the /api/public/info fetch lands.
  applyBranding();

  if (!state.key) {
    showOverlay(
      "Access key required. Open the shareable URL that includes ?key=…",
      "error",
    );
    els.start.disabled = true;
    return;
  }

  showOverlay("Press Start to begin streaming.", "info");
  els.start.addEventListener("click", startStreaming);
  els.stop.addEventListener("click", stop);
  els.fullscreen.addEventListener("click", toggleFullscreen);

  // Fetch the site / camera display names (best effort).
  fetchBranding();

  // Latest-snapshot polling: kicks in immediately so the visitor sees a
  // current still image before they (optionally) click Start. Continues
  // while streaming so the poster is up-to-date the next time they Stop.
  refreshLatestSnapshot();
  state.snapshotTimer = setInterval(refreshLatestSnapshot, SNAPSHOT_POLL_MS);
}

function cleanup() {
  if (state.snapshotTimer != null) {
    clearInterval(state.snapshotTimer);
    state.snapshotTimer = null;
  }
  teardown();
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("beforeunload", cleanup);
