"""aiohttp server: static UI, WebRTC signaling, snapshots API.

Endpoints:
    GET  /                 -> static UI index (no auth)
    GET  /static/<file>    -> UI assets (no auth)
    GET  /health           -> liveness probe (no auth)
    GET  /snapshots/<path> -> static snapshot JPEG files (no auth, so <img>
                              tags can embed them; Tailscale ACL is the gate)
    GET  /api/status       -> JSON status (requires bearer token)
    POST /api/offer        -> WebRTC SDP exchange (requires bearer token)
    POST /api/snapshot     -> trigger a manual snapshot (requires bearer)
    GET  /api/snapshots    -> recent snapshot index JSON (requires bearer)

Phase 2 will fold the real power-mode state machine into /api/status,
add /api/admin/force_mode for testing, and gate /api/offer on mode.
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

# Paths that bypass the bearer-token check. Static UI must be reachable
# anonymously so the browser can load the page that prompts for the token,
# and embedded snapshot <img> tags can't carry Authorization headers.
UNAUTH_PREFIXES = ("/static/", "/snapshots/")
UNAUTH_EXACT = {"/", "/health"}


def _make_auth_middleware(token: str):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if path in UNAUTH_EXACT or any(path.startswith(p) for p in UNAUTH_PREFIXES):
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"error": "missing bearer token"}, status=401)
        provided = auth[len("Bearer "):].strip()
        if provided != token:
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
            middlewares=[_make_auth_middleware(self.config.server.auth_token)]
        )
        app.router.add_get("/", self._index)
        app.router.add_get("/health", self._health)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/offer", self._offer)
        app.router.add_post("/api/snapshot", self._snapshot_now)
        app.router.add_get("/api/snapshots", self._snapshots_list)
        app.router.add_static("/snapshots", self.snapshots.base_path, show_index=False)
        app.router.add_static("/static", WEBUI_DIR)
        app.on_shutdown.append(self._on_shutdown)
        return app

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": __version__,
                # Phase 2 will replace this stub with the real state machine.
                "mode": "FULL",
                "viewers": len(self._pcs),
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
