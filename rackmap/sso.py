"""Single sign-on: letting an identity provider (Authentik, Keycloak, any OpenID Connect
provider) vouch for who is at the keyboard, alongside rackmap's own name and password.

The flow is the standard one (authorization code with PKCE). rackmap sends the browser to the
provider, the provider sends it back with a one-time code, and rackmap swaps that code for a
signed statement of who signed in. Only people on the list saved here are let in; each gets a
rackmap account of the same name, with no password of its own. rackmap's own sign-in keeps
working, so a provider that is down never locks anyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import auth, db, secrets_store

TIMEOUT = 10
PENDING_SECONDS = 600
STATE_COOKIE = "rackmap_sso"
CALLBACK_PATH = "/api/sso/callback"


class Invalid(ValueError):
    """Message is written for the person using the app."""


class Refused(Exception):
    """A sign-in through the provider didn't succeed; the message is shown on the sign-in page."""


# ---------- talking to the provider ----------

def _fetch(url: str, form: dict | None = None) -> dict:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(url, data=data, headers={"Accept": "application/json", "User-Agent": "rackmap"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 - the address is checked in _clean_issuer
            return json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read(10_000)).get("error_description") or "")[:200]
        except (ValueError, AttributeError):
            pass
        raise Refused(f"The sign-in provider answered with an error ({exc.code}{': ' + detail if detail else ''}).") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise Refused("rackmap couldn't reach the sign-in provider. Check its address, and that this machine trusts its certificate.") from None
    except ValueError:
        raise Refused("The sign-in provider's answer wasn't in the expected form.") from None


def _clean_issuer(text: str) -> str:
    url = (text or "").strip()
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise Invalid("The provider's address must start with https://.")
    if not parts.hostname or parts.query or parts.fragment or parts.username:
        raise Invalid("Enter the provider's issuer address, for example https://auth.example.com/application/o/rackmap/.")
    return url


def _discover(issuer: str) -> dict:
    found = _fetch(issuer.rstrip("/") + "/.well-known/openid-configuration")
    for key in ("authorization_endpoint", "token_endpoint", "issuer"):
        if not isinstance(found.get(key), str):
            raise Refused("That address answered, but not as an OpenID Connect provider. Check the issuer address.")
    if found["issuer"].rstrip("/") != issuer.rstrip("/"):
        raise Refused(f"That provider calls itself {found['issuer']}. Enter the issuer address exactly as the provider states it.")
    return found


def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _verified_claims(token: str, provider: dict, config: dict, nonce: str) -> dict:
    """The claims of an ID token, after checking its signature, who issued it, who it is for,
    that it hasn't expired, and that it answers the request rackmap made."""
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        head, claims, signature = json.loads(_b64(head_b64)), json.loads(_b64(body_b64)), _b64(sig_b64)
    except (ValueError, AttributeError):
        raise Refused("The provider's statement of who signed in couldn't be read.") from None
    signed = f"{head_b64}.{body_b64}".encode()
    algorithm = head.get("alg")
    if algorithm == "RS256":
        keys = [k for k in (_fetch(provider["jwks_uri"]).get("keys") or []) if k.get("kty") == "RSA" and (not head.get("kid") or k.get("kid") == head.get("kid"))]
        for key in keys:
            public = rsa.RSAPublicNumbers(int.from_bytes(_b64(key["e"]), "big"), int.from_bytes(_b64(key["n"]), "big")).public_key()
            try:
                public.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
                break
            except InvalidSignature:
                continue
        else:
            raise Refused("The provider's statement wasn't signed with a key the provider publishes.")
    elif algorithm == "HS256":
        if not hmac.compare_digest(hmac.new(config["secret"].encode(), signed, hashlib.sha256).digest(), signature):
            raise Refused("The provider's statement wasn't signed with this client's secret.")
    else:
        raise Refused("The provider signs in a way rackmap doesn't accept (it needs RS256 or HS256).")
    audience = claims.get("aud")
    if claims.get("iss") != provider["issuer"] or config["client_id"] not in (audience if isinstance(audience, list) else [audience]):
        raise Refused("The provider's statement was made by, or for, someone else.")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time() - 30:
        raise Refused("The provider's statement had expired. Check the clocks on both machines.")
    if not nonce or claims.get("nonce") != nonce:
        raise Refused("The provider's statement doesn't belong to this sign-in. Try again.")
    return claims


# ---------- settings ----------

