"""SQLite-backed workspace.

A workspace is a directory containing ``workspace.db``.  Source files are
imported into the workspace; nothing is written back to the read-only input
directory.  Different workspace directories are fully independent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .timeutil import format_ts, now_utc

DB_NAME = "workspace.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_rows (
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    revision TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (kind, identity, revision, content_hash)
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    input_dir TEXT NOT NULL,
    new_rows INTEGER NOT NULL,
    duplicate_rows INTEGER NOT NULL,
    digests TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    network TEXT,
    status TEXT NOT NULL,
    origin_type TEXT,
    origin_ref TEXT,
    notes TEXT,
    split_from TEXT,
    opened_at TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_sites (
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    site TEXT NOT NULL,
    impact_start TEXT,
    impact_end TEXT,
    tier INTEGER,
    discipline TEXT NOT NULL DEFAULT 'network',
    status TEXT NOT NULL,
    evidence_gap TEXT,
    opened_at TEXT NOT NULL,
    reopened_at TEXT,
    closed_at TEXT,
    closure_report TEXT,
    PRIMARY KEY (incident_id, site)
);

CREATE TABLE IF NOT EXISTS complaint_links (
    complaint_id TEXT NOT NULL,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    basis TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    linked_by TEXT NOT NULL,
    linked_after_close INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (complaint_id, incident_id)
);

CREATE TABLE IF NOT EXISTS complaints_state (
    complaint_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    discipline TEXT NOT NULL,
    assignee TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    planned_minutes REAL NOT NULL,
    status TEXT NOT NULL,
    actual_minutes REAL,
    actual_start TEXT,
    actual_end TEXT,
    cause TEXT,
    repair_scope TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    completed_at TEXT,
    completed_by TEXT
);

CREATE TABLE IF NOT EXISTS action_sites (
    action_id TEXT NOT NULL REFERENCES actions(id),
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    site TEXT NOT NULL,
    discipline TEXT NOT NULL,
    status TEXT NOT NULL,
    cause TEXT,
    repair_scope TEXT,
    completed_at TEXT,
    completed_by TEXT,
    actual_minutes REAL,
    PRIMARY KEY (action_id, incident_id, site)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    site TEXT NOT NULL,
    action_id TEXT,
    checked_at TEXT NOT NULL,
    since TEXT NOT NULL,
    sufficient INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    next_step TEXT,
    shift TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    supersedes TEXT,
    payload TEXT NOT NULL,
    note TEXT,
    UNIQUE (month, version)
);

CREATE TABLE IF NOT EXISTS report_recipients (
    report_id TEXT NOT NULL REFERENCES reports(id),
    recipient TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (report_id, recipient)
);

CREATE TABLE IF NOT EXISTS risk_acks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT,
    incident_id TEXT,
    site TEXT,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    impact TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    incident_id TEXT,
    action_id TEXT,
    report_id TEXT,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_incident ON events(incident_id);
CREATE INDEX IF NOT EXISTS idx_actions_assignee ON actions(assignee);
CREATE INDEX IF NOT EXISTS idx_obs_site ON observations(incident_id, site);
"""

KIND_KEYS = {
    "sites": ("site",),
    "contracts": ("contract",),
    "segments": ("id",),
    "probes": ("id",),
    "complaints": ("id",),
    "windows": ("id",),
    "staff": ("staff",),
}

class WorkspaceError(RuntimeError):
    """A user-facing workflow violation."""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(kind: str, row: dict) -> str:
    keys = KIND_KEYS[kind]
    return "|".join(str(row.get(key, "")) for key in keys)


def connect(workspace: str | Path, *, create: bool = False) -> sqlite3.Connection:
    root = Path(workspace)
    if not root.exists():
        if not create:
            raise WorkspaceError(f"工作目录不存在: {root}")
        root.mkdir(parents=True)
    db_path = root / DB_NAME
    if not db_path.exists() and not create:
        raise WorkspaceError(f"工作目录尚未初始化: {root}")
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialise(workspace: str | Path) -> Path:
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    connection = connect(root, create=True)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)",
            (format_ts(now_utc()),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')",
            (),
        )
        connection.commit()
    finally:
        connection.close()
    return root


def import_rows(
    connection: sqlite3.Connection,
    rows_by_kind: dict[str, list[dict]],
    digests: dict[str, str],
    *,
    actor: str,
    input_dir: str,
) -> dict[str, int]:
    """Merge rows, de-duplicating exact deliveries.

    A repeated identical row is not a new business activity; it is counted but
    not stored anew.  A changed row with the same identity is stored so that the
    source-version rules can resolve it (and report conflicts).
    """

    imported_at = format_ts(now_utc())
    new_rows = 0
    duplicate_rows = 0
    for kind, rows in rows_by_kind.items():
        if kind not in KIND_KEYS:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = _identity(kind, row)
            revision = str(row.get("revision", "")) if kind == "segments" else ""
            content_hash = hashlib.sha256(_canonical(row).encode("utf-8")).hexdigest()
            cursor = connection.execute(
                "INSERT OR IGNORE INTO source_rows(kind, identity, revision, content_hash, payload, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (kind, identity, revision, content_hash, _canonical(row), imported_at),
            )
            if cursor.rowcount:
                new_rows += 1
            else:
                duplicate_rows += 1
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                (f"digest:{kind}:{content_hash[:12]}", digests.get(f"{kind}.json", "")),
            )
    connection.execute(
        "INSERT INTO imports(imported_at, actor, input_dir, new_rows, duplicate_rows, digests)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (imported_at, actor, str(input_dir), new_rows, duplicate_rows, _canonical(digests)),
    )
    return {"new_rows": new_rows, "duplicate_rows": duplicate_rows}


def load_rows(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {kind: [] for kind in KIND_KEYS}
    for row in connection.execute(
        "SELECT kind, payload FROM source_rows ORDER BY kind, identity, revision, content_hash"
    ):
        result.setdefault(row["kind"], []).append(json.loads(row["payload"]))
    return result


def source_digests(connection: sqlite3.Connection) -> dict[str, str]:
    """Digest per source file, keeping the most recently imported value."""

    digests: dict[str, str] = {}
    for row in connection.execute("SELECT digests FROM imports ORDER BY id ASC"):
        digests.update(json.loads(row["digests"]))
    return digests


def record_event(
    connection: sqlite3.Connection,
    *,
    actor: str,
    kind: str,
    detail: dict | str,
    incident_id: str | None = None,
    action_id: str | None = None,
    report_id: str | None = None,
    at: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO events(at, actor, kind, incident_id, action_id, report_id, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            at or format_ts(now_utc()),
            actor,
            kind,
            incident_id,
            action_id,
            report_id,
            detail if isinstance(detail, str) else _canonical(detail),
        ),
    )


def next_id(connection: sqlite3.Connection, table: str, prefix: str) -> str:
    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    count = int(row["count"]) + 1
    candidate = f"{prefix}-{count:04d}"
    while connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone():
        count += 1
        candidate = f"{prefix}-{count:04d}"
    return candidate

    return [dict(row) for row in rows]
