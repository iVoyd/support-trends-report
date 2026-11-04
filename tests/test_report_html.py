import copy
import re

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, github, recommend, report_html, themes, trends

TS = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})


@pytest.fixture
def page(conn):
    since = github.window_start(8, FIXED_NOW)
    github.fetch_issues(conn, REPO, since, http_get=FakeGitHub(), now=FIXED_NOW,
                        sleep=lambda _: None)
    classify.theme_issues(FakeLLM(), conn, TS, REPO, since)
    wt = trends.week_trends(conn, REPO, TS.ids, TS.version, "2026-W44")
    weeks, chart = trends.chart_counts(conn, REPO, TS.version, TS.ids, "2026-W44")
    recs = [recommend.ThemeRecommendation("storage", [recommend.Action(
        "docs_fix", "Fix <upload> docs", "Say the limit & why.",
        ["https://github.com/supabase/supabase/issues/20148"])])]
    html = report_html.render(REPO, wt, TS, recs, weeks, chart,
                              empty_weeks=trends.empty_weeks(conn, REPO, weeks))
    return html, wt, weeks, chart


def test_chart_has_eight_weeks_and_one_row_per_theme(page):
    html, wt, weeks, chart = page
    assert weeks[0] == "2026-W37" and weeks[-1] == "2026-W44" and len(weeks) == 8
    assert html.count('<svg viewBox="0 0 218 37"') == len(TS.themes)


def test_bar_tooltips_match_the_counts(page):
    html, _, weeks, chart = page
    for theme in TS.themes:
        for week, n in zip(weeks, chart[theme.id], strict=True):
            word = "issue" if n == 1 else "issues"
            assert f"<title>{theme.name}, {week}: {n} {word}</title>" in html


def test_bars_drawn_only_for_nonzero_weeks(page):
    html, _, _, chart = page
    nonzero = sum(1 for counts in chart.values() for n in counts if n)
    assert len(re.findall(r'<path class="bar-(?:target|hist)"', html)) == nonzero


def test_rising_badge_only_on_rising_themes(page):
    html, wt, _, _ = page
    assert html.count('class="badge">Rising<') == len(wt.rising()) == 1
    assert 'Storage<span class="badge">Rising</span>' in html


def test_text_is_escaped_and_page_is_self_contained(page):
    html = page[0]
    assert "Fix &lt;upload&gt; docs" in html and "limit &amp; why" in html
    assert "<script" not in html and "<link" not in html
    assert "prefers-color-scheme: dark" in html


def test_unfetched_week_is_called_out(page):
    assert "No issues are stored for 2026-W37" in page[0]


def test_bar_path_is_flat_on_the_baseline():
    d = report_html._bar_path(0, 10, 20, 26)
    assert d.startswith("M0.0,36.0") and d.endswith("V36.0 Z")
