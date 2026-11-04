import copy

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, github, themes, trends

TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})


@pytest.fixture
def themed(conn):
    since = github.window_start(8, FIXED_NOW)
    github.fetch_issues(conn, REPO, since, http_get=FakeGitHub(), sleep=lambda _: None)
    classify.theme_issues(FakeLLM(), conn, TS, REPO, since)
    return conn


def test_week_helpers():
    assert str(trends.parse_week("2026-W44")) == "2026-10-26"
    assert trends.shift_week("2026-W01", -1) == "2025-W52"
    assert trends.week_range("2026-W44") == ("2026-10-26T00:00:00Z", "2026-11-02T00:00:00Z")
    assert trends.last_full_week(FIXED_NOW) == "2026-W44"
    with pytest.raises(ValueError):
        trends.parse_week("2026-44")


@pytest.mark.parametrize("count, history, rising", [
    (5, [0, 0, 0, 0], True),     # new theme with enough issues
    (4, [0, 0, 0, 0], False),    # too few issues
    (8, [4, 4, 4, 4], True),     # exactly 2x
    (7, [4, 4, 4, 4], False),    # under 2x
    (20, [10, 12, 9, 11], False),
])
def test_rising_rule(count, history, rising):
    t = trends.ThemeTrend("x", count, history[-1], history)
    assert t.rising is rising


def test_ratio_and_change():
    t = trends.ThemeTrend("x", 6, 2, [1, 2, 1, 2])
    assert t.average == 1.5 and t.ratio == 4.0 and t.change == 4
    assert trends.ThemeTrend("x", 3, 0, [0, 0, 0, 0]).ratio is None


def test_week_trends_match_raw_sql(themed):
    wt = trends.week_trends(themed, REPO, TS.ids, TS.version, "2026-W44")
    assert wt.weeks == ["2026-W40", "2026-W41", "2026-W42", "2026-W43", "2026-W44"]
    assert wt.unthemed == 0 and wt.missing_history == []
    for t in wt.trends:
        for week, n in zip(wt.weeks, t.history + [t.count], strict=True):
            start, end = trends.week_range(week)
            expected = themed.execute(
                "SELECT COUNT(*) FROM issues i JOIN issue_themes t USING (repo, number) "
                "WHERE t.theme = ? AND i.created_at >= ? AND i.created_at < ?",
                (t.theme, start, end)).fetchone()[0]
            assert n == expected, (t.theme, week)
    assert sum(t.count for t in wt.trends) == wt.total_issues


def test_storage_spike_is_rising(themed):
    wt = trends.week_trends(themed, REPO, TS.ids, TS.version, "2026-W44")
    rising = wt.rising()
    assert rising and rising[0].theme == "storage"
    assert rising[0].count >= 10


def test_top_issues_sorted_by_reactions(themed):
    wt = trends.week_trends(themed, REPO, TS.ids, TS.version, "2026-W44")
    storage = next(t for t in wt.trends if t.theme == "storage")
    reactions = [i.reactions for i in storage.top_issues]
    assert len(reactions) == trends.TOP_ISSUES and reactions == sorted(reactions, reverse=True)


def test_missing_history_is_flagged(themed):
    wt = trends.week_trends(themed, REPO, TS.ids, TS.version, "2026-W39")
    assert "2026-W35" in wt.missing_history  # before the fetched window


def test_report_ratio_labels():
    from trends.report import _ratio
    assert _ratio(trends.ThemeTrend("x", 0, 0, [0, 0, 0, 0])) == "-"
    assert _ratio(trends.ThemeTrend("x", 3, 0, [0, 0, 0, 0])) == "new"
    assert _ratio(trends.ThemeTrend("x", 4, 1, [1, 1, 1, 1])) == "4.0x"
