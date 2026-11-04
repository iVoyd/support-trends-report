"""Optional: import triage records (issue number, type, area) from support-triage-assistant.

That project's schema may change, so this looks for any table that has an issue-number
column plus `type` and/or `area` columns, rather than hard-coding one table name. It opens
the source database read-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

NUMBER_COLUMNS = ("issue_number", "number", "issue")


def find_table(src: sqlite3.Connection) -> tuple[str, str, str | None, str | None] | None:
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        cols = {r[1].lower(): r[1] for r in src.execute(f'PRAGMA table_info("{table}")')}
        number = next((cols[c] for c in NUMBER_COLUMNS if c in cols), None)
        type_col, area_col = cols.get("type"), cols.get("area")
        if number and (type_col or area_col):
            return table, number, type_col, area_col
    return None


def import_triage(conn: sqlite3.Connection, src_path: Path, repo: str) -> int:
    if not src_path.exists():
        raise FileNotFoundError(f"{src_path} not found")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        found = find_table(src)
        if found is None:
            raise ValueError(f"no table with an issue number and type/area columns in {src_path}")
        table, number, type_col, area_col = found
        cols = ", ".join(f'"{c}"' if c else "NULL" for c in (number, type_col, area_col))
        rows = src.execute(f'SELECT {cols} FROM "{table}"').fetchall()
    finally:
        src.close()
    n = 0
    for num, typ, area in rows:
        if num is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO triage_records VALUES (?, ?, ?, ?, ?)",
            (repo, int(num), typ, area, f"{src_path.name}:{table}"),
        )
        n += 1
    conn.commit()
    return n
