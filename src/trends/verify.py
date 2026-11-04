"""Check that the numbers in a Markdown report are right.

Two checks:
- `check_against_db`: recompute the week from SQLite and compare every number in the
  report's "All themes" table and header line. Use it after a fetch/theme run, or in
  `trends check-report`.
- `check_internal`: no database needed. The "All themes" table must agree with the
  "Weekly counts" table in the same file (this week, previous week, change, average,
  rising flag, totals). CI runs this on the committed sample reports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .themes import ThemeSet
from .trends import HISTORY_WEEKS, RISING_FACTOR, RISING_MIN_ISSUES, WeekTrends

HEADER_RE = re.compile(r"(\d+) issues opened; (\d+) themed against themes.yaml version (\d+)")


@dataclass
class Row:
    count: int
    previous: int
    change: int
    average: str        # as printed, e.g. "2.2"
    rising: bool


def _section(md: str, title: str) -> list[str]:
    lines, out, inside = md.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            inside = line.strip() == f"## {title}"
            continue
        if inside:
            out.append(line)
    return out


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = [ln for ln in lines if ln.startswith("|")]
    return [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows[2:]]


def parse(md: str) -> tuple[dict, dict[str, Row], tuple[int, int] | None, dict[str, list[int]]]:
    """Return (header numbers, All-themes rows by name, totals, weekly counts by name)."""
    m = HEADER_RE.search(md)
    header = ({"issues": int(m.group(1)), "themed": int(m.group(2)),
               "version": int(m.group(3))} if m else {})
    rows, totals = {}, None
    for cells in _table_rows(_section(md, "All themes")):
        if cells[0] == "**Total**":
            totals = (int(cells[1]), int(cells[2]))
            continue
        name, count, prev, change, avg, _ratio, rising = cells[:7]
        rows[name] = Row(int(count), int(prev), int(change), avg, rising == "yes")
    weekly = {cells[0]: [int(c) for c in cells[1:]]
              for cells in _table_rows(_section(md, "Weekly counts"))}
    return header, rows, totals, weekly


def check_internal(md: str) -> list[str]:
    header, rows, totals, weekly = parse(md)
    problems = []
    if not header:
        problems.append("header line with issue counts not found")
    if not rows:
        return problems + ["'All themes' table not found or empty"]
    for name, r in rows.items():
        w = weekly.get(name)
        if w is None or len(w) != HISTORY_WEEKS + 1:
            problems.append(f"{name}: missing or short row in 'Weekly counts'")
            continue
        history = w[:HISTORY_WEEKS]
        avg = sum(history) / HISTORY_WEEKS
        if r.count != w[-1]:
            problems.append(f"{name}: this week {r.count} != weekly table {w[-1]}")
        if r.previous != history[-1]:
            problems.append(f"{name}: previous week {r.previous} != weekly table {history[-1]}")
        if r.change != r.count - r.previous:
            problems.append(f"{name}: change {r.change} != {r.count} - {r.previous}")
        if r.average != f"{avg:.1f}":
            problems.append(f"{name}: average {r.average} != {avg:.1f}")
        rising = r.count >= RISING_MIN_ISSUES and r.count >= RISING_FACTOR * avg
        if r.rising != rising:
            problems.append(f"{name}: rising flag {r.rising} but the rule gives {rising}")
    if totals is None:
        problems.append("total row missing")
    else:
        if totals[0] != sum(r.count for r in rows.values()):
            problems.append(f"total {totals[0]} != sum of themes")
        if totals[1] != sum(r.previous for r in rows.values()):
            problems.append(f"previous-week total {totals[1]} != sum of themes")
        if header and header["themed"] != totals[0]:
            problems.append(f"header says {header['themed']} themed, table totals {totals[0]}")
    return problems


def check_against_db(md: str, wt: WeekTrends, ts: ThemeSet) -> list[str]:
    header, rows, totals, _ = parse(md)
    names = {t.id: t.name for t in ts.themes}
    problems = check_internal(md)
    if header:
        if header["version"] != wt.themes_version:
            problems.append(f"report uses themes version {header['version']}, "
                            f"database check uses {wt.themes_version}")
        if header["issues"] != wt.total_issues:
            problems.append(f"report says {header['issues']} issues, database has "
                            f"{wt.total_issues}")
        if header["themed"] != wt.total_issues - wt.unthemed:
            problems.append(f"report says {header['themed']} themed, database has "
                            f"{wt.total_issues - wt.unthemed}")
    for t in wt.trends:
        name = names.get(t.theme, t.theme)
        r = rows.get(name)
        if r is None:
            problems.append(f"{name}: in the database but not in the report")
            continue
        expected = Row(t.count, t.previous, t.change, f"{t.average:.1f}", t.rising)
        if r != expected:
            problems.append(f"{name}: report {r} != database {expected}")
    extra = set(rows) - {names.get(t.theme, t.theme) for t in wt.trends}
    problems += [f"{n}: in the report but not in the theme list" for n in sorted(extra)]
    return problems
