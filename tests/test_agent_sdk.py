"""Claude Agent SDK auto-instrumentation — mocked, no CLI subprocess, no network.

The Agent SDK runs the agent loop in a *subprocess* `claude` CLI (ADR 0008), so
these tests never spawn it. Instead they exercise the two interception layers
directly:

- **Entrypoint wrap** — the real `enable_agent_sdk_instrumentation` patches
  `InternalClient.process_query` (the method every `claude_agent_sdk.query()`
  call funnels through), so the tests replace *that method* with a fake yielding
  mocked `AssistantMessage` / `ResultMessage` objects and then call the genuine
  `claude_agent_sdk.query(...)`. That drives the actual production seam — method
  lookup is dynamic, so it also proves the fix for the import-order bug (a
  `query` imported before init is still traced). Consuming the stream must
  produce the agent → turn span tree with token usage read off the messages.
- **Tool hooks** — the tracing hook callbacks (`PreToolUse` / `PostToolUse` /
  `PostToolUseFailure`) are called directly with the payload the CLI would send,
  asserting tool-span shape, MCP-server parsing, and ERROR status. The live
  CLI-dispatched path is covered by the `genai` integration test.

A real end-to-end run through the CLI lands in the integration suite
(`tests/integration/test_agent_sdk_live.py`, marked `genai`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import indratrace.agent_sdk as agent_sdk
from indratrace import init_observability, trace_agent
from indratrace.init import _get_provider, _reset_for_tests

# The package must be importable (it's a dev dep); skip the whole module if a
# core-only environment somehow runs it.
claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
)

# ---------------------------------------------------------------------------
# Fakes & fixtures
# ---------------------------------------------------------------------------


def _assistant(model: str = "claude-haiku-4-5", **usage: int) -> AssistantMessage:
    """One assistant turn carrying a usage dict shaped like the CLI's."""
    return AssistantMessage(content=[], model=model, usage=usage or None)


def _result(
    session_id: str = "sess-1", is_error: bool = False, **usage: int
) -> ResultMessage:
    """The terminal result carrying run-total usage + session id."""
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id=session_id,
        usage=usage or None,
    )


def _fake_process_query_yielding(*messages: object):
    """A stand-in for `InternalClient.process_query` that yields the messages.

    Bound-method signature `(self, prompt, options, transport=None)` — this is
    the seam the real wrapper patches, so installing it as `_orig_process_query`
    makes the genuine `claude_agent_sdk.query(...)` drive our tracing stream over
    these messages, with no CLI and no network.
    """

    async def fake_process_query(self, prompt, options, transport=None):  # noqa: ANN001, ARG001
        for message in messages:
            yield message

    return fake_process_query


def _drain(agen) -> None:
    """Consume an async iterator to exhaustion, synchronously."""

    async def run() -> None:
        async for _ in agen:
            pass

    asyncio.run(run())


@pytest.fixture
def traced_query() -> Iterator[InMemorySpanExporter]:
    """Init the SDK (which patches `process_query` for real), tee spans in memory.

    The test then swaps the *inner* `process_query` for a message-yielding fake
    via `_set_messages` and calls the genuine `claude_agent_sdk.query(...)`, so
    the real production wrapper runs over mocked messages.
    """
    _reset_for_tests()

    exporter = InMemorySpanExporter()

    init_observability(product="agent-sdk-tests", instrument_fastapi=False)
    provider = _get_provider()
    assert provider is not None
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    yield exporter

    _reset_for_tests()


def _set_messages(exporter_owner: InMemorySpanExporter, *messages: object) -> None:
    """Point the real wrapper's inner `process_query` at these messages.

    `init` captured the SDK's real `process_query` as `_orig_process_query`;
    replace it with a fake yielding our messages, and re-patch the live method so
    the genuine `claude_agent_sdk.query(...)` traces our fake stream.
    """
    from claude_agent_sdk._internal.client import InternalClient

    fake = _fake_process_query_yielding(*messages)
    agent_sdk._orig_process_query = fake
    InternalClient.process_query = agent_sdk._wrap_process_query(fake)


