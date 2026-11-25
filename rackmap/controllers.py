"""Management controllers: the small computer inside a server (Dell iDRAC, HPE iLO, Supermicro
BMC and others) that can report on it and switch it on and off while the server itself is down.

rackmap talks to them with Redfish, the common standard, over HTTPS. Controller certificates are
almost always self-signed, so instead of trusting a certificate authority rackmap remembers the
certificate it saw when the controller was added and refuses a different one afterwards.

Two extras hang off the same record: a text console (the controller's serial-over-SSH feature,
shown in the same terminal as console-server ports) and the address of a network KVM, for
equipment whose controller has no remote screen.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time

from . import addresses, consoles, db, secrets_store

KINDS = {"redfish": "Redfish (Dell iDRAC, HPE iLO, Supermicro, Lenovo XCC…)"}
TIMEOUT = 15
SSH_PORT = 22  # where a controller offers its text console
# Friendly action -> Redfish reset types, in order of preference.
POWER_ACTIONS = {
    "on": ["On"],
    "shutdown": ["GracefulShutdown"],
    "restart": ["GracefulRestart", "ForceRestart"],
    "off": ["ForceOff"],
}


class Invalid(ValueError):
    """Message is written for the person using the app."""


class ControllerError(Exception):
    """The controller could not be reached or refused; the message is shown to the person."""


# ---------- talking Redfish ----------

def _context(legacy: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # identity is checked against the remembered certificate instead
    if legacy:  # controllers from the early 2010s only speak old TLS
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
        context.minimum_version = ssl.TLSVersion.TLSv1
    return context


class Redfish:
    def __init__(self, host: str, username: str, password: str, pinned: str | None, port: int = 443):
        self.host, self.port, self.pinned = host, port, pinned
        self.fingerprint: str | None = None
        self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._conn: http.client.HTTPSConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _open(self) -> http.client.HTTPSConnection:
        last: Exception | None = None
        for legacy in (False, True):
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT, context=_context(legacy))
            try:
                conn.connect()
            except ssl.SSLError as exc:
                last = exc
                continue
            except (OSError, socket.timeout) as exc:
                raise ControllerError("The controller didn't answer. Check its address and that rackmap can reach it.") from exc
            self.fingerprint = hashlib.sha256(conn.sock.getpeercert(True)).hexdigest()
            if self.pinned and self.fingerprint != self.pinned:
                conn.close()
                raise ControllerError("The controller's certificate has changed since it was added. If you replaced or reset it, "
                                      "re-enter its password under Edit to accept the new certificate.")
            return conn
        raise ControllerError("Couldn't make a secure connection to the controller.") from last

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        """One request. Reads share a connection: making a new secure connection for every
        question is what makes older controllers take half a minute to answer. Anything that
        changes the server always gets a connection of its own and is never sent twice."""
        headers = {"Accept": "application/json", "Authorization": self._auth}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        reading = method == "GET"
        if not reading:
            self.close()
        for attempt in (1, 2):
            reused = self._conn is not None
            conn = self._conn or self._open()  # the password is only sent after the certificate check above
            self._conn = None
            try:
                conn.request(method, path, body=data, headers=headers)
                response = conn.getresponse()
                raw = response.read(2_000_000)
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if reading and reused and attempt == 1:
                    continue  # the controller had hung up on the shared connection; ask again on a new one
                raise ControllerError("The controller stopped answering part-way through.") from exc
            if reading and not response.will_close:
                self._conn = conn
            else:
                conn.close()
            break
        if response.status in (401, 403):
            raise ControllerError("The controller refused that name and password.")
        if response.status == 404:
            raise ControllerError("The controller doesn't offer that (it may not support Redfish, or only part of it).")
        if response.status >= 400:
            raise ControllerError(f"The controller answered with an error ({response.status}).")
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ControllerError("The controller's answer wasn't in the expected format. It may not support Redfish.") from None

    def system_path(self) -> str:
        members = self.request("GET", "/redfish/v1/Systems").get("Members") or []
        if not members:
            raise ControllerError("The controller didn't list a server to manage.")
        return members[0]["@odata.id"]

    def summary(self) -> dict:
        try:
            return self._summary()
        finally:
            self.close()

    def _summary(self) -> dict:
        path = self.system_path()
        system = self.request("GET", path)
        reset = (system.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        allowed = reset.get("ResetType@Redfish.AllowableValues") or []
        out = {
            "power_state": system.get("PowerState") or "Unknown",
            "health": ((system.get("Status") or {}).get("HealthRollup") or (system.get("Status") or {}).get("Health") or ""),
            "manufacturer": (system.get("Manufacturer") or "").strip(),
            "model": (system.get("Model") or "").strip(),
            "serial": (system.get("SKU") or system.get("SerialNumber") or "").strip(),
            "bios": (system.get("BiosVersion") or "").strip(),
            "memory_gib": (system.get("MemorySummary") or {}).get("TotalSystemMemoryGiB"),
            "processors": " × ".join(str(x) for x in ((system.get("ProcessorSummary") or {}).get("Count"),
                                                         (system.get("ProcessorSummary") or {}).get("Model")) if x),
            "actions": [name for name, kinds in POWER_ACTIONS.items() if any(k in allowed for k in kinds)] if allowed else list(POWER_ACTIONS),
            "temperatures": [], "watts": None, "fans": None, "power_supplies": [],
        }
        try:  # sensors are a nicety; many controllers differ here
            chassis = (self.request("GET", "/redfish/v1/Chassis").get("Members") or [{}])[0].get("@odata.id")
            if chassis:
                thermal = self.request("GET", chassis + "/Thermal")
                temps = [(t.get("Name") or "", t.get("ReadingCelsius")) for t in thermal.get("Temperatures") or [] if t.get("ReadingCelsius") is not None]
                out["temperatures"] = [{"name": n, "celsius": c} for n, c in temps[:6]]
                out["fans"] = len(thermal.get("Fans") or []) or None
                power = self.request("GET", chassis + "/Power")
                watts = [p.get("PowerConsumedWatts") for p in power.get("PowerControl") or [] if p.get("PowerConsumedWatts") is not None]
                out["watts"] = watts[0] if watts else None
                out["power_supplies"] = [{"name": p.get("Name") or "", "health": (p.get("Status") or {}).get("Health") or ""}
                                         for p in power.get("PowerSupplies") or []]
        except (ControllerError, KeyError, TypeError, IndexError):
            pass
        return out

    def power(self, action: str) -> str:
        if action not in POWER_ACTIONS:
            raise Invalid("That isn't a power action rackmap knows.")
        try:
            path = self.system_path()
            system = self.request("GET", path)
        finally:
            self.close()
        reset = (system.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        target = reset.get("target") or path + "/Actions/ComputerSystem.Reset"
        allowed = reset.get("ResetType@Redfish.AllowableValues") or []
        choice = next((k for k in POWER_ACTIONS[action] if not allowed or k in allowed), None)
        if choice is None:
            raise ControllerError("This controller doesn't offer that power action.")
        self.request("POST", target, {"ResetType": choice})
        return choice


# ---------- records ----------

def _row(device_id: int):
    rows = db.query("SELECT * FROM controllers WHERE device_id = ?", (device_id,))
    return rows[0] if rows else None


def get(device_id: int) -> dict | None:
    r = _row(device_id)
    if not r:
        return None
    return {"kind": r["kind"], "kind_name": KINDS.get(r["kind"], r["kind"]), "host": r["host"], "username": r["username"],
            "console_command": r["console_command"], "kvm_url": r["kvm_url"], "has_console": bool(r["console_command"])}


def configured_ids() -> set[int]:
    return {r["device_id"] for r in db.query("SELECT device_id FROM controllers")}


def _client(r, password: str | None = None, pinned: str | None = "saved") -> Redfish:
    secret = password if password is not None else secrets_store.open_sealed(r["secret"])
    return Redfish(r["host"], r["username"], secret, r["cert_sha256"] if pinned == "saved" else pinned)


def _need(device_id: int):
    r = _row(device_id)
    if not r:
        raise Invalid("This equipment has no management controller connected.")
    return r


def save(device_id: int, values: dict) -> dict:
    """Connect or update a controller. rackmap signs in first and saves nothing unless that works;
    that is also when the controller's certificate is remembered."""
    if not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    kind = values.get("kind") or "redfish"
    if kind not in KINDS:
        raise Invalid("That kind of controller isn't supported yet.")
    host = str(values.get("host") or "").strip()
    username = str(values.get("username") or "").strip()
    console_command = str(values.get("console_command") or "").strip()
    kvm_url = str(values.get("kvm_url") or "").strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
        raise Invalid("Enter the controller's address, for example 192.168.1.60.")
    if addresses.is_public_ip(host):
        raise Invalid("The controller's address must be on a private network.")
    if not username:
        raise Invalid("Enter the account rackmap signs in with.")
    if console_command and not re.fullmatch(r"[A-Za-z0-9 _./:\-]{1,80}", console_command):
        raise Invalid("The console command can only contain letters, numbers, spaces and . _ / : -")
    if kvm_url and not kvm_url.lower().startswith(("http://", "https://")):
        raise Invalid("The KVM address must start with http:// or https://.")
    current = _row(device_id)
    password = values.get("password") or ""
    if not password:
        if current is None:
            raise Invalid("Enter the password for that account.")
        password = secrets_store.open_sealed(current["secret"])
    relearn = current is None or current["host"] != host or bool(values.get("password"))
    client = Redfish(host, username, password, None if relearn else current["cert_sha256"])
    try:
        client.summary()
    except ControllerError as exc:
        raise Invalid(str(exc)) from None
    row = (kind, host, username, secrets_store.seal(password), client.fingerprint, console_command, kvm_url)
    if current is None:
        db.execute("INSERT INTO controllers (kind, host, username, secret, cert_sha256, console_command, kvm_url, device_id) VALUES (?,?,?,?,?,?,?,?)",
                   row + (device_id,))
    else:
        # a new address or re-entered password also forgets the remembered SSH key for the text console
        ssh_key = "" if relearn else current["ssh_host_key"]
        db.execute("UPDATE controllers SET kind=?, host=?, username=?, secret=?, cert_sha256=?, console_command=?, kvm_url=?, ssh_host_key=? WHERE device_id=?",
                   row + (ssh_key, device_id))
    forget(device_id)
    return get(device_id)


