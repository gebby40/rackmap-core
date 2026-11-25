"""Console servers: the serial ports of equipment, reached through a box that speaks SSH.

A console server has a number of serial ports; each can be labelled and tied to a piece of
equipment. Opening a port gives a live byte stream in both directions, which the web front end
shows in a terminal. The first supported kind is the Digi CM family ("digi-cm"): rackmap signs
in over SSH, runs `connect <port>`, answers the port-server sign-in with the same account, and
then passes bytes through untouched.

The server's SSH host key is remembered the first time rackmap connects (trust on first use);
afterwards a different key is refused until someone re-saves the console server.
"""

from __future__ import annotations

import asyncio
import re

import asyncssh

from . import addresses, db, secrets_store

KINDS = {"digi-cm": "Digi CM / Passport"}
MAX_PORTS = 96
CONNECT_TIMEOUT = 12

# Old console servers only offer old algorithms; modern ones are listed first so they win when available.
_SSH_ALGS = dict(
    kex_algs=["curve25519-sha256", "ecdh-sha2-nistp256", "diffie-hellman-group16-sha512", "diffie-hellman-group14-sha256",
              "diffie-hellman-group14-sha1", "diffie-hellman-group-exchange-sha1", "diffie-hellman-group1-sha1"],
    server_host_key_algs=["ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256", "ssh-rsa", "ssh-dss"],
    encryption_algs=["chacha20-poly1305@openssh.com", "aes256-gcm@openssh.com", "aes128-gcm@openssh.com", "aes256-ctr",
                     "aes128-ctr", "aes256-cbc", "aes128-cbc", "3des-cbc"],
    mac_algs=["hmac-sha2-256-etm@openssh.com", "hmac-sha2-256", "hmac-sha1", "hmac-md5"],
)


class Invalid(ValueError):
    """Message is written for the person using the app."""


class ConsoleError(Exception):
    """A console session could not be opened; the message is shown in the terminal."""


# ---------- records ----------

def _server(r) -> dict:
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "kind_name": KINDS.get(r["kind"], r["kind"]), "host": r["host"],
            "port": r["port"], "username": r["username"], "port_count": r["port_count"], "host_key_fingerprint": r["host_key_fingerprint"],
            "device_id": r["device_id"]}


def list_servers() -> list[dict]:
    servers = [_server(r) for r in db.query("SELECT * FROM console_servers ORDER BY name COLLATE NOCASE, id")]
    for server in servers:
        server["ports"] = [
            {"id": r["id"], "number": r["number"], "label": r["label"],
             "device": {"id": r["device_id"], "name": r["device_name"]} if r["device_id"] and r["device_name"] else None}
            for r in db.query(
                "SELECT p.id, p.number, p.label, p.device_id, d.name AS device_name FROM console_ports p "
                "LEFT JOIN devices d ON d.id = p.device_id WHERE p.server_id = ? ORDER BY p.number", (server["id"],))
        ]
    return servers


def ports_for_device(device_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT p.id, p.number, p.label, s.name AS server_name FROM console_ports p JOIN console_servers s ON s.id = p.server_id "
        "WHERE p.device_id = ? ORDER BY s.name, p.number", (device_id,))]


def _row(server_id: int):
    rows = db.query("SELECT * FROM console_servers WHERE id = ?", (server_id,))
    if not rows:
        raise Invalid("That console server no longer exists.")
    return rows[0]


def _clean(values: dict) -> dict:
    name = str(values.get("name") or "").strip()
    host = str(values.get("host") or "").strip()
    username = str(values.get("username") or "").strip()
    kind = values.get("kind") or "digi-cm"
    if kind not in KINDS:
        raise Invalid("That kind of console server isn't supported yet.")
    if not name:
        raise Invalid("Give the console server a name.")
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
        raise Invalid("Enter the console server's address, for example 192.168.1.50.")
    if addresses.is_public_ip(host):
        raise Invalid("The console server's address must be on a private network.")
    if not username:
        raise Invalid("Enter the account rackmap signs in with.")
    port = int(values.get("port") or 22)
    count = int(values.get("port_count") or 0)
    if not 1 <= port <= 65535:
        raise Invalid("The SSH port is a number between 1 and 65535.")
    if not 1 <= count <= MAX_PORTS:
        raise Invalid(f"A console server has between 1 and {MAX_PORTS} serial ports.")
    device_id = values.get("device_id")
    if device_id is not None and not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    return {"name": name, "kind": kind, "host": host, "port": port, "username": username, "port_count": count, "device_id": device_id}


def _sync_ports(server_id: int, count: int) -> None:
    have = {r["number"] for r in db.query("SELECT number FROM console_ports WHERE server_id = ?", (server_id,))}
    for number in range(1, count + 1):
        if number not in have:
            db.execute("INSERT INTO console_ports (server_id, number) VALUES (?, ?)", (server_id, number))
    db.execute("DELETE FROM console_ports WHERE server_id = ? AND number > ?", (server_id, count))