def _by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {s.name: s for s in exporter.get_finished_spans()}


# ---------------------------------------------------------------------------
# Span tree + token usage from the message stream
# ---------------------------------------------------------------------------


class TestSpanTreeAndUsage:
    def test_agent_and_turn_spans_with_usage(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        _set_messages(
            traced_query,
            _assistant(
                model="claude-haiku-4-5",
                input_tokens=11,
                output_tokens=7,
                cache_read_input_tokens=3,
            ),
            _result(session_id="conv-9", input_tokens=11, output_tokens=7),
        )

        _drain(claude_agent_sdk.query(prompt="hi"))

        spans = _by_name(traced_query)
        agent = spans["agent claude-agent-sdk"]
        turn = spans["turn"]

        # Kinds + framework identity (docs/conventions.md).
        assert agent.attributes[agent_sdk.SPAN_KIND_ATTRIBUTE] == "agent"
        framework = agent.attributes[agent_sdk.AGENT_FRAMEWORK_ATTRIBUTE]
        assert framework == "claude-agent-sdk"
        assert turn.attributes[agent_sdk.SPAN_KIND_ATTRIBUTE] == "turn"

        # One trace, turn nested under agent, agent is the root.
        trace_ids = {s.context.trace_id for s in traced_query.get_finished_spans()}
        assert len(trace_ids) == 1
        assert agent.parent is None
        assert turn.parent is not None
        assert turn.parent.span_id == agent.context.span_id

        # Per-turn usage on the turn span, incl. cache tokens.
        assert turn.attributes[agent_sdk.GEN_AI_USAGE_INPUT_TOKENS] == 11
        assert turn.attributes[agent_sdk.GEN_AI_USAGE_OUTPUT_TOKENS] == 7
        assert turn.attributes[agent_sdk.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS] == 3
        assert turn.attributes[agent_sdk.GEN_AI_USAGE_TOTAL_TOKENS] == 18
        assert turn.attributes[agent_sdk.GEN_AI_RESPONSE_MODEL] == "claude-haiku-4-5"

        # Run-total usage + session id on the agent span.
        assert agent.attributes[agent_sdk.GEN_AI_USAGE_INPUT_TOKENS] == 11
        assert agent.attributes[agent_sdk.GEN_AI_USAGE_OUTPUT_TOKENS] == 7
        assert agent.attributes[agent_sdk.SESSION_ID_ATTRIBUTE] == "conv-9"

    def test_multiple_turns_each_get_a_span(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        _set_messages(
            traced_query,
            _assistant(input_tokens=1, output_tokens=1),
            _assistant(input_tokens=2, output_tokens=2),
            _assistant(input_tokens=3, output_tokens=3),
            _result(input_tokens=6, output_tokens=6),
        )

        _drain(claude_agent_sdk.query(prompt="hi"))

        turns = [s for s in traced_query.get_finished_spans() if s.name == "turn"]
        assert len(turns) == 3

    def test_no_cost_attribute_is_ever_stamped(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        """Raw counts only — no cost math in the SDK (ADR 0005)."""
        _set_messages(
            traced_query,
            _assistant(input_tokens=1, output_tokens=1),
            _result(input_tokens=1, output_tokens=1),
        )
        _drain(claude_agent_sdk.query(prompt="hi"))

        for span in traced_query.get_finished_spans():
            assert not any("cost" in k for k in span.attributes), (
                f"a cost attribute leaked onto {span.name}"
            )


class TestErrorAndAbandon:
    def test_error_run_marks_agent_span_error(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        from opentelemetry.trace import StatusCode

        _set_messages(
            traced_query,
            _assistant(input_tokens=1, output_tokens=1),
            _result(is_error=True, input_tokens=1, output_tokens=1),
        )
        _drain(claude_agent_sdk.query(prompt="hi"))

        agent = _by_name(traced_query)["agent claude-agent-sdk"]
        assert agent.status.status_code == StatusCode.ERROR

    def test_early_abandoned_stream_leaves_no_dangling_span(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        """Consume one message, abandon the rest — the agent span must still close.

        This is the acceptance criterion for streaming: a consumer that stops
        iterating early (break + GC) must not strand an open span.
        """
        _set_messages(
            traced_query,
            _assistant(input_tokens=1, output_tokens=1),
            _assistant(input_tokens=1, output_tokens=1),
            _assistant(input_tokens=1, output_tokens=1),
            _result(input_tokens=3, output_tokens=3),
        )

        async def abandon() -> None:
            agen = claude_agent_sdk.query(prompt="hi")
            async for _ in agen:
                break
            await agen.aclose()  # the generator's finally runs here

        asyncio.run(abandon())

        agents = [
            s
            for s in traced_query.get_finished_spans()
            if s.name == "agent claude-agent-sdk"
        ]
        assert len(agents) == 1
        assert agents[0].end_time is not None, "agent span left dangling after abandon"


class TestComposition:
    def test_nests_under_trace_agent(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        """An agent-sdk run inside a `@trace_agent` nests into that trace."""
        _set_messages(
            traced_query,
            _assistant(input_tokens=1, output_tokens=1),
            _result(input_tokens=1, output_tokens=1),
        )

        @trace_agent("outer")
        async def outer() -> None:
            async for _ in claude_agent_sdk.query(prompt="hi"):
                pass

        asyncio.run(outer())

        spans = _by_name(traced_query)
        outer_span = spans["agent outer"]
        inner = spans["agent claude-agent-sdk"]
        assert inner.parent is not None
        assert inner.parent.span_id == outer_span.context.span_id
        assert len({s.context.trace_id for s in traced_query.get_finished_spans()}) == 1


# ---------------------------------------------------------------------------
# Tool spans via the hook callbacks (the CLI-dispatched path is integration)
# ---------------------------------------------------------------------------


class TestToolHooks:
    """Drive the tracing hook callbacks directly with CLI-shaped payloads.

    The live CLI fires these over the control channel (ADR 0008); offline we call
    them with the same `input` dict the CLI would send and assert span shape.
    """

    def _run_tool(
        self,
        exporter: InMemorySpanExporter,
        *,
        tool_name: str,
        tool_use_id: str = "toolu_1",
        fail: bool = False,
    ) -> ReadableSpan:
        from opentelemetry import trace

        # An agent span for the tool span to parent under, and its context.
        agent = agent_sdk._get_tracer().start_span("agent claude-agent-sdk")
        tool_spans = agent_sdk._ToolSpans(trace.set_span_in_context(agent))

        async def drive() -> None:
            pre = {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": {"x": 1},
                "tool_use_id": tool_use_id,
            }
            await tool_spans.pre_tool_use(pre, tool_use_id, {"signal": None})
            if fail:
                await tool_spans.post_tool_use_failure(
                    {"tool_use_id": tool_use_id, "error": "boom"},
                    tool_use_id,
                    {"signal": None},
                )
            else:
                await tool_spans.post_tool_use(
                    {"tool_use_id": tool_use_id, "tool_response": "ok"},
                    tool_use_id,
                    {"signal": None},
                )

        asyncio.run(drive())
        agent.end()

        return next(
            s for s in exporter.get_finished_spans() if s.name.startswith("tool ")
        )

    def test_tool_span_opens_and_closes(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        tool = self._run_tool(traced_query, tool_name="Read")
        assert tool.name == "tool Read"
        assert tool.attributes[agent_sdk.SPAN_KIND_ATTRIBUTE] == "tool"
        assert tool.attributes[agent_sdk.TOOL_NAME_ATTRIBUTE] == "Read"
        framework = tool.attributes[agent_sdk.AGENT_FRAMEWORK_ATTRIBUTE]
        assert framework == "claude-agent-sdk"
        assert agent_sdk.TOOL_MCP_SERVER_ATTRIBUTE not in tool.attributes
        assert tool.end_time is not None

    def test_mcp_tool_records_server_name(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        tool = self._run_tool(traced_query, tool_name="mcp__github__create_issue")
        assert tool.name == "tool mcp__github__create_issue"
        assert tool.attributes[agent_sdk.TOOL_MCP_SERVER_ATTRIBUTE] == "github"

    def test_tool_failure_sets_error_status(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        from opentelemetry.trace import StatusCode

        tool = self._run_tool(traced_query, tool_name="Bash", fail=True)
        assert tool.status.status_code == StatusCode.ERROR

    def test_tool_span_parents_under_agent_span(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        tool = self._run_tool(traced_query, tool_name="Read")
        agent = next(
            s
            for s in traced_query.get_finished_spans()
            if s.name == "agent claude-agent-sdk"
        )
        assert tool.parent is not None
        assert tool.parent.span_id == agent.context.span_id


class TestHooksMergedNonDestructively:
    """Our hooks must be *added* to the app's, never replace them."""

    def test_existing_hooks_preserved(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        async def app_hook(input_data, tool_use_id, context):  # noqa: ANN001, ARG001
            return {}

        options = ClaudeAgentOptions(
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[app_hook])]}
        )
        tool_spans = agent_sdk._ToolSpans(None)
        result = agent_sdk._install_tracing_hooks(options, tool_spans)

        # The app's PreToolUse matcher survives; ours is appended.
        pre = result.hooks["PreToolUse"]
        all_callbacks = [cb for matcher in pre for cb in matcher.hooks]
        assert app_hook in all_callbacks
        assert tool_spans.pre_tool_use in all_callbacks
        # And our other two events were added.
        assert "PostToolUse" in result.hooks
        assert "PostToolUseFailure" in result.hooks


# ---------------------------------------------------------------------------
# Fail-silent + absent-extra
# ---------------------------------------------------------------------------


class TestFailSilent:
    def test_absent_extra_is_a_silent_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With `claude-agent-sdk` unimportable, enable is a no-op (no raise)."""
        import builtins

        from opentelemetry.sdk.trace import TracerProvider

        real_import = builtins.__import__

        def no_agent_sdk(name: str, *args: object, **kwargs: object):
            if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
                raise ImportError("simulated missing extra")
            return real_import(name, *args, **kwargs)

        agent_sdk._disable_agent_sdk_instrumentation()
        monkeypatch.setattr(builtins, "__import__", no_agent_sdk)
        # Must not raise, and must not mark itself patched.
        agent_sdk.enable_agent_sdk_instrumentation(TracerProvider())
        assert agent_sdk._patched is False

    def test_broken_hook_does_not_break_the_stream(
        self, traced_query: InMemorySpanExporter
    ) -> None:
        """A hook that raises internally must not propagate — the run still yields.

        Force the failure by making the span factory raise inside the hook; the
        callback swallows it and returns `{}`, so the CLI dispatch never sees an
        error and message delivery is unaffected.
        """
        tool_spans = agent_sdk._ToolSpans(None)

        def boom(*_a: object, **_k: object):
            raise RuntimeError("span start blew up")

        # `_start` calls the tracer; make the tracer explode.
        import indratrace.agent_sdk as mod

        original = mod._get_tracer
        mod._get_tracer = boom  # type: ignore[assignment]
        try:
            out = asyncio.run(
                tool_spans.pre_tool_use(
                    {"tool_name": "Read", "tool_use_id": "t1"}, "t1", {}
                )
            )
        finally:
            mod._get_tracer = original
        assert out == {}, "a failing hook must still return a valid empty output"

    def test_init_still_wires_other_signals_when_agent_sdk_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def no_agent_sdk(name: str, *args: object, **kwargs: object):
            if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
                raise ImportError("simulated missing extra")
            return real_import(name, *args, **kwargs)

        _reset_for_tests()
        monkeypatch.setattr(builtins, "__import__", no_agent_sdk)
        try:
            init_observability(product="no-agent-sdk", instrument_fastapi=False)
            assert _get_provider() is not None, "traces must still be wired"
        finally:
            monkeypatch.undo()
            _reset_for_tests()
