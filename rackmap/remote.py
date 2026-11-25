"""Remote access over Tailscale.

Tailscale joins machines into a private network (a "tailnet") that only its owner's devices can
reach, wherever they are. If the machine rackmap runs on is in one, rackmap can be published to
that tailnet under the machine's own name with a certificate browsers accept, using Tailscale's
own "serve" feature: nothing is opened to the internet and no router setting changes.

rackmap never installs or signs in to Tailscale; that is done once on the machine by its
administrator. It only reads Tailscale's state and switches "serve" on or off for its own port,
which needs the account rackmap runs under to be Tailscale's operator on the machine:

    sudo tailscale set --operator=<that account>
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess

TIMEOUT = 15
HTTPS_PORT = 443


class Invalid(ValueError):
    """Message is written for the person using the app."""


def _own_port() -> tuple[str, int]:
    """How Tailscale should reach rackmap on this machine."""
    secure = (os.environ.get("RACKMAP_TLS") or "on").strip().lower() != "off"
    port = int(os.environ.get("RACKMAP_PORT") or (8443 if secure else 8080))
    # rackmap's own certificate can't be verified, and needn't be: the hop never leaves this machine
    return ("https+insecure" if secure else "http"), port


def _run(*args: str) -> tuple[int, str]:
    binary = shutil.which("tailscale")
    if not binary:
        return 127, ""
    try:
        done = subprocess.run([binary, *args], capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return 124, "Tailscale on this machine didn't answer."
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _json(text: str) -> dict:
    try:
        start = text.index("{")
        data = json.loads(text[start:text.rindex("}") + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _denied(text: str) -> bool:
    lowered = text.lower()
    return "denied" in lowered or "operator" in lowered or "permission" in lowered


def account() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "rackmap"


def status() -> dict:
    """What the Remote access page shows."""
    scheme, port = _own_port()
    out = {"state": "missing", "name": "", "addresses": [], "tailnet": "", "certificates": False, "serving": False, "url": "",
           "direct": [], "account": account(), "port": port, "error": ""}
    if not shutil.which("tailscale"):
        return out
    code, text = _run("status", "--json")
    data = _json(text)
    if not data:
        out["state"] = "stopped"
        out["error"] = text.strip().splitlines()[-1][:200] if text.strip() else "Tailscale is installed but isn't running."
        return out
    backend = str(data.get("BackendState") or "")
    me = data.get("Self") or {}
    out["name"] = str(me.get("DNSName") or "").rstrip(".")
    out["addresses"] = [a for a in me.get("TailscaleIPs") or [] if "." in a]
    out["tailnet"] = str((data.get("CurrentTailnet") or {}).get("Name") or "")
    out["certificates"] = bool(data.get("CertDomains"))
    out["state"] = "running" if backend == "Running" else "needs-login" if backend in ("NeedsLogin", "NeedsMachineAuth", "NoState") else "stopped"
    if out["state"] != "running":
        return out
    web = "https" if scheme.startswith("https") else "http"
    out["direct"] = [f"{web}://{a}:{port}" for a in out["addresses"]]
    code, text = _run("serve", "status", "--json")
    if code != 0 and _denied(text):
        out["state"] = "no-permission"
        return out
    handlers = (_json(text).get("Web") or {})
    for host, site in handlers.items():
        for target in ((site or {}).get("Handlers") or {}).values():
            if str((target or {}).get("Proxy") or "").endswith(f"127.0.0.1:{port}") or str((target or {}).get("Proxy") or "").endswith(f"localhost:{port}"):
                out["serving"] = True
                name, _, served_port = host.partition(":")
                out["url"] = f"https://{name}" + ("" if served_port in ("", "443") else f":{served_port}")
    return out


def enable() -> dict:
    now = status()
    if now["state"] == "missing":
        raise Invalid("Tailscale isn't installed on the machine rackmap runs on.")
    if now["state"] in ("stopped", "needs-login"):
        raise Invalid("Tailscale on this machine isn't signed in to a tailnet yet.")
    scheme, port = _own_port()
    code, text = _run("serve", "--bg", f"--https={HTTPS_PORT}", f"{scheme}://127.0.0.1:{port}")
    if code != 0:
        if _denied(text):
            raise Invalid(f"Tailscale won't take that from rackmap's account. Run this once on the machine, then try again: sudo tailscale set --operator={account()}")
        said = " ".join(text.split())[:400] or "no reason given"
        raise Invalid(f"Tailscale couldn't publish rackmap. It said: {said}")
    return status()


def disable() -> dict:
    code, text = _run("serve", f"--https={HTTPS_PORT}", "off")
    if code != 0 and _denied(text):
        raise Invalid(f"Tailscale won't take that from rackmap's account. Run this once on the machine, then try again: sudo tailscale set --operator={account()}")
    return status()
