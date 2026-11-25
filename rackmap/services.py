"""Services: software running on a piece of equipment that rackmap can sign in to, such as
a hypervisor on a server. A service belongs to the equipment it runs on and is shown in its details.

This module is the frame: levels, the checks before anything is done, protection, the action
log. What a particular kind of service can do comes from a plug-in (see `register`).

Every connection has a level, chosen by the person who adds it, which is the most rackmap will
ever do through it:

  view     look only
  operate  also start, stop and restart what is already there
  full     everything the sign-in allows: create, delete, change settings

The level is enforced here, in `allowed`, whatever the sign-in itself could do. Certificates are
handled as for management controllers: the one seen when the connection is added is remembered
and a different one is refused afterwards.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time

from . import addresses, db, secrets_store

TIMEOUT = 15
LEVELS = {
    "view": "View only",
    "operate": "Operate: start, stop and restart",
    "full": "Full control: also consoles, snapshots and everything else the sign-in allows",
}
_RANK = {"view": 0, "operate": 1, "full": 2}


class Invalid(ValueError):
    """Message is written for the person using the app."""


class ServiceError(Exception):
    """The service could not be reached or refused; the message is shown to the person."""


class PinnedHttps:
    """HTTPS to a service with a self-signed certificate: the certificate is compared with the
    one remembered, and the sign-in is only sent after that check."""

    def __init__(self, host: str, port: int, pinned: str | None, headers: dict, what: str):
        self.host, self.port, self.pinned, self.headers, self.what = host, port, pinned, headers, what
        self.fingerprint: str | None = None
        self.certificate: bytes | None = None
        self._conn: http.client.HTTPSConnection | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _open(self) -> http.client.HTTPSConnection:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # identity is checked against the remembered certificate instead
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT, context=context)
        try:
            conn.connect()
        except ssl.SSLError as exc:
            raise ServiceError(f"Couldn't make a secure connection to {self.what}. Check the port.") from exc
        except (OSError, socket.timeout) as exc:
            raise ServiceError(f"{self.what} didn't answer. Check its address and that rackmap can reach it.") from exc
        self.certificate = conn.sock.getpeercert(True)
        self.fingerprint = hashlib.sha256(self.certificate).hexdigest()
        if self.pinned and self.fingerprint != self.pinned:
            conn.close()
            raise ServiceError(f"The certificate {self.what} shows has changed since it was connected. If you replaced or "
                               "reinstalled it, re-enter its secret under Edit to accept the new certificate.")
        return conn

    def get(self, path: str):
        for attempt in (1, 2):
            reused = self._conn is not None
            conn = self._conn or self._open()
            self._conn = None
            try:
                conn.request("GET", path, headers={"Accept": "application/json", **self.headers})
                response = conn.getresponse()
                raw = response.read(8_000_000)
            except (OSError, http.client.HTTPException) as exc:
                conn.close()
                if reused and attempt == 1:
                    continue
                raise ServiceError(f"{self.what} stopped answering part-way through.") from exc
            if not response.will_close:
                self._conn = conn
            else:
                conn.close()
            break
        if response.status == 401:
            raise ServiceError(f"{self.what} refused that sign-in. Check the account and secret.")
        if response.status == 403:
            raise ServiceError(f"{self.what} accepted the sign-in but it isn't allowed to look at this. Give it more permissions there.")
        if response.status >= 400:
            raise ServiceError(f"{self.what} answered with an error ({response.status}).")
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ServiceError(f"The answer wasn't in the format {self.what} uses. Check the address and port.") from None


def _post(self, path: str, fields: dict):
    """Something that changes the service: always a connection of its own, never sent twice."""
    import urllib.parse
    self.close()
    conn = self._open()
    try:
        conn.request("POST", path, body=urllib.parse.urlencode(fields),
                     headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", **self.headers})
        response = conn.getresponse()
        raw = response.read(1_000_000)
    except (OSError, http.client.HTTPException) as exc:
        raise ServiceError(f"{self.what} stopped answering part-way through. Check whether it was carried out before trying again.") from exc
    finally:
        conn.close()
    if response.status == 401:
        raise ServiceError(f"{self.what} refused that sign-in. Check the account and secret.")
    if response.status == 403:
        raise ServiceError(f"{self.what} says this sign-in isn't allowed to do that. Give it more permissions there.")
    if response.status >= 400:
        reason = (response.reason or "").strip()
        raise ServiceError(f"{self.what} refused ({response.status}{': ' + reason[:200] if reason else ''}).")
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}


PinnedHttps.post = _post


def addon(name: str = ""):
    """The add-ons package, or one module in it; None when it isn't installed. A fault inside
    an installed add-on is not hidden."""
    import importlib
    base = f"{__package__}.addons"
    wanted = f"{base}.{name}" if name else base
    try:
        return importlib.import_module(wanted)
    except ModuleNotFoundError as exc:
        if exc.name in (base, wanted):
            return None
        raise


_registered: dict = {}   # kinds added by register(), in the order they were added
_found: dict | None = None


def register(slug: str, module) -> None:
    """Add a kind of connection. `module` is anything with what rackmap/addons modules have:
    NAME, DEFAULT_PORT, ACCOUNT_LABEL, ACCOUNT_EXAMPLE, SECRET_LABEL, HELP, ACTIONS, offered(guest)
    and a Client(host, port, username, secret, pinned) with .fingerprint, .summary() and .act()."""
    global _found
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", slug or ""):
        raise ValueError("A kind's short name is lower-case letters, digits and underscores.")
    _registered[slug] = module
    _found = None


def _kinds() -> dict:
    """Every kind of connection this installation has. None are built in: they come from the
    add-ons folder when it is there, from installed packages that name themselves under the
    `rackmap.services` entry-point group, and from register()."""
    global _found
    if _found is None:
        found: dict = {}
        package = addon()   # here, not at the top: the kinds import this module
        if package is not None:
            found.update(package.services())
        try:
            from importlib import metadata
            for point in metadata.entry_points(group="rackmap.services"):
                try:
                    found.setdefault(point.name, point.load())
                except Exception:  # noqa: BLE001 - one broken plug-in must not stop rackmap starting
                    continue
        except Exception:  # noqa: BLE001
            pass
        found.update(_registered)
        _found = found
    return _found


def _optional(module) -> bool:
    return bool(getattr(module, "SECRET_OPTIONAL", False))


def kinds() -> dict:
    """What the set-up form needs to know about each kind of service."""
    return {slug: {"name": m.NAME, "port": m.DEFAULT_PORT, "account_label": m.ACCOUNT_LABEL, "account_example": m.ACCOUNT_EXAMPLE,
                   "secret_label": m.SECRET_LABEL, "help": m.HELP, "secret_optional": _optional(m),
                   "profiles": m.profiles() if hasattr(m, "profiles") else None} for slug, m in _kinds().items()}


def ssh_public_key() -> str:
    from . import sshrun
    return sshrun.public_key()


def allowed(service: dict, needs: str) -> bool:
    """Whether this connection's level covers something that needs `needs`."""
    return _RANK[service["level"]] >= _RANK[needs]


