"""The numbers in a report must match the database, and agree with themselves."""

import copy
from pathlib import Path

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, github, report, themes, trends, verify

REPORTS = sorted((Path(__file__).parent.parent / "reports").glob("*.md"))
TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})


@pytest.fixture
def week(conn):
    since = github.window_start(8, FIXED_NOW)
    github.fetch_issues(conn, REPO, since, http_get=FakeGitHub(), now=FIXED_NOW,
                        sleep=lambda _: None)
    classify.theme_issues(FakeLLM(), conn, TS, REPO, since)
    wt = trends.week_trends(conn, REPO, TS.ids, TS.version, "2026-W44")
    return conn, wt, report.render(REPO, wt, TS, None)


def test_at_least_two_sample_reports_are_committed():
    assert len(REPORTS) >= 2


@pytest.mark.parametrize("path", REPORTS, ids=lambda p: p.name)
def test_committed_reports_are_internally_consistent(path):
    assert verify.check_internal(path.read_text()) == []


def test_fresh_report_matches_database(week):
    _, wt, md = week
    assert verify.check_against_db(md, wt, TS) == []


def test_changed_count_is_caught(week):
    _, wt, md = week
    bad = md.replace("| Storage | 12 | 3 | +9 |", "| Storage | 11 | 3 | +8 |")
    assert bad != md
    problems = verify.check_against_db(bad, wt, TS)
    assert any("Storage: report" in p for p in problems)          # vs database
    assert any("weekly table" in p for p in problems)             # vs its own table


def test_changed_header_is_caught(week):
    _, wt, md = week
    bad = md.replace("24 issues opened; 24 themed", "25 issues opened; 24 themed")
    assert any("25 issues" in p for p in verify.check_against_db(bad, wt, TS))


def test_wrong_rising_flag_is_caught(week):
    _, _, md = week
    bad = md.replace("| 2.2 | 5.3x | yes |", "| 2.2 | 5.3x |  |")
    assert any("rising flag" in p for p in verify.check_internal(bad))


def test_report_after_retheming_no_longer_matches(week):
    conn, _, md = week
    start, end = trends.week_range("2026-W44")
    conn.execute(
        "UPDATE issue_themes SET theme = 'docs' WHERE number = (SELECT t.number FROM "
        "issue_themes t JOIN issues i USING (repo, number) WHERE t.theme = 'storage' "
        "AND i.created_at >= ? AND i.created_at < ? LIMIT 1)", (start, end))
    wt2 = trends.week_trends(conn, REPO, TS.ids, TS.version, "2026-W44")
    problems = verify.check_against_db(md, wt2, TS)
    assert any(p.startswith("Storage:") for p in problems)
    assert any(p.startswith("Docs:") for p in problems)


def test_missing_table_is_reported():
    assert "'All themes' table not found or empty" in verify.check_internal("# nothing")
