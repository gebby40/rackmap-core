"""A stand-in management controller for tests: Redfish over HTTPS with a self-signed certificate."""

import base64
import datetime
import json
import ssl
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-bmc")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(key, hashes.SHA256()))
    path = f"{directory}/bmc.pem"
    with open(path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    return path


class FakeRedfish:
    def __init__(self, username="root", password="bmc-secret", allowed=("On", "ForceOff", "ForceRestart", "GracefulShutdown")):
        self.username, self.password, self.allowed = username, password, list(allowed)
        self.power_state = "On"
        self.resets = []
        self._dir = tempfile.mkdtemp(prefix="fake-bmc-")
        self._server = None
        self.port = None
        self.connections = 0

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # like real controllers: several questions on one connection

            def setup(self):
                outer.connections += 1
                super().setup()

            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                expected = "Basic " + base64.b64encode(f"{outer.username}:{outer.password}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._authed():
                    return
                system = "/redfish/v1/Systems/1"
                pages = {
                    "/redfish/v1/Systems": {"Members": [{"@odata.id": system}]},
                    system: {"Manufacturer": "Dell Inc.", "Model": "PowerEdge R720xd", "SKU": "ABC1234", "BiosVersion": "2.9.0",
                             "PowerState": outer.power_state, "Status": {"Health": "OK", "HealthRollup": "OK"},
                             "MemorySummary": {"TotalSystemMemoryGiB": 64}, "ProcessorSummary": {"Count": 2, "Model": "Xeon E5"},
                             "Actions": {"#ComputerSystem.Reset": {"target": system + "/Actions/ComputerSystem.Reset",
                                                                   "ResetType@Redfish.AllowableValues": outer.allowed}}},
                    "/redfish/v1/Chassis": {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
                    "/redfish/v1/Chassis/1/Thermal": {"Temperatures": [{"Name": "Inlet Temp", "ReadingCelsius": 21}], "Fans": [{}, {}, {}]},
                    "/redfish/v1/Chassis/1/Power": {"PowerControl": [{"PowerConsumedWatts": 250}],
                                                    "PowerSupplies": [{"Name": "PS1", "Status": {"Health": "OK"}}]},
                }
                if self.path in pages:
                    self._send(200, pages[self.path])
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if not self._authed():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                kind = body.get("ResetType")
                if self.path != "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset" or kind not in outer.allowed:
                    self._send(400, {"error": "bad reset"})
                    return
                outer.resets.append(kind)
                outer.power_state = "Off" if kind in ("ForceOff", "GracefulShutdown") else "On"
                self._send(204, {})

        return Handler

    def start(self, port=0):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(_certificate(self._dir))
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def new_identity(self):
        """A replaced controller: same address, different certificate."""
        port = self.port
        self._server.shutdown()
        self._server.server_close()
        self.start(port)
