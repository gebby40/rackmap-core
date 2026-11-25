"""Start rackmap:  python -m rackmap

Settings (environment variables, all optional):
  RACKMAP_DATA      where rackmap keeps its database and keys (default ./data)
  RACKMAP_HOST      address to listen on (default 0.0.0.0, every network interface)
  RACKMAP_PORT      port to listen on (default 8443 with HTTPS, 8080 without)
  RACKMAP_TLS       "on" (default): serve HTTPS.  "off": plain HTTP, only for use behind
                    something else that encrypts the connection on the same machine
  RACKMAP_TLS_CERT  your own certificate (PEM, with any chain) instead of the home-made one
  RACKMAP_TLS_KEY   its private key (PEM)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

from . import db, tls


def settings(env=os.environ) -> dict:
    mode = (env.get("RACKMAP_TLS") or "on").strip().lower()
    if mode not in ("on", "off"):
        raise SystemExit('RACKMAP_TLS must be "on" or "off".')
    out: dict = {"host": env.get("RACKMAP_HOST") or "0.0.0.0", "server_header": False}
    cert, key = env.get("RACKMAP_TLS_CERT"), env.get("RACKMAP_TLS_KEY")
    if mode == "on":
        if bool(cert) != bool(key):
            raise SystemExit("Set both RACKMAP_TLS_CERT and RACKMAP_TLS_KEY, or neither.")
        if cert:
            for path in (cert, key):
                if not Path(path).is_file():
                    raise SystemExit(f"Can't read {path}.")
        else:
            made_cert, made_key = tls.own_certificate(db.DATA_DIR)
            cert, key = str(made_cert), str(made_key)
            out["note"] = ("Using rackmap's own certificate; browsers will ask you to confirm it once. "
                           f"Its SHA-256 fingerprint is {tls.fingerprint(made_cert)}")
        out.update(ssl_certfile=cert, ssl_keyfile=key)
    else:
        out["note"] = "HTTPS is off: passwords and console sessions are sent unencrypted unless something in front of rackmap encrypts them."
    try:
        out["port"] = int(env.get("RACKMAP_PORT") or (8443 if mode == "on" else 8080))
    except ValueError:
        raise SystemExit("RACKMAP_PORT must be a number.") from None
    return out


def main() -> None:
    options = settings()
    note = options.pop("note", "")
    if note:
        print(f"rackmap: {note}", file=sys.stderr, flush=True)
    uvicorn.run("rackmap.app:app", **options)


if __name__ == "__main__":
    main()
