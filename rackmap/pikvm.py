"""Talking to a PiKVM: its screen, keyboard, mouse and power buttons.

PiKVM has a documented HTTP API (https://docs.pikvm.org/api/). rackmap signs in with the
X-KVMD-User / X-KVMD-Passwd headers on every request. A PiKVM's certificate is self-signed,
so rackmap remembers the certificate it saw when the KVM was set up and accepts only that one
afterwards; the password is never sent to anything else.

The live picture is PiKVM's MJPEG stream, passed through to the browser. PiKVM only runs its
video capture while someone is connected to its event WebSocket, which is also the fastest way
to send keys and mouse movement, so an open screen in rackmap holds one such connection.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
from urllib.parse import urlencode, urlsplit

TIMEOUT = 12
KEY_NAME = re.compile(r"[A-Za-z0-9]{1,24}")  # PiKVM's key names are the browser's: KeyA, Enter, ControlLeft…
MOUSE_BUTTONS = ("left", "right", "middle")
POWER_ACTIONS = {
    # friendly name -> (PiKVM endpoint, parameter, value)
    "press": ("click", "button", "power"),
    "hold": ("click", "button", "power_long"),
    "reset": ("click", "button", "reset"),
}


class KvmError(Exception):
    """The KVM could not be reached or refused; the message is shown to the person."""


def _target(url: str) -> tuple[bool, str, int]:
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    return secure, parts.hostname or "", parts.port or (443 if secure else 80)


def learn_certificate(url: str) -> str:
    """The certificate the KVM presents right now, as PEM ("" for a plain-HTTP address).
    Nothing is sent to the KVM beyond the TLS greeting."""
    secure, host, port = _target(url)
    if not secure:
        return ""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
            return ssl.DER_cert_to_PEM_cert(tls.getpeercert(True))
    except (OSError, ssl.SSLError) as exc:
        raise KvmError("The KVM didn't answer. Check its address and that rackmap can reach it.") from exc


def pinned_context(cert_pem: str) -> ssl.SSLContext:
    """A TLS setup that trusts exactly one certificate: the remembered one."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # identity is the certificate itself, not the name in it
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=cert_pem)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return context


CHANGED = ("The KVM's certificate isn't the one rackmap remembered (it changed, or it has expired). "
           "Re-enter the password under Edit KVM to accept the current one.")


