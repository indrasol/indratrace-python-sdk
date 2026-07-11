"""Live Claude Agent SDK capture: one real, tiny agent run with a tool lands the
full span tree — agent → turn(s) → tool — in the harness ClickHouse, with
nonzero token usage and `agent.framework='claude-agent-sdk'`.

Marked `genai` (NOT `integration`): it runs the real `claude` CLI subprocess and
makes a real (tiny) API call, so it is excluded from the offline and plain
integration runs. It runs only under `pytest -m genai`, and auto-skips unless the
dev harness is up, `ANTHROPIC_API_KEY` is exported, AND the `claude` CLI is on
PATH (the Agent SDK spawns it — ADR 0008).

The tool is an in-process SDK MCP tool (`create_sdk_mcp_server` + `@tool`), so
the run needs no filesystem/network tool and the tool span is `mcp__…`. We keep
the prompt trivial and cap turns so the probe stays cheap.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest

from indratrace import init_observability
from indratrace.init import _get_provider, _reset_for_tests

OTLP_ENDPOINT = "http://localhost:4318"
CLICKHOUSE_URL = "http://localhost:8123"
CLICKHOUSE_AUTH = ("otel", "otel")

ROW_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5

MODEL = "claude-haiku-4-5"


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
HAS_CLI = shutil.which("claude") is not None

pytestmark = [
    pytest.mark.genai,
    pytest.mark.skipif(
        not harness_is_up(),
        reason="dev harness not reachable "
        "(docker compose -f dev/docker-compose.yml up -d)",
    ),
    pytest.mark.skipif(not HAS_KEY, reason="ANTHROPIC_API_KEY not set"),
    pytest.mark.skipif(
        not HAS_CLI, reason="claude CLI not on PATH (the Agent SDK spawns it)"
    ),
]


@pytest.fixture
def product() -> str:
    return f"agentsdk-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def _rows(sql: str) -> list[list[str]]:
    raw = clickhouse_query(sql)
    return [line.split("\t") for line in raw.splitlines()] if raw else []


def wait_for_rows(
    sql: str,
    predicate: Callable[[list[list[str]]], bool] | None = None,
) -> list[list[str]]:
    """Poll ClickHouse until `predicate(rows)` holds (or a deadline).

    The spans of one agent run arrive in **several OTLP batches** — the agent
    span ends *last* (it wraps the whole run) so it flushes after the turn/tool
    spans. Returning on the first non-empty result would race: the turn+tool rows
    can be visible a poll before the agent-span row is. So callers asserting on
    the *complete* tree pass a `predicate` (e.g. "an agent-kind row is present"),
    and this keeps polling until the whole tree is queryable. Default predicate
    is "any row", preserving the simple case.
    """
    if predicate is None:
        predicate = bool
    deadline = time.monotonic() + ROW_TIMEOUT_SECONDS
    rows: list[list[str]] = []
    while time.monotonic() < deadline:
        rows = _rows(sql)
        if predicate(rows):
            return rows
        time.sleep(POLL_INTERVAL_SECONDS)
    return rows


def test_real_agent_run_lands_the_full_tree(product: str) -> None:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        create_sdk_mcp_server,
        query,
        tool,
    )

    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
    )
    provider = _get_provider()
    assert provider is not None

    # A trivial in-process tool the agent is told to call — no fs/network needed,
    # and it surfaces as an mcp__ tool span.
    @tool("echo", "Echo back the given text", {"text": str})
    async def echo(args: dict) -> dict:
        return {"content": [{"type": "text", "text": args["text"]}]}

    server = create_sdk_mcp_server(name="probe", version="1.0.0", tools=[echo])
    options = ClaudeAgentOptions(
        model=MODEL,
        max_turns=3,
        mcp_servers={"probe": server},
        allowed_tools=["mcp__probe__echo"],
        permission_mode="bypassPermissions",
    )

    async def run() -> None:
        prompt = "Call the echo tool with text 'ok'. Then reply with just: done"
        async for _message in query(prompt=prompt, options=options):
            pass

    asyncio.run(run())

    assert provider.force_flush(timeout_millis=15_000), "spans never left the SDK"

    # Wait for the WHOLE tree: the agent span flushes last (it wraps the run), so
    # poll until an agent-kind row is present, not just until any row is.
    rows = wait_for_rows(
        "SELECT SpanName, SpanId, ParentSpanId, TraceId, "
        "SpanAttributes['indratrace.span.kind'], "
        "SpanAttributes['agent.framework'], "
        "SpanAttributes['gen_ai.usage.output_tokens'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "AND SpanAttributes['agent.framework'] = 'claude-agent-sdk' FORMAT TSV",
        predicate=lambda rows: any(r[4] == "agent" for r in rows)
        and any(r[4] == "turn" for r in rows),
    )
    assert rows, f"no agent-sdk spans for product={product}"

    kinds = {r[4] for r in rows}
    assert "agent" in kinds, f"no agent span; kinds={kinds}"
    assert "turn" in kinds, f"no turn span; kinds={kinds}"

    # Every agent-sdk span carries the framework attribute.
    assert all(r[5] == "claude-agent-sdk" for r in rows)

    # One trace across the whole tree.
    trace_ids = {r[3] for r in rows}
    assert len(trace_ids) == 1, f"agent-sdk spans split across traces: {trace_ids}"

    # The agent span is the root of the agent-sdk subtree; turns nest under it.
    agent = next(r for r in rows if r[4] == "agent")
    turns = [r for r in rows if r[4] == "turn"]
    assert all(t[2] == agent[1] for t in turns), "a turn span is not under the agent"

    # Nonzero output tokens on at least one turn (exact provider-reported usage).
    turn_out = [int(t[6]) for t in turns if t[6] not in ("", "0")]
    assert turn_out and max(turn_out) > 0, (
        f"no nonzero token usage on any turn span; turns={turns}"
    )

    # The tool the agent called shows up as an mcp__ tool span in the same trace.
    # Poll until the echo tool span specifically is present (same batching race).
    tool_rows = wait_for_rows(
        "SELECT SpanName, SpanAttributes['tool.mcp_server'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "AND SpanAttributes['indratrace.span.kind'] = 'tool' FORMAT TSV",
        predicate=lambda rows: any("echo" in r[0] for r in rows),
    )
    assert any("echo" in r[0] for r in tool_rows), (
        f"no echo tool span landed; tool spans={tool_rows}"
    )
    echo_row = next(r for r in tool_rows if "echo" in r[0])
    assert echo_row[1] == "probe", (
        f"echo tool span missing tool.mcp_server='probe'; got {echo_row}"
    )
