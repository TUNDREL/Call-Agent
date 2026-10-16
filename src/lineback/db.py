"""SQLite storage (stdlib only). One short-lived connection per operation."""

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id         TEXT PRIMARY KEY,
    customer_number TEXT,
    business_number TEXT,
    first_seen_at   TEXT NOT NULL
);

-- One row per Vapi tool call. The primary key makes webhook retries idempotent:
-- a repeated toolCallId returns the stored result instead of running again.
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    call_id      TEXT,
    name         TEXT NOT NULL,
    arguments    TEXT NOT NULL,
    result       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Appointment REQUESTS. Nothing here is a confirmed booking: a person confirms.
CREATE TABLE IF NOT EXISTS booking_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_call_id   TEXT UNIQUE,
    call_id        TEXT,
    customer_name  TEXT,
    customer_phone TEXT NOT NULL,
    service_address TEXT,
    service_type   TEXT,
    preferred_time TEXT NOT NULL,
    notes          TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    notified_at    TEXT,
    notify_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_booking_unsent
    ON booking_requests (notified_at, status);
"""


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def booking_reference(booking_id: int) -> str:
    return f"LB-{booking_id:04d}"


class Database:
    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self) -> None:
        Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
