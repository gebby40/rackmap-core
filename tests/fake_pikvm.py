"""A stand-in PiKVM for tests: HTTPS with a self-signed certificate, the parts of the API rackmap uses."""

import asyncio
import json
import socket
import tempfile
import threading
import time

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from fake_redfish import _certificate

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")  # not a real picture; enough to pass along


class FakePiKVM:
    def __init__(self, username="admin", password="kvm-secret"):
        self.username, self.password = username, password
        self.events: list[dict] = []      # what arrived on the event WebSocket
        self.calls: list[tuple] = []      # (path, query) of the HTTP calls that change something
        self.typed: list[str] = []
        self.power_on = True
        self.viewers = 0
        self.stream_tries = 0
        self.stream_refusals = 0
        self.port = None
        self._dir = tempfile.mkdtemp(prefix="fake-pikvm-")
        self._server = None

    def _ok(self, request) -> bool:
        return request.headers.get("x-kvmd-user") == self.username and request.headers.get("x-kvmd-passwd") == self.password

    def _app(self) -> FastAPI:
        app, outer = FastAPI(), self

        @app.middleware("http")
        async def auth(request: Request, call_next):
            if not outer._ok(request):
                return JSONResponse({"ok": False}, status_code=403 if request.headers.get("x-kvmd-user") else 401)
            return await call_next(request)

        @app.get("/api/auth/check")
        def check():
            return {"ok": True, "result": {}}

        @app.get("/api/atx")
        def atx():
            return {"ok": True, "result": {"busy": False, "enabled": True, "leds": {"hdd": False, "power": outer.power_on}}}

        @app.post("/api/atx/click")
        def click(button: str):
            outer.calls.append(("/api/atx/click", button))
            if button in ("power", "power_long"):
                outer.power_on = not outer.power_on
            return {"ok": True, "result": {}}

        @app.post("/api/switch/atx/click")
        def switch_click(port: int, button: str):
            outer.calls.append(("/api/switch/atx/click", port, button))
            return {"ok": True, "result": {}}

        @app.post("/api/switch/set_active")
        def set_active(port: int):
            outer.calls.append(("/api/switch/set_active", port))
            return {"ok": True, "result": {}}

        @app.post("/api/hid/print")
        async def hid_print(request: Request):
            outer.typed.append((await request.body()).decode())
            return {"ok": True, "result": {}}

        @app.get("/api/streamer/snapshot")
        def snapshot():
            return Response(JPEG, media_type="image/jpeg")

        @app.get("/streamer/stream")
        def stream():
            outer.stream_tries += 1
            if outer.stream_tries <= outer.stream_refusals:
                return JSONResponse({"ok": False}, status_code=502)  # like a real one whose capture hasn't started yet
            def frames():
                for _ in range(3):
                    yield b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(JPEG) + JPEG + b"\r\n"
            return StreamingResponse(frames(), media_type="multipart/x-mixed-replace;boundary=boundarydonotcross")

        @app.websocket("/api/ws")
        async def ws(websocket: WebSocket):
            if not outer._ok(websocket):
                await websocket.close(code=4403)
                return
            await websocket.accept()
            outer.viewers += 1
            try:
                await websocket.send_text(json.dumps({"event_type": "atx", "event": {"enabled": True, "busy": False, "acts": {"power": False, "reset": False}, "leds": {"power": outer.power_on, "hdd": False}}}))
                await websocket.send_text(json.dumps({"event_type": "streamer", "event": {"params": {}, "streamer": None}}))  # before capture starts
                await websocket.send_text(json.dumps({"event_type": "streamer", "event": {"streamer": {"source": {"online": True, "resolution": {"width": 1024, "height": 768}}}}}))
                await websocket.send_text(json.dumps({"event_type": "loop", "event": {}}))
                while True:
                    outer.events.append(json.loads(await websocket.receive_text()))
            except Exception:  # noqa: BLE001 - the client went away
                pass
            finally:
                outer.viewers -= 1

        return app

    def start(self):
        cert = _certificate(self._dir)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(self._app(), host="127.0.0.1", port=self.port, log_level="error", ssl_certfile=cert, ssl_keyfile=cert, ws="websockets")
        self._server = uvicorn.Server(config)
        threading.Thread(target=lambda: asyncio.run(self._server.serve()), daemon=True).start()
        for _ in range(100):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake PiKVM did not start")

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        time.sleep(0.2)