def remove(device_id: int) -> None:
    db.execute("DELETE FROM controllers WHERE device_id = ?", (device_id,))
    forget(device_id)


# The last answer from each controller, kept in memory so the page has something to show at once
# while a fresh answer (seconds on a good day, much longer on old hardware) is on its way.
_last: dict[int, tuple[float, dict]] = {}
_last_lock = threading.Lock()


def forget(device_id: int) -> None:
    with _last_lock:
        _last.pop(device_id, None)


def last_status(device_id: int) -> dict | None:
    """What the controller said last time, with how long ago that was, or None."""
    _need(device_id)
    with _last_lock:
        kept = _last.get(device_id)
    if not kept:
        return None
    return {**kept[1], "age_seconds": int(time.monotonic() - kept[0])}


def status(device_id: int) -> dict:
    try:
        answer = _client(_need(device_id)).summary()
    except ValueError as exc:  # unreadable saved password
        raise ControllerError(f"{exc} Re-enter the controller's password under Edit.") from None
    with _last_lock:
        _last[device_id] = (time.monotonic(), answer)
    return {**answer, "age_seconds": 0}


def power(device_id: int, action: str) -> str:
    sent = _client(_need(device_id)).power(action)
    forget(device_id)  # what we knew about its power state is no longer true
    return sent