# ---------- records ----------

def _public(r) -> dict:
    return {"id": r["id"], "device_id": r["device_id"], "kind": r["kind"], "kind_name": _kinds()[r["kind"]].NAME,
            "host": r["host"], "port": r["port"], "username": r["username"], "level": r["level"], "level_name": LEVELS[r["level"]],
            "web_url": (getattr(_kinds()[r["kind"]], "WEB", "") or "").format(host=r["host"], port=r["port"]),
            "uses_key": _optional(_kinds()[r["kind"]]) and not secrets_store.open_sealed(r["secret"]) if r["secret"] else False,
            "options": json.loads(r["options"] or "{}")}


def _need(service_id: int):
    rows = db.query("SELECT * FROM services WHERE id = ?", (service_id,))
    if not rows:
        raise Invalid("That connection no longer exists.")
    if rows[0]["kind"] not in _kinds():
        raise Invalid("That connection needs an add-on that isn't installed.")
    return rows[0]


def get(service_id: int) -> dict:
    return _public(_need(service_id))


def for_device(device_id: int) -> list[dict]:
    return [_public(r) for r in db.query("SELECT * FROM services WHERE device_id = ? ORDER BY kind, id", (device_id,)) if r["kind"] in _kinds()]


def _client(r, secret: str | None = None, pinned: str | None = "saved"):
    secret = secret if secret is not None else secrets_store.open_sealed(r["secret"])
    module = _kinds()[r["kind"]]
    extra = {"options": json.loads(r["options"] or "{}")} if getattr(module, "USES_OPTIONS", False) else {}
    return module.Client(r["host"], r["port"], r["username"], secret, r["cert_sha256"] if pinned == "saved" else pinned, **extra)


