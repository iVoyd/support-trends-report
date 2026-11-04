"""Thin wrapper around the Anthropic Messages API: JSON output, prompt caching, cost records.

Everything that talks to the API goes through `JsonLLM.call`, so tests can swap in a fake
with the same signature and no network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

DEFAULT_MODEL = "claude-haiku-5-5"

# USD per million tokens, prompts up to 100K tokens (Claude API list price, Nov 2026).
# Cache writes (5-minute TTL) cost 1.25x input; cache reads 0.1x input.
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
THINKING_HEADROOM = 8000
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


class LLMError(RuntimeError):
    """A single call failed; the caller may skip this item and carry on.

    `usage` is set when the API answered (and billed) but the answer was unusable.
    """

    usage: Usage | None = None


class LLMFatalError(LLMError):
    """Retrying will not help (bad key, unknown model, malformed request); stop the run."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.calls + other.calls,
        )

    def cost(self, model: str) -> float:
        if model not in PRICES:
            raise LLMError(f"no price on file for {model}; add it to trends.llm.PRICES")
        p = PRICES[model]
        per = 1_000_000
        return (
            self.input_tokens * p["input"] / per
            + self.cache_write_tokens * p["input"] * CACHE_WRITE_MULT / per
            + self.cache_read_tokens * p["input"] * CACHE_READ_MULT / per
            + self.output_tokens * p["output"] / per
        )


class LLM(Protocol):
    model: str

    def call(self, system: str, user: str, schema: dict, max_tokens: int = 4096
             ) -> tuple[dict, Usage]: ...


def usage_from_response(usage) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        calls=1,
    )


def parse_json_message(message) -> dict:
    """Pull the JSON object out of a Messages API response with structured output."""
    if message.stop_reason == "refusal":
        raise LLMError("model declined the request (stop_reason=refusal)")
    if message.stop_reason == "max_tokens":
        raise LLMError("response hit max_tokens before the JSON was complete")
    text = next((b.text for b in message.content if b.type == "text"), None)
    if text is None:
        raise LLMError("response had no text block")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"response was not valid JSON: {e}") from e


class AnthropicJSON:
    """Calls Claude with a cached system prompt and a JSON-schema-constrained answer."""

    def __init__(self, model: str = DEFAULT_MODEL, client=None, effort: str = "low",
                 thinking: str = "disabled"):
        if effort not in EFFORTS:
            raise LLMFatalError(f"effort must be one of {', '.join(EFFORTS)}")
        if thinking not in ("disabled", "adaptive"):
            raise LLMFatalError("thinking must be 'disabled' or 'adaptive'")
        if thinking == "disabled" and effort in ("xhigh", "max"):
            # Claude Haiku 5.5 accepts thinking disabled only at effort high or below.
            raise LLMFatalError("thinking 'disabled' needs effort high or below")
        self.effort, self.thinking = effort, thinking
        if client is None:
            import anthropic  # imported here so tests never need a key

            client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        self.client = client
        self.model = model

    def call(self, system: str, user: str, schema: dict, max_tokens: int = 4096
             ) -> tuple[dict, Usage]:
        import anthropic

        try:
            message = self._create(system, user, schema, max_tokens)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, anthropic.BadRequestError) as e:
            raise LLMFatalError(f"{type(e).__name__}: {e}") from e
        except anthropic.APIError as e:  # rate limit / 5xx / network after SDK retries
            raise LLMError(f"{type(e).__name__}: {e}") from e
        usage = usage_from_response(message.usage)
        try:
            return parse_json_message(message), usage
        except LLMError as e:
            e.usage = usage
            raise

    def _create(self, system: str, user: str, schema: dict, max_tokens: int):
        if self.thinking == "adaptive":
            max_tokens += THINKING_HEADROOM  # room for reasoning before the JSON answer
        return self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            # The system prompt (instructions + theme list) is identical on every call in a
            # run, so it is cached; only the per-batch user message is billed at full price.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            # Defaults: no thinking, low effort - classification is cheap and fast that way.
            # There is no temperature setting: Claude Haiku 5.5 rejects non-default sampling
            # parameters, and API version 2026-10-21 rejects them on every model.
            thinking={"type": self.thinking},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )


# --- run records ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(conn: sqlite3.Connection, command: str, model: str) -> int:
    cur = conn.execute(
        "INSERT INTO llm_runs (command, model, started_at) VALUES (?, ?, ?)",
        (command, model, _now()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, model: str, usage: Usage) -> float:
    cost = usage.cost(model)
    conn.execute(
        "UPDATE llm_runs SET finished_at=?, calls=?, input_tokens=?, output_tokens=?, "
        "cache_read_tokens=?, cache_write_tokens=?, cost_usd=? WHERE id=?",
        (_now(), usage.calls, usage.input_tokens, usage.output_tokens,
         usage.cache_read_tokens, usage.cache_write_tokens, cost, run_id),
    )
    conn.commit()
    return cost
