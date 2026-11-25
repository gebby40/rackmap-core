"""Equipment catalogue: the bundled list of device types, and their elevation images.

The list comes from the NetBox community device-type library (CC0 1.0). It is loaded into the
database on start whenever the bundled file changes. Images are not bundled; the first time one
is asked for it is downloaded from the library and kept in the data directory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import db

BUNDLE = Path(__file__).parent / "catalog.json.gz"
IMAGE_BASE = "https://raw.githubusercontent.com/netbox-community/devicetype-library/master/elevation-images/"
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_SAFE_IMAGE_PATH = re.compile(r"^[^/\\]+/[A-Za-z0-9._+\-]+\.(front|rear)\.(png|jpg|jpeg|webp)$")


def load() -> int:
    """Bring the device_types table in line with the bundled catalogue. Returns the row count."""
    raw = BUNDLE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if db.get_setting("catalog_sha256") != digest:
        doc = json.loads(gzip.decompress(raw))
        rows = [
            (
                t["s"], t["m"], t["n"], t.get("p"), t["u"], 0 if t.get("h") else 1, t.get("f"), t.get("r"),
                json.dumps({"i": t.get("i", []), "c": t.get("c", 0), "cs": t.get("cs", 0), "w": t.get("w", 0)}, separators=(",", ":")),
                f'{t["m"]} {t["n"]} {t.get("p", "")}'.lower(),
            )
            for t in doc["types"]
        ]
        db.execute_many("INSERT INTO device_types VALUES (?,?,?,?,?,?,?,?,?,?)", rows, before="DELETE FROM device_types")
        db.set_setting("catalog_sha256", digest)
        db.set_setting("catalog_commit", doc.get("commit", ""))
    return db.query("SELECT count(*) AS n FROM device_types")[0]["n"]


def _row(r) -> dict:
    return {
        "slug": r["slug"],
        "manufacturer": r["manufacturer"],
        "model": r["model"],
        "part_number": r["part_number"] or "",
        "u_height": r["u_height"],
        "full_depth": bool(r["full_depth"]),
        "has_front_image": bool(r["front_image"]),
        "has_rear_image": bool(r["rear_image"]),
        "ports": json.loads(r["ports"]),
    }


def search(text: str, limit: int = 40) -> list[dict]:
    """Every word must appear somewhere in maker, model or part number. Rack-mountable types first."""
    words = [w for w in re.split(r"\s+", text.lower().strip()) if w][:6]
    if not words:
        return []
    where = " AND ".join("search LIKE ? ESCAPE '\\'" for _ in words)
    params = tuple("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words)
    rows = db.query(
        f"SELECT * FROM device_types WHERE {where} ORDER BY (u_height = 0), length(model), manufacturer, model LIMIT ?",
        params + (limit,),
    )
    return [_row(r) for r in rows]


def get(slug: str) -> dict | None:
    rows = db.query("SELECT * FROM device_types WHERE slug = ?", (slug,))
    return _row(rows[0]) if rows else None


def image(slug: str, face: str) -> tuple[bytes, str] | None:
    """Return (bytes, media type) for a device type's elevation image, or None if it has none
    or it could not be fetched. Fetched images are cached under <data>/images."""
    if face not in ("front", "rear"):
        return None
    rows = db.query(f"SELECT {face}_image AS path FROM device_types WHERE slug = ?", (slug,))
    path = rows[0]["path"] if rows else None
    if not path or not _SAFE_IMAGE_PATH.match(path):
        return None
    media = IMAGE_TYPES[path.rsplit(".", 1)[1].lower()]
    cache = db.DATA_DIR / "images" / path.replace("/", "__")
    if cache.exists():
        return cache.read_bytes(), media
    try:
        with urllib.request.urlopen(IMAGE_BASE + urllib.parse.quote(path), timeout=10) as r:  # noqa: S310 (fixed https origin)
            data = r.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if len(data) > MAX_IMAGE_BYTES or not data:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data, media
