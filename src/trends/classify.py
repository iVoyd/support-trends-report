"""Assign each issue exactly one theme from the frozen list.

Issues go to the model in batches. The system prompt (rules + theme list) is the same for
every batch, so it is prompt-cached. Every answer is validated: each issue in the batch must
come back exactly once with a theme id from the list. Issues that fail validation are retried
once on their own; anything still invalid is left unthemed and reported, never guessed.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .llm import LLM, LLMError, LLMFatalError, Usage
from .themes import ThemeSet

CONFIDENCE = ("high", "medium", "low")
BODY_EXCERPT = 800


def system_prompt(ts: ThemeSet) -> str:
    lines = [
        "You sort GitHub issues for an open-source project into themes for a weekly",
        "\"what's breaking\" report. The theme list is fixed; never invent a theme.",
        "",
        "For every issue in the input, return exactly one object with:",
        "- number: the issue number, copied from the input.",
        "- theme: the id of the single best-fitting theme from the list below.",
        "- confidence: high (clearly this theme), medium (best fit, some overlap),",
        "  low (weak fit or vague issue).",
        "",
        "How to decide:",
        "- Judge by what is broken or asked for, not by which words appear. An auth error",
        "  shown in the dashboard is an auth issue if the problem is in auth.",
        "- Labels are hints from maintainers; prefer them when they point to one theme.",
        "- Questions, feature requests and bug reports all get a theme by product area.",
        "- Use 'other' only when no theme fits or the issue is too vague to place.",
        "- Return every issue once, in any order. Do not skip any.",
        "",
        f"Theme list (version {ts.version}):",
    ]
    for t in ts.themes:
        hint = f" Typical labels: {', '.join(t.labels)}." if t.labels else ""
        lines.append(f"- {t.id}: {t.name}. {t.description}{hint}")
    return "\n".join(lines)


def schema(ts: ThemeSet) -> dict:
    return {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "theme": {"type": "string", "enum": ts.ids},
                        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                    },
                    "required": ["number", "theme", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["assignments"],
        "additionalProperties": False,
    }


def user_prompt(issues: list) -> str:
    parts = [f"{len(issues)} issues follow.", ""]
    for r in issues:
        labels = r["labels"] if isinstance(r["labels"], list) else json.loads(r["labels"])
        body = " ".join((r["body"] or "").split())[:BODY_EXCERPT]
        parts.append(f"### #{r['number']}: {r['title']}")
        parts.append(f"labels: {', '.join(labels) or '-'}")
        if body:
            parts.append(f"body: {body}")
        parts.append("")
    return "\n".join(parts)


def validate_assignments(data: dict, expected: set[int], theme_ids: set[str]
                         ) -> tuple[dict[int, tuple[str, str]], list[str]]:
    """Return ({number: (theme, confidence)}, problems). Only fully valid entries are kept."""
    problems: list[str] = []
    items = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, ["response has no 'assignments' list"]
    seen: dict[int, list] = {}
    for item in items:
        if not isinstance(item, dict):
            problems.append(f"non-object entry {item!r}")
            continue
        seen.setdefault(item.get("number"), []).append(item)
    valid: dict[int, tuple[str, str]] = {}
    for number, entries in seen.items():
        if number not in expected:
            problems.append(f"#{number}: not in this batch")
            continue
        if len(entries) > 1 and len({e.get("theme") for e in entries}) > 1:
            problems.append(f"#{number}: returned {len(entries)} times with different themes")
            continue
        theme, conf = entries[0].get("theme"), entries[0].get("confidence")
        if theme not in theme_ids:
            problems.append(f"#{number}: unknown theme {theme!r}")
            continue
        if conf not in CONFIDENCE:
            problems.append(f"#{number}: bad confidence {conf!r}")
            continue
        valid[number] = (theme, conf)
    for number in sorted(expected - set(seen)):
        problems.append(f"#{number}: missing from response")
    return valid, problems


@dataclass
class ThemeRunResult:
    themed: int = 0
    skipped_already_done: int = 0
    failed: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def unthemed(conn: sqlite3.Connection, repo: str, since: str, version: int) -> list:
    return conn.execute(
        "SELECT i.* FROM issues i LEFT JOIN issue_themes t "
        "ON t.repo = i.repo AND t.number = i.number AND t.themes_version = ? "
        "WHERE i.repo = ? AND i.created_at >= ? AND t.number IS NULL ORDER BY i.number",
        (version, repo, since),
    ).fetchall()


def assign(llm: LLM, ts: ThemeSet, rows: list, batch_size: int = 20,
           result: ThemeRunResult | None = None,
           on_batch: Callable[[dict[int, tuple[str, str]]], None] | None = None
           ) -> tuple[dict[int, tuple[str, str]], ThemeRunResult]:
    """Theme `rows` in batches, with the single-issue retry. Saves nothing itself.

    Returns ({number: (theme, confidence)}, result). `on_batch` is called with each batch's
    valid answers as they arrive, so a caller can save progress as it goes.
    """
    result = result or ThemeRunResult()
    sys_prompt, sch, ids = system_prompt(ts), schema(ts), set(ts.ids)
    answers: dict[int, tuple[str, str]] = {}

    def run_batch(batch: list) -> list:
        """Theme one batch; return the rows that still need work."""
        expected = {r["number"] for r in batch}
        try:
            data, usage = llm.call(sys_prompt, user_prompt(batch), sch,
                                   max_tokens=64 + 40 * len(batch))
        except LLMFatalError:
            raise
        except LLMError as e:
            if e.usage:
                result.usage += e.usage
            result.problems.append(f"batch {sorted(expected)}: {e}")
            return batch
        result.usage += usage
        valid, problems = validate_assignments(data, expected, ids)
        result.problems += problems
        answers.update(valid)
        if on_batch:
            on_batch(valid)
        result.themed += len(valid)
        return [r for r in batch if r["number"] not in valid]

    retry: list = []
    for start in range(0, len(rows), batch_size):
        retry += run_batch(rows[start:start + batch_size])
    for row in retry:  # one more try, one issue at a time
        if run_batch([row]):
            result.failed.append(row["number"])
    return answers, result


def theme_issues(llm: LLM, conn: sqlite3.Connection, ts: ThemeSet, repo: str, since: str,
                 batch_size: int = 20, run_id: int | None = None,
                 limit: int | None = None) -> ThemeRunResult:
    total = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE repo = ? AND created_at >= ?", (repo, since)
    ).fetchone()[0]
    todo = unthemed(conn, repo, since, ts.version)
    result = ThemeRunResult(skipped_already_done=total - len(todo))
    if limit is not None:
        todo = todo[:limit]

    def save(valid: dict[int, tuple[str, str]]) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        for number, (theme, conf) in valid.items():
            conn.execute(
                "INSERT OR REPLACE INTO issue_themes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, number, ts.version, theme, conf, llm.model, run_id, now),
            )
        conn.commit()

    assign(llm, ts, todo, batch_size, result, on_batch=save)
    return result
