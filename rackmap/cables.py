"""Cables between pieces of equipment.

A cable joins two pieces of equipment and, where known, names the socket at each end. They are
entered by hand or accepted from suggestions: a monitoring source that knows which device is on
which switch port can propose the cable list, and the person confirms or dismisses each one.
Nothing is recorded without that confirmation.
"""

from __future__ import annotations

from . import db, monitoring


class Invalid(ValueError):
    """Message is written for the person using the app."""


def _ordered(a_device_id: int, a_port: str, b_device_id: int, b_port: str) -> tuple[int, str, int, str]:
    a_port, b_port = (a_port or "").strip()[:80], (b_port or "").strip()[:80]
    if a_device_id > b_device_id:
        return b_device_id, b_port, a_device_id, a_port
    return a_device_id, a_port, b_device_id, b_port


def _same(port_a: str, port_b: str) -> bool:
    """Two ways of writing a socket are the same cable end if they match, or if either is unknown."""
    return not port_a or not port_b or port_a.casefold() == port_b.casefold()


def list_cables() -> list[dict]:
    rows = db.query("SELECT c.id, c.a_device_id, c.a_port, c.b_device_id, c.b_port, c.note, a.name AS a_name, b.name AS b_name "
                    "FROM cables c JOIN devices a ON a.id = c.a_device_id JOIN devices b ON b.id = c.b_device_id ORDER BY a.name COLLATE NOCASE, c.a_port, c.id")
    return [dict(r) for r in rows]


def create(a_device_id: int, a_port: str, b_device_id: int, b_port: str, note: str = "") -> dict:
    if a_device_id == b_device_id:
        raise Invalid("A cable joins two different pieces of equipment.")
    known = {r["id"] for r in db.query("SELECT id FROM devices WHERE id IN (?, ?)", (a_device_id, b_device_id))}
    if known != {a_device_id, b_device_id}:
        raise Invalid("That equipment no longer exists.")
    a, ap, b, bp = _ordered(a_device_id, a_port, b_device_id, b_port)
    for row in db.query("SELECT a_port, b_port FROM cables WHERE a_device_id = ? AND b_device_id = ?", (a, b)):
        if row["a_port"].casefold() == ap.casefold() and row["b_port"].casefold() == bp.casefold():
            raise Invalid("That cable is already recorded.")
    for end_device, end_port in ((a, ap), (b, bp)):
        if end_port and db.query("SELECT 1 FROM cables WHERE (a_device_id = ? AND a_port = ? COLLATE NOCASE) OR (b_device_id = ? AND b_port = ? COLLATE NOCASE)",
                                 (end_device, end_port, end_device, end_port)):
            name = db.query("SELECT name FROM devices WHERE id = ?", (end_device,))[0]["name"]
            raise Invalid(f"Port {end_port} on “{name}” already has a cable. Remove that one first.")
    cable_id = db.execute("INSERT INTO cables (a_device_id, a_port, b_device_id, b_port, note) VALUES (?, ?, ?, ?, ?)", (a, ap, b, bp, (note or "").strip()[:200]))
    db.execute("DELETE FROM cable_dismissals WHERE a_device_id = ? AND b_device_id = ? AND a_port = ? AND b_port = ?", (a, b, ap, bp))
    return next(c for c in list_cables() if c["id"] == cable_id)


def delete(cable_id: int) -> None:
    if not db.query("SELECT 1 FROM cables WHERE id = ?", (cable_id,)):
        raise Invalid("That cable is no longer recorded.")
    db.execute("DELETE FROM cables WHERE id = ?", (cable_id,))


def dismiss(a_device_id: int, a_port: str, b_device_id: int, b_port: str) -> None:
    a, ap, b, bp = _ordered(a_device_id, a_port, b_device_id, b_port)
    db.execute("INSERT OR IGNORE INTO cable_dismissals (a_device_id, a_port, b_device_id, b_port) VALUES (?, ?, ?, ?)", (a, ap, b, bp))


def suggestions() -> list[dict]:
    """Cables rackmap has reason to believe exist that are neither recorded nor dismissed: from the
    monitoring source, and from the switches' own address tables."""
    recorded = [(c["a_device_id"], c["a_port"], c["b_device_id"], c["b_port"]) for c in list_cables()]
    dismissed = {(r["a_device_id"], r["a_port"].casefold(), r["b_device_id"], r["b_port"].casefold())
                 for r in db.query("SELECT a_device_id, a_port, b_device_id, b_port FROM cable_dismissals")}
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM devices")}
    out = []
    for s in (monitoring.cable_suggestions() if monitoring.configured() else []):
        a, ap, b, bp = _ordered(s["a_device_id"], s["a_port"], s["b_device_id"], s["b_port"])
        if (a, ap.casefold(), b, bp.casefold()) in dismissed:
            continue
        if any(ra == a and rb == b and _same(rap, ap) and _same(rbp, bp) for ra, rap, rb, rbp in recorded):
            continue
        out.append({"a_device_id": a, "a_port": ap, "a_name": names.get(a, ""), "b_device_id": b, "b_port": bp, "b_name": names.get(b, ""), "why": s["why"]})
    # ...and what rackmap works out itself from the switches' address tables, where the monitoring source hasn't already said it
    from . import services
    wiring = services.addon("wiring")
    if wiring is None:
        return sorted(out, key=lambda s: (s["a_name"].casefold(), s["a_port"], s["b_name"].casefold()))
    pairs = {frozenset((c["a_device_id"], c["b_device_id"])) for c in list_cables()} | {frozenset((s["a_device_id"], s["b_device_id"])) for s in out}
    for s in wiring.cable_suggestions():
        a, ap, b, bp = _ordered(s["a_device_id"], s["a_port"], s["b_device_id"], s["b_port"])
        if frozenset((a, b)) in pairs or (a, ap.casefold(), b, bp.casefold()) in dismissed or a not in names or b not in names:
            continue
        out.append({"a_device_id": a, "a_port": ap, "a_name": names[a], "b_device_id": b, "b_port": bp, "b_name": names[b], "why": s["why"]})
    out.sort(key=lambda s: (s["a_name"].casefold(), s["a_port"], s["b_name"].casefold()))
    return out
