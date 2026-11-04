"""Measure how repeatable theming is: theme the same issues several times, compare.

Nothing is saved to issue_themes. Each pass sends the issues in batches exactly like
`trends theme`. By default every pass uses the same batches, so differences come only
from the model. With shuffle=True, passes after the first use a different order, so each
issue is judged next to different neighbours; the extra disagreement measures how much
batch context matters.
"""

from __future__ import annotations

import random
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from .classify import assign
from .llm import LLM, Usage
from .themes import ThemeSet


@dataclass
class StabilityResult:
    issues: int                       # issues sampled
    passes: int
    compared: int = 0                 # answered validly in every pass
    agreed: int = 0                   # same theme in every pass
    agreed_with_stored: int = 0       # pass 1 theme == theme saved by `trends theme`
    stored_compared: int = 0
    disagreements: list[tuple[int, list[str], str]] = field(default_factory=list)
    pairs: Counter = field(default_factory=Counter)   # sorted (theme a, theme b) -> issues
    failed: int = 0                   # issues missing from at least one pass
    usage: Usage = field(default_factory=Usage)

    @property
    def rate(self) -> float:
        return self.agreed / self.compared if self.compared else 0.0

    @property
    def stored_rate(self) -> float:
        return self.agreed_with_stored / self.stored_compared if self.stored_compared else 0.0


def sample_rows(conn: sqlite3.Connection, repo: str, n: int | None, seed: int = 7) -> list:
    rows = conn.execute("SELECT * FROM issues WHERE repo = ? ORDER BY number",
                        (repo,)).fetchall()
    if n is not None and n < len(rows):
        rows = sorted(random.Random(seed).sample(rows, n), key=lambda r: r["number"])
    return rows


def measure(llm: LLM, conn: sqlite3.Connection, ts: ThemeSet, repo: str,
            sample: int | None = None, passes: int = 2, batch_size: int = 20,
            shuffle: bool = False, seed: int = 7) -> StabilityResult:
    if passes < 2:
        raise ValueError("need at least 2 passes to compare")
    rows = sample_rows(conn, repo, sample, seed)
    res = StabilityResult(issues=len(rows), passes=passes)
    runs = []
    for p in range(passes):
        order = list(rows)
        if shuffle and p > 0:
            random.Random(seed + p).shuffle(order)
        answers, run = assign(llm, ts, order, batch_size)
        res.usage += run.usage
        runs.append(answers)

    stored = dict(conn.execute(
        "SELECT number, theme FROM issue_themes WHERE repo = ? AND themes_version = ?",
        (repo, ts.version)).fetchall())
    for r in rows:
        n = r["number"]
        themes = [a[n][0] for a in runs if n in a]
        if len(themes) < passes:
            res.failed += 1
            continue
        res.compared += 1
        if len(set(themes)) == 1:
            res.agreed += 1
        else:
            res.disagreements.append((n, themes, r["title"]))
            for a, b in {tuple(sorted((x, y))) for x in themes for y in themes if x != y}:
                res.pairs[(a, b)] += 1
        if n in stored:
            res.stored_compared += 1
            res.agreed_with_stored += themes[0] == stored[n]
    return res


def render(res: StabilityResult, names: dict[str, str], settings: str,
           list_all: bool = False) -> str:
    out = [f"Stability: {res.issues} issues, {res.passes} passes ({settings}).",
           f"Same theme in every pass: {res.agreed} of {res.compared} "
           f"({100 * res.rate:.1f}%).",
           ]
    if res.stored_compared:
        out.append(f"Pass 1 vs themes saved by `trends theme`: {res.agreed_with_stored} of "
                   f"{res.stored_compared} ({100 * res.stored_rate:.1f}%).")
    if res.failed:
        out.append(f"{res.failed} issues had no valid answer in some pass (not compared).")
    if res.pairs:
        out += ["", "Themes the model wavered between (issues):"]
        out += [f"- {names.get(a, a)} / {names.get(b, b)}: {n}"
                for (a, b), n in res.pairs.most_common(10)]
    if list_all and res.disagreements:
        out += ["", "Issues that changed theme:"]
        out += [f"- {n}: {' / '.join(t)}: {title[:70]}" for n, t, title in res.disagreements]
    return "\n".join(out)