class Client:
    def __init__(self, url: str, username: str, password: str, cert_pem: str):
        self.secure, self.host, self.port = _target(url)
        self.cert_pem = cert_pem
        self._headers = {"X-KVMD-User": username, "X-KVMD-Passwd": password}

    def _context(self) -> ssl.SSLContext | None:
        if not self.secure:
            return None
        if not self.cert_pem:
            raise KvmError(CHANGED)
        return pinned_context(self.cert_pem)

    def _connect(self, timeout: float = TIMEOUT) -> http.client.HTTPConnection:
        if self.secure:
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self._context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.connect()
        except ssl.SSLCertVerificationError as exc:
            raise KvmError(CHANGED) from exc
        except (OSError, ssl.SSLError) as exc:
            raise KvmError("The KVM didn't answer. Check its address and that rackmap can reach it.") from exc
        return conn

    @staticmethod
    def _refusal(status: int) -> None:
        if status in (401, 403):
            raise KvmError("The KVM refused that name and password.")
        if status == 404:
            raise KvmError("The KVM doesn't offer that. Is this address really a PiKVM?")
        if status == 503:
            raise KvmError("The KVM has no picture to give right now.")
        if status >= 400:
            raise KvmError(f"The KVM answered with an error ({status}).")

    def request(self, method: str, path: str, params: dict | None = None, body: bytes | None = None) -> tuple[str, bytes]:
        """One request; returns (content type, body)."""
        if params:
            path += "?" + urlencode(params)
        conn = self._connect()
        try:
            headers = dict(self._headers)
            if body is not None:
                headers["Content-Type"] = "text/plain; charset=utf-8"
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read(8_000_000)
        except (OSError, http.client.HTTPException) as exc:
            raise KvmError("The KVM stopped answering part-way through.") from exc
        finally:
            conn.close()
        self._refusal(response.status)
        return response.getheader("Content-Type") or "", raw

    def _result(self, method: str, path: str, params: dict | None = None, body: bytes | None = None) -> dict:
        _kind, raw = self.request(method, path, params, body)
        try:
            return (json.loads(raw) if raw.strip() else {}).get("result") or {}
        except (ValueError, AttributeError):
            raise KvmError("The KVM's answer wasn't in the expected format. Is this address really a PiKVM?") from None

    # ---- what rackmap uses ----

    def check(self) -> None:
        """Raises unless the name and password are accepted."""
        self.request("GET", "/api/auth/check")

    def snapshot(self) -> bytes:
        kind, raw = self.request("GET", "/api/streamer/snapshot", {"allow_offline": 1})
        if not kind.startswith("image/"):
            raise KvmError("The KVM has no picture to give right now.")
        return raw

    def open_stream(self, patience: float = 8.0) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """The live MJPEG stream. The caller reads the response and closes the connection.

        A PiKVM starts capturing video only when someone connects to its event WebSocket, and
        takes a second or two to do so; until then the stream address answers with an error.
        So a refusal is retried for a short while before it counts."""
        deadline = time.monotonic() + patience
        while True:
            try:
                return self._open_stream_once()
            except KvmError as exc:
                if "name and password" in str(exc) or "certificate" in str(exc) or time.monotonic() + 0.6 > deadline:
                    raise
                time.sleep(0.6)

    def _open_stream_once(self) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = self._connect(timeout=30)
        try:
            conn.request("GET", "/streamer/stream", headers=self._headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise KvmError("The KVM stopped answering part-way through.") from exc
        if response.status != 200:
            conn.close()
            self._refusal(response.status)
            raise KvmError(f"The KVM answered with an error ({response.status}).")
        return conn, response

    def power_state(self) -> dict:
        state = self._result("GET", "/api/atx")
        leds = state.get("leds") or {}
        return {"wired": bool(state.get("enabled")), "on": bool(leds.get("power")), "busy": bool(state.get("busy"))}

    def power(self, action: str, switch_port: int | None = None) -> None:
        """Press the machine's power or reset button through the KVM's ATX wiring. With a
        multi-port PiKVM Switch, `switch_port` (counted from 0) says whose buttons."""
        if action not in POWER_ACTIONS:
            raise KvmError("That isn't a power action rackmap knows.")
        endpoint, name, value = POWER_ACTIONS[action]
        if switch_port is None:
            self._result("POST", f"/api/atx/{endpoint}", {name: value})
        else:
            self._result("POST", f"/api/switch/atx/{endpoint}", {"port": switch_port, name: value})

    def select_port(self, switch_port: int) -> None:
        """Point a multi-port PiKVM Switch at one machine (ports counted from 0)."""
        self._result("POST", "/api/switch/set_active", {"port": switch_port})

    def type_text(self, text: str) -> None:
        self._result("POST", "/api/hid/print", {"limit": 0}, text.encode())

    async def events(self):
        """An open connection to the KVM's event WebSocket."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus, WebSocketException

        host = f"[{self.host}]" if ":" in self.host else self.host
        uri = f"{'wss' if self.secure else 'ws'}://{host}:{self.port}/api/ws"
        try:
            return await connect(uri, ssl=self._context(), additional_headers=self._headers, open_timeout=TIMEOUT, max_size=4_000_000)
        except ssl.SSLCertVerificationError as exc:
            raise KvmError(CHANGED) from exc
        except InvalidStatus as exc:
            self._refusal(exc.response.status_code)
            raise KvmError("The KVM wouldn't open its control connection.") from exc
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise KvmError("The KVM didn't answer. Check its address and that rackmap can reach it.") from exc


def _clamp(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def translate(message: dict) -> list[dict]:
    """Turn one message from rackmap's screen page into PiKVM events. Unknown or malformed
    input becomes nothing: what reaches the KVM is only ever one of these shapes."""
    kind = message.get("t")
    if kind == "key":
        code = message.get("code")
        if isinstance(code, str) and KEY_NAME.fullmatch(code):
            return [{"event_type": "key", "event": {"key": code, "state": bool(message.get("down"))}}]
    elif kind == "move":
        try:
            x, y = float(message.get("x")), float(message.get("y"))
        except (TypeError, ValueError):
            return []
        # PiKVM's absolute pointer: 0,0 is the centre of the screen
        return [{"event_type": "mouse_move", "event": {"to": {"x": _clamp(x * 65535 - 32768, -32768, 32767), "y": _clamp(y * 65535 - 32768, -32768, 32767)}}}]
    elif kind == "button":
        if message.get("b") in MOUSE_BUTTONS:
            return [{"event_type": "mouse_button", "event": {"button": message["b"], "state": bool(message.get("down"))}}]
    elif kind == "wheel":
        return [{"event_type": "mouse_wheel", "event": {"delta": {"x": 0, "y": _clamp(message.get("dy"), -127, 127)}}}]
    elif kind == "shortcut":
        keys = message.get("keys")
        if isinstance(keys, list) and 1 <= len(keys) <= 6 and all(isinstance(k, str) and KEY_NAME.fullmatch(k) for k in keys):
            return ([{"event_type": "key", "event": {"key": k, "state": True}} for k in keys]
                    + [{"event_type": "key", "event": {"key": k, "state": False}} for k in reversed(keys)])
    return []


def summarize(event: dict) -> dict | None:
    """What the screen page wants to know from PiKVM's stream of state events."""
    kind, body = event.get("event_type"), event.get("event") or {}
    if not isinstance(body, dict):
        return None
    # current PiKVM software calls these "atx" and "streamer"; older releases "atx_state" and "streamer_state"
    if kind in ("atx", "atx_state"):
        leds = body.get("leds")
        if isinstance(leds, dict) and "power" in leds:
            return {"state": "power", "wired": bool(body.get("enabled", True)), "on": bool(leds["power"])}
    if kind in ("streamer", "streamer_state"):
        source = ((body.get("streamer") or {}).get("source")) or {}
        if "online" in source:
            return {"state": "signal", "online": bool(source["online"])}
    return None
