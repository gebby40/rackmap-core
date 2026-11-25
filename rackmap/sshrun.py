"""Running commands on a machine over SSH, for services that have no API of their own.

rackmap has one SSH key of its own (made on first use, kept in the data directory). A machine
can be given that key instead of a password: add the public half to the account's authorized
keys. The machine's own identity (its host key) is remembered the first time and a different
one is refused afterwards, as with certificates elsewhere.
"""

from __future__ import annotations

import asyncio
import os
import shlex

import asyncssh

from . import db
from .services import ServiceError

CONNECT_TIMEOUT = 12


def _key_path():
    return db.DATA_DIR / "ssh_key"


def _private_key() -> asyncssh.SSHKey:
    path = _key_path()
    if not path.exists():
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment="rackmap")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key.export_private_key("openssh"))
    return asyncssh.read_private_key(str(path))


def public_key() -> str:
    """The line to add to an account's authorized keys so rackmap can sign in without a password."""
    return _private_key().export_public_key("openssh").decode().strip()


async def _run(host, port, username, password, pinned, command, timeout) -> tuple[str, str]:
    known = ([asyncssh.import_public_key(pinned)], [], []) if pinned else None
    options = dict(port=port, username=username, known_hosts=known, client_keys=[_private_key()], agent_path=None, config=None,
                   preferred_auth=["publickey", "password", "keyboard-interactive"] if password else ["publickey"])
    if password:
        options["password"] = password
    conn = await asyncio.wait_for(asyncssh.connect(host, **options), timeout=CONNECT_TIMEOUT)
    try:
        host_key = conn.get_server_host_key().export_public_key("openssh").decode().strip()
        result = await asyncio.wait_for(conn.run(command, check=False, encoding="utf-8", errors="replace"), timeout=timeout)
        return str(result.stdout or ""), host_key
    finally:
        conn.close()


class Host:
    """One machine reached over SSH. `ask` runs several commands in one sign-in."""

    def __init__(self, host: str, port: int, username: str, password: str, pinned: str | None, what: str):
        self.host, self.port, self.username, self.password, self.pinned, self.what = host, port, username, password, pinned or None, what
        self.fingerprint: str | None = None

    def run(self, script: str, timeout: int = 40) -> str:
        return self.raw("/bin/sh -c " + shlex.quote("PATH=$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin; export PATH\n" + script), timeout)

    def raw(self, command: str, timeout: int = 40) -> str:
        """Run a command exactly as given, for machines that have no shell (network equipment)."""
        try:
            out, self.fingerprint = asyncio.run(_run(self.host, self.port, self.username, self.password, self.pinned, command, timeout))
        except asyncssh.HostKeyNotVerifiable:
            raise ServiceError(f"The identity (SSH host key) of {self.what} has changed since it was connected. If you reinstalled "
                               "or replaced it, open Edit, tick “Accept its new identity” and save.") from None
        except asyncssh.PermissionDenied:
            how = "that account and password" if self.password else "rackmap's key for that account (and no password was given)"
            raise ServiceError(f"{self.what} refused {how}.") from None
        except (asyncio.TimeoutError, TimeoutError):
            raise ServiceError(f"{self.what} didn't answer in time. Check its address and that rackmap can reach it.") from None
        except (OSError, asyncssh.Error) as exc:
            raise ServiceError(f"Couldn't connect to {self.what} over SSH ({type(exc).__name__}). Check the address, the port and that SSH is switched on there.") from None
        return out

    def ask(self, parts: dict[str, str], timeout: int = 40) -> dict[str, tuple[str, int]]:
        """Run each named command; returns name -> (what it printed, its exit code)."""
        script = "\n".join(f"echo '@@begin {name}'\n(\n{command}\n) 2>&1\nrc=$?; echo; echo '@@end {name} '$rc" for name, command in parts.items())
        out, found, current, lines = self.run(script, timeout), {}, None, []
        for line in out.splitlines():
            if line.startswith("@@begin "):
                current, lines = line[8:].strip(), []
            elif line.startswith("@@end ") and current:
                code = line.rsplit(" ", 1)[-1]
                found[current] = ("\n".join(lines).strip("\n"), int(code) if code.isdigit() else 1)
                current = None
            elif current:
                lines.append(line)
        missing = [name for name in parts if name not in found]
        if missing:
            raise ServiceError(f"{self.what} answered, but not in a way rackmap could read. Is that account given a normal command line?")
        return found


async def open_shell(host: "Host", command: str | None, cols: int, rows: int):
    """An interactive terminal on the machine: `command` with a terminal of its own, or the
    account's own command line when there is none. Returns a consoles.Session."""
    from . import consoles
    known = ([asyncssh.import_public_key(host.pinned)], [], []) if host.pinned else None
    options = dict(port=host.port, username=host.username, known_hosts=known, client_keys=[_private_key()], agent_path=None, config=None,
                   preferred_auth=["publickey", "password", "keyboard-interactive"] if host.password else ["publickey"], keepalive_interval=30)
    if host.password:
        options["password"] = host.password
    try:
        conn = await asyncio.wait_for(asyncssh.connect(host.host, **options), timeout=CONNECT_TIMEOUT)
    except asyncssh.HostKeyNotVerifiable:
        raise ServiceError(f"The identity (SSH host key) of {host.what} has changed since it was connected. Open Edit, tick “Accept its new identity” and save.") from None
    except asyncssh.PermissionDenied:
        raise ServiceError(f"{host.what} refused the sign-in.") from None
    except (asyncio.TimeoutError, TimeoutError, OSError, asyncssh.Error) as exc:
        raise ServiceError(f"Couldn't connect to {host.what} over SSH ({type(exc).__name__}).") from None
    try:
        process = await conn.create_process(command, term_type="xterm-256color", term_size=(cols, rows), encoding=None)
    except (asyncssh.Error, OSError) as exc:
        conn.close()
        raise ServiceError(f"Couldn't start the terminal ({type(exc).__name__}).") from None
    return consoles.Session(conn, process, b"")
