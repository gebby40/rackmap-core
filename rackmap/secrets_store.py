"""Keeping device passwords out of the database in clear.

Passwords for console servers are encrypted with a key held in a separate file in the data
directory (`secret.key`, readable only by the account rackmap runs as). This protects a copy
of the database on its own, such as a backup of `rackmap.db`; it does not protect against
someone who can read the whole data directory. Back up both files, and keep both private.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from . import db

_fernet: Fernet | None = None


def _box() -> Fernet:
    global _fernet
    if _fernet is None:
        path = db.DATA_DIR / "secret.key"
        if not path.exists():
            db.DATA_DIR.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        _fernet = Fernet(path.read_bytes().strip())
    return _fernet


def reset() -> None:
    """Forget the loaded key (used when the data directory changes, as in tests)."""
    global _fernet
    _fernet = None


def seal(text: str) -> str:
    return _box().encrypt(text.encode()).decode()


def open_sealed(token: str) -> str:
    """Raises ValueError if the token wasn't sealed with this installation's key."""
    try:
        return _box().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("This saved password can't be read with the current secret key.") from None
