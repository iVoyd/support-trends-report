"""SQLite storage: issues, fetch checkpoints, theme assignments, LLM run costs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("data/trends.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    state           TEXT NOT NULL,
    labels          TEXT NOT NULL DEFAULT '[]',   -- JSON list of label names
    comments        INTEGER NOT NULL DEFAULT 0,
    reactions       INTEGER NOT NULL DEFAULT 0,   -- reactions.total_count
    thumbs_up       INTEGER NOT NULL DEFAULT 0,
    author          TEXT,
    created_at      TEXT NOT NULL,                -- ISO 8601, UTC
    updated_at      TEXT NOT NULL,
    closed_at       TEXT,
    html_url        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS idx_issues_created ON issues (repo, created_at);

-- One row per fetched ISO week. done=1 once every page is stored and the week is over.
-- (Older databases may also have an unused fetch_state table from the issues-API fetch.)
CREATE TABLE IF NOT EXISTS fetch_chunks (
    repo            TEXT NOT NULL,
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    done            INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (repo, start_at, end_at)
);

CREATE TABLE IF NOT EXISTS issue_themes (
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    themes_version  INTEGER NOT NULL,
    theme           TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    model           TEXT NOT NULL,
    run_id          INTEGER,
    assigned_at     TEXT NOT NULL,
    PRIMARY KEY (repo, number, themes_version)
);

CREATE TABLE IF NOT EXISTS llm_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    command             TEXT NOT NULL,
    model               TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    calls               INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0
);

-- Recommendations per report week and theme, so regenerating a report reuses them.
CREATE TABLE IF NOT EXISTS recommendations (
    repo            TEXT NOT NULL,
    week            TEXT NOT NULL,
    themes_version  INTEGER NOT NULL,
    theme           TEXT NOT NULL,
    actions         TEXT NOT NULL,                -- JSON list of {kind,title,detail,citations}
    problems        TEXT NOT NULL DEFAULT '[]',   -- JSON list of check notes
    model           TEXT NOT NULL,
    run_id          INTEGER,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (repo, week, themes_version, theme)
);

-- Optional import from support-triage-assistant.
CREATE TABLE IF NOT EXISTS triage_records (
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    type            TEXT,
    area            TEXT,
    source          TEXT NOT NULL,
    PRIMARY KEY (repo, number)
);
"""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_issue(conn: sqlite3.Connection, issue: dict) -> None:
    row = dict(issue)
    row["labels"] = json.dumps(row["labels"])
    cols = list(row)
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("repo", "number"))
    conn.execute(
        f"INSERT INTO issues ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(repo, number) DO UPDATE SET {updates}",
        row,
    )


def issues_in_window(conn: sqlite3.Connection, repo: str, since: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM issues WHERE repo = ? AND created_at >= ? ORDER BY created_at",
        (repo, since),
    ).fetchall()
