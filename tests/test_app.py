"""End-to-end checks of setup, sign-in and sign-out against a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient

from rackmap import __version__

from harness import make_client, signed_in


def test_first_run_then_login_flow():
    with make_client() as c:
        assert c.get("/api/status").json() == {"version": __version__, "needs_setup": True, "user": None, "sso": None}
        assert c.get("/api/overview").status_code == 401

        # too-short password is refused
        assert c.post("/api/setup", json={"name": "admin", "password": "short"}).status_code == 422
        assert c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"}).status_code == 200
        assert c.get("/api/status").json()["user"] == "admin"
        assert c.get("/api/overview").json()["racks"] == []

        # setup can only happen once
        assert c.post("/api/setup", json={"name": "other", "password": "another long password"}).status_code == 409

        assert c.post("/api/logout", json={}).status_code == 200
        assert c.get("/api/overview").status_code == 401
        assert c.post("/api/login", json={"name": "admin", "password": "wrong password!"}).status_code == 401
        assert c.post("/api/login", json={"name": "admin", "password": "correct horse battery"}).status_code == 200
        assert c.get("/api/overview").status_code == 200


def test_lockout_after_repeated_failures():
    with make_client() as c:
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        c.post("/api/logout", json={})
        for _ in range(5):
            assert c.post("/api/login", json={"name": "admin", "password": "nope nope nope"}).status_code == 401
        # even the right password is refused during the lockout
        assert c.post("/api/login", json={"name": "admin", "password": "correct horse battery"}).status_code == 429


def test_front_end_and_headers():
    with make_client() as c:
        r = c.get("/")
        assert r.status_code == 200 and "rackmap" in r.text
        assert r.headers["x-frame-options"] == "DENY"
        script = c.get("/static/app.js")
        assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
        # an unchanged file is not sent again
        assert c.get("/static/app.js", headers={"If-None-Match": script.headers["etag"]}).status_code == 304
        assert c.get("/api/nope").status_code == 404
        # password hashes never leave the database in clear
        from rackmap import db
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        row = db.query("SELECT pw_hash FROM users")[0]["pw_hash"]
        assert row.startswith("scrypt$") and "correct horse" not in row


def test_catalog_search_and_placement_rules():
    c = signed_in()
    try:
        hits = c.get("/api/catalog", params={"q": "crs328 24p"}).json()["results"]
        assert hits and hits[0]["manufacturer"] == "MikroTik" and hits[0]["u_height"] == 1
        assert c.get("/api/catalog", params={"q": "zzzz-no-such-model"}).json()["results"] == []

        rack = c.post("/api/racks", json={"name": "Garage", "height_u": 12}).json()
        sw = c.post("/api/devices", json={"type_slug": hits[0]["slug"], "name": "Core", "rack_id": rack["id"], "position_u": 12}).json()
        assert sw["manufacturer"] == "MikroTik" and sw["full_depth"] is False and sw["kind"]["has_front_image"]

        # same units, same side: refused with a message that names what is in the way
        r = c.post("/api/devices", json={"name": "Shelf", "u_height": 1, "rack_id": rack["id"], "position_u": 12})
        assert r.status_code == 422 and "Core" in r.json()["detail"]
        # half-depth switch on the front leaves the rear of the same unit free for a half-depth device
        assert c.post("/api/devices", json={"name": "PDU", "u_height": 1, "full_depth": False, "face": "rear",
                                            "rack_id": rack["id"], "position_u": 12}).status_code == 200
        # a full-depth device blocks both sides
        assert c.post("/api/devices", json={"name": "Server", "u_height": 2, "rack_id": rack["id"], "position_u": 11}).status_code == 422
        server = c.post("/api/devices", json={"name": "Server", "u_height": 2, "rack_id": rack["id"], "position_u": 1}).json()
        # above the top of the rack
        assert c.post("/api/devices", json={"name": "Tall", "u_height": 4, "rack_id": rack["id"], "position_u": 10}).status_code == 422

        free = c.get(f"/api/racks/{rack['id']}/free", params={"u_height": 2}).json()["positions"]
        assert free[0] == 10 and 1 not in free and 2 not in free and 3 in free

        # moving a device onto itself (overlapping its own old place) is allowed
        assert c.patch(f"/api/devices/{server['id']}", json={"position_u": 2}).status_code == 200
        # shrinking the rack below its equipment is refused
        assert c.put(f"/api/racks/{rack['id']}", json={"name": "Garage", "height_u": 6}).status_code == 422

        # deleting the rack keeps the equipment, unplaced
        assert c.delete(f"/api/racks/{rack['id']}").status_code == 200
        over = c.get("/api/overview").json()
        assert over["racks"] == [] and len(over["unplaced"]) == 3
        assert c.delete(f"/api/devices/{sw['id']}").status_code == 200
        assert len(c.get("/api/overview").json()["unplaced"]) == 2
    finally:
        c.__exit__(None, None, None)


def test_equipment_needs_sign_in_and_sane_values():
    with make_client() as c:
        assert c.get("/api/catalog", params={"q": "dell"}).status_code == 401
        assert c.post("/api/racks", json={"name": "x", "height_u": 10}).status_code == 401
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        assert c.post("/api/racks", json={"name": "  ", "height_u": 10}).status_code == 422
        assert c.post("/api/racks", json={"name": "x", "height_u": 400}).status_code == 422
        assert c.post("/api/devices", json={"name": "x", "admin_url": "javascript:alert(1)"}).status_code == 422
        assert c.post("/api/devices", json={"type_slug": "not-a-real-model"}).status_code == 422
        assert c.get("/api/catalog/not-a-real-model/image/front").status_code == 404
        d = c.post("/api/devices", json={"manufacturer": "EnGenius", "model": "EWS357AP", "u_height": 0}).json()
        assert d["name"] == "EnGenius EWS357AP" and d["rack_id"] is None


def test_snmp_packets_round_trip():
    from rackmap import snmp

    request = snmp.encode_request("public", 0xA0, 1234, snmp.SYS_DESCR)
    # a known-good SNMPv2c GET for sysDescr.0 with community "public"
    assert request.hex() == "302702010104067075626c6963a01a020204d2020100020100300e300c06082b060102010101000500"
    binding = snmp._tlv(0x30, snmp._oid("1.3.6.1.2.1.4.22.1.2.7.192.168.7.20") + snmp._tlv(0x04, bytes.fromhex("020000a1b2c3")))
    pdu = snmp._tlv(0xA2, snmp._int(77) + snmp._int(0) + snmp._int(0) + snmp._tlv(0x30, binding))
    reply = snmp._tlv(0x30, snmp._int(1) + snmp._tlv(0x04, b"public") + pdu)
    assert snmp.decode_response(reply, 77) == ("1.3.6.1.2.1.4.22.1.2.7.192.168.7.20", 0x04, bytes.fromhex("020000a1b2c3"))
    import pytest
    with pytest.raises(snmp.SnmpError):
        snmp.decode_response(reply, 78)  # answer to someone else's question
    with pytest.raises(snmp.SnmpError):
        snmp.decode_response(reply[:20], 77)  # truncated


def test_discovery_scan_review_and_add():
    from rackmap import discovery

    c = signed_in()
    try:
        # only private networks, of a sensible size
        assert c.put("/api/discovery/settings", json={"networks": "8.8.8.0/24"}).status_code == 422
        assert c.put("/api/discovery/settings", json={"networks": "10.0.0.0/8"}).status_code == 422
        assert c.post("/api/discovery/scan", json={}).status_code == 422  # nothing to scan yet
        assert c.put("/api/discovery/settings", json={"networks": "192.168.50.0/29", "community": "public"}).status_code == 200
        view = c.get("/api/discovery").json()
        assert view["settings"]["community_set"] is True and "community" not in view["settings"]

        answers = {
            "192.168.50.1": {"ports": [22, 80, 8291], "hostname": "core.lan", "sys_name": "core", "sys_descr": "RouterOS CRS328-24P-4S+RM",
                             "http_title": "RouterOS router configuration page", "http_server": ""},
            "192.168.50.2": {"ports": [9100], "hostname": "", "sys_name": "", "sys_descr": "", "http_title": "", "http_server": ""},
        }

        def fake_probe(ip, community):
            return {"ip": ip, **answers[ip]} if ip in answers else None

        job = discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {"192.168.50.1": "48:a9:8a:00:00:01"}, wait=True)
        assert job["state"] == "finished" and job["total"] == 6 and job["found"] == 2

        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        core = hosts["192.168.50.1"]
        assert core["vendor"] == "Routerboard.com" and core["suggested_slug"] == "mikrotik-crs328-24p-4s-plus-rm"
        assert core["looks_like"] == "MikroTik router or switch" and core["best_name"] == "core" and core["device"] is None
        assert hosts["192.168.50.2"]["looks_like"] == "Printer"
        assert c.get("/api/overview").json()["new_found"] == 2

        # adding the equipment (with the found address) marks the result as recorded
        added = c.post("/api/devices", json={"type_slug": core["suggested_slug"], "name": "core", "ip": core["ip"], "mac": core["mac"]}).json()
        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        assert hosts["192.168.50.1"]["device"] == {"id": added["id"], "name": "core"}
        # ignoring hides a result from the count without deleting it
        assert c.post(f"/api/discovery/hosts/{hosts['192.168.50.2']['id']}/ignore", json={"ignored": True}).status_code == 200
        assert c.get("/api/overview").json()["new_found"] == 0
        # a silent device on the local network is still listed, from the neighbour table
        discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {"192.168.50.5": "58:47:ca:00:00:02", "10.9.9.9": "aa:aa:aa:aa:aa:aa"}, wait=True)
        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        assert hosts["192.168.50.5"]["ports"] == [] and "10.9.9.9" not in hosts
        c.post(f"/api/discovery/hosts/{hosts['192.168.50.5']['id']}/ignore", json={"ignored": True})
        # a second scan updates rather than duplicates
        discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {}, wait=True)
        again = c.get("/api/discovery").json()["hosts"]
        assert len(again) == 3 and {h["ip"]: h for h in again}["192.168.50.1"]["mac"] == "48:a9:8a:00:00:01"
    finally:
        c.__exit__(None, None, None)


def test_console_server_sessions_and_secrets():
    import json as jsonlib
    import sqlite3

    from fake_digi import FakeDigi
    from rackmap import db

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakeDigi(busy_ports={3}) as digi:
            body = {"name": "Rack console", "host": "127.0.0.1", "port": digi.port, "username": "root", "port_count": 4}
            # wrong password: nothing is saved, and the reason is readable
            r = c.post("/api/consoles", json={**body, "password": "nope"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.get("/api/consoles").json()["servers"] == []
            # public addresses are not accepted
            r = c.post("/api/consoles", json={**body, "host": "8.8.8.8", "password": "x"})
            assert r.status_code == 422 and "private network" in r.json()["detail"]  # refused before any connection is tried

            server = c.post("/api/consoles", json={**body, "password": "digi-secret"}).json()
            assert [p["number"] for p in server["ports"]] == [1, 2, 3, 4] and server["host_key_fingerprint"].startswith("SHA256:")
            # the password never comes back, and is not readable in the database file
            listing = c.get("/api/consoles").text
            assert "digi-secret" not in listing and "secret" not in server
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM console_servers").fetchone()[0]
            assert "digi-secret" not in raw and (db.DATA_DIR / "secret.key").stat().st_mode & 0o077 == 0

            # label a port and tie it to a piece of equipment
            device = c.post("/api/devices", json={"name": "Core switch", "u_height": 1}).json()
            port1 = server["ports"][0]
            assert c.put(f"/api/consoles/ports/{port1['id']}", json={"label": "core", "device_id": device["id"]}).status_code == 200
            over = c.get("/api/overview").json()
            assert over["unplaced"][0]["console_ports"][0]["number"] == 1

            # a console needs a signed-in browser on rackmap's own page
            import pytest
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(f"/ws/console/{port1['id']}", headers={"origin": "http://evil.example"}):
                    pass

            with c.websocket_connect(f"/ws/console/{port1['id']}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
                first = ws.receive_bytes()
                # the sign-in exchange is not shown; the device's own output is
                assert first == b"device-on-port-1 login: " and b"Password" not in first
                ws.send_text(jsonlib.dumps({"t": "size", "cols": 120, "rows": 40}))
                ws.send_text(jsonlib.dumps({"t": "in", "d": "hello\r"}))
                assert ws.receive_bytes() == b"echo:hello\r"
                ws.send_text(jsonlib.dumps({"t": "in", "d": "\u0004"}))  # device hangs up
                assert jsonlib.loads(ws.receive_text())["state"] == "closed"

            # a port someone else is using says so
            with c.websocket_connect(f"/ws/console/{server['ports'][2]['id']}", headers=origin) as ws:
                ws.receive_text()
                failed = jsonlib.loads(ws.receive_text())
                assert failed["state"] == "failed" and "in use" in failed["message"]

            # editing without a password keeps the saved one; shrinking drops the extra ports
            edited = c.put(f"/api/consoles/{server['id']}", json={**body, "name": "Renamed", "port_count": 2}).json()
            assert edited["name"] == "Renamed" and len(edited["ports"]) == 2 and edited["ports"][0]["label"] == "core"

            # a changed host key is refused until the password is entered again
            digi.new_identity()
            with c.websocket_connect(f"/ws/console/{port1['id']}", headers=origin) as ws:
                ws.receive_text()
                failed = jsonlib.loads(ws.receive_text())
                assert failed["state"] == "failed" and "identity" in failed["message"]
            assert c.put(f"/api/consoles/{server['id']}", json={**body, "port_count": 2}).status_code == 422
            relearned = c.put(f"/api/consoles/{server['id']}", json={**body, "port_count": 2, "password": "digi-secret"}).json()
            assert relearned["host_key_fingerprint"] != server["host_key_fingerprint"]

            # deleting the equipment unties the port; deleting the server removes its ports
            c.delete(f"/api/devices/{device['id']}")
            assert c.get("/api/consoles").json()["servers"][0]["ports"][0]["device"] is None
            assert c.delete(f"/api/consoles/{server['id']}").status_code == 200
            assert c.get("/api/consoles").json()["servers"] == []
    finally:
        c.__exit__(None, None, None)


def test_console_socket_needs_sign_in():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with make_client() as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/console/1", headers={"origin": "http://testserver"}):
                pass


def test_management_controller_power_and_console():
    import json as jsonlib
    import sqlite3

    from fake_digi import FakeDigi
    from fake_redfish import FakeRedfish
    from rackmap import controllers, db

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakeRedfish() as bmc, FakeDigi(username="root", password="bmc-secret") as ssh:
            controllers.SSH_PORT = ssh.port
            # the fake listens on a high port; tell the client where
            real_init = controllers.Redfish.__init__
            controllers.Redfish.__init__ = lambda self, host, username, password, pinned, port=None: real_init(self, host, username, password, pinned, bmc.port)

            server = c.post("/api/devices", json={"name": "PowerEdge", "u_height": 2, "ip": "10.99.0.5"}).json()
            assert server["has_controller"] is False
            url = f"/api/devices/{server['id']}/controller"
            body = {"host": "127.0.0.1", "username": "root", "console_command": "console com2", "kvm_url": "https://kvm.lan/"}

            r = c.put(url, json={**body, "host": "8.8.8.8", "password": "x"})
            assert r.status_code == 422 and "private network" in r.json()["detail"]
            # nothing is saved unless rackmap can sign in
            r = c.put(url, json={**body, "password": "wrong"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.get(url).json()["controller"] is None
            assert c.put(url, json={**body, "password": "bmc-secret", "kvm_url": "javascript:alert(1)"}).status_code == 422
            assert c.put(url, json={**body, "password": "bmc-secret", "console_command": "console com2; reboot"}).status_code == 422

            saved = c.put(url, json={**body, "password": "bmc-secret"}).json()["controller"]
            assert saved["has_console"] and "password" not in saved and "secret" not in saved
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM controllers").fetchone()[0]
            assert "bmc-secret" not in raw
            assert c.get("/api/overview").json()["unplaced"][0]["has_controller"] is True

            # saving forgets what was known, so nothing is remembered until the controller is asked
            assert c.get(url + "/status?last=true").json() == {"known": False}
            before = bmc.connections
            status = c.get(url + "/status").json()
            assert bmc.connections == before + 1  # five questions, one connection
            assert status["power_state"] == "On" and status["model"] == "PowerEdge R720xd" and status["watts"] == 250
            # ...and then the last answer is available at once, without asking again
            kept = c.get(url + "/status?last=true").json()
            assert kept["known"] and kept["power_state"] == "On" and bmc.connections == before + 1
            assert status["temperatures"] == [{"name": "Inlet Temp", "celsius": 21}]
            # only the actions this controller offers are listed ("restart" maps to ForceRestart here)
            assert set(status["actions"]) == {"on", "shutdown", "restart", "off"}

            assert c.post(url + "/power", json={"action": "shutdown"}).json() == {"sent": "GracefulShutdown"}
            assert c.get(url + "/status?last=true").json() == {"known": False}  # stale after a power action
            assert c.get(url + "/status").json()["power_state"] == "Off"
            assert c.post(url + "/power", json={"action": "restart"}).json() == {"sent": "ForceRestart"}
            assert c.post(url + "/power", json={"action": "explode"}).status_code == 422
            assert bmc.resets == ["GracefulShutdown", "ForceRestart"]

            # the text console runs in the same kind of terminal as a console-server port
            with c.websocket_connect(f"/ws/controller/{server['id']}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
                assert ws.receive_bytes().endswith(b"server login: ")
                ws.send_text(jsonlib.dumps({"t": "in", "d": "root\r"}))
                assert ws.receive_bytes() == b"echo:root\r"
                # End the session from the far end before leaving, as the console-server test does.
                # Walking away mid-session made the test client cancel the handler while it was
                # still running, which failed about one run in six on Python 3.14.
                ws.send_text(jsonlib.dumps({"t": "in", "d": "\u0004"}))
                assert jsonlib.loads(ws.receive_text())["state"] == "closed"

            # a different certificate at the same address is refused until the password is re-entered
            bmc.new_identity()
            r = c.get(url + "/status")
            assert r.status_code == 502 and "certificate has changed" in r.json()["detail"]
            assert c.post(url + "/power", json={"action": "off"}).status_code == 502 and bmc.resets[-1] == "ForceRestart"
            assert c.put(url, json=body).status_code == 422
            assert c.put(url, json={**body, "password": "bmc-secret"}).status_code == 200
            assert c.get(url + "/status").status_code == 200

            # removing the equipment removes its controller record
            c.delete(f"/api/devices/{server['id']}")
            assert sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT count(*) FROM controllers").fetchone()[0] == 0
    finally:
        c.__exit__(None, None, None)


def test_live_status_only_checks_private_addresses():
    from rackmap import monitor

    c = signed_in()
    try:
        up = c.post("/api/devices", json={"name": "switch", "u_height": 1, "ip": "192.168.7.2"}).json()
        down = c.post("/api/devices", json={"name": "nas", "u_height": 1, "ip": "192.168.7.3"}).json()
        c.post("/api/devices", json={"name": "cloud thing", "u_height": 0, "ip": "8.8.8.8"})
        c.post("/api/devices", json={"name": "no address", "u_height": 0})
        asked = []
        monitor.check_all(check=lambda ip: asked.append(ip) or ip.endswith(".2"))
        assert sorted(asked) == ["192.168.7.2", "192.168.7.3"]  # never a public address
        live = c.get("/api/live").json()["devices"]
        assert live[str(up["id"])]["up"] is True and live[str(down["id"])]["up"] is False and len(live) == 2
        c.delete(f"/api/devices/{down['id']}")
        monitor.check_all(check=lambda ip: True)
        assert list(c.get("/api/live").json()["devices"]) == [str(up["id"])]
    finally:
        c.__exit__(None, None, None)


def test_https_is_on_by_default_with_a_certificate_rackmap_makes(tmp_path):
    import ssl

    import pytest
    from cryptography import x509
    from rackmap import __main__ as launcher
    from rackmap import db, tls

    cert, key = tls.own_certificate(tmp_path)
    assert (key.stat().st_mode & 0o777) == 0o600
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)  # a usable pair
    names = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    before = cert.read_bytes()
    assert tls.own_certificate(tmp_path) == (cert, key) and cert.read_bytes() == before  # kept, not remade

    real = db.DATA_DIR
    db.DATA_DIR = tmp_path
    try:
        on = launcher.settings({})
        assert on["port"] == 8443 and on["ssl_certfile"] == str(cert) and "fingerprint" in on["note"]
        off = launcher.settings({"RACKMAP_TLS": "off"})
        assert off["port"] == 8080 and "ssl_certfile" not in off and "unencrypted" in off["note"]
        own = launcher.settings({"RACKMAP_TLS_CERT": str(cert), "RACKMAP_TLS_KEY": str(key), "RACKMAP_PORT": "443"})
        assert own["port"] == 443 and "note" not in own
        with pytest.raises(SystemExit):
            launcher.settings({"RACKMAP_TLS_CERT": str(cert)})
        with pytest.raises(SystemExit):
            launcher.settings({"RACKMAP_TLS": "maybe"})
    finally:
        db.DATA_DIR = real


def test_console_server_as_equipment_offers_its_ports_to_the_rest():
    from fake_digi import FakeDigi

    c = signed_in()
    try:
        with FakeDigi() as digi:
            digi_box = c.post("/api/devices", json={"name": "Digi CM 48", "u_height": 1}).json()
            firewall = c.post("/api/devices", json={"name": "Firewall", "u_height": 1}).json()
            switch = c.post("/api/devices", json={"name": "Switch", "u_height": 1}).json()
            assert digi_box["console_server_id"] is None
            assert c.get("/api/overview").json()["console_servers"] == []

            body = {"name": "Digi CM 48", "host": "127.0.0.1", "port": digi.port, "username": "root", "password": "digi-secret", "port_count": 4}
            assert c.post("/api/consoles", json={**body, "device_id": 9999}).status_code == 422
            server = c.post("/api/consoles", json={**body, "device_id": digi_box["id"]}).json()
            assert server["device_id"] == digi_box["id"]
            overview = c.get("/api/overview").json()
            assert overview["console_servers"][0]["id"] == server["id"] and "secret" not in str(overview["console_servers"])
            by_name = {d["name"]: d for d in overview["unplaced"]}
            assert by_name["Digi CM 48"]["console_server_id"] == server["id"] and by_name["Firewall"]["console_server_id"] is None

            port1, port2 = server["ports"][0]["id"], server["ports"][1]["id"]
            url = f"/api/devices/{firewall['id']}/console-port"
            assert c.put(url, json={"port_id": port1}).status_code == 200
            ports = c.get("/api/consoles").json()["servers"][0]["ports"]
            assert ports[0]["device"]["id"] == firewall["id"] and ports[0]["label"] == "Firewall"  # an empty label takes the name
            # a port serves one piece of equipment, and the console server isn't plugged into itself
            assert c.put(f"/api/devices/{switch['id']}/console-port", json={"port_id": port1}).status_code == 422
            assert c.put(f"/api/devices/{digi_box['id']}/console-port", json={"port_id": port2}).status_code == 422
            assert c.put(url, json={"port_id": 9999}).status_code == 422
            # moving to another port frees the first; "not connected" frees it altogether
            assert c.put(url, json={"port_id": port2}).status_code == 200
            ports = c.get("/api/consoles").json()["servers"][0]["ports"]
            assert ports[0]["device"] is None and ports[1]["device"]["id"] == firewall["id"]
            assert c.put(url, json={"port_id": None}).status_code == 200
            assert all(p["device"] is None for p in c.get("/api/consoles").json()["servers"][0]["ports"])

            # deleting the equipment keeps the console server, just no longer tied to it
            assert c.delete(f"/api/devices/{digi_box['id']}").status_code == 200
            assert c.get("/api/consoles").json()["servers"][0]["device_id"] is None
    finally:
        c.__exit__(None, None, None)


def test_kvm_as_equipment_offers_its_ports_to_the_rest():
    c = signed_in()
    try:
        kvm_box = c.post("/api/devices", json={"name": "Dell KVM", "u_height": 1}).json()
        server = c.post("/api/devices", json={"name": "Server", "u_height": 2}).json()
        other = c.post("/api/devices", json={"name": "Other", "u_height": 1}).json()
        assert kvm_box["kvm_id"] is None and server["kvm_ports"] == []
        assert c.get("/api/overview").json()["kvms"] == []

        body = {"name": "Dell KVM", "kind": "dell", "url": "192.168.1.40", "port_count": 8, "device_id": kvm_box["id"]}
        assert c.post("/api/kvms", json={**body, "url": "javascript:alert(1)"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "url": "https://8.8.8.8"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "kind": "nope"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "device_id": 9999}).status_code == 422
        kvm = c.post("/api/kvms", json=body).json()
        assert kvm["url"] == "https://192.168.1.40" and len(kvm["ports"]) == 8 and kvm["kind_name"] == "Dell KVM switch"
        assert c.post("/api/kvms", json=body).status_code == 422  # one KVM per piece of equipment
        assert c.post("/api/kvms", json={**body, "url": "", "device_id": other["id"], "port_count": 1}).json()["url"] == ""  # not on the network

        port1, port2 = kvm["ports"][0]["id"], kvm["ports"][1]["id"]
        url = f"/api/devices/{server['id']}/kvm-port"
        assert c.put(url, json={"port_id": port1}).status_code == 200
        by_name = {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}
        assert by_name["Dell KVM"]["kvm_id"] == kvm["id"]
        assert by_name["Server"]["kvm_ports"][0]["number"] == 1 and by_name["Server"]["kvm_ports"][0]["url"] == "https://192.168.1.40"
        assert c.put(f"/api/devices/{kvm_box['id']}/kvm-port", json={"port_id": port2}).status_code == 422  # not into itself
        spare = c.post("/api/devices", json={"name": "Spare", "u_height": 1}).json()
        assert c.put(f"/api/devices/{spare['id']}/kvm-port", json={"port_id": port1}).status_code == 422  # port taken
        assert c.put(url, json={"port_id": port2}).status_code == 200  # moving frees the first
        ports = c.get("/api/overview").json()["kvms"][0]["ports"]
        assert ports[0]["device"] is None and ports[1]["device"]["id"] == server["id"]

        # fewer ports drops the ones that go; removing the equipment removes its KVM set-up
        assert len(c.put(f"/api/kvms/{kvm['id']}", json={**body, "port_count": 1}).json()["ports"]) == 1
        assert {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}["Server"]["kvm_ports"] == []
        assert c.delete(f"/api/devices/{kvm_box['id']}").status_code == 200
        assert [k["name"] for k in c.get("/api/overview").json()["kvms"]] == ["Dell KVM"]  # only the one on "Other" is left
        assert c.get("/api/overview").json()["kvms"][0]["device_id"] == other["id"]
    finally:
        c.__exit__(None, None, None)


def test_pikvm_screen_keyboard_mouse_and_power():
    import json as jsonlib
    import sqlite3
    import time

    from fake_pikvm import JPEG, FakePiKVM
    from rackmap import db, pikvm

    # what the browser sends becomes only the event shapes PiKVM knows
    assert pikvm.translate({"t": "key", "code": "KeyA", "down": True}) == [{"event_type": "key", "event": {"key": "KeyA", "state": True}}]
    assert pikvm.translate({"t": "key", "code": "../../x", "down": True}) == []
    assert pikvm.translate({"t": "move", "x": 0.5, "y": 0}) == [{"event_type": "mouse_move", "event": {"to": {"x": 0, "y": -32768}}}]
    assert pikvm.translate({"t": "move", "x": 9, "y": "no"}) == []
    assert pikvm.translate({"t": "button", "b": "fire", "down": True}) == []
    assert [e["event"] for e in pikvm.translate({"t": "shortcut", "keys": ["ControlLeft", "AltLeft", "Delete"]})][2:4] == [
        {"key": "Delete", "state": True}, {"key": "Delete", "state": False}]
    assert pikvm.translate({"t": "raw", "event_type": "anything"}) == []
    # state events, under the names of both current and older PiKVM software
    for name in ("atx", "atx_state"):
        assert pikvm.summarize({"event_type": name, "event": {"enabled": True, "leds": {"power": True}}}) == {"state": "power", "wired": True, "on": True}
    assert pikvm.summarize({"event_type": "streamer", "event": {"streamer": None}}) is None
    assert pikvm.summarize({"event_type": "streamer_state", "event": {"streamer": {"source": {"online": False}}}}) == {"state": "signal", "online": False}

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakePiKVM() as pi:
            box = c.post("/api/devices", json={"name": "PiKVM", "u_height": 0}).json()
            nas = c.post("/api/devices", json={"name": "NAS", "u_height": 2}).json()
            body = {"name": "PiKVM", "kind": "pikvm", "url": pi.url, "port_count": 1, "device_id": box["id"], "username": "admin"}

            # nothing is saved unless rackmap can sign in
            r = c.post("/api/kvms", json={**body, "password": "wrong"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.post("/api/kvms", json=body).status_code == 422  # no password at all
            assert c.get("/api/overview").json()["kvms"] == []

            kvm = c.post("/api/kvms", json={**body, "password": "kvm-secret"}).json()
            assert kvm["live"] is True and "secret" not in kvm and "password" not in kvm and "cert_pem" not in kvm
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM kvms").fetchone()[0]
            assert "kvm-secret" not in raw
            # other details can change without re-entering the password
            assert c.put(f"/api/kvms/{kvm['id']}", json={**body, "name": "PiKVM on the NAS"}).json()["live"] is True

            port = kvm["ports"][0]["id"]
            assert c.put(f"/api/devices/{nas['id']}/kvm-port", json={"port_id": port}).status_code == 200
            mine = {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}["NAS"]["kvm_ports"][0]
            assert mine["live"] is True and mine["id"] == port

            url = f"/api/kvm-ports/{port}"
            assert c.get(url).json()["device"] == "NAS"
            shot = c.get(url + "/snapshot")
            assert shot.content == JPEG and shot.headers["content-type"] == "image/jpeg"
            pi.stream_refusals = 2  # capture takes a moment to start on a real PiKVM; rackmap waits for it
            stream = c.get(url + "/stream")
            assert stream.headers["content-type"].startswith("multipart/x-mixed-replace") and stream.content.count(JPEG) == 3
            assert pi.stream_tries == 3

            assert c.get(url + "/power").json() == {"wired": True, "on": True, "busy": False}
            assert c.post(url + "/power", json={"action": "hold"}).status_code == 200
            assert c.post(url + "/power", json={"action": "explode"}).status_code == 502
            assert pi.calls == [("/api/atx/click", "power_long")] and c.get(url + "/power").json()["on"] is False
            assert c.post(url + "/text", json={"text": "hello\n"}).status_code == 200 and pi.typed == ["hello\n"]

            # the screen's control connection: state comes back, input goes through translated
            with c.websocket_connect(f"/ws/kvm/{port}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                opened = jsonlib.loads(ws.receive_text())
                assert opened["state"] == "open" and opened["device"] == "NAS"
                assert jsonlib.loads(ws.receive_text()) == {"state": "power", "wired": True, "on": False}
                assert jsonlib.loads(ws.receive_text()) == {"state": "signal", "online": True}
                assert pi.viewers == 1  # which is what keeps a real PiKVM's video running
                ws.send_text(jsonlib.dumps({"t": "key", "code": "Enter", "down": True}))
                ws.send_text("not json")
                ws.send_text(jsonlib.dumps({"event_type": "key", "event": {"key": "Sneaky"}}))  # not passed through as-is
                ws.send_text(jsonlib.dumps({"t": "button", "b": "left", "down": True}))
                for _ in range(40):
                    if len(pi.events) >= 2:
                        break
                    time.sleep(0.05)
            assert pi.events == [{"event_type": "key", "event": {"key": "Enter", "state": True}},
                                 {"event_type": "mouse_button", "event": {"button": "left", "state": True}}]
            # a page on another site can't open the control connection
            import pytest
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(f"/ws/kvm/{port}", headers={"origin": "http://evil.example"}):
                    pass

            # a multi-port PiKVM Switch is pointed at the right machine first, and its buttons are per port
            c.put(f"/api/kvms/{kvm['id']}", json={**body, "port_count": 4})
            ports = c.get("/api/overview").json()["kvms"][0]["ports"]
            c.put(f"/api/devices/{nas['id']}/kvm-port", json={"port_id": ports[2]["id"]})
            pi.calls.clear()
            with c.websocket_connect(f"/ws/kvm/{ports[2]['id']}", headers=origin) as ws:
                ws.receive_text()
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
            c.post(f"/api/kvm-ports/{ports[2]['id']}/power", json={"action": "press"})
            assert pi.calls == [("/api/switch/set_active", 2), ("/api/switch/atx/click", 2, "power")]

        # a different certificate at the same address is refused before the password is sent
        with FakePiKVM() as impostor:
            sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("UPDATE kvms SET url = ?", (impostor.url,)).connection.commit()
            r = c.get(f"/api/kvm-ports/{ports[2]['id']}/snapshot")
            assert r.status_code == 502 and "certificate" in r.json()["detail"]

        # without an account a PiKVM is only a link, and the screen endpoints say so
        plain = c.post("/api/devices", json={"name": "Other PiKVM", "u_height": 0}).json()
        other = c.post("/api/kvms", json={"name": "Other PiKVM", "kind": "pikvm", "url": "https://192.168.1.50", "port_count": 1, "device_id": plain["id"]}).json()
        assert other["live"] is False
        assert c.get(f"/api/kvm-ports/{other['ports'][0]['id']}/snapshot").status_code == 422
    finally:
        c.__exit__(None, None, None)


def test_monitoring_source_health_alerts_and_links():
    from fake_librenms import FakeLibreNMS
    from rackmap import db, monitoring

    c = signed_in()
    fake = FakeLibreNMS()
    port = fake.start()
    try:
        # nothing shows until a source is set up
        assert c.get("/api/monitoring").json()["source"] is None
        assert c.get("/api/overview").json()["monitoring"] is False
        assert c.get("/api/live").json()["health"] == {}

        url = f"http://127.0.0.1:{port}"
        assert c.put("/api/monitoring", json={"url": url}).status_code == 422            # a token is needed
        assert c.put("/api/monitoring", json={"url": "ftp://x", "token": "t"}).status_code == 422
        refused = c.put("/api/monitoring", json={"url": url, "token": "wrong"})
        assert refused.status_code == 502 and "token" in refused.json()["detail"]
        assert c.get("/api/monitoring").json()["source"] is None                           # a failed check saves nothing
        saved = c.put("/api/monitoring", json={"url": url + "/", "token": "lnms-token"})
        assert saved.status_code == 200 and saved.json()["source"] == {"kind": "librenms", "url": url, "pinned": False, "says": "LibreNMS 26.11.0"}
        assert "lnms-token" not in saved.text and "lnms-token" not in db.get_setting("monitoring")  # stored sealed, never sent back
        assert c.get("/api/overview").json()["monitoring"] is True

        switch = c.post("/api/devices", json={"name": "switch", "u_height": 1, "ip": "192.168.7.2"}).json()   # matched by address
        nas = c.post("/api/devices", json={"name": "NAS", "u_height": 2}).json()                              # matched by name (nas.lan)
        printer = c.post("/api/devices", json={"name": "printer", "u_height": 0}).json()                      # not monitored
        live = c.get("/api/live").json()
        assert live["health"][str(switch["id"])] == {"level": "warning", "why": "Port Speed Degraded"}
        assert live["health"][str(nas["id"])]["level"] == "alert" and str(printer["id"]) not in live["health"]
        assert live["alerts"] == 2

        view = c.get("/api/monitoring").json()
        assert [(a["device_id"], a["level"]) for a in view["alerts"]] == [(nas["id"], "alert"), (switch["id"], "warning")]
        assert [u["name"] for u in view["unmatched"]] == ["Access point"] and view["state"]["matched"] == 2

        health = c.get(f"/api/devices/{switch['id']}/health").json()
        assert health["matched"]["name"] == "Core switch" and health["matched"]["level"] == "warning" and health["link"] is None
        assert health["sensors"][0] == {"kind": "temperature", "label": "Board", "value": 71.5, "unit": "°C", "level": "alert"}
        assert [s["label"] for s in health["sensors"]] == ["Board", "Fan 1", "ether1 POE"]   # only this device's readings
        assert health["ports"] == {"total": 2, "up": 1, "in_bps": 8000, "out_bps": 4000, "errors": 3}
        assert health["graphs"] == [{"name": "device_bits", "title": "Traffic"}]
        assert {e["kind"] for e in health["events"]} == {"event", "log"}
        assert c.get(f"/api/devices/{printer['id']}/health").json()["matched"] is None

        picture = c.get(f"/api/devices/{switch['id']}/health/graph", params={"name": "device_bits", "period": "week"})
        assert picture.status_code == 200 and picture.headers["content-type"].startswith("image/svg+xml") and "sandbox" in picture.headers["content-security-policy"]
        assert c.get(f"/api/devices/{switch['id']}/health/graph", params={"name": "../../system"}).status_code == 422
        assert c.get(f"/api/devices/{printer['id']}/health/graph", params={"name": "device_bits"}).status_code == 422

        # a match can be set by hand, switched off, and put back to automatic
        assert c.put(f"/api/devices/{printer['id']}/monitoring-link", json={"key": "3"}).status_code == 200
        assert c.get(f"/api/devices/{printer['id']}/health").json()["matched"]["name"] == "Access point"
        assert c.put(f"/api/devices/{printer['id']}/monitoring-link", json={"key": "999"}).status_code == 422
        c.put(f"/api/devices/{switch['id']}/monitoring-link", json={"key": ""})
        assert str(switch["id"]) not in c.get("/api/live").json()["health"]
        c.put(f"/api/devices/{switch['id']}/monitoring-link", json={"key": None})
        assert str(switch["id"]) in c.get("/api/live").json()["health"]

        # the source going away keeps the last answer and says why
        fake.stop()
        monitoring.refresh()
        state = c.get("/api/monitoring").json()["state"]
        assert "didn't answer" in state["error"] and state["devices"] == 3

        assert c.delete("/api/monitoring").status_code == 200
        assert c.get("/api/monitoring").json()["source"] is None and c.get("/api/live").json()["health"] == {}
    finally:
        fake.stop()
        c.__exit__(None, None, None)


def test_monitoring_ports_what_is_plugged_in_and_readouts():
    from fake_librenms import FakeLibreNMS
    from rackmap import monitoring

    c = signed_in()
    fake = FakeLibreNMS()
    port = fake.start()
    try:
        switch = c.post("/api/devices", json={"name": "switch", "u_height": 1, "ip": "192.168.7.2"}).json()
        wifi = c.post("/api/devices", json={"name": "Wi-Fi", "u_height": 0, "ip": "192.168.7.9"}).json()
        laptop = c.post("/api/devices", json={"name": "laptop", "u_height": 0, "ip": "192.168.7.50"}).json()
        other = c.post("/api/devices", json={"name": "unmonitored", "u_height": 0}).json()
        assert c.get(f"/api/devices/{switch['id']}/ports").json() == {"ports": [], "error": ""}     # no source yet
        assert c.put("/api/monitoring", json={"url": f"http://127.0.0.1:{port}", "token": "lnms-token"}).status_code == 200
        monitoring.refresh_details()   # what the background reader does once a minute

        ports = c.get(f"/api/devices/{switch['id']}/ports").json()["ports"]
        assert [p["name"] for p in ports] == ["ether1", "ether2", "ether3"]                        # the bridge and the VLAN are not sockets
        assert [p["state"] for p in ports] == ["up", "down", "off"] and all("mac" not in p for p in ports)
        one, two, three = ports
        assert one["speed"] == 1000000000 and one["in_bps"] == 8000 and one["out_bps"] == 4000 and one["poe"] == 6.5
        # a neighbour that announces itself wins over the addresses seen on the port
        assert one["plugged"] == [{"name": "Wi-Fi", "device_id": wifi["id"], "address": "", "port": "eth0", "note": "", "announced": True}]
        assert two["label"] == "laptop dock" and two["errors"] == 3 and two["poe"] is None
        # one address seen on the port, which the source's address table ties to equipment recorded here
        assert two["plugged"] == [{"name": "laptop", "device_id": laptop["id"], "address": "192.168.7.50", "note": "", "port": "", "announced": False}]
        assert two["addresses"] == 1 and three["plugged"] == [] and three["behind"] == 6                                    # many addresses: an uplink, not a device

        # the access point: the switch is on its one port, and an address nothing else knows is shown as it is
        eth0 = c.get(f"/api/devices/{wifi['id']}/ports").json()["ports"][0]
        assert {p["name"] for p in eth0["plugged"]} == {"switch", "f4:f5:e8:00:00:01"}
        assert c.get(f"/api/devices/{other['id']}/ports").json()["ports"] == []

        readouts = c.get("/api/live").json()["readouts"]
        assert readouts[str(switch["id"])] == {"temp": 71.5, "power": 6.5, "power_kind": "poe", "in_bps": 8000, "out_bps": 4000, "lights": "udo"}
        assert readouts[str(wifi["id"])] == {"temp": 40, "in_bps": 80, "out_bps": 160, "lights": "u"}
        assert str(other["id"]) not in readouts and str(laptop["id"]) not in readouts

        # the source going away keeps the last copy of the ports and says why
        fake.stop()
        monitoring._ports.update({key: (0.0, rows) for key, (_at, rows) in monitoring._ports.items()})   # as if read long ago
        gone = c.get(f"/api/devices/{switch['id']}/ports").json()
        assert [p["name"] for p in gone["ports"]] == ["ether1", "ether2", "ether3"] and "didn't answer" in gone["error"]
    finally:
        fake.stop()
        c.__exit__(None, None, None)


def test_monitoring_tells_sockets_from_pseudo_interfaces_and_battery_runtime_from_battery_age():
    from fake_librenms import FakeLibreNMS, port
    from rackmap import monitoring

    c = signed_in()
    fake = FakeLibreNMS()
    fake.ports[1] = [
        port(1, 1, "igc0", "up", kind="ieee8023adLag", speed=1000000000, mac="02aa00000001"),       # member of a link group on a firewall
        port(2, 2, "10G LAGG", "up", kind="ieee8023adLag", speed=20000000000, mac="02aa00000002"),  # the group itself on a switch
        port(3, 3, "br-09fc6f953bca", "up", mac="02aa00000003"), port(4, 4, "br-lan", "up", mac="02aa00000004"),
        port(5, 5, "veth1a2b3c", "up", mac="02aa00000005"), port(6, 6, "lagg1.20", "up", mac="02aa00000006"),
        port(7, 7, "gigabitethernet 0/1", "up", speed=1000000000, mac="02aa00000007"), port(8, 8, "lo", "up", mac="02aa00000008"),
        port(9, 9, "sfp-sfpplus1", "up", speed=10000000000, mac="02aa00000009"), port(10, 10, "eth9", "up", mac=None)]
    fake.sensors = [
        {"sensor_id": 1, "device_id": 1, "sensor_class": "runtime", "sensor_descr": "Runtime", "sensor_current": 29, "sensor_deleted": 0},
        {"sensor_id": 2, "device_id": 1, "sensor_class": "runtime", "sensor_descr": "Battery Recommended Days Remaining", "sensor_current": 5738799, "sensor_deleted": 0},
        {"sensor_id": 3, "device_id": 1, "sensor_class": "runtime", "sensor_descr": "Last Battery Replacement", "sensor_current": -803120, "sensor_deleted": 0},
        {"sensor_id": 4, "device_id": 1, "sensor_class": "power", "sensor_descr": "Active Power", "sensor_current": 706, "sensor_deleted": 0},
        {"sensor_id": 5, "device_id": 3, "sensor_class": "power", "sensor_descr": "poe-out-consumption", "sensor_current": 0, "sensor_deleted": 0}]
    del fake.ports[3]   # a device the source only pings: LibreNMS answers "not found" for its ports
    port_ = fake.start()
    try:
        box = c.post("/api/devices", json={"name": "box", "u_height": 1, "ip": "192.168.7.2"}).json()
        pinged = c.post("/api/devices", json={"name": "pinged", "u_height": 0, "ip": "192.168.7.9"}).json()
        c.put("/api/monitoring", json={"url": f"http://127.0.0.1:{port_}", "token": "lnms-token"})
        monitoring.refresh_details()
        assert [p["name"] for p in c.get(f"/api/devices/{box['id']}/ports").json()["ports"]] == ["igc0", "gigabitethernet 0/1", "sfp-sfpplus1"]
        assert c.get(f"/api/devices/{pinged['id']}/ports").json() == {"ports": [], "error": ""}
        readouts = c.get("/api/live").json()["readouts"]
        assert readouts[str(box["id"])]["runtime"] == 29 and readouts[str(box["id"])]["power"] == 706 and readouts[str(box["id"])]["power_kind"] == "draw"
        assert readouts[str(pinged["id"])] == {"power": 0, "power_kind": "poe"}
    finally:
        fake.stop()
        c.__exit__(None, None, None)


def test_cables_by_hand_and_suggested_by_the_monitoring_source():
    from fake_librenms import FakeLibreNMS
    from rackmap import monitoring

    c = signed_in()
    fake = FakeLibreNMS()
    # the access point announces the switch too, and the laptop's address also shows on the access point's port (it is behind the switch)
    fake.links.append({"id": 2, "local_port_id": 301, "local_device_id": 3, "remote_port_id": 101, "active": 1, "remote_hostname": "switch", "remote_device_id": 1, "remote_port": "ether1"})
    port = fake.start()
    try:
        rack = c.post("/api/racks", json={"name": "Rack", "height_u": 10}).json()
        switch = c.post("/api/devices", json={"name": "switch", "u_height": 1, "ip": "192.168.7.2", "rack_id": rack["id"], "position_u": 9}).json()
        wifi = c.post("/api/devices", json={"name": "Wi-Fi", "u_height": 1, "ip": "192.168.7.9", "rack_id": rack["id"], "position_u": 5}).json()
        laptop = c.post("/api/devices", json={"name": "laptop", "u_height": 0, "ip": "192.168.7.50"}).json()
        assert c.get("/api/cables").json() == {"cables": [], "suggestions": []}

        # by hand
        made = c.post("/api/cables", json={"a_device_id": wifi["id"], "a_port": "eth0", "b_device_id": laptop["id"], "note": "spare"})
        assert made.status_code == 200 and made.json()["a_name"] == "Wi-Fi" and made.json()["b_port"] == ""
        assert c.post("/api/cables", json={"a_device_id": wifi["id"], "a_port": "eth0", "b_device_id": laptop["id"]}).status_code == 422   # already recorded
        assert c.post("/api/cables", json={"a_device_id": wifi["id"], "a_port": "ETH0", "b_device_id": switch["id"]}).status_code == 422   # that socket is taken
        assert c.post("/api/cables", json={"a_device_id": wifi["id"], "b_device_id": wifi["id"]}).status_code == 422
        assert c.post("/api/cables", json={"a_device_id": wifi["id"], "b_device_id": 9999}).status_code == 422
        assert [x["id"] for x in c.get("/api/overview").json()["cables"]] == [made.json()["id"]]
        assert c.delete(f"/api/cables/{made.json()['id']}").status_code == 200 and c.delete(f"/api/cables/{made.json()['id']}").status_code == 422

        # suggested: nothing until a source is connected and read
        c.put("/api/monitoring", json={"url": f"http://127.0.0.1:{port}", "token": "lnms-token"})
        monitoring.refresh_details()
        found = c.get("/api/cables").json()["suggestions"]
        assert [(s["a_name"], s["a_port"], s["b_name"], s["b_port"]) for s in found] == [("switch", "ether1", "Wi-Fi", "eth0"), ("switch", "ether2", "laptop", "")]
        assert found[0]["why"] == "The two announce themselves to each other" and found[1]["why"] == "Its hardware address is seen on that port"
        assert c.get("/api/live").json()["cable_suggestions"] == 2

        # accepting one records it and it stops being suggested; a dismissed one stays away
        first = {k: found[0][k] for k in ("a_device_id", "a_port", "b_device_id", "b_port")}
        assert c.post("/api/cables", json=first).status_code == 200
        second = {k: found[1][k] for k in ("a_device_id", "a_port", "b_device_id", "b_port")}
        assert c.post("/api/cables/dismiss", json=second).status_code == 200
        after = c.get("/api/cables").json()
        assert after["suggestions"] == [] and [(x["a_name"], x["a_port"], x["b_name"], x["b_port"]) for x in after["cables"]] == [("switch", "ether1", "Wi-Fi", "eth0")]
        # recording a dismissed cable by hand is still possible
        assert c.post("/api/cables", json=second).status_code == 200

        # deleting equipment takes its cables with it
        c.delete(f"/api/devices/{wifi['id']}")
        assert [x["b_name"] for x in c.get("/api/cables").json()["cables"]] == ["laptop"]

        # Discovery offers what the source watches that is not recorded here
        assert [m["name"] for m in c.get("/api/discovery").json()["monitored"]] == ["nas.lan", "Access point"]
    finally:
        fake.stop()
        c.__exit__(None, None, None)


def test_remote_access_over_tailscale(monkeypatch):
    from fake_tailscale import FakeTailscale

    c = signed_in()
    empty = tempfile.mkdtemp(prefix="rackmap-nopath-")
    monkeypatch.setenv("PATH", empty)                    # a machine without Tailscale
    monkeypatch.delenv("RACKMAP_PORT", raising=False)
    monkeypatch.delenv("RACKMAP_TLS", raising=False)
    try:
        assert c.get("/api/remote").json()["state"] == "missing"
        assert c.post("/api/remote/enable").status_code == 422

        fake = FakeTailscale(tempfile.mkdtemp(prefix="rackmap-ts-"))
        fake.set(backend="NeedsLogin")
        assert c.get("/api/remote").json()["state"] == "needs-login" and c.post("/api/remote/enable").status_code == 422
        fake.set(backend="down")
        assert c.get("/api/remote").json()["state"] == "stopped"

        fake.set(backend="Running", noise=True)          # warnings printed before the answer don't confuse it
        view = c.get("/api/remote").json()
        assert view["state"] == "running" and view["name"] == "box.tail1234.ts.net" and view["addresses"] == ["100.64.0.7"] and view["tailnet"] == "example.org"
        assert view["certificates"] is True and view["serving"] is False and view["url"] == "" and view["direct"] == ["https://100.64.0.7:8443"]

        # rackmap's account has to be Tailscale's operator; the answer says how
        fake.set(operator=False)
        refused = c.post("/api/remote/enable")
        assert refused.status_code == 422 and "sudo tailscale set --operator=" in refused.json()["detail"] and fake.state["calls"] == []
        fake.set(operator=True, https=False)
        refused = c.post("/api/remote/enable")
        assert refused.status_code == 422 and "login.tailscale.com/f/serve" in refused.json()["detail"]

        fake.set(https=True)
        on = c.post("/api/remote/enable").json()
        assert on["serving"] is True and on["url"] == "https://box.tail1234.ts.net"
        assert fake.state["calls"][-1] == ["serve", "--bg", "--https=443", "https+insecure://127.0.0.1:8443"]
        assert c.get("/api/remote").json()["url"] == "https://box.tail1234.ts.net"

        # something else published on the machine is not mistaken for rackmap
        fake.set(serve={"Web": {"box.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}})
        assert c.get("/api/remote").json()["serving"] is False
        fake.set(status_denied=True)
        assert c.get("/api/remote").json()["state"] == "no-permission"
        fake.set(status_denied=False)

        c.post("/api/remote/enable")
        off = c.post("/api/remote/disable").json()
        assert off["serving"] is False and fake.state["calls"][-1] == ["serve", "--https=443", "off"]

        # with HTTPS off (behind tailscale only), rackmap is published over plain HTTP on the same machine
        monkeypatch.setenv("RACKMAP_TLS", "off")
        c.post("/api/remote/enable")
        assert fake.state["calls"][-1][-1] == "http://127.0.0.1:8080"

        c.post("/api/logout", json={})
        assert c.get("/api/remote").status_code == 401 and c.post("/api/remote/enable").status_code == 401
    finally:
        c.__exit__(None, None, None)


def test_sign_in_lockout_counts_the_forwarded_address_only_from_this_machine():
    from rackmap import app as app_module

    class Req:
        def __init__(self, host, forwarded):
            self.client = type("C", (), {"host": host})()
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}

    with make_client():
        assert app_module.client_address(Req("127.0.0.1", "100.64.0.9, 10.0.0.1")) == "100.64.0.9"   # passed on by tailscale serve on this machine
        assert app_module.client_address(Req("192.168.1.50", "100.64.0.9")) == "192.168.1.50"        # anyone else could claim anything
        assert app_module.client_address(Req("127.0.0.1", "")) == "127.0.0.1"


def test_single_sign_on_with_an_openid_connect_provider():
    import urllib.request
    from urllib.parse import parse_qsl, urlsplit

    from fake_oidc import FakeOidc

    c = signed_in()
    try:
        with FakeOidc() as idp:
            assert c.get("/api/status").json()["sso"] is None
            view = c.get("/api/sso").json()
            assert view == {"sso": None, "return_address": "http://testserver/api/sso/callback"}
            body = {"issuer": idp.issuer, "client_id": idp.client_id, "secret": idp.client_secret, "allowed": "Carol, boss@example.com", "label": "Sign in with Authentik"}
            assert "https://" in c.put("/api/sso", json={**body, "issuer": "http://auth.example.com/o/"}).json()["detail"]
            assert c.put("/api/sso", json={**body, "allowed": " "}).status_code == 422
            assert c.put("/api/sso", json={**body, "issuer": idp.issuer + "nowhere/"}).status_code == 422  # not a provider
            saved = c.put("/api/sso", json=body).json()["sso"]
            assert saved["allowed"] == ["boss@example.com", "carol"] and "secret" not in saved
            assert c.put("/api/sso", json={**body, "secret": ""}).status_code == 200  # keeps the saved secret

            c.post("/api/logout", json={})
            assert c.get("/api/status").json() == {"version": __version__, "needs_setup": False, "user": None, "sso": "Sign in with Authentik"}
            assert c.get("/api/sso").status_code == 401

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            def at_the_provider(location):
                """What the browser does: visit the provider, which signs the person in and sends it back."""
                try:
                    urllib.request.build_opener(NoRedirect).open(location)
                except urllib.error.HTTPError as answer:
                    back = urlsplit(answer.headers["Location"])
                    return back.path + "?" + back.query
                raise AssertionError("the provider didn't send the browser back")

            def sign_in():
                started = c.get("/api/sso/start", follow_redirects=False)
                assert started.status_code == 302 and started.headers["location"].startswith(idp.issuer + "authorize?")
                asked = dict(parse_qsl(urlsplit(started.headers["location"]).query))
                assert asked["redirect_uri"] == "http://testserver/api/sso/callback" and asked["scope"] == "openid profile email" and len(asked["code_challenge"]) == 43
                return at_the_provider(started.headers["location"])

            # someone the provider knows, and who is on rackmap's list
            done = c.get(sign_in())
            assert done.status_code == 200 and 'content="0;url=/"' in done.text
            assert c.get("/api/status").json()["user"] == "carol"
            assert c.get("/api/overview").status_code == 200
            # the account made for them has no password of its own
            c.post("/api/logout", json={})
            assert c.post("/api/login", json={"name": "carol", "password": "sso$-$-"}).status_code == 401
            assert c.post("/api/login", json={"name": "carol", "password": ""}).status_code in (401, 422)

            # a code can't be used twice, and a callback this browser didn't start is refused
            back = sign_in()
            assert c.get(back).status_code == 200 and c.get("/api/status").json()["user"] == "carol"
            c.post("/api/logout", json={})
            assert "sso_error" in c.get(back).text and c.get("/api/status").json()["user"] is None
            back = sign_in()
            c.cookies.delete("rackmap_sso")
            assert "sso_error" in c.get(back).text and c.get("/api/status").json()["user"] is None

            # someone the provider lets through but rackmap's list doesn't
            idp.person = {"preferred_username": "mallory", "email": "mallory@example.com"}
            refused = c.get(sign_in())
            assert "sso_error" in refused.text and "mallory" in refused.text and c.get("/api/status").json()["user"] is None
            # a statement that was altered after signing
            idp.person, idp.tamper = {"preferred_username": "carol"}, True
            assert "sso_error" in c.get(sign_in()).text and c.get("/api/status").json()["user"] is None
            idp.tamper = False
            # matched by email when that is what the list holds; rackmap's own sign-in still works throughout
            idp.person = {"preferred_username": "theboss", "email": "Boss@Example.com"}
            c.get(sign_in())
            assert c.get("/api/status").json()["user"] == "theboss"
            c.post("/api/logout", json={})
            assert c.post("/api/login", json={"name": "admin", "password": "correct horse battery"}).status_code == 200
            assert c.delete("/api/sso").json() == {} and c.get("/api/status").json()["sso"] is None
            assert "sso_error" in c.get("/api/sso/start", follow_redirects=False).text
    finally:
        c.__exit__(None, None, None)


def test_password_change_and_second_step_with_an_authenticator_code():
    import base64
    import sqlite3
    import time

    from rackmap import auth, db

    c = signed_in()
    try:
        login = {"name": "admin", "password": "correct horse battery"}
        assert c.get("/api/account").json() == {"name": "admin", "has_password": True, "second_step": False, "recovery_left": 0}

        # the password: the current one is needed, and the new one must be long enough
        assert c.post("/api/account/password", json={"current": "wrong", "new": "a much longer new password"}).status_code == 422
        assert c.post("/api/account/password", json={"current": login["password"], "new": "short"}).status_code == 422
        assert c.post("/api/account/password", json={"current": login["password"], "new": "a much longer new password"}).status_code == 200
        login["password"] = "a much longer new password"
        assert c.get("/api/account").status_code == 200  # this browser stays signed in

        # switching the second step on: nothing changes until a code proves the app has the secret
        made = c.post("/api/account/second-step/start").json()
        secret = made["secret"].replace(" ", "")
        assert len(base64.b32decode(secret)) == 20 and f"secret={secret}" in made["uri"] and made["qr"].startswith("data:image/svg+xml;base64,")
        assert c.post("/api/account/second-step/confirm", json={"code": "000000"}).status_code == 422
        assert c.get("/api/account").json()["second_step"] is False
        now = int(time.time()) // 30
        done = c.post("/api/account/second-step/confirm", json={"code": auth.totp(secret, now)}).json()
        codes = done["recovery_codes"]
        assert done["second_step"] is True and len(codes) == 8 and len(set(codes)) == 8 and done["recovery_left"] == 8
        row = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT totp_secret, recovery FROM users").fetchone()
        assert secret not in row[0] and codes[0] not in row[1]
        assert c.post("/api/account/second-step/start").status_code == 422  # already on

        # signing in now takes the password and then a code
        c.post("/api/logout", json={})
        r = c.post("/api/login", json=login)
        assert r.status_code == 401 and r.json()["needs_code"] is True and c.get("/api/account").status_code == 401
        assert c.post("/api/login", json={**login, "password": "wrong", "code": auth.totp(secret, now + 1)}).json().get("needs_code") is None
        assert c.post("/api/login", json={**login, "code": "123456"}).status_code == 401
        # the code that switched it on has been used; the next one works, once
        assert c.post("/api/login", json={**login, "code": auth.totp(secret, now)}).status_code == 401
        fresh = auth.totp(secret, now + 1)
        auth._failures.clear()  # the wrong tries above were adding up toward the lockout
        assert c.post("/api/login", json={**login, "code": fresh[:3] + " " + fresh[3:]}).status_code == 200
        c.post("/api/logout", json={})
        auth._failures.clear()
        assert c.post("/api/login", json={**login, "code": fresh}).status_code == 401

        # a recovery code gets in when the phone is lost, and is then used up
        assert c.post("/api/login", json={**login, "code": codes[3].upper()}).status_code == 200
        assert c.get("/api/account").json()["recovery_left"] == 7
        c.post("/api/logout", json={})
        assert c.post("/api/login", json={**login, "code": codes[3]}).status_code == 401
        auth._failures.clear()
        assert c.post("/api/login", json={**login, "code": codes[4]}).status_code == 200

        # wrong codes count toward the lockout like wrong passwords
        c.post("/api/logout", json={})
        for _ in range(5):
            c.post("/api/login", json={**login, "code": "000000"})
        assert c.post("/api/login", json={**login, "code": codes[5]}).status_code == 429
        auth._failures.clear()
        assert c.post("/api/login", json={**login, "code": codes[5]}).status_code == 200

        # switching it off needs the password
        assert c.post("/api/account/second-step/remove", json={"password": "wrong"}).status_code == 422
        assert c.post("/api/account/second-step/remove", json={"password": login["password"]}).json()["second_step"] is False
        c.post("/api/logout", json={})
        assert c.post("/api/login", json=login).status_code == 200
    finally:
        c.__exit__(None, None, None)


def test_connections_are_plug_ins_and_the_checks_live_in_the_frame():
    """A stand-in kind, registered the way a plug-in would be: levels, confirm-by-name, protection and the log are rackmap's own."""
    from rackmap import db, services

    c = signed_in()
    try:
        sent = []

        class Client:
            fingerprint = "f" * 64

            def __init__(self, host, port, username, secret, pinned):
                self.secret = secret

            def summary(self):
                if self.secret != "demo-secret":
                    raise services.ServiceError("The demo refused that sign-in.")
                return {"version": "1.0", "nodes": [], "storage": [], "facts": [], "guests_title": "Things",
                        "guests": [{"key": "a", "name": "alpha", "label": "thing", "running": True, "state": "running"}]}

            def act(self, guest, action):
                sent.append((guest["key"], action))
                return ""

        class Demo:
            NAME, DEFAULT_PORT, ACCOUNT_LABEL, ACCOUNT_EXAMPLE, SECRET_LABEL, HELP = "Demo", 9, "Account", "admin", "Password", "A stand-in."
            ACTIONS = {"stop": {"label": "Stop", "needs": "operate", "stops": True}}
            offered = staticmethod(lambda guest: ["stop"] if guest["running"] else [])

        Demo.Client = Client
        services.register("demo", Demo)
        assert c.get("/api/services/kinds").json()["kinds"]["demo"]["name"] == "Demo"
        box = c.post("/api/devices", json={"name": "Box", "u_height": 1}).json()
        url, body = f"/api/devices/{box['id']}/services", {"kind": "demo", "host": "127.0.0.1", "username": "admin"}
        assert c.post(url, json={**body, "kind": "skynet", "secret": "demo-secret"}).status_code == 422
        refused = c.post(url, json={**body, "secret": "wrong"})
        assert refused.status_code == 422 and "refused" in refused.json()["detail"]   # nothing is saved unless the sign-in works
        sid = c.post(url, json={**body, "secret": "demo-secret"}).json()["service"]["id"]
        act = f"/api/services/{sid}/action"
        # view only: nothing offered, nothing sent, and the refusal is on record
        assert c.get(f"/api/services/{sid}/status").json()["guests"][0]["actions"] == []
        assert c.post(act, json={"target": "a", "action": "stop", "confirm": "alpha"}).status_code == 422 and sent == []
        assert c.get("/api/actions").json()["actions"][0]["outcome"] == "refused"
        # operate: offered, but anything that stops something needs its name typed
        assert c.put(f"/api/services/{sid}", json={**body, "level": "operate"}).status_code == 200
        assert c.get(f"/api/services/{sid}/status").json()["guests"][0]["actions"] == ["stop"]
        assert "Confirm by name" in c.post(act, json={"target": "a", "action": "stop"}).json()["detail"] and sent == []
        # protected: refused whatever the level
        assert c.put(f"/api/services/{sid}/protected", json={"target": "a", "protected": True}).status_code == 200
        assert "protected" in c.post(act, json={"target": "a", "action": "stop", "confirm": "alpha"}).json()["detail"] and sent == []
        c.put(f"/api/services/{sid}/protected", json={"target": "a", "protected": False})
        assert c.post(act, json={"target": "a", "action": "stop", "confirm": "alpha"}).status_code == 200 and sent == [("a", "stop")]
        assert c.get("/api/dashboard").json()["services"][0]["kind_name"] == "Demo"

        # a connection whose plug-in is gone is left in the database, not shown, and can still be removed
        db.execute("UPDATE services SET kind = 'gone' WHERE id = ?", (sid,))
        assert c.get("/api/overview").json()["unplaced"][0]["services"] == [] and c.get("/api/dashboard").json()["services"] == []
        assert "add-on" in c.get(f"/api/services/{sid}/status").json()["detail"]
        assert c.delete(f"/api/services/{sid}").status_code == 200 and db.query("SELECT 1 FROM services") == []
    finally:
        c.__exit__(None, None, None)


