"""Recommendations for the top rising themes, grounded in the issues behind them.

For each theme the model sees only that theme's issues from the target week, with their
URLs, and writes 1-2 concrete actions. Every action must cite issue URLs from that input.
The answer is checked in code; if anything is off, the model gets one retry with the list
of problems. Actions that still cite unknown URLs, or cite nothing, are dropped and
reported, so the report never shows an uncited or invented reference.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .llm import LLM, LLMError, LLMFatalError, Usage
from .themes import Theme
from .trends import IssueRef, ThemeTrend

MAX_THEMES = 3
MAX_ACTIONS = 2
MAX_ISSUES_IN_PROMPT = 40
BODY_EXCERPT = 400
KINDS = {
    "docs_fix": "Docs fix",
    "triage_priority": "Bug triage priority",
    "faq_entry": "FAQ entry",
}

SYSTEM = """You help the maintainers and support team of an open-source project decide what
to do about a theme of GitHub issues that is rising this week.

You get one theme, its weekly numbers, and the issues opened in it this week, each with its
URL. Write 1 or 2 actions. Each action is one of:
- docs_fix: a specific docs page or section to add or correct, and what it should say.
- triage_priority: a bug (or cluster of duplicates) to prioritise, and why now.
- faq_entry: a question users keep asking, with the short answer support should give.

Rules:
- Base every action only on the issues given. Cite the URLs of the issues it is based on,
  copied exactly from the input. Cite at least one, and only issues that support the action.
- Be specific: name the feature, error, or page. No generic advice such as "improve docs"
  or "monitor the situation".
- title: under 90 characters. detail: 1-3 plain sentences.
- If the issues are too varied or vague for a useful action, return one triage_priority
  action that says so and cites the most-reacted issues.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "detail", "citations"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


@dataclass
class Action:
    kind: str
    title: str
    detail: str
    citations: list[str]


@dataclass
class ThemeRecommendation:
    theme: str
    actions: list[Action] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def user_prompt(theme: Theme, trend: ThemeTrend, issues: list[IssueRef]) -> str:
    avg = f"{trend.average:.1f}"
    lines = [
        f"Theme: {theme.name} ({theme.id}). {theme.description}",
        f"This week: {trend.count} new issues. Last week: {trend.previous}. "
        f"Average of the previous {len(trend.history)} weeks: {avg}.",
        "",
        f"Issues opened this week in this theme ({len(issues)}, most reactions first):",
    ]
    for i in issues:
        body = " ".join(i.body.split())[:BODY_EXCERPT]
        lines.append(f"- {i.url}")
        lines.append(f"  title: {i.title}")
        lines.append(f"  labels: {', '.join(i.labels) or '-'} | state: {i.state} | "
                     f"reactions: {i.reactions} | comments: {i.comments}")
        if body:
            lines.append(f"  body: {body}")
    return "\n".join(lines)


def validate_actions(data: dict, allowed_urls: set[str]) -> tuple[list[Action], list[str]]:
    """Keep actions whose citations are all real input URLs. Returns (actions, problems)."""
    problems: list[str] = []
    raw = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        return [], ["no actions returned"]
    if len(raw) > MAX_ACTIONS:
        problems.append(f"{len(raw)} actions returned; kept the first {MAX_ACTIONS}")
        raw = raw[:MAX_ACTIONS]
    actions = []
    for n, a in enumerate(raw, start=1):
        if not isinstance(a, dict):
            problems.append(f"action {n}: not an object")
            continue
        kind, title, detail = a.get("kind"), str(a.get("title", "")).strip(), \
            str(a.get("detail", "")).strip()
        cites = a.get("citations") if isinstance(a.get("citations"), list) else []
        cites = list(dict.fromkeys(str(c).strip().rstrip("/") for c in cites))  # dedupe
        unknown = [c for c in cites if c not in allowed_urls]
        if kind not in KINDS:
            problems.append(f"action {n}: unknown kind {kind!r}")
        elif not title or not detail:
            problems.append(f"action {n}: empty title or detail")
        elif not cites:
            problems.append(f"action {n}: cites no issues")
        elif unknown:
            problems.append(f"action {n}: cites issues not in the input: {', '.join(unknown)}")
        else:
            actions.append(Action(kind, title, detail, cites))
    return actions, problems


def recommend_theme(llm: LLM, theme: Theme, trend: ThemeTrend, issues: list[IssueRef]
                    ) -> tuple[ThemeRecommendation, Usage]:
    issues = issues[:MAX_ISSUES_IN_PROMPT]
    allowed = {i.url.rstrip("/") for i in issues}
    prompt = user_prompt(theme, trend, issues)
    rec, usage = ThemeRecommendation(theme.id), Usage()
    for attempt in range(2):
        try:
            data, u = llm.call(SYSTEM, prompt, SCHEMA, max_tokens=1500)
        except LLMFatalError:
            raise
        except LLMError as e:
            usage += e.usage or Usage()
            rec.problems.append(f"attempt {attempt + 1}: {e}")
            continue
        usage += u
        actions, problems = validate_actions(data, allowed)
        if actions and not problems:
            rec.actions = actions
            return rec, usage
        if actions:  # keep the best valid set so far in case the retry is worse
            rec.actions = actions
        rec.problems += [f"attempt {attempt + 1}: {p}" for p in problems]
        prompt += ("\n\nYour previous answer had these problems; fix them and answer again:\n"
                   + "\n".join(f"- {p}" for p in problems))
    return rec, usage


def recommend(llm: LLM, themes_by_id: dict[str, Theme], rising: list[ThemeTrend],
              issues_by_theme: dict[str, list[IssueRef]]
              ) -> tuple[list[ThemeRecommendation], Usage]:
    """Recommendations for up to MAX_THEMES rising themes ('other' is skipped)."""
    recs, usage = [], Usage()
    for trend in [t for t in rising if t.theme != "other"][:MAX_THEMES]:
        rec, u = recommend_theme(llm, themes_by_id[trend.theme], trend,
                                 issues_by_theme[trend.theme])
        recs.append(rec)
        usage += u
    return recs, usage


# --- storage -----------------------------------------------------------------------------

def save(conn: sqlite3.Connection, repo: str, week: str, version: int,
         rec: ThemeRecommendation, model: str, run_id: int | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO recommendations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (repo, week, version, rec.theme, json.dumps([asdict(a) for a in rec.actions]),
         json.dumps(rec.problems), model, run_id,
         datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


def load(conn: sqlite3.Connection, repo: str, week: str, version: int, theme: str
         ) -> ThemeRecommendation | None:
    row = conn.execute(
        "SELECT actions, problems FROM recommendations WHERE repo = ? AND week = ? AND "
        "themes_version = ? AND theme = ?", (repo, week, version, theme)).fetchone()
    if row is None:
        return None
    return ThemeRecommendation(theme, [Action(**a) for a in json.loads(row["actions"])],
                               json.loads(row["problems"]))