def _saved() -> dict | None:
    raw = db.get_setting("sso")
    return json.loads(raw) if raw else None


def _names(text) -> list[str]:
    items = text if isinstance(text, list) else str(text or "").replace(";", ",").replace("\n", ",").split(",")
    return sorted({str(i).strip().lower() for i in items if str(i).strip()})


def public() -> dict | None:
    saved = _saved()
    if not saved:
        return None
    return {"issuer": saved["issuer"], "client_id": saved["client_id"], "label": saved["label"], "allowed": saved["allowed"]}


def label() -> str | None:
    saved = _saved()
    return saved["label"] if saved else None


def save(values: dict) -> dict:
    current = _saved()
    issuer = _clean_issuer(values.get("issuer"))
    client_id = str(values.get("client_id") or "").strip()
    secret = str(values.get("secret") or "")
    shown = str(values.get("label") or "").strip() or "Sign in with single sign-on"
    allowed = _names(values.get("allowed"))
    if not client_id:
        raise Invalid("Enter the client ID the provider gave for rackmap.")
    if not secret:
        if not current:
            raise Invalid("Enter the client secret the provider gave for rackmap.")
        secret = secrets_store.open_sealed(current["secret"])
    if not allowed:
        raise Invalid("List at least one person who may sign in this way, by their user name or email at the provider.")
    if len(shown) > 60:
        raise Invalid("Keep the button's wording under 60 characters.")
    try:
        _discover(issuer)
    except Refused as exc:
        raise Invalid(str(exc)) from None
    db.set_setting("sso", json.dumps({"issuer": issuer, "client_id": client_id, "secret": secrets_store.seal(secret), "label": shown, "allowed": allowed}))
    return public()


def remove() -> None:
    db.execute("DELETE FROM settings WHERE key = 'sso'")


# ---------- the sign-in itself ----------

_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def begin(redirect_uri: str) -> tuple[str, str]:
    """Where to send the browser, and the value to remember in its cookie meanwhile."""
    saved = _saved()
    if not saved:
        raise Refused("Single sign-on isn't set up.")
    provider = _discover(saved["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    with _pending_lock:
        for old in [k for k, v in _pending.items() if v["at"] < time.time() - PENDING_SECONDS]:
            del _pending[old]
        _pending[state] = {"at": time.time(), "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urllib.parse.urlencode({"response_type": "code", "client_id": saved["client_id"], "redirect_uri": redirect_uri, "scope": "openid profile email",
                                    "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"})
    joiner = "&" if "?" in provider["authorization_endpoint"] else "?"
    return provider["authorization_endpoint"] + joiner + query, state


def finish(code: str, state: str, cookie_state: str | None) -> int:
    """Complete a sign-in; returns the rackmap user id. Raises Refused with the reason otherwise."""
    saved = _saved()
    if not saved:
        raise Refused("Single sign-on isn't set up.")
    with _pending_lock:
        waiting = _pending.pop(state, None) if state else None
    if not waiting or not cookie_state or not hmac.compare_digest(state, cookie_state) or waiting["at"] < time.time() - PENDING_SECONDS:
        raise Refused("That sign-in had expired or wasn't started from this browser. Try again.")
    if not code:
        raise Refused("The provider didn't complete the sign-in.")
    config = {**saved, "secret": secrets_store.open_sealed(saved["secret"])}
    provider = _discover(saved["issuer"])
    tokens = _fetch(provider["token_endpoint"], {"grant_type": "authorization_code", "code": code, "redirect_uri": waiting["redirect_uri"],
                                                 "client_id": config["client_id"], "client_secret": config["secret"], "code_verifier": waiting["verifier"]})
    claims = _verified_claims(str(tokens.get("id_token") or ""), provider, config, waiting["nonce"])
    username = str(claims.get("preferred_username") or "").strip()
    email = str(claims.get("email") or "").strip()
    allowed = set(saved["allowed"])
    if not ({username.lower(), email.lower()} - {""}) & allowed:
        raise Refused(f"“{username or email or 'That account'}” isn't on rackmap's list of people who may sign in this way.")
    name = (username or email)[:64]
    rows = db.query("SELECT id FROM users WHERE name = ?", (name,))
    if rows:
        return rows[0]["id"]
    # A new account that can only ever be entered through the provider: "sso" is not a password scheme.
    return db.execute("INSERT INTO users (name, pw_hash) VALUES (?, ?)", (name, "sso$-$-"))
