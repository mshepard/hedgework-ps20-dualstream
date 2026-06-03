"use strict";

// DualStream public per-camera viewer — snapshot streaming.
//
// This page intentionally does NOT use WebRTC. WebRTC media is UDP, and
// the typical deployment for this project (Raspberry Pi on solar power
// behind an industrial LTE SIM, optionally reached via Tailscale
// Funnel) has at least one network hop that won't carry sustained UDP
// reliably. Instead we poll the existing /api/public/snapshots/latest
// endpoint and swap the <img> on each fresh frame. That goes entirely
// over HTTPS/TCP, so the page works over the tailnet, over Funnel, and
// over any plain reverse proxy.
//
// Two cadences:
//   * Idle  — page open but Start hasn't been clicked. Polls every
//     IDLE_POLL_MS (matches the cam page's old poster-refresh rate).
//     The image is whatever the snapshot worker last persisted, so it
//     reflects the configured snapshots.interval_seconds (default
//     5 minutes).
//   * Active — Start clicked. Polls every ACTIVE_POLL_MS. Each poll is
//     treated by the server as a "viewer is active" signal, so the
//     snapshot worker on the Pi shortens its capture cadence to
//     snapshots.active_interval_seconds for as long as polls keep
//     arriving. Together that produces a ~1–2 s slideshow.
//
// Live WebRTC is still available on the admin dual-tile UI (at "/"),
// which is reachable on the tailnet where WebRTC works cleanly.

const els = {
  start: document.getElementById("start-btn"),
  stop: document.getElementById("stop-btn"),
  fullscreen: document.getElementById("fullscreen-btn"),
  brand: document.getElementById("brand-name"),
  label: document.getElementById("cam-label"),
  snapshotMeta: document.getElementById("snapshot-meta"),
  frame: document.getElementById("frame"),
  stage: document.getElementById("cam-stage"),
  overlay: document.getElementById("overlay"),
  overlayMsg: document.getElementById("overlay-message"),
  liveIndicator: document.getElementById("live-indicator"),
};

const state = {
  cameraNum: null,
  key: null,
  siteName: "HEDGEWORK @ PS 20",
  cameraName: null,
  // Most recent snapshot URL we've shown. We compare on each poll so we
  // can skip swapping <img>.src when the file hasn't changed (avoids
  // an unnecessary network round-trip and a brief decode flash).
  lastUrl: null,
  // Wall-clock timestamp of the most recent fresh snapshot, used to
  // render the "last snapshot HH:MM:SS" label.
  lastTimestamp: null,
  // Polling state.
  active: false,
  pollTimer: null,
  // True if we've successfully rendered at least one frame; controls
  // whether the overlay sits on top or is hidden.
  haveImage: false,
  // Consecutive <img onerror> count. A single failure is usually
  // transient (e.g. the snapshot worker happened to be mid-write when
  // we fetched the URL, before the server-side atomic-rename was in
  // place) so we don't want to throw a scary warning the moment one
  // load fails. The pill only appears once we've missed
  // OVERLAY_ERROR_THRESHOLD frames in a row, and is cleared on the
  // next successful load.
  consecutiveErrors: 0,
};

// Number of consecutive image-decode failures before we surface the
// "Snapshot failed to load. Reload the URL." overlay. At
// ACTIVE_POLL_MS = 1500 ms this corresponds to ~4.5 s of solid failure
// before the user sees anything.
const OVERLAY_ERROR_THRESHOLD = 3;

// Polling cadences. Keep these in sync with snapshots.active_interval_seconds
// and the historical 30-s poll cadence of the old cam page.
const ACTIVE_POLL_MS = 1500;
const IDLE_POLL_MS = 30_000;

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

