"""Session/user context + feedback API.

Covers docs/conventions.md § "Session / user context" and § "Feedback spans":

- `session(...)` stamps `session.id`/`user.id` on decorator spans AND on
  spans the SDK did not create (a raw provider span, and a mocked GenAI model
  span) — proving the baggage+processor mechanism, not per-decorator plumbing.
- nesting overrides per key; async/await propagation.
- the imperative middleware handle (`detach`/`close`).
- `record_feedback` span shape with an explicit trace_id, an ambient trace, and
  no trace; and that feedback inside a session carries the session ids.
- everything is a no-op without init.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from indratrace import (
    current_trace_id,
    init_observability,
    record_feedback,
    session,
    trace_agent,
    trace_tool,
)
from indratrace.context import (
    FEEDBACK_COMMENT_ATTRIBUTE,
    FEEDBACK_SCORE_ATTRIBUTE,
    FEEDBACK_SPAN_KIND,
    FEEDBACK_TRACE_ID_ATTRIBUTE,
    SESSION_ID_KEY,
    SPAN_KIND_ATTRIBUTE,
    USER_ID_KEY,
)
from indratrace.init import _get_provider, _reset_for_tests


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """An initialized SDK whose spans are teed into memory.

    Reads the SDK's own provider — OTel freezes the global at the first
    `set_tracer_provider` in the process (architecture.md).
    """
    _reset_for_tests()
    init_observability(product="context-tests", instrument_fastapi=False)
    provider = _get_provider()
    assert provider is not None
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    _reset_for_tests()


def by_name(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, (
        f"expected one {name!r}, got {[s.name for s in exporter.get_finished_spans()]}"
    )
    return matches[0]


def emit_raw_span(name: str = "raw-span") -> None:
    """A span the SDK's decorators did NOT create — stands in for an
    auto-instrumented span (FastAPI/GenAI): all go through the same provider,
    so the processor stamps them identically."""
    provider = _get_provider()
    assert provider is not None
    with provider.get_tracer("indratrace.tests").start_as_current_span(name):
        pass


# ---------------------------------------------------------------------------
# session(...) stamps the ids on every span
# ---------------------------------------------------------------------------


class TestSessionAttributesOnSpans:
    def test_decorator_spans_carry_both_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def tool() -> None: ...

        @trace_agent("checker")
        def run() -> None:
            tool()

        with session(session_id="s1", user_id="u1"):
            run()

        for name in ("agent checker", "tool tool"):
            span = by_name(spans, name)
            assert span.attributes[SESSION_ID_KEY] == "s1"
            assert span.attributes[USER_ID_KEY] == "u1"

    def test_non_decorator_span_also_carries_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        """The mechanism is a processor, not decorator code — so a raw provider
        span (what FastAPI/GenAI auto-instrumentation produces) is tagged too."""
        with session(session_id="s2", user_id="u2"):
            emit_raw_span("http get /x")

        span = by_name(spans, "http get /x")
        assert span.attributes[SESSION_ID_KEY] == "s2"
        assert span.attributes[USER_ID_KEY] == "u2"

    def test_mocked_genai_model_span_carries_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        """A GenAI instrumentor's model span — created entirely outside our
        code — is stamped, because it runs through the same provider."""
        from opentelemetry.sdk.trace import TracerProvider

        from indratrace.genai import enable_genai_instrumentation
        from tests.test_genai import _mocked_anthropic_call

        provider = _get_provider()
        assert isinstance(provider, TracerProvider)
        enable_genai_instrumentation(provider)

        @trace_agent("with-model")
        def run() -> None:
            _mocked_anthropic_call()

        with session(session_id="s3", user_id="u3"):
            run()

        model = next(
            s for s in spans.get_finished_spans() if "anthropic" in s.name.lower()
        )
        assert model.attributes[SESSION_ID_KEY] == "s3"
        assert model.attributes[USER_ID_KEY] == "u3"

    def test_only_session_id(self, spans: InMemorySpanExporter) -> None:
        with session(session_id="only-session"):
            emit_raw_span("s")
        span = by_name(spans, "s")
        assert span.attributes[SESSION_ID_KEY] == "only-session"
        assert USER_ID_KEY not in span.attributes

    def test_only_user_id(self, spans: InMemorySpanExporter) -> None:
        with session(user_id="only-user"):
            emit_raw_span("u")
        span = by_name(spans, "u")
        assert span.attributes[USER_ID_KEY] == "only-user"
        assert SESSION_ID_KEY not in span.attributes

    def test_span_outside_session_has_no_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        with session(session_id="s", user_id="u"):
            emit_raw_span("inside")
        emit_raw_span("outside")

        outside = by_name(spans, "outside")
        assert SESSION_ID_KEY not in outside.attributes
        assert USER_ID_KEY not in outside.attributes


class TestNesting:
    def test_inner_overrides_only_its_key(
        self, spans: InMemorySpanExporter
    ) -> None:
        with session(session_id="s-outer", user_id="u-outer"):
            emit_raw_span("outer")
            with session(user_id="u-inner"):
                emit_raw_span("inner")
            emit_raw_span("outer-again")

        inner = by_name(spans, "inner")
        assert inner.attributes[SESSION_ID_KEY] == "s-outer", "session.id must survive"
        assert inner.attributes[USER_ID_KEY] == "u-inner"

        # The outer context is restored after the inner scope exits.
        again = by_name(spans, "outer-again")
        assert again.attributes[USER_ID_KEY] == "u-outer"


class TestAsyncPropagation:
    def test_ids_cross_await_boundaries(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        async def deep() -> None:
            await asyncio.sleep(0)

        @trace_agent("async-agent")
        async def run() -> None:
            await asyncio.sleep(0)
            await deep()

        async def main() -> None:
            with session(session_id="s-async", user_id="u-async"):
                await run()

        asyncio.run(main())

        for name in ("agent async-agent", "tool deep"):
            span = by_name(spans, name)
            assert span.attributes[SESSION_ID_KEY] == "s-async"
            assert span.attributes[USER_ID_KEY] == "u-async"

    def test_concurrent_sessions_do_not_bleed(
        self, spans: InMemorySpanExporter
    ) -> None:
        """Two tasks each in their own session must not see each other's ids —
        contextvars isolate them."""

        async def worker(sid: str) -> None:
            with session(session_id=sid):
                await asyncio.sleep(0)
                emit_raw_span(f"span-{sid}")

        async def main() -> None:
            await asyncio.gather(worker("A"), worker("B"))

        asyncio.run(main())

        assert by_name(spans, "span-A").attributes[SESSION_ID_KEY] == "A"
        assert by_name(spans, "span-B").attributes[SESSION_ID_KEY] == "B"


class TestImperativeHandle:
    def test_detach_restores_prior_context(
        self, spans: InMemorySpanExporter
    ) -> None:
        handle = session(session_id="mw", user_id="mw-user")
        try:
            emit_raw_span("during")
        finally:
            handle.detach()
        emit_raw_span("after")

        during = by_name(spans, "during")
        assert during.attributes[SESSION_ID_KEY] == "mw"

        after = by_name(spans, "after")
        assert SESSION_ID_KEY not in after.attributes, "detach did not restore context"

    def test_close_is_an_alias_for_detach(
        self, spans: InMemorySpanExporter
    ) -> None:
        handle = session(session_id="mw2")
        emit_raw_span("during2")
        handle.close()
        emit_raw_span("after2")

        assert by_name(spans, "during2").attributes[SESSION_ID_KEY] == "mw2"
        assert SESSION_ID_KEY not in by_name(spans, "after2").attributes

    def test_double_detach_is_safe(self, spans: InMemorySpanExporter) -> None:
        handle = session(session_id="mw3")
        handle.detach()
        handle.detach()  # must not raise


# ---------------------------------------------------------------------------
# current_trace_id()
# ---------------------------------------------------------------------------


class TestCurrentTraceId:
    def test_returns_the_active_trace_id(self, spans: InMemorySpanExporter) -> None:
        from opentelemetry import trace

        seen: dict[str, str] = {}

        @trace_agent("has-trace")
        def run() -> None:
            ctx = trace.get_current_span().get_span_context()
            seen["expected"] = format(ctx.trace_id, "032x")
            seen["reported"] = current_trace_id() or ""

        run()

        assert seen["reported"] == seen["expected"]
        assert len(seen["reported"]) == 32

    def test_returns_none_outside_a_span(self, spans: InMemorySpanExporter) -> None:
        assert current_trace_id() is None


# ---------------------------------------------------------------------------
# record_feedback()
# ---------------------------------------------------------------------------


class TestFeedbackSpanShape:
    def _feedback(self, exporter: InMemorySpanExporter) -> ReadableSpan:
        return by_name(exporter, "feedback")

    def test_explicit_trace_id(self, spans: InMemorySpanExporter) -> None:
        record_feedback(1, comment="great", trace_id="a" * 32)

        span = self._feedback(spans)
        assert span.attributes[SPAN_KIND_ATTRIBUTE] == FEEDBACK_SPAN_KIND
        assert span.attributes[FEEDBACK_SCORE_ATTRIBUTE] == 1
        assert span.attributes[FEEDBACK_COMMENT_ATTRIBUTE] == "great"
        assert span.attributes[FEEDBACK_TRACE_ID_ATTRIBUTE] == "a" * 32

    def test_ambient_trace_id_used_when_arg_omitted(
        self, spans: InMemorySpanExporter
    ) -> None:
        seen: dict[str, str] = {}

        @trace_agent("answering")
        def run() -> None:
            seen["tid"] = current_trace_id() or ""
            record_feedback(1)  # no explicit trace_id → pick up the ambient one

        run()

        span = self._feedback(spans)
        assert span.attributes[FEEDBACK_TRACE_ID_ATTRIBUTE] == seen["tid"]

    def test_explicit_trace_id_wins_over_ambient(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_agent("answering")
        def run() -> None:
            record_feedback(0, trace_id="b" * 32)

        run()

        assert self._feedback(spans).attributes[FEEDBACK_TRACE_ID_ATTRIBUTE] == "b" * 32

    def test_no_trace_id_and_no_ambient_trace(
        self, spans: InMemorySpanExporter
    ) -> None:
        """The span is still emitted, just without linkage — never drop a score."""
        record_feedback(-1, comment="bad")

        span = self._feedback(spans)
        assert span.attributes[FEEDBACK_SCORE_ATTRIBUTE] == -1
        assert FEEDBACK_TRACE_ID_ATTRIBUTE not in span.attributes

    def test_comment_optional(self, spans: InMemorySpanExporter) -> None:
        record_feedback(1, trace_id="c" * 32)
        assert FEEDBACK_COMMENT_ATTRIBUTE not in self._feedback(spans).attributes

    def test_float_score_preserved(self, spans: InMemorySpanExporter) -> None:
        record_feedback(4.5, trace_id="d" * 32)
        assert self._feedback(spans).attributes[FEEDBACK_SCORE_ATTRIBUTE] == 4.5

    def test_feedback_inside_session_carries_ids(
        self, spans: InMemorySpanExporter
    ) -> None:
        with session(session_id="s-fb", user_id="u-fb"):
            record_feedback(1, trace_id="e" * 32)

        span = self._feedback(spans)
        assert span.attributes[SESSION_ID_KEY] == "s-fb"
        assert span.attributes[USER_ID_KEY] == "u-fb"


# ---------------------------------------------------------------------------
# No-op without init (ADR 0003)
# ---------------------------------------------------------------------------


class TestWorksWithoutInit:
    @pytest.fixture(autouse=True)
    def uninitialized(self) -> Iterator[None]:
        _reset_for_tests()
        yield
        _reset_for_tests()

    def test_session_context_manager_is_inert(self) -> None:
        assert _get_provider() is None
        with session(session_id="s", user_id="u"):
            pass  # must not raise

    def test_session_handle_is_inert(self) -> None:
        handle = session(session_id="s")
        handle.detach()  # must not raise

    def test_current_trace_id_is_none(self) -> None:
        assert current_trace_id() is None

    def test_record_feedback_is_a_silent_no_op(self) -> None:
        record_feedback(1, comment="x", trace_id="f" * 32)  # must not raise

    def test_broken_tracer_does_not_break_record_feedback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> None:
            raise RuntimeError("tracer machinery exploded")

        monkeypatch.setattr("indratrace.context._get_tracer", boom)
        record_feedback(1, trace_id="0" * 32)  # swallowed at debug, never raised


class TestSessionSurvivesBrokenBaggage:
    """A processor failure must not break the span it's stamping (ADR 0003)."""

    def test_on_start_failure_is_swallowed(
        self, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("baggage exploded")

        monkeypatch.setattr("indratrace.context.baggage.get_baggage", boom)

        with session(session_id="s"):
            emit_raw_span("still-emitted")

        # The span still lands; it just carries no ids.
        span = by_name(spans, "still-emitted")
        assert SESSION_ID_KEY not in span.attributes
