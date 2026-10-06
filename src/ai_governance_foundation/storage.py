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
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    supersedes_version INTEGER,
    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'retracted', 'expired')),
    status_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    status_changed_at TEXT,
    PRIMARY KEY (dataset_id, version)
);
CREATE TABLE IF NOT EXISTS run_records (
    run_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    external_key TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    dataset_version INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    parameters_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    internal_notes TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'retracted', 'expired')),
    status_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    status_changed_at TEXT,
    UNIQUE(organization_id, external_key),
    FOREIGN KEY (dataset_id, dataset_version) REFERENCES dataset_versions(dataset_id, version)
);
CREATE INDEX IF NOT EXISTS idx_run_records_dataset ON run_records(dataset_id, dataset_version);
CREATE TABLE IF NOT EXISTS judgments (
    judgment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    external_key TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES run_records(run_id),
    verdict TEXT NOT NULL,
    rationale TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'retracted', 'expired')),
    status_reason TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    status_changed_at TEXT,
    UNIQUE(organization_id, external_key)
);
CREATE INDEX IF NOT EXISTS idx_judgments_run ON judgments(run_id);
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    title TEXT NOT NULL,
    statement TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft', 'published')),
    basis_json TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS conclusion_evidence (
    conclusion_id TEXT NOT NULL REFERENCES conclusions(conclusion_id),
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('run', 'judgment')),
    evidence_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (conclusion_id, evidence_type, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_conclusion_evidence_target ON conclusion_evidence(evidence_type, evidence_id);
CREATE TABLE IF NOT EXISTS impact_statements (
    impact_id TEXT PRIMARY KEY,
    conclusion_id TEXT NOT NULL REFERENCES conclusions(conclusion_id),
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('dataset_version', 'run', 'judgment')),
    evidence_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('retracted', 'expired')),
    reason TEXT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conclusion_id, evidence_type, evidence_id, event)
);
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
