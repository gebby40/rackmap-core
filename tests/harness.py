"""Shared by the tests: a fresh rackmap on a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient


def make_client():
    os.environ["RACKMAP_DATA"] = tempfile.mkdtemp(prefix="rackmap-test-")
    os.environ["RACKMAP_MONITOR"] = "0"  # tests drive the monitor by hand
    from rackmap import app as app_module, auth, cables, catalog, consoles, controllers, db, discovery, inventory, monitor, monitoring, remote, secrets_store, services, sshrun, sso

    db.close()
    addons = services.addon()   # None in the open-source part on its own
    extra = []
    if addons is not None:
        extra = [addons] + [importlib.import_module(f"rackmap.addons.{name}") for name in ("wiring", "netcli", "docker") + tuple(n for n in addons.SERVICES if n != "docker")]
    for module in (db, secrets_store, auth, catalog, services, sshrun, *extra, sso, inventory, discovery, consoles, controllers, monitor, monitoring, cables, remote, app_module):
        importlib.reload(module)
    return TestClient(app_module.app)


def signed_in():
    c = make_client()
    c.__enter__()
    c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
    return c