async def save_server(values: dict, server_id: int | None = None) -> dict:
    """Create or update a console server. The connection is tried first, and nothing is saved
    unless rackmap can sign in; that is also when the server's host key is remembered."""
    d = _clean(values)
    password = values.get("password") or ""
    current = _row(server_id) if server_id is not None else None
    if not password:
        if current is None:
            raise Invalid("Enter the password for that account.")
        password = secrets_store.open_sealed(current["secret"])
    # A new address, or an explicit re-save with the password, re-learns the host key.
    relearn = current is None or current["host"] != d["host"] or current["port"] != d["port"] or bool(values.get("password"))
    trusted = None if relearn else current["host_key"]
    try:
        conn = await _connect(d["host"], d["port"], d["username"], password, trusted)
    except ConsoleError as exc:
        raise Invalid(str(exc)) from None
    key = conn.get_server_host_key()
    host_key = key.export_public_key("openssh").decode().strip()
    fingerprint = key.get_fingerprint()
    conn.close()
    secret = secrets_store.seal(password)
    if d["device_id"] is not None:
        # a piece of equipment is at most one console server, and isn't plugged into itself
        db.execute("UPDATE console_servers SET device_id = NULL WHERE device_id = ? AND id IS NOT ?", (d["device_id"], server_id))
    row = (d["name"], d["kind"], d["host"], d["port"], d["username"], secret, host_key, fingerprint, d["port_count"], d["device_id"])
    if current is None:
        server_id = db.execute(
            "INSERT INTO console_servers (name, kind, host, port, username, secret, host_key, host_key_fingerprint, port_count, device_id) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
    else:
        db.execute(
            "UPDATE console_servers SET name=?, kind=?, host=?, port=?, username=?, secret=?, host_key=?, host_key_fingerprint=?, port_count=?, device_id=? WHERE id=?",
            row + (server_id,))
    _sync_ports(server_id, d["port_count"])
    if d["device_id"] is not None:
        db.execute("UPDATE console_ports SET device_id = NULL WHERE server_id = ? AND device_id = ?", (server_id, d["device_id"]))
    return next(s for s in list_servers() if s["id"] == server_id)


def delete_server(server_id: int) -> None:
    _row(server_id)
    db.execute("DELETE FROM console_ports WHERE server_id = ?", (server_id,))
    db.execute("DELETE FROM console_servers WHERE id = ?", (server_id,))


def update_port(port_id: int, label: str, device_id: int | None) -> None:
    if not db.query("SELECT 1 FROM console_ports WHERE id = ?", (port_id,)):
        raise Invalid("That console port no longer exists.")
    if device_id is not None and not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    db.execute("UPDATE console_ports SET label = ?, device_id = ? WHERE id = ?", (label.strip()[:80], device_id, port_id))


def set_device_port(device_id: int, port_id: int | None) -> None:
    """Say which console-server port a piece of equipment's console cable is plugged into
    (None: not plugged into one). A port serves one piece of equipment."""
    names = db.query("SELECT name FROM devices WHERE id = ?", (device_id,))
    if not names:
        raise Invalid("That piece of equipment no longer exists.")
    if port_id is not None:
        rows = db.query("SELECT p.label, p.device_id, s.device_id AS server_device FROM console_ports p "
                        "JOIN console_servers s ON s.id = p.server_id WHERE p.id = ?", (port_id,))
        if not rows:
            raise Invalid("That console port no longer exists.")
        if rows[0]["server_device"] == device_id:
            raise Invalid("A console server can't be plugged into its own port.")
        if rows[0]["device_id"] not in (None, device_id):
            raise Invalid("That port is already used by other equipment. Free it there first.")
    db.execute("UPDATE console_ports SET device_id = NULL WHERE device_id = ?", (device_id,))
    if port_id is not None:
        db.execute("UPDATE console_ports SET device_id = ?, label = CASE WHEN label = '' THEN ? ELSE label END WHERE id = ?",
                   (device_id, names[0]["name"][:80], port_id))


# ---------- live sessions ----------

class _PasswordClient(asyncssh.SSHClient):
    """Answers password sign-in, and any follow-up keyboard-interactive step, with the one password.

    Some devices (Dell iDRAC among them) accept the password and then insist on a second,
    keyboard-interactive round, usually with no questions at all. asyncssh's built-in handling
    gives up at that point because it has already used the password once.
    """

    def __init__(self, password: str):
        self._password = password
        self._tried_password = False
        self._tried_interactive = False
        self._answered = False

    # Each step is tried once. Offering the same wrong password again would only burn through
    # the device's failed-sign-in allowance and get rackmap's address blocked.
    def password_auth_requested(self) -> str | None:
        if self._tried_password:
            return None
        self._tried_password = True
        return self._password

    def kbdint_auth_requested(self) -> str | None:
        if self._tried_interactive:
            return None
        self._tried_interactive = True
        return ""

    def kbdint_challenge_received(self, name, instructions, lang, prompts):
        if not prompts:
            return []
        if self._answered:
            return None
        self._answered = True
        return [self._password for _ in prompts]


async def _connect(host: str, port: int, username: str, password: str, trusted_key: str | None) -> asyncssh.SSHClientConnection:
    known = None
    if trusted_key:
        known = ([asyncssh.import_public_key(trusted_key)], [], [])
    try:
        return await asyncio.wait_for(
            asyncssh.connect(host, port=port, username=username, client_factory=lambda: _PasswordClient(password),
                             known_hosts=known, client_keys=None,
                             agent_path=None, config=None, preferred_auth=["password", "keyboard-interactive"],
                             keepalive_interval=30, **_SSH_ALGS),
            timeout=CONNECT_TIMEOUT)
    except asyncssh.HostKeyNotVerifiable:
        raise ConsoleError("The console server's identity (SSH host key) has changed since rackmap first connected. If you replaced or "
                           "reset it, re-enter its password under Edit to accept the new key.") from None
    except asyncssh.PermissionDenied:
        raise ConsoleError("The console server refused that name and password.") from None
    except (asyncio.TimeoutError, TimeoutError):
        raise ConsoleError("The console server didn't answer in time. Check its address and that rackmap can reach it.") from None
    except (OSError, asyncssh.Error) as exc:
        raise ConsoleError(f"Couldn't connect to the console server ({type(exc).__name__}).") from None


class Session:
    """One open serial port. read() returns b"" when the far end closes."""

    def __init__(self, conn: asyncssh.SSHClientConnection, process: asyncssh.SSHClientProcess, pending: bytes):
        self._conn = conn
        self._process = process
        self._pending = pending

    async def read(self) -> bytes:
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        try:
            return await self._process.stdout.read(4096)
        except (asyncssh.Error, OSError):
            return b""

    def write(self, data: bytes) -> None:
        try:
            self._process.stdin.write(data)
        except (asyncssh.Error, OSError, BrokenPipeError):
            pass

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._process.change_terminal_size(max(20, min(cols, 400)), max(5, min(rows, 200)))
        except (asyncssh.Error, OSError):
            pass

    def close(self) -> None:
        self._process.close()
        self._conn.close()


async def _digi_sign_in(process: asyncssh.SSHClientProcess, username: str, password: str) -> bytes:
    """Answer the Digi's own port sign-in. The password is typed only when a password prompt is
    showing. Returns any device output that arrived after the sign-in finished."""
    buffer = b""
    sent_name = sent_password = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    quiet_since = loop.time()
    while loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=0.5)
        except asyncio.TimeoutError:
            chunk = None
        if chunk == b"":
            raise ConsoleError("The console server closed the port. It may be in use by someone else.")
        if chunk:
            buffer += chunk.replace(b"\x00", b"")
            quiet_since = loop.time()
        text = buffer.decode("latin-1")
        entered = re.search(r"Entering server port[^\n]*\n", text)
        if entered:
            return text[entered.end():].encode("latin-1")
        if re.search(r"(incorrect|denied|invalid|failed)", text[-200:], re.IGNORECASE) and sent_password:
            raise ConsoleError("The console server refused the sign-in for that port.")
        if re.search(r"(busy|in use|not available)", text[-200:], re.IGNORECASE):
            raise ConsoleError("That port is in use by another session.")
        if not sent_name and re.search(r"Login\s*:\s*$", text):
            process.stdin.write(username.encode() + b"\r")
            sent_name, buffer = True, b""
        elif sent_name and not sent_password and re.search(r"Password\s*:\s*$", text):
            process.stdin.write(password.encode() + b"\r")
            sent_password, buffer = True, b""
        elif not sent_name and buffer and loop.time() - quiet_since > 3:
            return buffer  # this port asks for no sign-in; it is already passing data through
    raise ConsoleError("The console server didn't finish opening the port.")


async def open_port(port_id: int, cols: int = 80, rows: int = 24) -> Session:
    rows_ = db.query("SELECT p.number, s.* FROM console_ports p JOIN console_servers s ON s.id = p.server_id WHERE p.id = ?", (port_id,))
    if not rows_:
        raise ConsoleError("That console port no longer exists.")
    r = rows_[0]
    try:
        password = secrets_store.open_sealed(r["secret"])
    except ValueError as exc:
        raise ConsoleError(f"{exc} Re-enter the console server's password under Edit.") from None
    conn = await _connect(r["host"], r["port"], r["username"], password, r["host_key"])
    try:
        process = await conn.create_process(f"connect {int(r['number'])}", term_type="xterm", term_size=(cols, rows), encoding=None)
        pending = await _digi_sign_in(process, r["username"], password)
    except ConsoleError:
        conn.close()
        raise
    except (asyncssh.Error, OSError) as exc:
        conn.close()
        raise ConsoleError(f"Couldn't open the port ({type(exc).__name__}).") from None
    return Session(conn, process, pending)
