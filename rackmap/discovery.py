"""Finding equipment on the network.

A scan walks the private address ranges it is given and, for each address that answers, collects
what can be learned without credentials: its name, its hardware address and maker, which common
services it offers, the title of its web page and (if it speaks SNMP) what it says it is. Where a
device can't be seen directly, a router can be asked for its table of addresses. Nothing found is
added to the inventory automatically; the person using rackmap decides, and says where it sits.
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import json
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, snmp

MAX_HOSTS = 4096
WORKERS = 64
QUICK_PORTS = (22, 80, 443, 445)
MORE_PORTS = (23, 3389, 5000, 8006, 8080, 8291, 8443, 9100)
PORT_NAMES = {22: "SSH", 23: "Telnet", 80: "web page", 443: "secure web page", 445: "file sharing", 3389: "Remote Desktop",
              5000: "web app", 8006: "Proxmox", 8080: "web page", 8291: "MikroTik Winbox", 8443: "secure web page", 9100: "printing"}
_OUI_FILE = Path(__file__).parent / "data" / "oui.json.gz"

_lock = threading.Lock()
_job: dict = {"state": "idle"}
_oui: dict[str, str] | None = None
_models: list[tuple[str, str, str, str]] | None = None


class Invalid(ValueError):
    """Message is written for the person using the app."""


# ---------- settings ----------

def parse_networks(text: str) -> list[ipaddress.IPv4Network]:
    """Accepts one network per line or separated by commas/spaces. Private IPv4 ranges only."""
    networks = []
    for part in re.split(r"[\s,;]+", text.strip()):
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise Invalid(f"“{part}” isn't a network. Write it like 192.168.1.0/24.") from None
        if net.version != 4:
            raise Invalid("Only IPv4 networks can be scanned for now.")
        if not (net.is_private and not net.is_loopback and not net.is_link_local):
            raise Invalid(f"{net} isn't a private network. rackmap only scans private ranges such as 10.x, 172.16–31.x and 192.168.x.")
        networks.append(net)
    if sum(n.num_addresses for n in networks) > MAX_HOSTS:
        raise Invalid(f"That is more than {MAX_HOSTS} addresses in one scan. Use smaller networks (a /24 is 256 addresses).")
    return networks


def own_networks() -> list[str]:
    """The private networks this machine is directly attached to, as suggestions."""
    found = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
        net = ipaddress.ip_interface(match.group(1)).network
        if net.is_private and net.num_addresses <= MAX_HOSTS and str(net) not in found:
            found.append(str(net))
    return found


def get_settings() -> dict:
    return {
        "networks": db.get_setting("discovery_networks") or "",
        "router": db.get_setting("discovery_router") or "",
        "community": db.get_setting("discovery_community") or "",
    }


def save_settings(networks: str, router: str, community: str) -> dict:
    parse_networks(networks)
    router = router.strip()
    if router:
        try:
            address = ipaddress.ip_address(router)
        except ValueError:
            raise Invalid("Enter the router's IP address, for example 192.168.1.1.") from None
        if not address.is_private:
            raise Invalid("The router address must be on a private network.")
    db.set_setting("discovery_networks", networks.strip())
    db.set_setting("discovery_router", router)
    db.set_setting("discovery_community", community.strip())
    return get_settings()


# ---------- what a single address tells us ----------

def vendor_for(mac: str) -> str:
    global _oui
    if _oui is None:
        _oui = json.loads(gzip.decompress(_OUI_FILE.read_bytes()))
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    if len(digits) != 12:
        return ""
    if int(digits[1], 16) & 2:
        return "Private (randomised) address"
    return _oui.get(digits[:6], "")


def neighbours() -> dict[str, str]:
    """Hardware addresses this machine has learned for directly attached devices (Linux)."""
    table: dict[str, str] = {}
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return table
    for line in lines:
        cols = line.split()
        if len(cols) >= 4 and cols[2] != "0x0" and cols[3] != "00:00:00:00:00:00":
            table[cols[0]] = cols[3].lower()
    return table


def ping(ip: str) -> bool:
    exe = shutil.which("ping")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-n", "-c", "1", "-W", "1", ip], capture_output=True, timeout=4).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def port_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def reverse_name(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def web_page(ip: str, secure: bool, port: int) -> tuple[str, str]:
    """(page title, server header) of the device's web page, either possibly empty."""
    try:
        if secure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # device certificates are almost always self-signed
            conn = http.client.HTTPSConnection(ip, port, timeout=3, context=context)
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=3)
        try:
            conn.request("GET", "/", headers={"User-Agent": "rackmap-discovery", "Accept": "text/html"})
            response = conn.getresponse()
            body = response.read(65536).decode("utf-8", errors="replace")
            server = (response.getheader("Server") or "")[:120]
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, ValueError):
        return "", ""
    match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", match.group(1)).strip()[:160] if match else ""
    if 300 <= response.status < 400 or re.match(r"^30\d\b", title):
        title = ""  # a redirect page says nothing about the device; the caller tries the next port
    return title, server


