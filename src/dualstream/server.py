"""aiohttp server: static UI, WebRTC signaling, snapshots API.

Two distinct surfaces:

  * **Admin (dual-tile diagnostic UI)** — requires the admin bearer token on
    every API call. This is the page at "/" with the activity log, manual
    snapshot button, and side-by-side camera tiles.

  * **Public per-camera pages** — single shareable URL per camera at
    /cam0 and /cam1. Authenticated by an opaque "viewer_token" passed as a
    "?key=<token>" query parameter (or, equivalently, a Bearer header). The
    page has just the video plus Start/Stop/Fullscreen controls.

Endpoints:

  Static / unauthenticated:
    GET  /                         -> admin dual-tile UI (HTML only; the API
                                       calls it makes still require admin
                                       bearer token)
    GET  /cam0, /cam1              -> public per-camera viewer HTML
    GET  /static/<file>            -> UI assets
    GET  /snapshots/<path>         -> static snapshot JPEG files
    GET  /health                   -> liveness probe

  Admin (admin auth_token required, Bearer header):
    GET  /api/status               -> JSON status + viewer share URLs
    POST /api/offer                -> WebRTC SDP exchange (multi-camera)
    POST /api/snapshot             -> trigger a manual snapshot
    GET  /api/snapshots            -> recent snapshot index JSON

  Public (viewer_token OR admin auth_token; Bearer or ?key= accepted):
    POST /api/public/offer                -> WebRTC SDP exchange (single camera)
    GET  /api/public/snapshots/latest     -> most recent snapshot URL + ts
                                              for a given camera (so the
                                              public per-camera pages can
                                              show a still poster when the
                                              live stream isn't running)
    GET  /api/public/info                 -> site_name + per-camera display
                                              names for branding the public
                                              pages

Phase 2 will fold the real power-mode state machine into /api/status, add
/api/admin/force_mode for testing, and gate the offer endpoints on mode.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from dualstream import __version__
from dualstream.cameras import Camera, CameraManager
from dualstream.config import AppConfig
from dualstream.snapshots import SnapshotWorker
from dualstream.tracks import CameraTrack

logger = logging.getLogger("dualstream.server")


WEBUI_DIR = Path(__file__).resolve().parent / "webui"

# Paths that bypass auth entirely. Static UI must be reachable anonymously
# so the browser can load the page (which then sends the appropriate token
# on subsequent API calls). Embedded snapshot <img> tags can't carry
# Authorization headers either.
UNAUTH_PREFIXES = ("/static/", "/snapshots/")
UNAUTH_EXACT = {"/", "/health", "/cam0", "/cam1"}

# Prefix for endpoints that accept the viewer token (or admin token) via
# Bearer header OR ?key= query parameter. Everything else requires admin
# auth via Bearer header only.
PUBLIC_API_PREFIX = "/api/public/"


def _extract_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        if token:
            return token
    key = request.query.get("key", "").strip()
    return key or None


def _make_auth_middleware(admin_token: str, viewer_token: str):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if path in UNAUTH_EXACT or any(path.startswith(p) for p in UNAUTH_PREFIXES):
            return await handler(request)

        if path.startswith(PUBLIC_API_PREFIX):
            if not viewer_token:
                return web.json_response(
                    {"error": "public endpoints disabled (viewer_token not set)"},
                    status=503,
                )
            provided = _extract_token(request)
            if provided is None:
                return web.json_response(
                    {"error": "missing access key"}, status=401
                )
            if provided != viewer_token and provided != admin_token:
                return web.json_response(
                    {"error": "invalid access key"}, status=401
                )
            return await handler(request)

        # Default: admin-only, bearer header only. The admin token isn't
        # acceptable via ?key= to keep it out of URLs / browser history /
        # access logs.
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "missing bearer token"}, status=401)
        provided = auth[len("Bearer "):].strip()
        if provided != admin_token:
            return web.json_response({"error": "invalid token"}, status=401)
        return await handler(request)

    return auth_middleware


class DualStreamServer:
    def __init__(
        self,
        config: AppConfig,
        cameras: CameraManager,
        snapshots: SnapshotWorker,
    ) -> None:
        self.config = config
        self.cameras = cameras
        self.snapshots = snapshots
        self._pcs: set[RTCPeerConnection] = set()
        self._pc_holds: dict[RTCPeerConnection, list[Camera]] = {}
        self._pc_lock = asyncio.Lock()

    def build(self) -> web.Application:
        # Ensure the snapshots directory exists before aiohttp validates it.
        self.snapshots.base_path.mkdir(parents=True, exist_ok=True)

        app = web.Application(
            middlewares=[
                _make_auth_middleware(
                    self.config.server.auth_token,
                    self.config.server.viewer_token,
                )
            ]
        )
        # Public / unauthenticated HTML pages.
        app.router.add_get("/", self._index)
        app.router.add_get("/cam0", self._camera_page)
        app.router.add_get("/cam1", self._camera_page)
        app.router.add_get("/health", self._health)

        # Admin API.
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/offer", self._offer)
        app.router.add_post("/api/snapshot", self._snapshot_now)
        app.router.add_get("/api/snapshots", self._snapshots_list)

        # Public per-camera API.
        app.router.add_post("/api/public/offer", self._public_offer)
        app.router.add_get(
            "/api/public/snapshots/latest", self._public_latest_snapshot
        )
        app.router.add_get("/api/public/info", self._public_info)

        # Static assets.
        app.router.add_static("/snapshots", self.snapshots.base_path, show_index=False)
        app.router.add_static("/static", WEBUI_DIR)

        app.on_shutdown.append(self._on_shutdown)
        return app

    # ---------- HTML routes ----------

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def _camera_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "cam.html")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    # ---------- Admin API ----------

    async def _status(self, request: web.Request) -> web.Response:
        viewer_token_set = bool(self.config.server.viewer_token)
        return web.json_response(
            {
                "version": __version__,
                # Phase 2 will replace this stub with the real state machine.
                "mode": "FULL",
                "viewers": len(self._pcs),
                "viewer_token_set": viewer_token_set,
                # Relative share URLs (so the browser uses whatever host /
                # scheme the admin loaded the page from). Empty list when
                # public access is disabled.
                "viewer_share_urls": (
                    [
                        {
                            "camera_num": cam.camera_num,
                            "path": f"/cam{cam.camera_num}?key={self.config.server.viewer_token}",
                        }
                        for cam in self.cameras.all()
                    ]
                    if viewer_token_set
                    else []
                ),
                "cameras": [
                    {
                        "camera_num": cam.camera_num,
                        "running": cam.running,
                        "refcount": cam.refcount,
                        "resolution": [cam.quality.width, cam.quality.height],
                        "framerate": cam.quality.framerate,
                        "bitrate_kbps": cam.quality.bitrate_kbps,
                    }
                    for cam in self.cameras.all()
                ],
            }
        )

    async def _offer(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            sdp = body["sdp"]
            type_ = body["type"]
        except KeyError as ex:
            return web.json_response({"error": f"missing field: {ex}"}, status=400)

        requested = body.get("cameras", self.cameras.numbers())
        try:
            requested_nums = [int(n) for n in requested]
        except (TypeError, ValueError):
            return web.json_response({"error": "cameras must be a list of ints"}, status=400)

        unknown = [n for n in requested_nums if n not in self.cameras.numbers()]
        if unknown:
            return web.json_response({"error": f"unknown camera(s): {unknown}"}, status=400)

        return await self._negotiate_video_offer(requested_nums, sdp, type_)

    # ---------- Public API ----------

    async def _public_offer(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)
        try:
            sdp = body["sdp"]
            type_ = body["type"]
            camera = int(body["camera"])
        except (KeyError, TypeError, ValueError) as ex:
            return web.json_response(
                {"error": f"missing or invalid field: {ex}"}, status=400
            )
        if camera not in self.cameras.numbers():
            return web.json_response(
                {"error": f"unknown camera: {camera}"}, status=400
            )
        return await self._negotiate_video_offer([camera], sdp, type_)

    def _camera_display_name(self, camera_num: int) -> str:
        """Resolve the human-friendly label for a camera, falling back to
        "Camera N" when no name is configured."""
        cfg = getattr(self.config, f"camera{camera_num}", None)
        if cfg is not None and getattr(cfg, "name", ""):
            return cfg.name
        return f"Camera {camera_num}"

    async def _public_info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "site_name": self.config.server.site_name or "HEDGEWORK @ PS 20",
                "cameras": [
                    {
                        "camera_num": cam.camera_num,
                        "display_name": self._camera_display_name(cam.camera_num),
                    }
                    for cam in self.cameras.all()
                ],
            }
        )

    async def _public_latest_snapshot(self, request: web.Request) -> web.Response:
        raw = request.query.get("camera")
        if raw is None:
            return web.json_response(
                {"error": "camera query parameter required"}, status=400
            )
        try:
            camera = int(raw)
        except ValueError:
            return web.json_response(
                {"error": "camera must be an int"}, status=400
            )
        if camera not in self.cameras.numbers():
            return web.json_response(
                {"error": f"unknown camera: {camera}"}, status=400
            )
        items = self.snapshots.list_snapshots(camera_num=camera, limit=1)
        if not items:
            return web.json_response(
                {"camera": camera, "url": None, "timestamp": None}
            )
        item = items[0]
        return web.json_response(
            {
                "camera": camera,
                "url": item["url"],
                "timestamp": item["timestamp"],
                "filename": item["filename"],
            }
        )

    # ---------- Shared offer/negotiation pipeline ----------

    async def _negotiate_video_offer(
        self,
        requested_nums: list[int],
        sdp: str,
        type_: str,
    ) -> web.Response:
        cameras = [self.cameras.get(n) for n in requested_nums]

        # Acquire all requested cameras up-front so the tracks have running
        # devices on first recv(). If any acquire fails, release the rest.
        acquired: list[Camera] = []
        try:
            for cam in cameras:
                await cam.acquire()
                acquired.append(cam)
        except Exception:
            logger.exception("Camera acquire failed")
            for cam in reversed(acquired):
                try:
                    await cam.release()
                except Exception:
                    logger.exception("Release after acquire failure")
            return web.json_response({"error": "camera unavailable"}, status=503)

        pc_id = uuid.uuid4().hex[:8]
        # No STUN/TURN: over Tailscale the host candidate is reachable directly.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        async with self._pc_lock:
            self._pcs.add(pc)
            self._pc_holds[pc] = acquired

        log = logger.getChild(pc_id)
        log.info("New offer: cameras=%s", requested_nums)

        @pc.on("iceconnectionstatechange")
        async def _on_ice_state() -> None:
            log.info("ICE state -> %s", pc.iceConnectionState)
            if pc.iceConnectionState in ("failed", "closed"):
                await self._close_pc(pc)

        @pc.on("connectionstatechange")
        async def _on_conn_state() -> None:
            log.info("PC state -> %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self._close_pc(pc)

        try:
            # setRemoteDescription first so the offer's m-lines create
            # transceivers in the order the browser intended; then bind one
            # CameraTrack to each video transceiver in that same order via
            # replaceTrack. This is much more reliable than calling addTrack
            # before setRemoteDescription, which leaves it to aiortc's merge
            # logic to decide which track maps to which m-line.
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))

            video_txs = [t for t in pc.getTransceivers() if t.kind == "video"]
            if len(video_txs) != len(cameras):
                log.warning(
                    "transceiver/camera count mismatch: %d video m-lines, %d cameras requested",
                    len(video_txs),
                    len(cameras),
                )

            track_map: list[dict] = []
            for tx, cam in zip(video_txs, cameras):
                track = CameraTrack(cam, label=f"camera{cam.camera_num}")
                tx.sender.replaceTrack(track)
                # We only send media; the browser side is recvonly.
                tx.direction = "sendonly"
                track_map.append({"mid": tx.mid, "camera_num": cam.camera_num})

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
        except Exception:
            log.exception("Failed to negotiate offer")
            await self._close_pc(pc)
            return web.json_response({"error": "negotiation failed"}, status=500)

        log.info("Answer ready: tracks=%s", track_map)
        return web.json_response(
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "pc_id": pc_id,
                "cameras": requested_nums,
                # tracks[i] tells the browser which camera_num is on mid=tracks[i].mid
                "tracks": track_map,
            }
        )

    async def _close_pc(self, pc: RTCPeerConnection) -> None:
        async with self._pc_lock:
            if pc not in self._pcs:
                return
            self._pcs.discard(pc)
            holds = self._pc_holds.pop(pc, [])
        try:
            await pc.close()
        except Exception:
            logger.exception("Error closing PC")
        for cam in holds:
            try:
                await cam.release()
            except Exception:
                logger.exception("Error releasing camera %d", cam.camera_num)

    async def _snapshot_now(self, request: web.Request) -> web.Response:
        try:
            results = await self.snapshots.capture_now()
        except Exception:
            logger.exception("Manual snapshot failed")
            return web.json_response({"error": "snapshot failed"}, status=500)
        return web.json_response(
            {
                "saved": {
                    str(cam_num): {
                        "filename": p.name,
                        "url": f"/snapshots/{p.relative_to(self.snapshots.base_path).as_posix()}",
                    }
                    for cam_num, p in results.items()
                }
            }
        )

    async def _snapshots_list(self, request: web.Request) -> web.Response:
        camera = request.query.get("camera")
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 500))
        except ValueError:
            return web.json_response({"error": "limit must be an int"}, status=400)
        camera_num: int | None
        if camera is None:
            camera_num = None
        else:
            try:
                camera_num = int(camera)
            except ValueError:
                return web.json_response({"error": "camera must be an int"}, status=400)
        items = self.snapshots.list_snapshots(camera_num=camera_num, limit=limit)
        return web.json_response({"snapshots": items})

    async def _on_shutdown(self, app: web.Application) -> None:
        logger.info("Server shutting down: closing %d PC(s)", len(self._pcs))
        for pc in list(self._pcs):
            await self._close_pc(pc)
