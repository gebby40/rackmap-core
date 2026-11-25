"""Accounts and sessions.

There is no built-in account: the first visitor creates the administrator. Passwords are stored as
scrypt hashes; session cookies hold a random token whose SHA-256 is what the database keeps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import db, secrets_store

SESSION_COOKIE = "rackmap_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10
_SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)

# Failed sign-ins per client address: {address: (count, first_failure_time)}
_failures: dict[str, tuple[int, float]] = {}
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest, bytes.fromhex(digest_hex))


def has_users() -> bool:
    return bool(db.query("SELECT 1 FROM users LIMIT 1"))


def create_user(name: str, password: str) -> int:
    return db.execute("INSERT INTO users (name, pw_hash) VALUES (?, ?)", (name, hash_password(password)))


def locked_out(client: str) -> bool:
    count, since = _failures.get(client, (0, 0.0))
    if count >= MAX_FAILURES and time.time() - since < LOCKOUT_SECONDS:
        return True
    if time.time() - since >= LOCKOUT_SECONDS:
        _failures.pop(client, None)
    return False


def check_login(name: str, password: str, client: str) -> int | None:
    """Return the user id when the name and password match, else None (and count the failure)."""
    rows = db.query("SELECT id, pw_hash FROM users WHERE name = ?", (name,))
    # Always do one hash so a wrong name takes as long as a wrong password.
    stored = rows[0]["pw_hash"] if rows else hash_password("placeholder-password")
    if verify_password(password, stored) and rows:
        return rows[0]["id"]  # the caller forgets earlier failures once the whole sign-in has succeeded
    count_failure(client)
    return None


def forget_failures(client: str) -> None:
    _failures.pop(client, None)


def count_failure(client: str) -> None:
    count, since = _failures.get(client, (0, time.time()))
    _failures[client] = (count + 1, since)


# ---------- the account's own password ----------

class Invalid(ValueError):
    """Message is written for the person using the app."""


def account(user_id: int) -> dict:
    r = db.query("SELECT name, pw_hash, totp_secret, recovery FROM users WHERE id = ?", (user_id,))[0]
    return {"name": r["name"], "has_password": r["pw_hash"].startswith("scrypt$"), "second_step": bool(r["totp_secret"]),
            "recovery_left": len(json.loads(r["recovery"] or "[]"))}


def _require_password(user_id: int, password: str) -> None:
    stored = db.query("SELECT pw_hash FROM users WHERE id = ?", (user_id,))[0]["pw_hash"]
    if not verify_password(password or "", stored):
        raise Invalid("That isn't your current password.")


def change_password(user_id: int, current: str, new: str, keep_token: str | None) -> None:
    _require_password(user_id, current)
    if len(new or "") < MIN_PASSWORD:
        raise Invalid(f"Use a password of at least {MIN_PASSWORD} characters.")
    db.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(new), user_id))
    # anyone else signed in as this account is signed out
    db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _hash_token(keep_token or "")))


# ---------- second step: a code from an authenticator app (TOTP, RFC 6238) ----------

TOTP_STEP = 30
_enrolling: dict[int, str] = {}  # secrets shown but not yet confirmed with a code


def totp(secret: str, step: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _matching_step(secret: str, code: str, after: int = 0) -> int | None:
    """The time-step a code belongs to: this one, or the one either side to allow for clocks that
    disagree a little. Steps already used (`after`) don't count."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    now = int(time.time()) // TOTP_STEP
    for step in (now, now - 1, now + 1):
        if step > after and len(code) == 6 and hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def second_step_on(user_id: int) -> bool:
    return bool(db.query("SELECT totp_secret FROM users WHERE id = ?", (user_id,))[0]["totp_secret"])


def begin_second_step(user_id: int) -> dict:
    info = account(user_id)
    if not info["has_password"]:
        raise Invalid("This account signs in through your sign-in provider, which has its own second step.")
    if info["second_step"]:
        raise Invalid("The second step is already on for this account.")
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    _enrolling[user_id] = secret
    label = urllib.parse.quote(f"rackmap:{info['name']}")
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
            "uri": f"otpauth://totp/{label}?secret={secret}&issuer=rackmap&algorithm=SHA1&digits=6&period=30"}


def confirm_second_step(user_id: int, code: str) -> list[str]:
    """Switch the second step on once a code proves the app has the secret. Returns the recovery
    codes, which are shown this once and stored only as hashes."""
    secret = _enrolling.get(user_id)
    if not secret:
        raise Invalid("Start again: that set-up had expired.")
    step = _matching_step(secret, code)
    if step is None:
        raise Invalid("That code doesn't match. Check the app shows “rackmap” and that this machine's clock is right, then enter the newest code.")
    codes = ["-".join(base64.b32encode(secrets.token_bytes(5)).decode().lower()[i:i + 4] for i in (0, 4)) for _ in range(8)]
    db.execute("UPDATE users SET totp_secret = ?, totp_last = ?, recovery = ? WHERE id = ?",
               (secrets_store.seal(secret), step, json.dumps([_hash_token(c) for c in codes]), user_id))
    _enrolling.pop(user_id, None)
    return codes


def remove_second_step(user_id: int, password: str) -> None:
    _require_password(user_id, password)
    db.execute("UPDATE users SET totp_secret = '', totp_last = 0, recovery = '[]' WHERE id = ?", (user_id,))


def check_code(user_id: int, code: str) -> bool:
    """A sign-in's second step: the app's current code, or an unused recovery code (used up by this)."""
    r = db.query("SELECT totp_secret, totp_last, recovery FROM users WHERE id = ?", (user_id,))[0]
    try:
        secret = secrets_store.open_sealed(r["totp_secret"])
    except ValueError:
        return False
    step = _matching_step(secret, code, r["totp_last"])
    if step is not None:
        db.execute("UPDATE users SET totp_last = ? WHERE id = ?", (step, user_id))
        return True
    typed = _hash_token((code or "").strip().lower())
    left = json.loads(r["recovery"] or "[]")
    if typed in left:
        left.remove(typed)
        db.execute("UPDATE users SET recovery = ? WHERE id = ?", (json.dumps(left), user_id))
        return True
    return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), user_id, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    rows = db.query(
        "SELECT u.id, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at > datetime('now')",
        (_hash_token(token),),
    )
    return dict(rows[0]) if rows else None


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
