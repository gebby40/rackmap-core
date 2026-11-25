"""Racks and the equipment placed in them.

Positions follow the usual convention: U1 is the bottom of the rack and a device's position is
the lowest U it occupies. Heights and positions may be in half units. A full-depth device blocks
both faces of the units it occupies; a half-depth device blocks only its own face.
"""

from __future__ import annotations

from . import catalog, db, kvms, services

MAX_RACK_U = 60
DEVICE_FIELDS = ("name", "type_slug", "manufacturer", "model", "u_height", "full_depth", "rack_id",
                 "position_u", "face", "ip", "mac", "serial", "admin_url", "notes")


class Invalid(ValueError):
    """The request can't be carried out; the message is written for the person using the app."""


def _device(r) -> dict:
    d = {k: r[k] for k in ("id",) + DEVICE_FIELDS}
    d["full_depth"] = bool(d["full_depth"])
    # What the rack view needs to draw it: whether photographs exist, and its ports.
    d["kind"] = catalog.get(d["type_slug"]) if d["type_slug"] else None
    d["console_ports"] = [dict(r) for r in db.query(
        "SELECT p.id, p.number, p.label, s.name AS server_name FROM console_ports p JOIN console_servers s ON s.id = p.server_id "
        "WHERE p.device_id = ? ORDER BY s.name, p.number", (d["id"],))]
    servers = db.query("SELECT id FROM console_servers WHERE device_id = ?", (d["id"],))
    d["console_server_id"] = servers[0]["id"] if servers else None
    d["kvm_id"] = kvms.id_for_device(d["id"])
    d["kvm_ports"] = kvms.ports_for_device(d["id"])
    d["has_controller"] = bool(db.query("SELECT 1 FROM controllers WHERE device_id = ?", (d["id"],)))
    d["services"] = services.for_device(d["id"])
    return d


def list_racks() -> list[dict]:
    racks = [dict(r) for r in db.query("SELECT id, name, height_u, notes FROM racks ORDER BY name COLLATE NOCASE, id")]
    for rack in racks:
        rack["devices"] = [_device(r) for r in db.query(
            "SELECT * FROM devices WHERE rack_id = ? AND position_u IS NOT NULL ORDER BY position_u DESC", (rack["id"],))]
    return racks


def unplaced_devices() -> list[dict]:
    return [_device(r) for r in db.query(
        "SELECT * FROM devices WHERE rack_id IS NULL OR position_u IS NULL ORDER BY name COLLATE NOCASE, id")]


def get_rack(rack_id: int) -> dict:
    rows = db.query("SELECT id, name, height_u, notes FROM racks WHERE id = ?", (rack_id,))
    if not rows:
        raise Invalid("That rack no longer exists.")
    return dict(rows[0])