def save(device_id: int, values: dict, service_id: int | None = None) -> dict:
    """Connect or update a service. rackmap signs in first and saves nothing unless that works;
    that is also when the service's certificate is remembered."""
    current = _need(service_id) if service_id is not None else None
    if current is not None:
        device_id = current["device_id"]
    elif not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    kind = current["kind"] if current is not None else str(values.get("kind") or "")
    if kind not in _kinds():
        raise Invalid("That kind of connection isn't installed here.")
    module = _kinds()[kind]
    host = str(values.get("host") or "").strip()
    username = str(values.get("username") or "").strip()
    level = str(values.get("level") or "view")
    try:
        port = int(values.get("port") or module.DEFAULT_PORT)
    except (TypeError, ValueError):
        raise Invalid("The port is a number.") from None
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
        raise Invalid("Enter the address the service is on, for example 192.168.1.60.")
    if addresses.is_public_ip(host):
        raise Invalid("The address must be on a private network.")
    if not 1 <= port <= 65535:
        raise Invalid("The port is a number between 1 and 65535.")
    if not username or not re.fullmatch(r"[^\s=]{1,128}", username):
        raise Invalid(f"Enter the {module.ACCOUNT_LABEL.lower()}, for example {module.ACCOUNT_EXAMPLE}.")
    if level not in LEVELS:
        raise Invalid("Choose how much rackmap may do through this connection.")
    if current is None and db.query("SELECT 1 FROM services WHERE device_id = ? AND kind = ?", (device_id, kind)):
        raise Invalid(f"{module.NAME} is already connected for this equipment. Edit that connection instead.")
    secret = str(values.get("secret") or "")
    if not secret:
        if current is None:
            if not _optional(module):
                raise Invalid(f"Enter the {module.SECRET_LABEL.lower()}.")
        else:
            try:
                secret = secrets_store.open_sealed(current["secret"])
            except ValueError as exc:
                raise Invalid(f"{exc} Enter it again.") from None
    relearn = current is None or current["host"] != host or current["port"] != port or bool(values.get("secret")) or bool(values.get("relearn"))
    options = {}
    try:
        if getattr(module, "USES_OPTIONS", False):
            given = values.get("options") or (json.loads(current["options"] or "{}") if current is not None else {})
            options = module.clean_options(given)
        client = module.Client(host, port, username, secret, None if relearn else current["cert_sha256"], **({"options": options} if options else {}))
        answer = client.summary()
    except ServiceError as exc:
        raise Invalid(str(exc)) from None
    row = (host, port, username, secrets_store.seal(secret), client.fingerprint or "", level, json.dumps(options))
    if current is None:
        service_id = db.execute("INSERT INTO services (host, port, username, secret, cert_sha256, level, options, device_id, kind) VALUES (?,?,?,?,?,?,?,?,?)",
                                row + (device_id, kind))
    else:
        db.execute("UPDATE services SET host=?, port=?, username=?, secret=?, cert_sha256=?, level=?, options=? WHERE id=?", row + (service_id,))
    with _last_lock:
        _last[service_id] = (time.monotonic(), answer)
    return get(service_id)


# ---------- doing things ----------

def _log(user: str, r, target: str, action: str, outcome: str, detail: str = "") -> None:
    device = db.query("SELECT name FROM devices WHERE id = ?", (r["device_id"],))
    db.execute("INSERT INTO action_log (user, device_name, service_name, target, action, outcome, detail) VALUES (?,?,?,?,?,?,?)",
               (user, device[0]["name"] if device else "", _kinds()[r["kind"]].NAME, target, action, outcome, detail[:300]))


def action_log(limit: int = 200) -> list[dict]:
    return [dict(r) for r in db.query("SELECT at, user, device_name, service_name, target, action, outcome, detail FROM action_log ORDER BY id DESC LIMIT ?", (limit,))]


def _label(guest: dict) -> str:
    return guest["name"] if guest["name"] == guest.get("label") else f"{guest['name']} ({guest.get('label') or guest['key']})"


def _protected(service_id: int) -> set[str]:
    return {r["target"] for r in db.query("SELECT target FROM service_protected WHERE service_id = ?", (service_id,))}