def probe(ip: str, community: str) -> dict | None:
    """Everything learnable about one address, or None if nothing answers there."""
    alive = ping(ip)
    ports = [p for p in QUICK_PORTS if port_open(ip, p)]
    if not alive and not ports:
        return None
    ports += [p for p in MORE_PORTS if port_open(ip, p)]
    host = {"ip": ip, "ports": sorted(ports), "hostname": reverse_name(ip), "sys_name": "", "sys_descr": "", "http_title": "", "http_server": ""}
    for port, secure in ((80, False), (443, True), (8080, False), (8443, True)):
        if port in ports:
            title, server = web_page(ip, secure, port)
            host["http_server"] = host["http_server"] or server
            if title:
                host["http_title"] = title
                break
    if community:
        try:
            host["sys_descr"] = (snmp.get_text(ip, community, snmp.SYS_DESCR) or "")[:300]
            host["sys_name"] = (snmp.get_text(ip, community, snmp.SYS_NAME) or "")[:120]
        except snmp.SnmpError:
            pass
    return host


# ---------- guessing the model ----------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def suggest_model(vendor: str, *texts: str) -> str | None:
    """A catalogue model whose name appears in what the device says about itself."""
    global _models
    if _models is None:
        _models = [(_norm(r["model"]), r["slug"], _norm(r["manufacturer"].split()[0]), r["manufacturer"])
                   for r in db.query("SELECT slug, manufacturer, model FROM device_types")]
    haystack = _norm(" ".join(texts))
    if len(haystack) < 5:
        return None
    vendor_norm = _norm(vendor)
    best: tuple[int, str] | None = None
    for model, slug, maker, _ in _models:
        if len(model) < 5 or model not in haystack:
            continue
        maker_seen = bool(maker) and (maker in haystack or maker in vendor_norm)
        if not maker_seen and len(model) < 8:
            continue  # short model numbers match too easily without the maker agreeing
        score = len(model) + (100 if maker_seen else 0)
        if best is None or score > best[0]:
            best = (score, slug)
    return best[1] if best else None


def looks_like(host: dict) -> str:
    ports = set(host["ports"])
    text = " ".join((host["sys_descr"], host["http_title"], host["http_server"], host["hostname"])).lower()
    if 9100 in ports or "printer" in text:
        return "Printer"
    if 8006 in ports or "proxmox" in text:
        return "Proxmox server"
    if 8291 in ports or "routeros" in text or "mikrotik" in text:
        return "MikroTik router or switch"
    if "pfsense" in text or "opnsense" in text:
        return "Firewall"
    if "idrac" in text or "ilo" in text or "ipmi" in text:
        return "Server management controller"
    if "switch" in text:
        return "Network switch"
    if "access point" in text or re.search(r"\bap\b", text):
        return "Wireless access point"
    if 3389 in ports:
        return "Windows computer"
    if "home assistant" in text:
        return "Home Assistant"
    if 445 in ports:
        return "Computer or NAS with file sharing"
    if ports & {80, 443, 8080, 8443} and 22 in ports:
        return "Server or network device"
    if ports & {80, 443, 8080, 8443}:
        return "Device with a web page"
    if 22 in ports:
        return "Linux or network device"
    return "Answers, but offers no common services (often a computer with its firewall on)"


# ---------- running a scan ----------

def status() -> dict:
    with _lock:
        return dict(_job)


def start_scan(probe_fn=probe, neighbours_fn=neighbours, wait: bool = False) -> dict:
    settings = get_settings()
    networks = parse_networks(settings["networks"])
    if not networks:
        raise Invalid("Enter at least one network to scan.")
    addresses = [str(ip) for net in networks for ip in (net.hosts() if net.prefixlen < 31 else net)]
    with _lock:
        if _job.get("state") == "running":
            raise Invalid("A scan is already running.")
        _job.clear()
        _job.update(state="running", total=len(addresses), done=0, found=0, started=time.time(), note="")
    thread = threading.Thread(target=_run, args=(addresses, settings, probe_fn, neighbours_fn), daemon=True, name="rackmap-scan")
    thread.start()
    if wait:
        thread.join()
    return status()


