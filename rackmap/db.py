"""SQLite storage. One file in the data directory; schema changes are additive and versioned."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("RACKMAP_DATA", "./data")).resolve()

# Each entry is applied once, in order. Never edit an entry that has shipped; add a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        pw_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    -- Equipment catalogue (loaded from the bundled file; safe to rebuild at any time)
    CREATE TABLE device_types (
        slug TEXT PRIMARY KEY,
        manufacturer TEXT NOT NULL,
        model TEXT NOT NULL,
        part_number TEXT,
        u_height REAL NOT NULL,
        full_depth INTEGER NOT NULL,
        front_image TEXT,
        rear_image TEXT,
        ports TEXT NOT NULL,
        search TEXT NOT NULL
    );
    CREATE INDEX device_types_maker ON device_types (manufacturer, model);

    CREATE TABLE racks (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        height_u INTEGER NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- A device keeps its own copy of maker/model/height so it survives catalogue changes
    -- and so equipment that is not in the catalogue can be recorded.
    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        type_slug TEXT,
        manufacturer TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        u_height REAL NOT NULL DEFAULT 1,
        full_depth INTEGER NOT NULL DEFAULT 1,
        rack_id INTEGER REFERENCES racks(id) ON DELETE SET NULL,
        position_u REAL,
        face TEXT NOT NULL DEFAULT 'front',
        ip TEXT NOT NULL DEFAULT '',
        mac TEXT NOT NULL DEFAULT '',
        serial TEXT NOT NULL DEFAULT '',
        admin_url TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX devices_rack ON devices (rack_id);
    """,
    """
    -- What network scans have turned up. Kept apart from devices: nothing here is inventory
    -- until someone adds it.
    CREATE TABLE found_hosts (
        id INTEGER PRIMARY KEY,
        ip TEXT NOT NULL UNIQUE,
        mac TEXT NOT NULL DEFAULT '',
        vendor TEXT NOT NULL DEFAULT '',
        hostname TEXT NOT NULL DEFAULT '',
        sys_name TEXT NOT NULL DEFAULT '',
        sys_descr TEXT NOT NULL DEFAULT '',
        http_title TEXT NOT NULL DEFAULT '',
        http_server TEXT NOT NULL DEFAULT '',
        ports TEXT NOT NULL DEFAULT '[]',
        suggested_slug TEXT,
        ignored INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    -- Console servers and their serial ports. `secret` is the sign-in password, encrypted
    -- with the key in secret.key; `host_key` is the SSH host key remembered on first use.
    CREATE TABLE console_servers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL DEFAULT 22,
        username TEXT NOT NULL,
        secret TEXT NOT NULL,
        host_key TEXT NOT NULL,
        host_key_fingerprint TEXT NOT NULL,
        port_count INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE console_ports (
        id INTEGER PRIMARY KEY,
        server_id INTEGER NOT NULL REFERENCES console_servers(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        label TEXT NOT NULL DEFAULT '',
        device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
        UNIQUE (server_id, number)
    );
    """,
    """
    -- Management controllers (iDRAC, iLO, BMC), one per piece of equipment. `cert_sha256` is
    -- the HTTPS certificate remembered on first use; `ssh_host_key` likewise for the text console.
    CREATE TABLE controllers (
        device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        host TEXT NOT NULL,
        username TEXT NOT NULL,
        secret TEXT NOT NULL,
        cert_sha256 TEXT NOT NULL,
        ssh_host_key TEXT NOT NULL DEFAULT '',
        console_command TEXT NOT NULL DEFAULT '',
        kvm_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    -- A console server can itself be a piece of equipment in the rack.
    ALTER TABLE console_servers ADD COLUMN device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL;
    """,
    """
    -- KVMs. Each is a piece of equipment in the rack; `url` is its web page, if it has one.
    CREATE TABLE kvms (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '',
        port_count INTEGER NOT NULL,
        device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE kvm_ports (
        id INTEGER PRIMARY KEY,
        kvm_id INTEGER NOT NULL REFERENCES kvms(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
        UNIQUE (kvm_id, number)
    );
    """,
    """
    -- Sign-in for KVMs rackmap can drive itself (PiKVM). `secret` is the password, encrypted
    -- with the key in secret.key; `cert_pem` is the HTTPS certificate remembered on set-up.
    ALTER TABLE kvms ADD COLUMN username TEXT NOT NULL DEFAULT '';
    ALTER TABLE kvms ADD COLUMN secret TEXT NOT NULL DEFAULT '';
    ALTER TABLE kvms ADD COLUMN cert_pem TEXT NOT NULL DEFAULT '';
    """,
    """
    -- Which device in the monitoring source a piece of equipment is, when set by hand.
    -- An empty source_key means "not monitored". No row: matched by address, then name.
    CREATE TABLE monitor_links (
        device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL
    );
    """,
    """
    -- Cables between two pieces of equipment. A port is the name the equipment itself uses for
    -- the socket ("ether15"), or empty when that isn't known. The lower device id is always `a`,
    -- so a cable is recorded once whichever end it was entered from.
    CREATE TABLE cables (
        id INTEGER PRIMARY KEY,
        a_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        a_port TEXT NOT NULL DEFAULT '',
        b_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        b_port TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX cables_a ON cables (a_device_id);
    CREATE INDEX cables_b ON cables (b_device_id);
    -- Cables the monitoring source suggested and the person said are not there.
    CREATE TABLE cable_dismissals (
        a_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        a_port TEXT NOT NULL,
        b_device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        b_port TEXT NOT NULL,
        PRIMARY KEY (a_device_id, a_port, b_device_id, b_port)
    );
    """,
    """
    -- Software on a piece of equipment that rackmap signs in to (Proxmox…). `secret` is encrypted
    -- like the other saved passwords. `level` is the most rackmap may do: view, operate or full.
    CREATE TABLE services (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        username TEXT NOT NULL DEFAULT '',
        secret TEXT NOT NULL DEFAULT '',
        cert_sha256 TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL DEFAULT 'view',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (device_id, kind)
    );
    """,
    """
    -- Things rackmap must never stop through a service, marked by the person ("node/qemu/130").
    CREATE TABLE service_protected (
        service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        target TEXT NOT NULL,
        PRIMARY KEY (service_id, target)
    );
    -- Everything rackmap was asked to do through a service, whether it was sent, refused or failed.
    -- Names are copied in so the record still reads after the equipment or connection is gone.
    CREATE TABLE action_log (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL DEFAULT (datetime('now')),
        user TEXT NOT NULL,
        device_name TEXT NOT NULL,
        service_name TEXT NOT NULL,
        target TEXT NOT NULL,
        action TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    );
    """,
    """
    -- A second step for signing in with a name and password: a code from an authenticator app.
    -- `totp_secret` is encrypted like saved device passwords; empty means the step is off.
    -- `totp_last` is the last time-step a code was accepted for, so a code can't be used twice.
    -- `recovery` is a JSON list of SHA-256 hashes of the unused one-time recovery codes.
    ALTER TABLE users ADD COLUMN totp_secret TEXT NOT NULL DEFAULT '';
    ALTER TABLE users ADD COLUMN totp_last INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE users ADD COLUMN recovery TEXT NOT NULL DEFAULT '[]';
    """,
    """
    -- Choices that only some kinds of service have, as JSON (a switch's family and adjusted commands).
    ALTER TABLE services ADD COLUMN options TEXT NOT NULL DEFAULT '{}';
    """,
]

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    """Open (once) and migrate the database."""
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(DATA_DIR, 0o700)
            conn = sqlite3.connect(DATA_DIR / "rackmap.db", check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _migrate(conn)
            _conn = conn
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version={i};COMMIT;")


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock_for(connect()):
        return connect().execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> int:
    """Run a write; returns the last inserted row id."""
    with _lock_for(connect()):
        return connect().execute(sql, params).lastrowid or 0


def execute_many(sql: str, rows: list[tuple], before: str | None = None) -> None:
    """Run one statement for many rows in a single transaction (optionally after `before`)."""
    with _lock_for(connect()):
        conn = connect()
        conn.execute("BEGIN")
        try:
            if before:
                conn.execute(before)
            conn.executemany(sql, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_setting(key: str) -> str | None:
    rows = query("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


_write_lock = threading.Lock()


def _lock_for(_conn: sqlite3.Connection) -> threading.Lock:
    # One connection shared across threads: serialise every statement.
    return _write_lock