def protect(service_id: int, target: str, on: bool, user: str) -> None:
    r = _need(service_id)
    if not re.fullmatch(r"[A-Za-z0-9_.:\-/]{1,120}", target or ""):
        raise Invalid("That isn't something rackmap knows on this service.")
    if on:
        db.execute("INSERT OR IGNORE INTO service_protected (service_id, target) VALUES (?, ?)", (service_id, target))
    else:
        db.execute("DELETE FROM service_protected WHERE service_id = ? AND target = ?", (service_id, target))
    with _last_lock:
        kept = _last.get(service_id)
    guest = next((g for g in (kept[1].get("guests") or []) if g["key"] == target), None) if kept else None
    _log(user, r, _label(guest) if guest else target, "protect" if on else "unprotect", "done")


def _dress(r, answer: dict) -> dict:
    """Add to each guest what may be done to it now: by its state, the connection's level and protection."""
    module, service, protected = _kinds()[r["kind"]], _public(r), _protected(r["id"])
    guests = []
    for g in answer.get("guests") or []:
        shielded = g["key"] in protected
        offered = [a for a in module.offered(g) if allowed(service, module.ACTIONS[a]["needs"]) and not (shielded and module.ACTIONS[a]["stops"])]
        guests.append({**g, "protected": shielded, "actions": offered, "console": bool(g.get("console")) and allowed(service, "full")})
    return {**answer, "guests": guests, "host_shell": bool(getattr(module, "HOST_SHELL", False)) and allowed(service, "full"), "action_labels": {a: spec["label"] for a, spec in module.ACTIONS.items()},
            "action_warnings": {a: spec.get("warning", "") for a, spec in module.ACTIONS.items()},
            "action_asks": {a: spec["asks"] for a, spec in module.ACTIONS.items() if spec.get("asks")},
            "choices": answer.get("choices") or {},
            "stopping": [a for a, spec in module.ACTIONS.items() if spec["stops"]]}


def act(service_id: int, target: str, action: str, user: str, confirm: str = "", value: str = "") -> dict:
    """Carry out one action on one guest. Checked here, in this order: the action exists, the
    connection's level covers it, the guest is still there, it isn't protected, and (for anything
    that stops it) the name the person confirmed is the name it has now."""
    r = _need(service_id)
    module, service = _kinds()[r["kind"]], _public(r)
    spec = module.ACTIONS.get(action)
    if not spec:
        raise Invalid("That isn't an action rackmap knows for this service.")
    if not allowed(service, spec["needs"]):
        _log(user, r, target, action, "refused", f"level is {service['level']}")
        raise Invalid(f"This connection is set to “{LEVELS[service['level']].split(':')[0]}”, which doesn't cover this. "
                      "Change the level under Edit connection if you want rackmap to do it.")
    try:
        client = _client(r)
    except ValueError as exc:
        raise ServiceError(f"{exc} Re-enter the secret under Edit.") from None
    guest = next((g for g in client.summary()["guests"] if g["key"] == target), None)
    if guest is None:
        raise Invalid("That is no longer there. Close the details and open them again to see what is.")
    label = _label(guest)
    if spec["stops"] and target in _protected(service_id):
        _log(user, r, label, action, "refused", "protected")
        raise Invalid(f"“{guest['name']}” is protected, so rackmap won't stop or restart it. Remove the protection first if you mean to.")
    if spec["stops"] and confirm != guest["name"]:
        raise Invalid(f"Confirm by name: this would affect “{guest['name']}”.")
    if action not in module.offered(guest):
        raise Invalid(f"“{guest['name']}” is {'running' if guest['running'] else 'not running'}, so that doesn't apply right now.")
    try:
        detail = client.act(guest, action, value) if spec.get("asks") else client.act(guest, action)
    except ServiceError as exc:
        _log(user, r, label, action, "failed", str(exc))
        raise
    _log(user, r, label, action, "sent", detail)
    with _last_lock:
        _last.pop(service_id, None)  # what we knew is no longer true
    return {"sent": spec["label"], "target": guest["name"], "detail": detail}


