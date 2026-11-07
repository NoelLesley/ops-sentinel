"""SQLite connection + schema for Ops Sentinel."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("OPS_SENTINEL_DB", Path(__file__).resolve().parent.parent / "data" / "ops.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    student          TEXT NOT NULL,
    counsellor       TEXT NOT NULL,
    partner          TEXT NOT NULL,          -- partner university / institution
    country          TEXT NOT NULL,
    stage            TEXT NOT NULL,          -- applied, offer, deposit_paid, visa_filed, visa_approved, enrolled
    stage_updated_at TEXT NOT NULL,
    visa_approved_at TEXT
);

-- What ops EXPECTS to collect (internal system of record)
CREATE TABLE IF NOT EXISTS deposits (
    deposit_id      TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    amount_expected REAL NOT NULL,
    amount_received REAL,
    due_date        TEXT NOT NULL,
    received_at     TEXT,
    gateway_ref     TEXT,                    -- payment gateway reference once paid
    status          TEXT NOT NULL            -- pending, received
);

-- What the bank / payment gateway SAYS settled (external feed)
CREATE TABLE IF NOT EXISTS ledger (
    txn_id      TEXT PRIMARY KEY,
    gateway_ref TEXT NOT NULL,
    amount      REAL NOT NULL,
    txn_at      TEXT NOT NULL,
    channel     TEXT NOT NULL                -- UPI, NEFT, card, wire
);

-- Revenue side: invoice to partner after visa approval, then commission received
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id             TEXT PRIMARY KEY,
    case_id                TEXT NOT NULL REFERENCES cases(case_id),
    raised_at              TEXT NOT NULL,
    commission_amount      REAL NOT NULL,
    commission_due_date    TEXT NOT NULL,
    commission_received_at TEXT
);

-- Issue register: checks upsert here so issues have a lifecycle
CREATE TABLE IF NOT EXISTS issues (
    issue_id     TEXT PRIMARY KEY,           -- deterministic: <rule>:<entity>
    rule         TEXT NOT NULL,
    severity     TEXT NOT NULL,              -- critical, high, medium, low
    entity       TEXT NOT NULL,
    summary      TEXT NOT NULL,
    amount_at_risk REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open',  -- open, acknowledged, resolved
    owner        TEXT,
    note         TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
