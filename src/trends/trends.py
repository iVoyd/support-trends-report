"""Weekly counts per theme, week-over-week change, and the "rising" rule.

Weeks are ISO weeks (Monday to Sunday, UTC), named like 2026-W44. An issue belongs to the
week it was opened in. All numbers come straight from SQL over `issues` + `issue_themes`
for one themes.yaml version, so the report can be checked against the database.

Rising rule (from SPEC.md): a theme is rising in week W when it has at least
RISING_MIN_ISSUES issues in W and at least RISING_FACTOR times its average over the
HISTORY_WEEKS weeks before W. "other" can be rising (that hints the theme list is missing
something) but it never gets recommendations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

HISTORY_WEEKS = 4
RISING_FACTOR = 2.0
RISING_MIN_ISSUES = 5
TOP_ISSUES = 3

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_week(week: str) -> date:
    """'2026-W44' -> the Monday of that ISO week."""
    m = WEEK_RE.match(week)
    if not m:
        raise ValueError(f"week must look like 2026-W44, got {week!r}")
    return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def week_name(monday: date) -> str:
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def shift_week(week: str, n: int) -> str:
    return week_name(parse_week(week) + timedelta(weeks=n))


def last_full_week(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return week_name(now.date() - timedelta(days=now.weekday() + 7))


def week_range(week: str) -> tuple[str, str]:
    """ISO timestamps [start, end) for a week, comparable with created_at strings."""
    monday = parse_week(week)
    fmt = "%Y-%m-%dT00:00:00Z"
    return monday.strftime(fmt), (monday + timedelta(weeks=1)).strftime(fmt)


@dataclass
class IssueRef:
    number: int
    title: str
    url: str
    reactions: int
    comments: int
    state: str
    labels: list[str]
    body: str = ""


@dataclass
class ThemeTrend:
    theme: str
    count: int                      # issues opened in the target week
    previous: int                   # issues opened the week before
    history: list[int]              # counts for the HISTORY_WEEKS weeks before, oldest first
    top_issues: list[IssueRef] = field(default_factory=list)

    @property
    def change(self) -> int:
        return self.count - self.previous

    @property
    def average(self) -> float:
        return sum(self.history) / len(self.history) if self.history else 0.0

    @property
    def ratio(self) -> float | None:
        """Target week vs the history average; None when the average is 0."""
        return self.count / self.average if self.average else None

    @property
    def rising(self) -> bool:
        return self.count >= RISING_MIN_ISSUES and self.count >= RISING_FACTOR * self.average


@dataclass
class WeekTrends:
    week: str
    themes_version: int
    total_issues: int               # all issues opened in the week
    unthemed: int                   # of those, how many have no theme for this version
    weeks: list[str]                # history weeks + target week, oldest first
    trends: list[ThemeTrend]        # every theme in the list, busiest first
    missing_history: list[str]      # history weeks with no issues at all (likely not fetched)
    unthemed_history: dict[str, int] = field(default_factory=dict)  # week -> unthemed issues

    def rising(self) -> list[ThemeTrend]:
        """Rising themes, biggest jump over the average first."""
        return sorted((t for t in self.trends if t.rising),
                      key=lambda t: (-(t.count - t.average), -t.count, t.theme))


def counts_by_week(conn: sqlite3.Connection, repo: str, version: int, weeks: list[str]
                   ) -> dict[str, dict[str, int]]:
    """{theme: {week: count}} for the given weeks."""
    out: dict[str, dict[str, int]] = {}
    for week in weeks:
        start, end = week_range(week)
        for theme, n in conn.execute(
            "SELECT t.theme, COUNT(*) FROM issues i JOIN issue_themes t "
            "ON t.repo = i.repo AND t.number = i.number AND t.themes_version = ? "
            "WHERE i.repo = ? AND i.created_at >= ? AND i.created_at < ? GROUP BY t.theme",
            (version, repo, start, end),
        ):
            out.setdefault(theme, {})[week] = n
    return out


def top_issues(conn: sqlite3.Connection, repo: str, version: int, theme: str, week: str,
               limit: int | None = TOP_ISSUES) -> list[IssueRef]:
    """Issues of one theme in one week, most reactions first (then comments, then newest)."""
    start, end = week_range(week)
    sql = (
        "SELECT i.* FROM issues i JOIN issue_themes t "
        "ON t.repo = i.repo AND t.number = i.number AND t.themes_version = ? "
        "WHERE i.repo = ? AND t.theme = ? AND i.created_at >= ? AND i.created_at < ? "
        "ORDER BY i.reactions DESC, i.comments DESC, i.number DESC"
    )
    params: list = [version, repo, theme, start, end]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        IssueRef(r["number"], r["title"], r["html_url"], r["reactions"], r["comments"],
                 r["state"], json.loads(r["labels"]), r["body"])
        for r in conn.execute(sql, params)
    ]


def week_trends(conn: sqlite3.Connection, repo: str, theme_ids: list[str], version: int,
                week: str) -> WeekTrends:
    history = [shift_week(week, -n) for n in range(HISTORY_WEEKS, 0, -1)]
    weeks = history + [week]
    counts = counts_by_week(conn, repo, version, weeks)
    start, end = week_range(week)
    total = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE repo = ? AND created_at >= ? AND created_at < ?",
        (repo, start, end),
    ).fetchone()[0]
    themed = sum(c.get(week, 0) for c in counts.values())
    missing = empty_weeks(conn, repo, history)

    trends = []
    for theme in theme_ids:
        c = counts.get(theme, {})
        trends.append(ThemeTrend(
            theme=theme,
            count=c.get(week, 0),
            previous=c.get(history[-1], 0),
            history=[c.get(w, 0) for w in history],
            top_issues=top_issues(conn, repo, version, theme, week),
        ))
    trends.sort(key=lambda t: (-t.count, t.theme))
    return WeekTrends(week, version, total, total - themed, weeks, trends, missing,
                      unthemed_by_week(conn, repo, version, history))


CHART_WEEKS = 8


def chart_counts(conn: sqlite3.Connection, repo: str, version: int, theme_ids: list[str],
                 week: str, n: int = CHART_WEEKS) -> tuple[list[str], dict[str, list[int]]]:
    """Counts per theme for the n weeks ending with `week`, oldest first."""
    weeks = [shift_week(week, -k) for k in range(n - 1, -1, -1)]
    counts = counts_by_week(conn, repo, version, weeks)
    return weeks, {t: [counts.get(t, {}).get(w, 0) for w in weeks] for t in theme_ids}


def empty_weeks(conn: sqlite3.Connection, repo: str, weeks: list[str]) -> list[str]:
    """Weeks with no stored issues at all (usually: not fetched)."""
    out = []
    for w in weeks:
        s, e = week_range(w)
        if not conn.execute("SELECT 1 FROM issues WHERE repo = ? AND created_at >= ? AND "
                            "created_at < ? LIMIT 1", (repo, s, e)).fetchone():
            out.append(w)
    return out


def unthemed_by_week(conn: sqlite3.Connection, repo: str, version: int, weeks: list[str]
                     ) -> dict[str, int]:
    """{week: issues with no theme for this version}, only weeks where that is > 0.

    Counts only include themed issues, so an unthemed week looks quieter than it was.
    """
    out = {}
    for w in weeks:
        s, e = week_range(w)
        n = conn.execute(
            "SELECT COUNT(*) FROM issues i LEFT JOIN issue_themes t ON t.repo = i.repo AND "
            "t.number = i.number AND t.themes_version = ? WHERE i.repo = ? AND "
            "i.created_at >= ? AND i.created_at < ? AND t.number IS NULL",
            (version, repo, s, e)).fetchone()[0]
        if n:
            out[w] = n
    return out
