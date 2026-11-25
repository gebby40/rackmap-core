"""A stand-in OpenID Connect provider for tests: discovery, sign-in page (which signs the
configured person in at once), token exchange with PKCE, and a published signing key."""

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeOidc:
    def __init__(self, client_id="rackmap-client", client_secret="oidc-secret"):
        self.client_id, self.client_secret = client_id, client_secret
        self.person = {"preferred_username": "carol", "email": "carol@example.com"}
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        self.tamper = False
        self.redirects = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.issuer = f"http://127.0.0.1:{self._server.server_address[1]}/application/o/rackmap/"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def token_for(self, nonce):
        head = _b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        body = _b64(json.dumps({"iss": self.issuer, "aud": self.client_id, "exp": int(time.time()) + 300, "nonce": nonce, "sub": "abc", **self.person}).encode())
        signature = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        if self.tamper:
            body = _b64(json.dumps({"iss": self.issuer, "aud": self.client_id, "exp": int(time.time()) + 300, "nonce": nonce, "preferred_username": "admin"}).encode())
        return f"{head}.{body}.{_b64(signature)}"

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                base = outer.issuer.rstrip("/")
                if url.path.endswith("/.well-known/openid-configuration"):
                    return self._json(200, {"issuer": outer.issuer, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token", "jwks_uri": base + "/jwks"})
                if url.path.endswith("/jwks"):
                    numbers = outer.key.public_key().public_numbers()
                    return self._json(200, {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
                                                      "n": _b64(numbers.n.to_bytes(256, "big")), "e": _b64(numbers.e.to_bytes(3, "big"))}]})
                if url.path.endswith("/authorize"):
                    if query.get("client_id") != outer.client_id or query.get("code_challenge_method") != "S256":
                        return self._json(400, {"error": "invalid_request"})
                    code = secrets.token_urlsafe(8)
                    outer.codes[code] = query
                    outer.redirects.append(query["redirect_uri"])
                    self.send_response(302)
                    self.send_header("Location", query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": query["state"]}))
                    self.end_headers()
                    return
                self._json(404, {})

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()))
                asked = outer.codes.pop(form.get("code"), None)
                challenge = _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
                if (not asked or form.get("client_secret") != outer.client_secret or form.get("redirect_uri") != asked["redirect_uri"]
                        or challenge != asked["code_challenge"]):
                    return self._json(400, {"error": "invalid_grant", "error_description": "The provided authorization grant is invalid"})
                self._json(200, {"access_token": "at", "token_type": "Bearer", "id_token": outer.token_for(asked["nonce"])})

        return Handler

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
