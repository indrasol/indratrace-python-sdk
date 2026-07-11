"""Live GenAI capture: a real, tiny Claude call lands a model span in the
harness ClickHouse with nonzero token counts and correct trace lineage.

Marked `genai` (NOT `integration`), so it is excluded from both the offline
suite and the plain integration run — it costs a real (tiny) API call. It runs
only under `pytest -m genai`, and even then auto-skips unless BOTH the dev
harness is up and `ANTHROPIC_API_KEY` is exported.

Two calls are exercised: a normal one and a STREAMING one. Streaming is the
known-hard case — usage arrives only in the final `message_delta` event — so we
assert token counts on the streamed model span specifically.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

from indratrace import init_observability, trace_agent, trace_tool
from indratrace.init import _get_provider, _reset_for_tests

OTLP_ENDPOINT = "http://localhost:4318"
CLICKHOUSE_URL = "http://localhost:8123"
CLICKHOUSE_AUTH = ("otel", "otel")

ROW_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5

# Cheapest current Claude, smallest possible response — a live token-count probe,
# not a generation. Kept tiny on purpose (max_tokens ~16).
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 16


def clickhouse_query(sql: str) -> str:
    response = httpx.post(
        CLICKHOUSE_URL, content=sql, auth=CLICKHOUSE_AUTH, timeout=10.0
    )
    response.raise_for_status()
    return response.text.strip()


def harness_is_up() -> bool:
    try:
        if httpx.get(f"{CLICKHOUSE_URL}/ping", timeout=2.0).status_code != 200:
            return False
        httpx.post(f"{OTLP_ENDPOINT}/v1/traces", content=b"", timeout=2.0)
        return True
    except (httpx.HTTPError, OSError):
        return False


HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY"))

pytestmark = [
    pytest.mark.genai,
    pytest.mark.skipif(
        not harness_is_up(),
        reason="dev harness not reachable "
        "(docker compose -f dev/docker-compose.yml up -d)",
    ),
    pytest.mark.skipif(not HAS_KEY, reason="ANTHROPIC_API_KEY not set"),
]


@pytest.fixture
def product() -> str:
    return f"genai-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def wait_for_rows(sql: str) -> list[list[str]]:
    deadline = time.monotonic() + ROW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        raw = clickhouse_query(sql)
        if raw:
            return [line.split("\t") for line in raw.splitlines()]
        time.sleep(POLL_INTERVAL_SECONDS)
    return []


def _init(product: str) -> None:
    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
    )
    assert _get_provider() is not None


def _model_span_rows(product: str) -> list[list[str]]:
    """Model spans for `product`: name, ids, lineage, and token counts.

    The instrumentor names the span `anthropic.chat`; filter on the presence of
    the token attribute so we assert on the model span, not the agent/tool ones.
    """
    return wait_for_rows(
        "SELECT SpanName, SpanId, ParentSpanId, TraceId, "
        "SpanAttributes['gen_ai.provider.name'], "
        "SpanAttributes['gen_ai.usage.input_tokens'], "
        "SpanAttributes['gen_ai.usage.output_tokens'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "AND mapContains(SpanAttributes, 'gen_ai.usage.input_tokens') "
        "FORMAT TSV"
    )


def test_non_streaming_claude_call_lands_a_model_span(product: str) -> None:
    import anthropic

    _init(product)
    provider = _get_provider()
    client = anthropic.Anthropic()

    @trace_tool
    def ask_model() -> str:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        return msg.content[0].text if msg.content else ""

    @trace_agent("token-probe")
    def run() -> str:
        return ask_model()

    run()

    assert provider.force_flush(timeout_millis=15_000), "spans never left the SDK"

    # The whole trace, to prove lineage.
    all_rows = wait_for_rows(
        "SELECT SpanName, SpanId, ParentSpanId, TraceId "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' FORMAT TSV"
    )
    assert all_rows, f"no spans for product={product}"
    names = {r[0] for r in all_rows}
    assert "agent token-probe" in names
    assert "tool ask_model" in names

    model_rows = _model_span_rows(product)
    assert len(model_rows) == 1, f"expected one model span, got {model_rows}"
    (name, span_id, parent_id, trace_id, provider_name, in_tok, out_tok) = model_rows[0]

    # Token counts: present and nonzero (exact provider-reported usage).
    assert int(in_tok) > 0, "input tokens must be nonzero"
    assert int(out_tok) > 0, "output tokens must be nonzero"
    assert provider_name == "anthropic"

    # Lineage: agent -> tool -> model, one trace.
    by_id = {r[1]: r for r in all_rows}
    agent = next(r for r in all_rows if r[0] == "agent token-probe")
    tool = next(r for r in all_rows if r[0] == "tool ask_model")
    assert trace_id == agent[3], "model span split from the agent's trace"
    assert parent_id == tool[1], "model span is not a child of the tool span"
    assert tool[2] == agent[1], "tool span is not a child of the agent span"
    assert agent[2] == "", "agent span is not the trace root"
    assert parent_id in by_id


def test_streaming_claude_call_still_captures_usage(product: str) -> None:
    """The hard case: usage arrives only in the final streamed event."""
    import anthropic

    _init(product)
    provider = _get_provider()
    client = anthropic.Anthropic()

    @trace_tool
    def ask_model_streaming() -> None:
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        ) as stream:
            for _ in stream.text_stream:
                pass
            stream.get_final_message()

    @trace_agent("stream-probe")
    def run() -> None:
        ask_model_streaming()

    run()

    assert provider.force_flush(timeout_millis=15_000), "spans never left the SDK"

    model_rows = _model_span_rows(product)
    assert len(model_rows) == 1, (
        f"streaming produced no model span with usage; got {model_rows}"
    )
    (_name, _span_id, parent_id, _trace_id, provider_name, in_tok, out_tok) = (
        model_rows[0]
    )
    assert int(in_tok) > 0, "streaming input tokens must be nonzero"
    assert int(out_tok) > 0, "streaming output tokens must be nonzero"
    assert provider_name == "anthropic"
