"""Build rackmap's bundled equipment catalogue from the NetBox community device-type library.

    git clone --depth 1 https://github.com/netbox-community/devicetype-library
    python tools/build_catalog.py devicetype-library

The library is CC0 1.0 (public domain dedication). Only the facts rackmap draws with are kept:
maker, model, height, depth, which elevation images exist, and the ordered list of port types.
Images are not bundled (the set is over a gigabyte); rackmap fetches one the first time it is
shown and keeps it in its data directory.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import yaml

OUT = Path(__file__).resolve().parent.parent / "rackmap" / "catalog" / "catalog.json.gz"


def port_class(kind: str) -> str:
    """Collapse NetBox's interface types into the handful of shapes rackmap draws."""
    k = (kind or "").lower()
    if k in ("virtual", "lag", "bridge") or k.startswith("ieee802.11") or k.startswith("ieee802.15"):
        return ""  # nothing to draw
    if "qsfp" in k or "osfp" in k or "cfp" in k or "cxp" in k:
        return "qsfp"
    if "sfp" in k or "xfp" in k or "x2" in k or "gbic" in k or "xenpak" in k or k.endswith("fc"):
        return "sfp"
    if "base-t" in k or "base-tx" in k:
        return "rj45"
    return "other"


def run_length(classes: list[str]) -> list[list]:
    out: list[list] = []
    for c in classes:
        if out and out[-1][0] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out


def main(library: Path) -> None:
    images: dict[str, dict[str, str]] = {}
    for f in (library / "elevation-images").glob("*/*"):
        parts = f.name.rsplit(".", 2)
        if len(parts) == 3 and parts[1] in ("front", "rear"):
            images.setdefault(parts[0], {})[parts[1]] = f"{f.parent.name}/{f.name}"

    types = []
    for f in sorted((library / "device-types").glob("*/*.y*ml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        slug = d["slug"]
        ports = [port_class(i.get("type", "")) for i in (d.get("interfaces") or [])]
        ports = [p for p in ports if p]
        img = images.get(slug, {})
        entry = {
            "s": slug,
            "m": str(d["manufacturer"]),
            "n": str(d["model"]),
            "u": float(d.get("u_height") or 0),
        }
        if d.get("part_number"):
            entry["p"] = str(d["part_number"])
        if d.get("is_full_depth") is False:
            entry["h"] = 1  # half depth
        if ports:
            entry["i"] = run_length(ports)
        if d.get("console-ports"):
            entry["c"] = len(d["console-ports"])
        if d.get("console-server-ports"):
            entry["cs"] = len(d["console-server-ports"])
        if d.get("power-ports"):
            entry["w"] = len(d["power-ports"])
        if img.get("front"):
            entry["f"] = img["front"]
        if img.get("rear"):
            entry["r"] = img["rear"]
        types.append(entry)

    commit = subprocess.run(["git", "-C", str(library), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    doc = {
        "source": "https://github.com/netbox-community/devicetype-library",
        "licence": "CC0-1.0",
        "commit": commit,
        "types": types,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(OUT, "wb", mtime=0) as fh:
        fh.write(json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    print(f"{len(types)} device types from {len({t['m'] for t in types})} makers, "
          f"{sum(1 for t in types if 'f' in t or 'r' in t)} with images -> {OUT} ({OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
