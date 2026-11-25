"""Whether recorded equipment is answering on the network right now.

A background thread checks every piece of equipment that has a private IP address about once a
minute: a ping, and failing that a connection attempt on a few common ports (plenty of devices
ignore pings). Results are kept in memory only.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, discovery

INTERVAL = 60
_status: dict[int, dict] = {}
_lock = threading.Lock()
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None


def _checkable(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return address.version == 4 and address.is_private and not address.is_loopback


def answers(ip: str) -> bool:
    return discovery.ping(ip) or any(discovery.port_open(ip, port, 0.5) for port in discovery.QUICK_PORTS)


def check_all(check=None) -> None:
    check = check or answers
    devices = [(r["id"], r["ip"].strip()) for r in db.query("SELECT id, ip FROM devices WHERE ip != ''") if _checkable(r["ip"])]
    now = time.time()
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda d: (d[0], bool(check(d[1]))), devices))
    with _lock:
        seen = set()
        for device_id, up in results:
            seen.add(device_id)
            previous = _status.get(device_id, {})
            _status[device_id] = {"up": up, "checked": now, "since": previous.get("since", now) if previous.get("up") == up else now}
        for device_id in list(_status):
            if device_id not in seen:
                del _status[device_id]


def snapshot() -> dict[int, dict]:
    with _lock:
        return {k: dict(v) for k, v in _status.items()}


def _loop() -> None:
    while not _stop.is_set():
        try:
            check_all()
        except Exception:  # noqa: BLE001 - monitoring must never take the app down
            pass
        _wake.wait(INTERVAL)
        _wake.clear()


def start() -> None:
    global _thread
    _stop.clear()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, daemon=True, name="rackmap-monitor")
        _thread.start()


def poke() -> None:
    """Check again soon, for example because equipment was just added or given an address."""
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()
