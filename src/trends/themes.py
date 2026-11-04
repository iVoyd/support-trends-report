"""The theme list: seeded once from GitHub labels plus an LLM pass, then frozen in themes.yaml.

Weeks only compare fairly if every week is themed against the same list, so `trends theme`
reads themes.yaml and never changes it. To change the list, edit the file (or reseed with
--force), which bumps `version`; issues are then re-themed against the new version.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .llm import LLM, Usage

DEFAULT_PATH = Path("themes.yaml")
OTHER = "other"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
MIN_THEMES, MAX_THEMES = 4, 25


class ThemeError(ValueError):
    pass


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThemeSet:
    version: int
    themes: tuple[Theme, ...]
    repo: str = ""
    created: str = ""
    model: str = ""

    @property
    def ids(self) -> list[str]:
        return [t.id for t in self.themes]


def validate(data: dict) -> ThemeSet:
    """Check a parsed themes.yaml (or an LLM proposal shaped the same way)."""
    if not isinstance(data, dict):
        raise ThemeError("themes file must be a mapping")
    version = data.get("version", 1)
    if not isinstance(version, int) or version < 1:
        raise ThemeError("version must be a positive integer")
    raw = data.get("themes")
    if not isinstance(raw, list):
        raise ThemeError("'themes' must be a list")
    themes, seen = [], set()
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise ThemeError(f"theme #{i} is not a mapping")
        tid = str(t.get("id", "")).strip()
        if not ID_RE.match(tid):
            raise ThemeError(f"theme id {tid!r} must be lowercase letters, digits, dashes")
        if tid in seen:
            raise ThemeError(f"duplicate theme id {tid!r}")
        seen.add(tid)
        name = str(t.get("name", "")).strip()
        desc = str(t.get("description", "")).strip()
        if not name or not desc:
            raise ThemeError(f"theme {tid!r} needs a name and a description")
        labels = t.get("labels") or []
        if not isinstance(labels, list):
            raise ThemeError(f"theme {tid!r}: labels must be a list")
        themes.append(Theme(tid, name, desc, tuple(str(x) for x in labels)))
    if OTHER not in seen:
        raise ThemeError(f"theme list must include an {OTHER!r} theme")
    if not MIN_THEMES <= len(themes) <= MAX_THEMES:
        raise ThemeError(f"need {MIN_THEMES}-{MAX_THEMES} themes, got {len(themes)}")
    return ThemeSet(
        version=version,
        themes=tuple(themes),
        repo=str(data.get("repo", "")),
        created=str(data.get("created", "")),
        model=str(data.get("model", "")),
    )


def load(path: Path = DEFAULT_PATH) -> ThemeSet:
    if not path.exists():
        raise ThemeError(f"{path} not found; run `trends seed-themes` first")
    return validate(yaml.safe_load(path.read_text()))


def dump(ts: ThemeSet, path: Path = DEFAULT_PATH) -> None:
    data = {
        "version": ts.version,
        "repo": ts.repo,
        "created": ts.created,
        "model": ts.model,
        "themes": [
            {"id": t.id, "name": t.name, "description": t.description, "labels": list(t.labels)}
            for t in ts.themes
        ],
    }
    header = (
        "# Frozen theme list. `trends theme` reads this and never edits it.\n"
        "# Edit by hand if needed and bump `version`; issues are re-themed per version.\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


# --- seeding --------------------------------------------------------------------------------

SEED_SYSTEM = """You design a small, stable list of themes for grouping GitHub issues of one
open-source project, so a weekly report can count issues per theme and spot rising problems.

Rules for the list:
- 8 to 15 themes. Each is a product area or failure mode a maintainer would recognise
  (for example "Auth: sign-in and sessions", "Local dev and CLI"), not a severity or status.
- Themes must not overlap: an issue should clearly belong to one.
- Use the repository's labels as hints for areas, but merge or split them where that makes
  clearer themes. Ignore status/triage labels such as "bug", "needs-triage", "good first issue".
- Do not include an "other" theme; it is added automatically.
- id: short lowercase slug with dashes. name: under 40 characters. description: one sentence
  saying what belongs in the theme, specific enough to decide borderline cases.
- labels: the repository labels (exact names from the input) that map to the theme, or [].
"""

SEED_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "name", "description", "labels"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}

OTHER_THEME = {
    "id": OTHER,
    "name": "Other",
    "description": "Fits none of the other themes, or is too vague to place.",
    "labels": [],
}


def label_counts(conn: sqlite3.Connection, repo: str, since: str) -> Counter:
    counts: Counter = Counter()
    for row in conn.execute(
        "SELECT labels FROM issues WHERE repo = ? AND created_at >= ?", (repo, since)
    ):
        counts.update(json.loads(row["labels"]))
    return counts


def sample_issues(rows: list, n: int, seed: int = 7) -> list:
    if len(rows) <= n:
        return list(rows)
    return sorted(random.Random(seed).sample(list(rows), n), key=lambda r: r["number"])


def build_seed_prompt(repo: str, labels: Counter, sample: list) -> str:
    lines = [f"Repository: {repo}", "", "Labels in use (name: issue count):"]
    lines += [f"- {name}: {count}" for name, count in labels.most_common(60)]
    lines += ["", f"A sample of {len(sample)} recent issues (title | labels):"]
    for r in sample:
        lbls = ", ".join(json.loads(r["labels"])) or "-"
        lines.append(f"- #{r['number']} {r['title'][:140]} | {lbls}")
    lines += ["", "Propose the theme list."]
    return "\n".join(lines)


def seed(llm: LLM, conn: sqlite3.Connection, repo: str, since: str, sample_size: int = 80,
         version: int = 1) -> tuple[ThemeSet, Usage]:
    rows = conn.execute(
        "SELECT number, title, labels FROM issues WHERE repo = ? AND created_at >= ?",
        (repo, since),
    ).fetchall()
    if not rows:
        raise ThemeError("no issues in the database for this window; run `trends fetch` first")
    labels = label_counts(conn, repo, since)
    prompt = build_seed_prompt(repo, labels, sample_issues(rows, sample_size))
    data, usage = llm.call(SEED_SYSTEM, prompt, SEED_SCHEMA, max_tokens=4096)
    proposed = [t for t in data.get("themes", []) if str(t.get("id")) != OTHER]
    known = set(labels)
    for t in proposed:  # keep only label names that really exist
        t["labels"] = [x for x in t.get("labels", []) if x in known]
    ts = validate({
        "version": version,
        "repo": repo,
        "created": datetime.now(UTC).strftime("%Y-%m-%d"),
        "model": llm.model,
        "themes": proposed + [OTHER_THEME],
    })
    return ts, usage