async def open_console(service_id: int, target: str, user: str, cols: int = 80, rows: int = 24):
    """A terminal on one guest, or (target "host") on the machine itself. This is the most
    rackmap can hand over, so it needs the connection's level to be Full control, and every
    opening is written to the action log."""
    import asyncio

    from . import consoles
    try:
        r = _need(service_id)
    except Invalid as exc:
        raise consoles.ConsoleError(str(exc)) from None
    module, service = _kinds()[r["kind"]], _public(r)
    if not hasattr(module, "open_console"):
        raise consoles.ConsoleError("This kind of service has no console.")
    if not allowed(service, "full"):
        _log(user, r, target, "console", "refused", f"level is {service['level']}")
        raise consoles.ConsoleError("A console needs this connection's level set to “Full control”. Change it under Edit connection if you want that.")
    try:
        client = _client(r)
        guest = None
        if target != "host":
            guest = next((g for g in (await asyncio.to_thread(client.summary))["guests"] if g["key"] == target and g.get("console")), None)
            if guest is None:
                raise consoles.ConsoleError("That isn't running any more, or has no console.")
        elif not getattr(module, "HOST_SHELL", False):
            raise consoles.ConsoleError("This kind of service has no command line of its own.")
        session = await module.open_console(client, guest, cols, rows)
    except ValueError as exc:
        raise consoles.ConsoleError(f"{exc} Re-enter the secret under Edit.") from None
    except ServiceError as exc:
        _log(user, r, _label(guest) if guest else "the machine itself", "console", "failed", str(exc))
        raise consoles.ConsoleError(str(exc)) from None
    _log(user, r, _label(guest) if guest else "the machine itself", "console", "opened")
    return session


def logs(service_id: int, target: str) -> dict:
    """The last lines a container wrote, for kinds that can show them. Looking only."""
    r = _need(service_id)
    try:
        client = _client(r)
    except ValueError as exc:
        raise ServiceError(f"{exc} Re-enter the secret under Edit.") from None
    if not hasattr(client, "logs"):
        raise Invalid("This kind of service has no logs to show.")
    guest = next((g for g in client.summary()["guests"] if g["key"] == target and g.get("logs")), None)
    if guest is None:
        raise Invalid("That is no longer there. Close the details and open them again to see what is.")
    return {"name": guest["name"], "text": client.logs(guest)}


def overview() -> list[dict]:
    """Every connection with what it said last, for the dashboard. Nothing is asked of any
    service here: `known` is false for one that hasn't been asked since rackmap started."""
    out = []
    for r in db.query("SELECT s.*, d.name AS device_name FROM services s JOIN devices d ON d.id = s.device_id ORDER BY d.name COLLATE NOCASE, s.kind"):
        if r["kind"] not in _kinds():
            continue
        with _last_lock:
            kept = _last.get(r["id"])
        guests = [g for g in (kept[1].get("guests") or []) if not g.get("group") and not g.get("uncounted")] if kept else []
        out.append({"id": r["id"], "device_id": r["device_id"], "device_name": r["device_name"], "kind": r["kind"], "kind_name": _kinds()[r["kind"]].NAME,
                    "level": r["level"], "level_name": LEVELS[r["level"]].split(":")[0], "known": kept is not None,
                    "age_seconds": int(time.monotonic() - kept[0]) if kept else None, "total": len(guests), "running": sum(1 for g in guests if g["running"]),
                    "word": (kept[1].get("count_word") if kept else None) or "running", "what": (kept[1].get("guests_title") if kept else "") or "",
                    "unhealthy": [g["name"] for g in guests if g.get("unhealthy")][:5]})
    return out


def remove(service_id: int) -> None:
    if not db.query("SELECT 1 FROM services WHERE id = ?", (service_id,)):
        raise Invalid("That connection no longer exists.")
    db.execute("DELETE FROM services WHERE id = ?", (service_id,))
    with _last_lock:
        _last.pop(service_id, None)


# The last answer from each service, kept in memory so the page has something to show at once.
_last: dict[int, tuple[float, dict]] = {}
_last_lock = threading.Lock()


def last_status(service_id: int) -> dict | None:
    _need(service_id)
    with _last_lock:
        kept = _last.get(service_id)
    if not kept:
        return None
    return {**_dress(_need(service_id), kept[1]), "age_seconds": int(time.monotonic() - kept[0])}


def status(service_id: int) -> dict:
    r = _need(service_id)
    try:
        answer = _client(r).summary()
    except ValueError as exc:  # unreadable saved secret
        raise ServiceError(f"{exc} Re-enter the secret under Edit.") from None
    with _last_lock:
        _last[service_id] = (time.monotonic(), answer)
    return {**_dress(r, answer), "age_seconds": 0}
