import copy
import re

import pytest
from conftest import FIXED_NOW, REPO, SEED_THEMES, FakeGitHub, FakeLLM

from trends import classify, compare, github, themes

SINCE = github.window_start(8, FIXED_NOW)
V1 = themes.validate({"version": 1,
                      "themes": copy.deepcopy(SEED_THEMES) + [themes.OTHER_THEME]})
V2 = themes.ThemeSet(version=2, themes=V1.themes)


class MovingLLM(FakeLLM):
    """Like FakeLLM, but sends every 'Session' issue from auth to dashboard."""

    def call(self, system, user, schema, max_tokens=4096):
        data, usage = super().call(system, user, schema, max_tokens)
        titles = dict(re.findall(r"^### #(\d+): (.*)$", user, re.M))
        for a in data.get("assignments", []):
            if "Session" in titles.get(str(a["number"]), ""):
                a["theme"] = "dashboard"
        return data, usage



@pytest.fixture
def both(conn):
    github.fetch_issues(conn, REPO, SINCE, http_get=FakeGitHub(), now=FIXED_NOW,
                        sleep=lambda _: None)
    classify.theme_issues(FakeLLM(), conn, V1, REPO, SINCE)
    classify.theme_issues(MovingLLM(), conn, V2, REPO, SINCE)
    return conn


def test_counts_and_moves(both):
    c = compare.compare(both, REPO, 1, 2)
    sessions = both.execute("SELECT COUNT(*) FROM issues i JOIN issue_themes t "
                            "USING (repo, number) WHERE t.themes_version = 1 AND "
                            "t.theme = 'auth' AND i.title LIKE '%Session%'").fetchone()[0]
    assert sessions > 0
    assert c.moved_count == sessions == c.moves[("auth", "dashboard")]
    assert c.new_counts["auth"] == c.old_counts["auth"] - sessions
    assert c.new_counts["dashboard"] == c.old_counts["dashboard"] + sessions
    assert sum(c.old_counts.values()) == sum(c.new_counts.values()) == c.compared
    assert c.only_old == 0


def test_week_scope(both):
    full = compare.compare(both, REPO, 1, 2)
    w44 = compare.compare(both, REPO, 1, 2, "2026-W44")
    assert 0 < w44.compared < full.compared


def test_not_yet_rethemed_is_reported(conn):
    github.fetch_issues(conn, REPO, SINCE, http_get=FakeGitHub(), now=FIXED_NOW,
                        sleep=lambda _: None)
    classify.theme_issues(FakeLLM(), conn, V1, REPO, SINCE)
    classify.theme_issues(FakeLLM(), conn, V2, REPO, SINCE, limit=10)
    c = compare.compare(conn, REPO, 1, 2)
    assert c.compared == 10 and c.only_old > 0 and c.moved_count == 0
    assert "run `trends theme`" in compare.render(c, {}, "all")


def test_render_table(both):
    c = compare.compare(both, REPO, 1, 2)
    text = compare.render(c, {t.id: t.name for t in V2.themes}, "all stored issues", True)
    assert "| Theme | v1 | v2 | Change |" in text
    assert "- Auth -> Dashboard: " in text and "Moved issues:" in text
    d = c.new_counts["dashboard"] - c.old_counts["dashboard"]
    assert f"| Dashboard | {c.old_counts['dashboard']} | {c.new_counts['dashboard']} | +{d} |" \
        in text