def _run(addresses: list[str], settings: dict, probe_fn, neighbours_fn) -> None:
    notes = []
    try:
        hosts: list[dict] = []

        def one(ip: str) -> None:
            result = probe_fn(ip, settings["community"])
            with _lock:
                _job["done"] += 1
                if result:
                    _job["found"] += 1
            if result:
                hosts.append(result)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(one, addresses))

        macs = dict(neighbours_fn())
        # A device that ignores pings and offers no services still answers at the hardware level
        # when it is on rackmap's own network, so it shows up in the neighbour table.
        answered = {h["ip"] for h in hosts}
        for ip in set(addresses) & set(macs) - answered:
            hosts.append({"ip": ip, "ports": [], "hostname": reverse_name(ip), "sys_name": "", "sys_descr": "", "http_title": "", "http_server": ""})
            with _lock:
                _job["found"] += 1
        if settings["router"]:
            if not settings["community"]:
                notes.append("The router wasn't asked for hardware addresses because no SNMP community is set.")
            else:
                try:
                    table = snmp.arp_table(settings["router"], settings["community"])
                    macs = {**table, **macs}
                    if not table:
                        notes.append("The router answered but had no addresses to share.")
                except snmp.SnmpError:
                    notes.append("The router didn't answer SNMP, so hardware addresses are only known for devices on rackmap's own network.")
        for host in hosts:
            host["mac"] = macs.get(host["ip"], "")
            host["vendor"] = vendor_for(host["mac"]) if host["mac"] else ""
            host["suggested_slug"] = suggest_model(host["vendor"], host["sys_descr"], host["http_title"], host["sys_name"], host["hostname"])
            _store(host)
        with _lock:
            _job.update(state="finished", finished=time.time(), note=" ".join(notes))
    except Exception as exc:  # a scan must never take the app down
        with _lock:
            _job.update(state="failed", finished=time.time(), note=f"The scan stopped unexpectedly ({type(exc).__name__}).")


def _store(host: dict) -> None:
    ports = json.dumps(host["ports"])
    existing = db.query("SELECT id FROM found_hosts WHERE ip = ?", (host["ip"],))
    values = (host["mac"], host["vendor"], host["hostname"], host["sys_name"], host["sys_descr"], host["http_title"],
              host["http_server"], ports, host["suggested_slug"])
    if existing:
        db.execute("UPDATE found_hosts SET mac = CASE WHEN ? != '' THEN ? ELSE mac END, vendor = CASE WHEN ? != '' THEN ? ELSE vendor END, "
                   "hostname = ?, sys_name = ?, sys_descr = ?, http_title = ?, http_server = ?, ports = ?, suggested_slug = ?, "
                   "last_seen = datetime('now') WHERE id = ?",
                   (host["mac"], host["mac"], host["vendor"], host["vendor"]) + values[2:] + (existing[0]["id"],))
    else:
        db.execute("INSERT INTO found_hosts (ip, mac, vendor, hostname, sys_name, sys_descr, http_title, http_server, ports, suggested_slug) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)", (host["ip"],) + values)


# ---------- what was found ----------

def list_hosts() -> list[dict]:
    devices = db.query("SELECT id, name, ip, mac FROM devices")
    by_mac = {d["mac"].lower(): d for d in devices if d["mac"]}
    by_ip = {d["ip"]: d for d in devices if d["ip"]}
    out = []
    for r in db.query("SELECT * FROM found_hosts ORDER BY ignored, ip"):
        host = dict(r)
        host["ports"] = json.loads(host["ports"])
        host["ignored"] = bool(host["ignored"])
        host["services"] = [PORT_NAMES.get(p, str(p)) for p in host["ports"]]
        host["looks_like"] = looks_like(host)
        known = (by_mac.get(host["mac"].lower()) if host["mac"] else None) or by_ip.get(host["ip"])
        host["device"] = {"id": known["id"], "name": known["name"]} if known else None
        host["best_name"] = host["sys_name"] or host["hostname"].split(".")[0] or host["http_title"] or ""
        out.append(host)
    out.sort(key=lambda h: (h["ignored"], h["device"] is not None, tuple(int(x) for x in h["ip"].split("."))))
    return out


def set_ignored(host_id: int, ignored: bool) -> None:
    if not db.query("SELECT 1 FROM found_hosts WHERE id = ?", (host_id,)):
        raise Invalid("That discovery result no longer exists.")
    db.execute("UPDATE found_hosts SET ignored = ? WHERE id = ?", (1 if ignored else 0, host_id))


def forget(host_id: int) -> None:
    db.execute("DELETE FROM found_hosts WHERE id = ?", (host_id,))
