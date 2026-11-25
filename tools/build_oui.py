"""Build rackmap's maker lookup (first three bytes of a MAC address -> maker name).

    curl -L -o oui.csv https://standards-oui.ieee.org/oui/oui.csv
    python tools/build_oui.py oui.csv

Source: the IEEE Registration Authority's public MA-L assignment list.
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "rackmap" / "data" / "oui.json.gz"
SUFFIXES = re.compile(
    r"[\s,]+(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|gmbh|ag|sa|s\.a\.|bv|b\.v\.|nv|ab|oy|as|a/s|"
    r"srl|s\.r\.l\.|spa|s\.p\.a\.|kg|plc|pte|pty|kk|k\.k\.|co\.,?\s*ltd)\.?$",
    re.IGNORECASE,
)


def tidy(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    for _ in range(3):
        name = SUFFIXES.sub("", name).strip(" ,.")
    return name


def main(source: Path) -> None:
    table: dict[str, str] = {}
    with source.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            prefix = row["Assignment"].strip().upper()
            if len(prefix) == 6 and row["Organization Name"].strip():
                table[prefix] = tidy(row["Organization Name"])
    with gzip.GzipFile(OUT, "wb", mtime=0) as fh:
        fh.write(json.dumps(table, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    print(f"{len(table)} prefixes -> {OUT} ({OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