async def open_console(device_id: int, cols: int = 80, rows: int = 24) -> consoles.Session:
    """The controller's text console (serial over SSH), as a terminal session."""
    r = _row(device_id)
    if not r or not r["console_command"]:
        raise consoles.ConsoleError("No text console is set up for this equipment.")
    try:
        password = secrets_store.open_sealed(r["secret"])
    except ValueError as exc:
        raise consoles.ConsoleError(f"{exc} Re-enter the controller's password under Edit.") from None
    conn = await consoles._connect(r["host"], SSH_PORT, r["username"], password, r["ssh_host_key"] or None)
    if not r["ssh_host_key"]:  # first use: remember the controller's SSH identity
        db.execute("UPDATE controllers SET ssh_host_key = ? WHERE device_id = ?",
                   (conn.get_server_host_key().export_public_key("openssh").decode().strip(), device_id))
    try:
        # Controllers run their console command from their own command line, not as an SSH
        # "run this" request, so open the command line, let its greeting finish, then type it.
        process = await conn.create_process(term_type="xterm", term_size=(cols, rows), encoding=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 8
        got_any = False
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=1.0 if got_any else 4.0)
            except asyncio.TimeoutError:
                break  # quiet: the prompt is waiting
            if not chunk:
                raise consoles.ConsoleError("The controller closed the connection before its command line appeared.")
            got_any = True
        process.stdin.write(r["console_command"].encode() + b"\r")
    except consoles.ConsoleError:
        conn.close()
        raise
    except Exception as exc:  # noqa: BLE001 - any failure here means the console could not start
        conn.close()
        raise consoles.ConsoleError(f"Couldn't start the text console ({type(exc).__name__}).") from None
    return consoles.Session(conn, process, b"")
