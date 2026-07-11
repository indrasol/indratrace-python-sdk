"""@trace_agent / @trace_tool: span shape, nesting, errors, transparency."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from indratrace import init_observability, trace_agent, trace_step, trace_tool
from indratrace.agent import (
    AGENT_SPAN_KIND,
    SPAN_KIND_ATTRIBUTE,
    STEP_SPAN_KIND,
    TOOL_SPAN_KIND,
)
from indratrace.init import _get_provider, _reset_for_tests


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """An initialized SDK whose spans are teed into memory.

    Reads the SDK's own provider, not the global one: OTel freezes the global
    at the first `set_tracer_provider` in the process (architecture.md).
    """
    _reset_for_tests()
    init_observability(product="agent-tests", instrument_fastapi=False)

    provider = _get_provider()
    assert provider is not None
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    yield exporter

    _reset_for_tests()


def by_name(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    finished = exporter.get_finished_spans()
    matches = [s for s in finished if s.name == name]
    assert len(matches) == 1, (
        f"expected exactly one {name!r} span, got {[s.name for s in finished]}"
    )
    return matches[0]


class TestSpanShape:
    """docs/conventions.md § Span conventions is the contract under test."""

    def test_agent_span_name_and_attributes(self, spans: InMemorySpanExporter) -> None:
        @trace_agent("compliance-checker")
        def run() -> str:
            return "done"

        assert run() == "done"

        span = by_name(spans, "agent compliance-checker")
        assert span.attributes[SPAN_KIND_ATTRIBUTE] == AGENT_SPAN_KIND
        assert span.attributes["agent.name"] == "compliance-checker"
        assert span.status.status_code is StatusCode.UNSET

    def test_tool_span_name_and_attributes(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        def risk_score(vendor: str) -> int:
            return len(vendor)

        assert risk_score("acme") == 4

        span = by_name(spans, "tool risk_score")
        assert span.attributes[SPAN_KIND_ATTRIBUTE] == TOOL_SPAN_KIND
        assert span.attributes["tool.name"] == "risk_score"

    def test_trace_tool_accepts_call_form(self, spans: InMemorySpanExporter) -> None:
        """`@trace_tool()` must behave exactly like bare `@trace_tool`."""

        @trace_tool()
        def lookup() -> None:
            return None

        lookup()

        span = by_name(spans, "tool lookup")
        assert span.attributes["tool.name"] == "lookup"

    def test_spans_carry_the_resource(self, spans: InMemorySpanExporter) -> None:
        @trace_agent("resourced")
        def run() -> None: ...

        run()

        assert by_name(spans, "agent resourced").resource.attributes["product"] == (
            "agent-tests"
        )


class TestTraceStep:
    """`@trace_step` — the neutral sibling of `@trace_tool` (conventions.md)."""

    def test_step_span_name_and_attributes(self, spans: InMemorySpanExporter) -> None:
        @trace_step
        def parse_document(raw: str) -> int:
            return len(raw)

        assert parse_document("abc") == 3

        span = by_name(spans, "step parse_document")
        assert span.attributes[SPAN_KIND_ATTRIBUTE] == STEP_SPAN_KIND
        assert span.attributes["step.name"] == "parse_document"
        assert span.status.status_code is StatusCode.UNSET
        # It is a step, not a tool: no tool.name leaks onto it.
        assert "tool.name" not in span.attributes

    def test_trace_step_accepts_call_form(self, spans: InMemorySpanExporter) -> None:
        """`@trace_step()` must behave exactly like bare `@trace_step`."""

        @trace_step()
        def query_db() -> None:
            return None

        query_db()

        assert by_name(spans, "step query_db").attributes["step.name"] == "query_db"

    def test_async_step(self, spans: InMemorySpanExporter) -> None:
        @trace_step
        async def load() -> int:
            await asyncio.sleep(0)
            return 7

        assert asyncio.run(load()) == 7
        assert by_name(spans, "step load") is not None

    def test_step_nests_under_agent_and_tool(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_step
        def validate() -> None: ...

        @trace_tool
        def enrich() -> None:
            validate()

        @trace_agent("pipeline")
        def run() -> None:
            enrich()

        run()

        agent = by_name(spans, "agent pipeline")
        tool = by_name(spans, "tool enrich")
        step = by_name(spans, "step validate")

        assert tool.parent.span_id == agent.context.span_id
        assert step.parent is not None
        assert step.parent.span_id == tool.context.span_id
        assert len({s.context.trace_id for s in spans.get_finished_spans()}) == 1

    def test_step_error_sets_status_and_re_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_step
        def failing() -> None:
            raise ValueError("bad row")

        with pytest.raises(ValueError, match="bad row"):
            failing()

        assert by_name(spans, "step failing").status.status_code is StatusCode.ERROR

    def test_step_runs_without_init(self) -> None:
        _reset_for_tests()
        try:
            assert _get_provider() is None

            @trace_step
            def parse(x: int) -> int:
                return x + 1

            assert parse(1) == 2
        finally:
            _reset_for_tests()


class TestAsync:
    def test_async_agent_and_tool(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        async def fetch(x: int) -> int:
            await asyncio.sleep(0)
            return x * 2

        @trace_agent("async-agent")
        async def run() -> int:
            return await fetch(21)

        assert asyncio.run(run()) == 42

        assert by_name(spans, "agent async-agent") is not None
        assert by_name(spans, "tool fetch") is not None

    def test_async_wrapper_is_still_a_coroutine_function(self) -> None:
        """Frameworks introspect this; the wrapper must not turn async into sync."""

        @trace_tool
        async def tool() -> None: ...

        assert asyncio.iscoroutinefunction(tool)


class TestNesting:
    def test_tool_span_is_a_child_of_the_agent_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def inner() -> None: ...

        @trace_tool
        def outer() -> None:
            inner()

        @trace_agent("nesting")
        def run() -> None:
            outer()

        run()

        agent = by_name(spans, "agent nesting")
        outer_span = by_name(spans, "tool outer")
        inner_span = by_name(spans, "tool inner")

        assert agent.parent is None, "the agent span is the trace root"
        assert outer_span.parent is not None
        assert outer_span.parent.span_id == agent.context.span_id
        assert inner_span.parent is not None
        assert inner_span.parent.span_id == outer_span.context.span_id

        # One trace, three spans.
        trace_ids = {s.context.trace_id for s in spans.get_finished_spans()}
        assert len(trace_ids) == 1

    def test_async_nesting(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        async def tool() -> None:
            await asyncio.sleep(0)

        @trace_agent("async-nesting")
        async def run() -> None:
            await tool()

        asyncio.run(run())

        agent = by_name(spans, "agent async-nesting")
        assert by_name(spans, "tool tool").parent.span_id == agent.context.span_id


class TestErrors:
    """The decorators are transparent: they observe, they never intercept."""

    def test_exception_sets_error_status_and_re_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        sentinel = ValueError("vendor not found")

        @trace_tool
        def failing() -> None:
            raise sentinel

        with pytest.raises(ValueError) as caught:
            failing()

        assert caught.value is sentinel, "the app's exception must pass through intact"

        span = by_name(spans, "tool failing")
        assert span.status.status_code is StatusCode.ERROR
        assert "vendor not found" in (span.status.description or "")

        (event,) = span.events
        assert event.name == "exception"
        assert event.attributes["exception.type"] == "ValueError"

    def test_exception_recorded_once(self, spans: InMemorySpanExporter) -> None:
        """The span context manager must not also record what we recorded."""

        @trace_tool
        def failing() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            failing()

        assert len(by_name(spans, "tool failing").events) == 1

    def test_async_exception_sets_error_status_and_re_raises(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        async def failing() -> None:
            await asyncio.sleep(0)
            raise KeyError("missing")

        with pytest.raises(KeyError):
            asyncio.run(failing())

        assert by_name(spans, "tool failing").status.status_code is StatusCode.ERROR

    def test_agent_span_records_a_tool_failure_that_escapes(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def failing() -> None:
            raise RuntimeError("boom")

        @trace_agent("propagates")
        def run() -> None:
            failing()

        with pytest.raises(RuntimeError):
            run()

        assert by_name(spans, "tool failing").status.status_code is StatusCode.ERROR
        assert by_name(spans, "agent propagates").status.status_code is StatusCode.ERROR

    def test_agent_span_unaffected_when_the_tool_error_is_handled(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def failing() -> None:
            raise RuntimeError("boom")

        @trace_agent("recovers")
        def run() -> str:
            try:
                failing()
            except RuntimeError:
                return "recovered"
            return "unreachable"

        assert run() == "recovered"

        assert by_name(spans, "tool failing").status.status_code is StatusCode.ERROR
        assert by_name(spans, "agent recovers").status.status_code is StatusCode.UNSET


class TestWorksWithoutInit:
    """A decorated app must run when init never happened, or failed."""

    @pytest.fixture(autouse=True)
    def uninitialized(self) -> Iterator[None]:
        _reset_for_tests()
        yield
        _reset_for_tests()

    def test_sync_function_still_runs(self) -> None:
        assert _get_provider() is None

        @trace_agent("no-init")
        def run(x: int) -> int:
            return x + 1

        assert run(1) == 2

    def test_async_function_still_runs(self) -> None:
        @trace_tool
        async def tool() -> str:
            return "ok"

        assert asyncio.run(tool()) == "ok"

    def test_exceptions_still_propagate(self) -> None:
        @trace_tool
        def failing() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing()

    def test_spans_are_non_recording(self) -> None:
        """No provider means no spans — but the call still succeeds."""
        from opentelemetry import trace as otel_trace

        captured: list[otel_trace.Span] = []

        @trace_agent("no-init")
        def run() -> None:
            captured.append(otel_trace.get_current_span())

        run()

        (span,) = captured
        assert not span.is_recording()

    def test_broken_tracer_does_not_break_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule 2: instrumentation failures never reach the host app."""

        def boom() -> None:
            raise RuntimeError("tracer machinery exploded")

        monkeypatch.setattr("indratrace.agent._get_tracer", boom)

        @trace_agent("broken")
        def run() -> str:
            return "still ran"

        assert run() == "still ran"


class TestWrapsMetadata:
    def test_sync_metadata_preserved(self) -> None:
        @trace_tool
        def risk_score(vendor: str) -> int:
            """Score a vendor."""
            return 0

        assert risk_score.__name__ == "risk_score"
        assert risk_score.__doc__ == "Score a vendor."
        assert risk_score.__wrapped__ is not None

    def test_async_metadata_preserved(self) -> None:
        @trace_agent("named")
        async def run() -> None:
            """Run the agent."""

        assert run.__name__ == "run"
        assert run.__doc__ == "Run the agent."

    def test_tool_name_comes_from_the_function_not_the_wrapper(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A stale `functools.wraps` order would name the span `tool sync_wrapper`."""

        @trace_tool
        def specific_name() -> None: ...

        specific_name()

        assert by_name(spans, "tool specific_name")
