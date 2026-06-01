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
  label: document.getElementById("cam-label"),
  video: document.getElementById("video"),
  stage: document.getElementById("cam-stage"),
  overlay: document.getElementById("overlay"),
  overlayMsg: document.getElementById("overlay-message"),
};

const state = {
  cameraNum: null,
  key: null,
  pc: null,
};

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
  els.start.disabled = false;
  els.stop.disabled = true;
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
  els.label.textContent = `Camera ${state.cameraNum}`;
  document.title = `DualStream · Camera ${state.cameraNum}`;

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
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("beforeunload", teardown);
