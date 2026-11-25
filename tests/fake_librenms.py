"""A stand-in LibreNMS for tests: just enough of its HTTP API for rackmap's monitoring source."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def device(device_id, hostname, display, status=1, sys_name=None, hardware="", version="", uptime=1000):
    return {"device_id": device_id, "hostname": hostname, "sysName": sys_name or hostname, "ip": hostname if hostname[0].isdigit() else "",
            "display": display, "os": "routeros", "hardware": hardware, "version": version, "status": status, "ignore": 0, "disabled": 0,
            "uptime": uptime, "last_polled": "2026-01-01T00:00:00.000000Z"}


def port(port_id, index, name, oper, admin="up", kind="ethernetCsmacd", speed=None, rate_in=None, rate_out=None, errors_in=None, mac=None, alias=None):
    return {"port_id": port_id, "ifIndex": index, "ifName": name, "ifAlias": alias or name, "ifType": kind, "ifOperStatus": oper, "ifAdminStatus": admin,
            "ifSpeed": speed, "ifPhysAddress": mac, "ifInOctets_rate": rate_in, "ifOutOctets_rate": rate_out, "ifInErrors_delta": errors_in, "ifOutErrors_delta": None}


class FakeLibreNMS:
    def __init__(self, token="lnms-token"):
        self.token = token
        self.devices = [
            device(1, "192.168.7.2", "Core switch", hardware="CRS328", version="7.1", uptime=200000),
            device(2, "nas.lan", "nas.lan", status=0),
            device(3, "192.168.7.9", "Access point"),
        ]
        self.alerts = [{"id": 5, "device_id": 1, "rule_id": 9, "state": 1, "timestamp": "2026-01-01 10:00:00", "severity": "warning"}]
        self.rules = [{"id": 9, "name": "Port Speed Degraded", "severity": "warning"}]
        self.sensors = [
            {"sensor_id": 1, "device_id": 1, "sensor_class": "temperature", "sensor_descr": "Board", "sensor_current": 71.5, "sensor_limit": 70, "sensor_deleted": 0},
            {"sensor_id": 2, "device_id": 1, "sensor_class": "fanspeed", "sensor_descr": "Fan 1", "sensor_current": 4200, "sensor_limit": None, "sensor_deleted": 0},
            {"sensor_id": 3, "device_id": 3, "sensor_class": "temperature", "sensor_descr": "Radio", "sensor_current": 40, "sensor_limit": 80, "sensor_deleted": 0},
            {"sensor_id": 4, "device_id": 1, "sensor_class": "power", "sensor_descr": "ether1 POE", "sensor_current": 6.5, "sensor_limit": None, "sensor_deleted": 0, "group": "Ports"},
        ]
        # device id -> its interfaces; the bridge and the VLAN are not sockets and must not be drawn as ports
        self.ports = {
            1: [port(101, 1, "ether1", "up", speed=1000000000, rate_in=1000, rate_out=500, mac="02aa00000001"),
                port(102, 2, "ether2", "down", errors_in=3, mac="02aa00000002", alias="laptop dock"),
                port(103, 3, "ether3", "down", admin="down", mac="02aa00000003"),
                port(104, 4, "bridge", "up", kind="bridge", rate_in=99999, rate_out=99999, mac="02aa00000001"),
                port(105, 5, "vlan10", "up", kind="l2vlan", rate_in=99999, mac="02aa00000001")],
            3: [port(301, 1, "eth0", "up", speed=1000000000, rate_in=10, rate_out=20, mac="02aa00000301")],
        }
        self.links = [{"id": 1, "local_port_id": 101, "local_device_id": 1, "remote_port_id": 301, "active": 1, "protocol": "lldp",
                       "remote_hostname": "AP", "remote_device_id": 3, "remote_port": "eth0"}]
        self.fdb = ([{"port_id": 101, "mac_address": "02aa00000301", "vlan": 1, "device_id": 1},
                     {"port_id": 102, "mac_address": "02bb00000009", "vlan": 1, "device_id": 1},
                     {"port_id": 102, "mac_address": "02bb00000009", "vlan": 3, "device_id": 1},
                     {"port_id": 301, "mac_address": "02aa00000001", "vlan": 1, "device_id": 3},
                     {"port_id": 301, "mac_address": "f4f5e8000001", "vlan": 1, "device_id": 3}]
                    + [{"port_id": 103, "mac_address": f"02cc000000{i:02x}", "vlan": 1, "device_id": 1} for i in range(6)])
        self.arp = [{"port_id": 104, "device_id": 1, "mac_address": "02bb00000009", "ipv4_address": "192.168.7.50"}]
        self.paths = []
        self._server = None
        self.port = None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status, body, kind="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                fake.paths.append(path)
                if self.headers.get("X-Auth-Token") != fake.token:
                    return self._send(401, {"message": "Unauthenticated."})
                if not path.startswith("/api/v0/"):
                    return self._send(404, {"status": "error"})
                parts = path[len("/api/v0/"):].split("/")
                if parts == ["system"]:
                    return self._send(200, {"status": "ok", "system": [{"local_ver": "26.11.0"}]})
                if parts == ["devices"]:
                    return self._send(200, {"status": "ok", "devices": fake.devices})
                if parts == ["alerts"]:
                    return self._send(200, {"status": "ok", "alerts": fake.alerts})
                if parts == ["rules"]:
                    return self._send(200, {"status": "ok", "rules": fake.rules})
                if parts == ["resources", "sensors"]:
                    return self._send(200, {"status": "ok", "sensors": fake.sensors})
                if len(parts) == 3 and parts[0] == "devices" and parts[2] == "ports":
                    if int(parts[1]) not in fake.ports:
                        return self._send(404, {"status": "error", "message": "No ports found"})
                    return self._send(200, {"status": "ok", "ports": fake.ports[int(parts[1])]})
                if parts == ["resources", "links"]:
                    return self._send(200, {"status": "ok", "links": fake.links})
                if parts == ["resources", "fdb"]:
                    return self._send(200, {"status": "ok", "ports_fdb": fake.fdb})
                if parts == ["resources", "ip", "arp", "all"]:
                    return self._send(200, {"status": "ok", "arp": fake.arp})
                if len(parts) == 3 and parts[0] == "devices" and parts[2] == "graphs":
                    return self._send(200, {"status": "ok", "graphs": [{"name": "device_poller_perf", "desc": "Poller Time"}, {"name": "device_bits", "desc": "Traffic"}]})
                if len(parts) == 3 and parts[0] == "logs":
                    key = "datetime" if parts[1] == "eventlog" else "timestamp"
                    text = "message" if parts[1] == "eventlog" else "msg"
                    return self._send(200, {"status": "ok", "logs": [{key: "2026-01-01 09:00:00", text: f"{parts[1]} line for {parts[2]}"}]})
                if len(parts) == 3 and parts[0] == "devices" and parts[2].startswith("device_"):
                    return self._send(200, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
                return self._send(404, {"status": "error"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
