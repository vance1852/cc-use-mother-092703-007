"""成果许可服务的 SQLite 模式与事务辅助。

模式设计要点：

- ``agreements`` 的每一行是一个协议 *版本*，许可、改进、收益都外键到
  ``(agreement_id, version_no)``，旧版本永不被覆盖；
- ``license_grants`` 是签发时物化的授权网格（时间×地域×领域），
  冲突检测只扫描 ``state='issued'`` 的网格；
- 收益分配行 ``revenue_allocations`` 保存分成比例与协议版本快照，
  结算后金额不可变，新协议版本无法回写历史分配。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS lic_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('manager','finance','legal','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS achievements (
    achievement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    field_kind TEXT NOT NULL,
    completed_on TEXT NOT NULL,
    basis_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES lic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    version_label TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','effective','superseded','terminated')),
    effective_on TEXT NOT NULL,
    expires_on TEXT,
    basis_summary TEXT NOT NULL DEFAULT '',
    terms_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    supersedes_version_no INTEGER,
    ratified_at TEXT,
    created_by TEXT NOT NULL REFERENCES lic_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(agreement_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_agreements_achievement
ON agreements(achievement_id, version_no);

CREATE TABLE IF NOT EXISTS ownership_terms (
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    position INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    share_percent TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(agreement_id, version_no, owner_id),
    FOREIGN KEY(agreement_id, version_no) REFERENCES agreements(agreement_id, version_no)
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    licensee_id TEXT NOT NULL,
    license_type TEXT NOT NULL CHECK(license_type IN ('exclusive','sole','non_exclusive')),
    field_scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    improvement_policy TEXT NOT NULL
        CHECK(improvement_policy IN ('licensor','licensee','joint','case_by_case')),
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','superseded','terminated')),
    renewal_of_license_id TEXT REFERENCES licenses(license_id),
    superseded_by_license_id TEXT REFERENCES licenses(license_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES lic_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(agreement_id, version_no) REFERENCES agreements(agreement_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_licenses_achievement
ON licenses(achievement_id, valid_from, license_id);

CREATE INDEX IF NOT EXISTS idx_licenses_licensee
ON licenses(licensee_id, valid_from, license_id);

CREATE TABLE IF NOT EXISTS license_regions (
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    position INTEGER NOT NULL,
    region_code TEXT NOT NULL,
    field_scope TEXT NOT NULL,
    PRIMARY KEY(license_id, position)
);

CREATE TABLE IF NOT EXISTS license_revenue_split (
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    position INTEGER NOT NULL,
    payee_id TEXT NOT NULL,
    share_percent TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY(license_id, position)
);

CREATE TABLE IF NOT EXISTS license_grants (
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    licensee_id TEXT NOT NULL,
    license_type TEXT NOT NULL,
    region_code TEXT NOT NULL,
    field_scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT
);

CREATE INDEX IF NOT EXISTS idx_grants_active
ON license_grants(region_code, field_scope, valid_from, valid_until);

CREATE TABLE IF NOT EXISTS renewal_negotiations (
    negotiation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_license_id TEXT NOT NULL REFERENCES licenses(license_id),
    state TEXT NOT NULL DEFAULT 'negotiating'
        CHECK(state IN ('negotiating','completed','abandoned')),
    note TEXT NOT NULL DEFAULT '',
    new_license_id TEXT REFERENCES licenses(license_id),
    opened_by TEXT NOT NULL REFERENCES lic_users(user_id),
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_renewal_open
ON renewal_negotiations(old_license_id, state);

CREATE TABLE IF NOT EXISTS improvements (
    improvement_id TEXT PRIMARY KEY,
    achievement_id TEXT NOT NULL REFERENCES achievements(achievement_id),
    parent_license_id TEXT NOT NULL REFERENCES licenses(license_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    ownership TEXT NOT NULL CHECK(ownership IN ('licensor','licensee','joint','case_by_case')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES lic_users(user_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY(agreement_id, version_no) REFERENCES agreements(agreement_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_improvements_license
ON improvements(parent_license_id, improvement_id);

CREATE TABLE IF NOT EXISTS improvement_owners (
    improvement_id TEXT NOT NULL REFERENCES improvements(improvement_id),
    position INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    share_percent TEXT NOT NULL,
    PRIMARY KEY(improvement_id, position)
);

CREATE TABLE IF NOT EXISTS revenue_installments (
    installment_id TEXT PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(license_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    period_label TEXT NOT NULL,
    estimated_amount TEXT NOT NULL,
    gross_amount TEXT NOT NULL,
    assessed INTEGER NOT NULL DEFAULT 0 CHECK(assessed IN (0,1)),
    currency TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'estimated'
        CHECK(state IN ('estimated','assessed','disputed','settled')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES lic_users(user_id),
    created_at TEXT NOT NULL,
    assessed_by TEXT REFERENCES lic_users(user_id),
    assessed_at TEXT,
    settled_at TEXT,
    FOREIGN KEY(agreement_id, version_no) REFERENCES agreements(agreement_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_installments_license
ON revenue_installments(license_id, period_label);

CREATE TABLE IF NOT EXISTS revenue_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    installment_id TEXT NOT NULL REFERENCES revenue_installments(installment_id),
    payee_id TEXT NOT NULL,
    share_percent TEXT NOT NULL,
    allocated_amount TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','frozen','settled')),
    settled_amount TEXT,
    settled_at TEXT,
    source TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    UNIQUE(installment_id, payee_id)
);

CREATE INDEX IF NOT EXISTS idx_allocations_status
ON revenue_allocations(installment_id, status);

CREATE TABLE IF NOT EXISTS revenue_disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    installment_id TEXT NOT NULL REFERENCES revenue_installments(installment_id),
    payee_id TEXT NOT NULL,
    frozen_amount TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
    opened_by TEXT NOT NULL REFERENCES lic_users(user_id),
    opened_at TEXT NOT NULL,
    resolved_by TEXT REFERENCES lic_users(user_id),
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT '',
    UNIQUE(installment_id, payee_id, state)
);

CREATE TABLE IF NOT EXISTS lic_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS lic_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_lic_audit_entity
ON lic_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread
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
