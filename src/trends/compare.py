"""Compare theme assignments between two themes.yaml versions.

Used after editing the theme list: which issues changed theme, and how the per-theme
counts differ. Only issues themed under both versions are compared.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from .trends import week_range


@dataclass
class Comparison:
    old: int
    new: int
    old_counts: Counter = field(default_factory=Counter)
    new_counts: Counter = field(default_factory=Counter)
    compared: int = 0
    moves: Counter = field(default_factory=Counter)        # (from, to) -> count
    moved: list[tuple[int, str, str, str]] = field(default_factory=list)  # number, from, to, title
    only_old: int = 0                                      # themed under old only

    @property
    def moved_count(self) -> int:
        return len(self.moved)


def compare(conn: sqlite3.Connection, repo: str, old: int, new: int,
            week: str | None = None) -> Comparison:
    sql = (
        "SELECT i.number, i.title, a.theme AS old_theme, b.theme AS new_theme "
        "FROM issues i JOIN issue_themes a ON a.repo = i.repo AND a.number = i.number "
        "AND a.themes_version = ? LEFT JOIN issue_themes b ON b.repo = i.repo "
        "AND b.number = i.number AND b.themes_version = ? WHERE i.repo = ?"
    )
    params: list = [old, new, repo]
    if week:
        start, end = week_range(week)
        sql += " AND i.created_at >= ? AND i.created_at < ?"
        params += [start, end]
    c = Comparison(old, new)
    for r in conn.execute(sql + " ORDER BY i.number", params):
        if r["new_theme"] is None:
            c.only_old += 1
            continue
        c.compared += 1
        c.old_counts[r["old_theme"]] += 1
        c.new_counts[r["new_theme"]] += 1
        if r["old_theme"] != r["new_theme"]:
            c.moves[(r["old_theme"], r["new_theme"])] += 1
            c.moved.append((r["number"], r["old_theme"], r["new_theme"], r["title"]))
    return c


def render(c: Comparison, names: dict[str, str], scope: str, list_moved: bool = False) -> str:
    themes = sorted(set(c.old_counts) | set(c.new_counts),
                    key=lambda t: (-c.new_counts[t], -c.old_counts[t], t))
    out = [f"Theme assignments, v{c.old} vs v{c.new}, {scope}: {c.compared} issues themed "
           f"under both, {c.moved_count} moved ({_pct(c.moved_count, c.compared)}).", "",
           f"| Theme | v{c.old} | v{c.new} | Change |", "|---|---:|---:|---:|"]
    for t in themes:
        d = c.new_counts[t] - c.old_counts[t]
        out.append(f"| {names.get(t, t)} | {c.old_counts[t]} | {c.new_counts[t]} | "
                   f"{'+' if d > 0 else ''}{d if d else 0} |")
    out.append(f"| **Total** | {sum(c.old_counts.values())} | {sum(c.new_counts.values())} | |")
    if c.moves:
        out += ["", "Moves (from -> to: issues):"]
        for (a, b), n in c.moves.most_common():
            out.append(f"- {names.get(a, a)} -> {names.get(b, b)}: {n}")
    if list_moved and c.moved:
        out += ["", "Moved issues:"]
        out += [f"- {n}: {a} -> {b}: {title[:80]}" for n, a, b, title in c.moved]
    if c.only_old:
        out += ["", f"{c.only_old} issues are themed under v{c.old} but not yet v{c.new}; "
                "run `trends theme`."]
    return "\n".join(out)


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.0f}%" if total else "0%"
