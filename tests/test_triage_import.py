import sqlite3

import pytest

from trends.triage_import import import_triage


def make_src(path, ddl, rows):
    src = sqlite3.connect(path)
    src.execute(ddl)
    src.executemany(f"INSERT INTO {ddl.split()[2]} VALUES (?, ?, ?)", rows)
    src.commit()
    src.close()


def test_imports_matching_table(conn, tmp_path):
    path = tmp_path / "triage.db"
    make_src(path, "CREATE TABLE triage (issue_number INTEGER, type TEXT, area TEXT)",
             [(1, "bug", "auth"), (2, "question", "storage"), (None, "x", "y")])
    assert import_triage(conn, path, "supabase/supabase") == 2
    rows = conn.execute("SELECT number, type, area FROM triage_records ORDER BY number")
    assert [tuple(r) for r in rows] == [(1, "bug", "auth"), (2, "question", "storage")]


def test_no_matching_table(conn, tmp_path):
    path = tmp_path / "other.db"
    make_src(path, "CREATE TABLE notes (id INTEGER, a TEXT, b TEXT)", [])
    with pytest.raises(ValueError, match="no table"):
        import_triage(conn, path, "supabase/supabase")


def test_missing_file(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_triage(conn, tmp_path / "missing.db", "supabase/supabase")