function setLiveIndicator(active) {
  if (!els.liveIndicator) return;
  els.liveIndicator.classList.toggle("hidden", !active);
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

async function pollOnce() {
  if (!state.key || state.cameraNum == null) return;
  try {
    const url =
      `/api/public/snapshots/latest` +
      `?camera=${state.cameraNum}&key=${encodeURIComponent(state.key)}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      // 401 / 503 here would be auth or "public endpoints disabled" —
      // surface those rather than silently retrying.
      if (resp.status === 401) {
        showOverlay("Access key rejected. Check the URL.", "error");
        stopPolling();
        return;
      }
      if (resp.status === 503) {
        showOverlay(
          "Public viewing is disabled (viewer_token not set on server).",
          "warn",
        );
        stopPolling();
        return;
      }
      // Transient errors: leave the previous frame on screen and try
      // again next tick. Don't spam the overlay.
      return;
    }
    const data = await resp.json();
    if (!data.url) {
      // Service is up, but no snapshots exist yet (fresh install, or
      // the worker hasn't completed its first tick).
      showOverlay("Waiting for first snapshot…", "info");
      return;
    }
    applyFrame(data);
  } catch (_) {
    // Network blip; previous frame stays.
  }
}

function applyFrame(data) {
  // Update header timestamp first so the user sees freshness even on the
  // very first frame (before the <img> decodes).
  state.lastTimestamp = data.timestamp;
  updateSnapshotMeta();

  if (data.url === state.lastUrl) {
    // Same file as the previous poll — worker hasn't produced a new
    // frame yet. Nothing to do.
    return;
  }
  state.lastUrl = data.url;

  const img = els.frame;
  // onload runs once the new frame is decoded — that's when we want to
  // hide the "Loading…" / "Waiting…" overlay so the user never sees a
  // blank stage flash between frames.
  img.onload = () => {
    state.haveImage = true;
    state.consecutiveErrors = 0;
    hideOverlay();
  };
  img.onerror = () => {
    state.consecutiveErrors += 1;
    if (state.consecutiveErrors >= OVERLAY_ERROR_THRESHOLD) {
      // Probably a real problem: revoked token, server down, or the
      // snapshot worker producing corrupt files. Tell the user.
      showOverlay("Snapshot failed to load. Reload the URL.", "warn");
    }
    // Otherwise: stay silent — the previous frame is still showing and
    // the next poll will almost certainly succeed.
  };
  img.src = data.url;
}

function updateSnapshotMeta() {
  if (state.lastTimestamp != null) {
    const date = new Date(state.lastTimestamp * 1000);
    els.snapshotMeta.textContent = `· last snapshot ${formatTime(date)}`;
    els.snapshotMeta.title = date.toLocaleString();
  } else {
    els.snapshotMeta.textContent = "";
    els.snapshotMeta.title = "";
  }
}

function formatTime(date) {
  return date.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function startPolling() {
  if (state.active) return;
  state.active = true;
  els.start.disabled = true;
  els.stop.disabled = false;
  setLiveIndicator(true);
  if (!state.haveImage) {
    showOverlay("Loading…", "info");
  } else {
    hideOverlay();
  }
  // Schedule the first poll immediately, then on the active cadence.
  // Setting state.active first means pollOnce() can safely re-arm via
  // schedulePoll().
  pollOnce();
  schedulePoll();
}

function stopPolling() {
  state.active = false;
  els.start.disabled = false;
  els.stop.disabled = true;
  setLiveIndicator(false);
  if (state.pollTimer != null) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
  // Drop back to the slow idle cadence so the visible frame still
  // gradually refreshes for visitors who walked away from Start but
  // left the tab open. schedulePoll() keys off state.active so it
  // picks the longer delay automatically.
  schedulePoll();
}

function schedulePoll() {
  // Single timer; always reschedules itself so the page also keeps
  // refreshing on the slow idle cadence between Start clicks. The
  // delay is recomputed each round, so Start/Stop transitions take
  // effect on the next tick.
  if (state.pollTimer != null) clearTimeout(state.pollTimer);
  const delay = state.active ? ACTIVE_POLL_MS : IDLE_POLL_MS;
  state.pollTimer = setTimeout(async () => {
    state.pollTimer = null;
    await pollOnce();
    schedulePoll();
  }, delay);
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
  // the default text before /api/public/info lands.
  applyBranding();

  if (!state.key) {
    showOverlay(
      "Access key required. Open the shareable URL that includes ?key=…",
      "error",
    );
    els.start.disabled = true;
    return;
  }

  showOverlay("Loading latest snapshot…", "info");
  els.start.addEventListener("click", startPolling);
  els.stop.addEventListener("click", stopPolling);
  els.fullscreen.addEventListener("click", toggleFullscreen);

  // Fetch the site / camera display names (best effort) and kick off
  // an idle-cadence poll so visitors see a recent still even before
  // clicking Start. Clicking Start later upshifts to ACTIVE_POLL_MS.
  fetchBranding();
  pollOnce();
  schedulePoll();
}

function cleanup() {
  if (state.pollTimer != null) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

window.addEventListener("DOMContentLoaded", init);
window.addEventListener("beforeunload", cleanup);
