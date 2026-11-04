import json

import anthropic
import pytest
from anthropic.types import Message
from conftest import FIXTURES

from trends import llm

MESSAGE = json.loads((FIXTURES / "anthropic" / "theme_response.json").read_text())


def message(**overrides) -> Message:
    return Message.model_validate({**MESSAGE, **overrides})


class FakeMessages:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


def test_cost_haiku():
    u = llm.Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    # 0.10 input + 0.50 output + 0.01 cache read + 0.125 cache write
    assert u.cost("claude-haiku-5-5") == pytest.approx(0.735)


def test_cost_unknown_model_is_an_error():
    with pytest.raises(llm.LLMError):
        llm.Usage(input_tokens=1).cost("some-model")


def test_usage_adds_up():
    total = llm.Usage(1, 2, 3, 4, 1) + llm.Usage(10, 20, 30, 40, 1)
    assert total == llm.Usage(11, 22, 33, 44, 2)


def test_call_parses_fixture_and_reads_usage():
    client = FakeClient(message())
    data, usage = llm.AnthropicJSON(client=client).call("SYS", "USER", {"type": "object"})
    assert data["assignments"][0] == {"number": 20101, "theme": "storage", "confidence": "high"}
    assert usage == llm.Usage(input_tokens=412, output_tokens=48, cache_read_tokens=905,
                              cache_write_tokens=0, calls=1)


def test_call_request_shape():
    client = FakeClient(message())
    schema = {"type": "object", "properties": {}}
    llm.AnthropicJSON(client=client).call("SYS", "USER", schema, max_tokens=100)
    kw = client.messages.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["max_tokens"] == 100
    assert kw["system"] == [{"type": "text", "text": "SYS",
                             "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"]["format"] == {"type": "json_schema", "schema": schema}


def test_refusal_raises_and_keeps_usage():
    client = FakeClient(message(stop_reason="refusal"))
    with pytest.raises(llm.LLMError) as exc:
        llm.AnthropicJSON(client=client).call("SYS", "USER", {})
    assert exc.value.usage.input_tokens == 412  # billed tokens still get recorded


def test_truncated_and_invalid_json_raise():
    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.parse_json_message(message(stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError, match="not valid JSON"):
        llm.parse_json_message(message(content=[{"type": "text", "text": "{nope"}]))


def _status_error(cls, code):
    try:  # anthropic 2.x uses httpx2; older releases use httpx
        import httpx2 as httpx
    except ImportError:
        import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


def test_auth_error_is_fatal_rate_limit_is_not():
    with pytest.raises(llm.LLMFatalError):
        llm.AnthropicJSON(client=FakeClient(
            _status_error(anthropic.AuthenticationError, 401))).call("S", "U", {})
    with pytest.raises(llm.LLMError) as exc:
        llm.AnthropicJSON(client=FakeClient(
            _status_error(anthropic.RateLimitError, 429))).call("S", "U", {})
    assert not isinstance(exc.value, llm.LLMFatalError)


def test_run_records(conn):
    run_id = llm.start_run(conn, "theme", "claude-haiku-5-5")
    cost = llm.finish_run(conn, run_id, "claude-haiku-5-5",
                          llm.Usage(input_tokens=2_000_000, calls=3))
    row = conn.execute("SELECT * FROM llm_runs WHERE id = ?", (run_id,)).fetchone()
    assert cost == pytest.approx(0.2)
    assert row["calls"] == 3 and row["cost_usd"] == pytest.approx(0.2)
    assert row["finished_at"]


def test_effort_and_thinking_settings():
    client = FakeClient(message())
    llm.AnthropicJSON(client=client, effort="medium", thinking="adaptive").call(
        "SYS", "USER", {}, max_tokens=100)
    kw = client.messages.kwargs
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "medium"
    assert kw["max_tokens"] == 100 + llm.THINKING_HEADROOM
    assert "temperature" not in kw


def test_default_settings_and_invalid_combinations():
    client = FakeClient(message())
    llm.AnthropicJSON(client=client).call("SYS", "USER", {}, max_tokens=100)
    assert client.messages.kwargs["thinking"] == {"type": "disabled"}
    assert client.messages.kwargs["output_config"]["effort"] == "low"
    with pytest.raises(llm.LLMFatalError):
        llm.AnthropicJSON(client=client, effort="max")  # disabled thinking needs <= high
    with pytest.raises(llm.LLMFatalError):
        llm.AnthropicJSON(client=client, effort="extreme")
