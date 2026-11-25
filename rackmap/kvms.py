"""KVMs: boxes that carry a machine's screen, keyboard and mouse somewhere else.

A KVM is a piece of equipment in the rack, with a number of ports; other equipment says which
port it is plugged into. If the KVM has a web page (PiKVM, and "digital" KVM switches from
Dell, Avocent, Raritan and others), rackmap links to it as the remote screen. A KVM without a
network connection can still be recorded, so the rack view shows what is plugged in where.

A PiKVM can do more: given an account on it, rackmap shows its screen and passes on keyboard,
mouse and power buttons (see pikvm.py). For every other kind rackmap stores no sign-in; the
KVM's own web page asks for that.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from . import addresses, db, pikvm, secrets_store

KINDS = {
    "pikvm": "PiKVM",
    "dell": "Dell KVM switch",
    "other": "Other KVM",
}
MAX_PORTS = 64


class Invalid(ValueError):
    """Message is written for the person using the app."""


def _kvm(r) -> dict:
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "kind_name": KINDS.get(r["kind"], r["kind"]),
            "url": r["url"], "port_count": r["port_count"], "device_id": r["device_id"],
            "username": r["username"], "live": _live(r)}


def _live(r) -> bool:
    """Can rackmap show this KVM's screen itself?"""
    return r["kind"] == "pikvm" and bool(r["url"]) and bool(r["username"]) and bool(r["secret"])


def list_kvms() -> list[dict]:
    kvms = [_kvm(r) for r in db.query("SELECT * FROM kvms ORDER BY name COLLATE NOCASE, id")]
    for kvm in kvms:
        kvm["ports"] = [
            {"id": r["id"], "number": r["number"],
             "device": {"id": r["device_id"], "name": r["device_name"]} if r["device_id"] and r["device_name"] else None}
            for r in db.query("SELECT p.id, p.number, p.device_id, d.name AS device_name FROM kvm_ports p "
                              "LEFT JOIN devices d ON d.id = p.device_id WHERE p.kvm_id = ? ORDER BY p.number", (kvm["id"],))
        ]
    return kvms


def ports_for_device(device_id: int) -> list[dict]:
    return [{"id": r["id"], "number": r["number"], "kvm_id": r["kvm_id"], "kvm_name": r["name"], "url": r["url"],
             "port_count": r["port_count"], "live": _live(r)}
            for r in db.query("SELECT p.id, p.number, p.kvm_id, k.name, k.url, k.port_count, k.kind, k.username, k.secret FROM kvm_ports p "
                              "JOIN kvms k ON k.id = p.kvm_id WHERE p.device_id = ? ORDER BY k.name, p.number", (device_id,))]


def id_for_device(device_id: int) -> int | None:
    rows = db.query("SELECT id FROM kvms WHERE device_id = ?", (device_id,))
    return rows[0]["id"] if rows else None


