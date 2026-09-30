"""成果许可服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS office_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','finance','legal','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS achievements (
    achievement_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    title TEXT NOT NULL,
    version TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    supersedes_agreement_id TEXT REFERENCES agreements(agreement_id),
    state TEXT NOT NULL DEFAULT 'effective'
        CHECK(state IN ('draft','effective','superseded','terminated')),
    content_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES office_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(achievement_id, version)
);

CREATE INDEX IF NOT EXISTS idx_agreements_achievement
ON agreements(achievement_id, effective_date, agreement_id);

CREATE TABLE IF NOT EXISTS ownership_shares (
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    owner_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    share TEXT NOT NULL,
    basis TEXT NOT NULL,
    PRIMARY KEY(agreement_id, owner_id)
);

CREATE TABLE IF NOT EXISTS licensees (
    licensee_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_code TEXT NOT NULL UNIQUE,
    contact TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    grant_kind TEXT NOT NULL CHECK(grant_kind IN ('non_exclusive','exclusive','sole')),
    fields_of_use TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','suspended','terminated')),
    royalty_type TEXT NOT NULL,
    rate TEXT,
    fixed_amount TEXT,
    created_by TEXT NOT NULL REFERENCES office_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grants_achievement_window
ON grants(achievement_id, start_date, end_date);

CREATE INDEX IF NOT EXISTS idx_grants_licensee
ON grants(licensee_id, start_date, end_date);

CREATE TABLE IF NOT EXISTS grant_territories (
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    region_code TEXT NOT NULL,
    region_name TEXT NOT NULL,
    PRIMARY KEY(grant_id, region_code)
);

CREATE TABLE IF NOT EXISTS improvements (
    improvement_id TEXT PRIMARY KEY,
    parent_achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    title TEXT NOT NULL,
    ownership_rule TEXT NOT NULL
        CHECK(ownership_rule IN ('licensee','licensor','joint','follow_agreement')),
    contributor_licensee_id TEXT REFERENCES licensees(licensee_id),
    derived_grant_id TEXT REFERENCES grants(grant_id),
    recorded_by TEXT NOT NULL REFERENCES office_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_improvements_parent
ON improvements(parent_achievement_id, created_at);

CREATE TABLE IF NOT EXISTS revenue_periods (
    period_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    licensee_id TEXT NOT NULL REFERENCES licensees(licensee_id),
    period_date TEXT NOT NULL,
    estimated_amount TEXT NOT NULL,
    assessed_amount TEXT,
    gross_amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'estimated'
        CHECK(status IN ('estimated','assessed','settled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES office_users(user_id),
    created_at TEXT NOT NULL,
    assessed_by TEXT REFERENCES office_users(user_id),
    assessed_at TEXT,
    UNIQUE(grant_id, period_date)
);

CREATE INDEX IF NOT EXISTS idx_periods_achievement
ON revenue_periods(achievement_id, period_date);

CREATE INDEX IF NOT EXISTS idx_periods_licensee
ON revenue_periods(licensee_id, period_date);

CREATE TABLE IF NOT EXISTS revenue_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT NOT NULL REFERENCES revenue_periods(period_id),
    owner_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL,
    share TEXT NOT NULL,
    basis TEXT NOT NULL,
    gross_amount TEXT NOT NULL,
    frozen_amount TEXT NOT NULL DEFAULT '0',
    settled_amount TEXT NOT NULL DEFAULT '0',
    dispute_id INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE(period_id, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_allocations_owner
ON revenue_allocations(owner_id, period_id);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id TEXT REFERENCES revenue_periods(period_id),
    grant_id TEXT REFERENCES grants(grant_id),
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    opened_by TEXT NOT NULL REFERENCES office_users(user_id),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS dispute_subjects (
    dispute_id INTEGER NOT NULL REFERENCES disputes(dispute_id),
    owner_id TEXT NOT NULL,
    PRIMARY KEY(dispute_id, owner_id)
);

CREATE TABLE IF NOT EXISTS renewals (
    renewal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    achievement_id TEXT NOT NULL,
    licensee_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'negotiating'
        CHECK(state IN ('negotiating','signed','abandoned')),
    note TEXT NOT NULL,
    new_grant_id TEXT REFERENCES grants(grant_id),
    opened_by TEXT NOT NULL REFERENCES office_users(user_id),
    opened_at TEXT NOT NULL,
    concluded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_renewals_open
ON renewals(achievement_id, licensee_id, state);

CREATE TABLE IF NOT EXISTS office_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS office_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_office_audit_entity
ON office_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