def get_device(device_id: int) -> dict:
    rows = db.query("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not rows:
        raise Invalid("That piece of equipment no longer exists.")
    return _device(rows[0])


def _clean_rack(name: str, height_u: int) -> tuple[str, int]:
    name = (name or "").strip()
    if not name:
        raise Invalid("Give the rack a name.")
    if not 1 <= int(height_u) <= MAX_RACK_U:
        raise Invalid(f"A rack is between 1 and {MAX_RACK_U} units tall.")
    return name, int(height_u)


def create_rack(name: str, height_u: int, notes: str = "") -> dict:
    name, height_u = _clean_rack(name, height_u)
    return get_rack(db.execute("INSERT INTO racks (name, height_u, notes) VALUES (?, ?, ?)", (name, height_u, notes.strip())))


def update_rack(rack_id: int, name: str, height_u: int, notes: str = "") -> dict:
    get_rack(rack_id)
    name, height_u = _clean_rack(name, height_u)
    tallest = db.query("SELECT max(position_u + u_height - 1) AS top FROM devices WHERE rack_id = ? AND position_u IS NOT NULL", (rack_id,))[0]["top"]
    if tallest is not None and tallest > height_u:
        raise Invalid(f"Equipment reaches U{tallest:g} in this rack. Move or remove it before making the rack shorter.")
    db.execute("UPDATE racks SET name = ?, height_u = ?, notes = ? WHERE id = ?", (name, height_u, notes.strip(), rack_id))
    return get_rack(rack_id)


def delete_rack(rack_id: int) -> None:
    """Remove a rack. Its equipment is kept and becomes unplaced."""
    get_rack(rack_id)
    db.execute("UPDATE devices SET rack_id = NULL, position_u = NULL WHERE rack_id = ?", (rack_id,))
    db.execute("DELETE FROM racks WHERE id = ?", (rack_id,))


def _is_half_step(value: float) -> bool:
    return abs(value * 2 - round(value * 2)) < 1e-9


def _check_placement(d: dict, ignore_id: int | None) -> None:
    """Raise Invalid unless the device fits where it is being put."""
    if d["position_u"] is None:
        return  # not placed in a rack (yet)
    if d["rack_id"] is None:
        raise Invalid("Choose the rack this equipment goes in.")
    rack = get_rack(d["rack_id"])
    height = d["u_height"]
    if height <= 0:
        raise Invalid("This equipment has no rack height, so it can't be placed in a rack. Set its height in U first.")
    bottom, top = d["position_u"], d["position_u"] + height - 1
    if not _is_half_step(bottom) or bottom < 1:
        raise Invalid("Positions start at U1 and go up in whole or half units.")
    if top > rack["height_u"]:
        raise Invalid(f"That would reach U{top:g}, above the top of this {rack['height_u']}U rack.")
    for other in db.query("SELECT id, name, position_u, u_height, face, full_depth FROM devices WHERE rack_id = ? AND position_u IS NOT NULL", (d["rack_id"],)):
        if other["id"] == ignore_id:
            continue
        o_bottom, o_top = other["position_u"], other["position_u"] + other["u_height"] - 1
        overlaps = bottom <= o_top and o_bottom <= top
        same_side = other["face"] == d["face"] or other["full_depth"] or d["full_depth"]
        if overlaps and same_side:
            span = f"U{o_bottom:g}" if o_bottom == o_top else f"U{o_bottom:g}–U{o_top:g}"
            raise Invalid(f"“{other['name']}” is already at {span}.")


def _clean_device(values: dict, current: dict | None) -> dict:
    d = dict(current or {"type_slug": None, "manufacturer": "", "model": "", "u_height": 1.0, "full_depth": True,
                         "rack_id": None, "position_u": None, "face": "front", "ip": "", "mac": "", "serial": "",
                         "admin_url": "", "notes": "", "name": ""})
    for key in DEVICE_FIELDS:
        if key in values:
            d[key] = values[key]
    # Picking a catalogue type fills in maker, model, height and depth from it.
    if values.get("type_slug") and (current is None or values["type_slug"] != current.get("type_slug")):
        kind = catalog.get(values["type_slug"])
        if not kind:
            raise Invalid("That model isn't in the equipment database.")
        d.update(manufacturer=kind["manufacturer"], model=kind["model"], full_depth=kind["full_depth"])
        if "u_height" not in values or not values["u_height"]:
            d["u_height"] = kind["u_height"]
    for key in ("name", "manufacturer", "model", "ip", "mac", "serial", "admin_url", "notes"):
        d[key] = str(d[key] or "").strip()
    if not d["name"]:
        d["name"] = " ".join(x for x in (d["manufacturer"], d["model"]) if x).strip()
    if not d["name"]:
        raise Invalid("Give the equipment a name.")
    try:
        d["u_height"] = float(d["u_height"] or 0)
    except (TypeError, ValueError):
        raise Invalid("Height must be a number of rack units.") from None
    if d["u_height"] < 0 or d["u_height"] > MAX_RACK_U or not _is_half_step(d["u_height"]):
        raise Invalid("Height is in whole or half rack units.")
    d["full_depth"] = bool(d["full_depth"])
    if d["face"] not in ("front", "rear"):
        raise Invalid("Equipment faces the front or the rear of the rack.")
    if d["admin_url"] and not d["admin_url"].lower().startswith(("http://", "https://")):
        raise Invalid("The admin page address must start with http:// or https://.")
    if d["rack_id"] is not None:
        d["rack_id"] = int(d["rack_id"])
    if d["position_u"] is not None:
        try:
            d["position_u"] = float(d["position_u"])
        except (TypeError, ValueError):
            raise Invalid("Position must be a rack unit number.") from None
    if d["rack_id"] is None:
        d["position_u"] = None
    return d


def create_device(values: dict) -> dict:
    d = _clean_device(values, None)
    _check_placement(d, None)
    cols = ", ".join(DEVICE_FIELDS)
    marks = ", ".join("?" for _ in DEVICE_FIELDS)
    return get_device(db.execute(f"INSERT INTO devices ({cols}) VALUES ({marks})", tuple(d[k] for k in DEVICE_FIELDS)))


def update_device(device_id: int, values: dict) -> dict:
    current = get_device(device_id)
    d = _clean_device(values, {k: current[k] for k in DEVICE_FIELDS})
    _check_placement(d, device_id)
    sets = ", ".join(f"{k} = ?" for k in DEVICE_FIELDS)
    db.execute(f"UPDATE devices SET {sets} WHERE id = ?", tuple(d[k] for k in DEVICE_FIELDS) + (device_id,))
    return get_device(device_id)


def delete_device(device_id: int) -> None:
    get_device(device_id)
    db.execute("DELETE FROM devices WHERE id = ?", (device_id,))


def free_positions(rack_id: int, u_height: float, face: str, full_depth: bool, ignore_id: int | None = None) -> list[float]:
    """Every whole-unit position where a device of this size would fit, top of the rack first."""
    rack = get_rack(rack_id)
    out = []
    probe = {"rack_id": rack_id, "u_height": u_height, "face": face, "full_depth": full_depth, "position_u": None}
    for u in range(rack["height_u"], 0, -1):
        probe["position_u"] = float(u)
        try:
            _check_placement(dict(probe), ignore_id)
        except Invalid:
            continue
        out.append(float(u))
    return out