def test_without_add_ons_everything_else_still_works():
    from rackmap import services

    c = signed_in()
    try:
        home = c.get("/").text
        assert 'id="nav-dashboard"' in home and 'id="drawer"' in home and "/static/views.js" in home
        assert c.get("/api/dashboard").json() == {"equipment": 0, "racks": 0, "unplaced": 0, "services": [], "actions": [], "alerts": [], "monitoring": False, "new_found": 0}
        overview = c.get("/api/overview").json()
        assert c.get("/api/cables").json()["suggestions"] == []
        if services.addon() is None:   # the open-source part on its own
            assert overview["service_kinds"] == {} and overview["wiring"] is False
            assert c.get("/api/wiring").json() == {"wires": [], "wireless": [], "switches": 0, "addresses": 0, "identified": 0}
            assert c.post("/api/wiring/refresh", json={}).json()["failed"] == []
            box = c.post("/api/devices", json={"name": "Box", "u_height": 1}).json()
            r = c.post(f"/api/devices/{box['id']}/services", json={"kind": "proxmox", "host": "127.0.0.1", "username": "x", "secret": "y"})
            assert r.status_code == 422 and "isn't installed" in r.json()["detail"]
        else:
            assert "proxmox" in overview["service_kinds"] and overview["wiring"] is True
    finally:
        c.__exit__(None, None, None)