def _clean(values: dict) -> dict:
    name = str(values.get("name") or "").strip()[:80]
    kind = values.get("kind") or "other"
    url = str(values.get("url") or "").strip()
    if kind not in KINDS:
        raise Invalid("That kind of KVM isn't one rackmap knows.")
    if not name:
        raise Invalid("Give the KVM a name.")
    if url:
        if "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        try:
            parts.port  # raises on a port that isn't a number
            bad = parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 300
        except ValueError:
            bad = True
        if bad:
            raise Invalid("The KVM's web address should look like https://192.168.1.70 or https://pikvm.lan.")
        if addresses.is_public_ip(parts.hostname):
            raise Invalid("The KVM's address must be on a private network.")
    try:
        count = int(values.get("port_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if not 1 <= count <= MAX_PORTS:
        raise Invalid(f"A KVM has between 1 and {MAX_PORTS} ports.")
    device_id = values.get("device_id")
    if device_id is None or not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    username = str(values.get("username") or "").strip()[:64] if kind == "pikvm" else ""
    if username and not url:
        raise Invalid("Enter the PiKVM's web address so rackmap can sign in to it.")
    return {"name": name, "kind": kind, "url": url, "port_count": count, "device_id": device_id, "username": username}


def save(values: dict, kvm_id: int | None = None) -> dict:
    d = _clean(values)
    if kvm_id is not None and not db.query("SELECT 1 FROM kvms WHERE id = ?", (kvm_id,)):
        raise Invalid("That KVM no longer exists.")
    other = id_for_device(d["device_id"])
    if other is not None and other != kvm_id:
        raise Invalid("That piece of equipment is already set up as a KVM.")
    current = db.query("SELECT * FROM kvms WHERE id = ?", (kvm_id,))[0] if kvm_id is not None else None
    secret = cert_pem = ""
    if d["username"]:
        # rackmap signs in before saving anything; that is also when the certificate is remembered
        password = values.get("password") or ""
        if not password:
            if current is None or not current["secret"]:
                raise Invalid("Enter the password for that account.")
            password = secrets_store.open_sealed(current["secret"])
        relearn = current is None or current["url"] != d["url"] or not current["cert_pem"] or bool(values.get("password"))
        try:
            cert_pem = pikvm.learn_certificate(d["url"]) if relearn else current["cert_pem"]
            pikvm.Client(d["url"], d["username"], password, cert_pem).check()
        except pikvm.KvmError as exc:
            raise Invalid(str(exc)) from None
        secret = secrets_store.seal(password)
    row = (d["name"], d["kind"], d["url"], d["port_count"], d["device_id"], d["username"], secret, cert_pem)
    if kvm_id is None:
        kvm_id = db.execute("INSERT INTO kvms (name, kind, url, port_count, device_id, username, secret, cert_pem) VALUES (?,?,?,?,?,?,?,?)", row)
    else:
        db.execute("UPDATE kvms SET name=?, kind=?, url=?, port_count=?, device_id=?, username=?, secret=?, cert_pem=? WHERE id=?", row + (kvm_id,))
    have = {r["number"] for r in db.query("SELECT number FROM kvm_ports WHERE kvm_id = ?", (kvm_id,))}
    for number in range(1, d["port_count"] + 1):
        if number not in have:
            db.execute("INSERT INTO kvm_ports (kvm_id, number) VALUES (?, ?)", (kvm_id, number))
    db.execute("DELETE FROM kvm_ports WHERE kvm_id = ? AND number > ?", (kvm_id, d["port_count"]))
    db.execute("UPDATE kvm_ports SET device_id = NULL WHERE kvm_id = ? AND device_id = ?", (kvm_id, d["device_id"]))  # not plugged into itself
    return next(k for k in list_kvms() if k["id"] == kvm_id)


def delete(kvm_id: int) -> None:
    if not db.query("SELECT 1 FROM kvms WHERE id = ?", (kvm_id,)):
        raise Invalid("That KVM no longer exists.")
    db.execute("DELETE FROM kvm_ports WHERE kvm_id = ?", (kvm_id,))
    db.execute("DELETE FROM kvms WHERE id = ?", (kvm_id,))


def set_device_port(device_id: int, port_id: int | None) -> None:
    """Say which KVM port a piece of equipment is plugged into (None: none). A port serves one machine."""
    if not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That piece of equipment no longer exists.")
    if port_id is not None:
        rows = db.query("SELECT p.device_id, k.device_id AS kvm_device FROM kvm_ports p JOIN kvms k ON k.id = p.kvm_id WHERE p.id = ?", (port_id,))
        if not rows:
            raise Invalid("That KVM port no longer exists.")
        if rows[0]["kvm_device"] == device_id:
            raise Invalid("A KVM can't be plugged into its own port.")
        if rows[0]["device_id"] not in (None, device_id):
            raise Invalid("That port is already used by other equipment. Free it there first.")
    db.execute("UPDATE kvm_ports SET device_id = NULL WHERE device_id = ?", (device_id,))
    if port_id is not None:
        db.execute("UPDATE kvm_ports SET device_id = ? WHERE id = ?", (device_id, port_id))


def live_port(port_id: int) -> tuple[pikvm.Client, int | None, dict]:
    """For a KVM port rackmap can drive: a signed-in client, the port to select on a multi-port
    PiKVM Switch (counted from 0; None for a single-machine PiKVM), and a description."""
    rows = db.query("SELECT p.number, p.device_id, d.name AS device_name, k.* FROM kvm_ports p JOIN kvms k ON k.id = p.kvm_id "
                    "LEFT JOIN devices d ON d.id = p.device_id WHERE p.id = ?", (port_id,))
    if not rows:
        raise Invalid("That KVM port no longer exists.")
    r = rows[0]
    if not _live(r):
        raise Invalid("rackmap has no sign-in for this KVM. Open its own page instead, or add an account under Edit KVM.")
    try:
        password = secrets_store.open_sealed(r["secret"])
    except ValueError as exc:
        raise Invalid(f"{exc} Re-enter the KVM's password under Edit KVM.") from None
    about = {"kvm": r["name"], "number": r["number"], "port_count": r["port_count"], "device": r["device_name"] or "", "url": r["url"]}
    return pikvm.Client(r["url"], r["username"], password, r["cert_pem"]), (r["number"] - 1 if r["port_count"] > 1 else None), about
