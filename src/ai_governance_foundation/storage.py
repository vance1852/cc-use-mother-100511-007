"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    evidence_key TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'retracted')),
    supersedes TEXT,
    replaced_by TEXT,
    expires_at TEXT,
    retraction_reason TEXT NOT NULL DEFAULT '',
    retracted_by TEXT NOT NULL DEFAULT '',
    retracted_at TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, evidence_key, version),
    UNIQUE(site_id, evidence_key, evidence_type, payload_hash)
);
CREATE TABLE IF NOT EXISTS evidence_links (
    source_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    target_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_target ON evidence_links(target_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    client_run_key TEXT NOT NULL,
    dataset_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    parameters_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    result_id TEXT REFERENCES evidence(evidence_id),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, client_run_key)
);
CREATE INDEX IF NOT EXISTS idx_runs_site ON runs(site_id);
CREATE INDEX IF NOT EXISTS idx_runs_dataset ON runs(dataset_id);
CREATE INDEX IF NOT EXISTS idx_runs_result ON runs(result_id);
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    conclusion_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'invalidated', 'superseded', 'published')),
    supersedes TEXT,
    published_at TEXT,
    invalidated_at TEXT,
    invalidation_json TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, conclusion_key, version)
);
CREATE TABLE IF NOT EXISTS conclusion_basis (
    conclusion_id TEXT NOT NULL REFERENCES conclusions(conclusion_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    basis_role TEXT NOT NULL,
    snapshot_hash TEXT,
    PRIMARY KEY(conclusion_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_basis_evidence ON conclusion_basis(evidence_id);
CREATE TABLE IF NOT EXISTS impact_statements (
    statement_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    trigger_evidence_id TEXT NOT NULL,
    trigger_status TEXT NOT NULL,
    conclusion_id TEXT,
    run_id TEXT,
    scope TEXT NOT NULL CHECK(scope IN ('draft_invalidated', 'published_preserved', 'run_affected')),
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_impact_trigger ON impact_statements(trigger_evidence_id);
CREATE INDEX IF NOT EXISTS idx_impact_conclusion ON impact_statements(conclusion_id);
CREATE INDEX IF NOT EXISTS idx_impact_run ON impact_statements(run_id);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
