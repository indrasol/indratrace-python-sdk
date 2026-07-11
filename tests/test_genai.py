"""GenAI capture: mocked provider calls (no network), attribute correctness,
nesting under agent spans, graceful absence of extras, and the manual fallback.

The provider clients are patched at their low-level `post`/`_post` so the
instrumentors' wrappers still run — patching `create()` directly would replace
the very method the instrumentor wrapped, and no span would be emitted. Nothing
here touches the network.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from indratrace import (
    init_observability,
    record_llm_usage,
    trace_agent,
    trace_tool,
)
from indratrace.genai import (
    GEN_AI_INPUT_MESSAGES,
    GEN_AI_OUTPUT_MESSAGES,
    GEN_AI_PROVIDER_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    enable_genai_instrumentation,
)
from indratrace.init import _get_provider, _reset_for_tests

# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """An initialized SDK with GenAI instrumentors on, spans teed into memory.

    Reads the SDK's own provider — OTel freezes the global at the first
    `set_tracer_provider` in the process (architecture.md).
    """
    _reset_for_tests()
    init_observability(product="genai-tests", instrument_fastapi=False)
    provider = _get_provider()
    assert provider is not None
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    _reset_for_tests()


def _fake_anthropic_message(input_tokens: int = 11, output_tokens: int = 7):
    from anthropic.types import Message, TextBlock, Usage

    return Message(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-3-5-haiku-20241022",
        content=[TextBlock(type="text", text="ok")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _fake_openai_completion(prompt_tokens: int = 9, completion_tokens: int = 5):
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.completion_usage import CompletionUsage

    return ChatCompletion(
        id="chatcmpl-test",
        object="chat.completion",
        created=0,
        model="gpt-4o-mini",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content="ok"),
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def _mocked_anthropic_call(input_tokens: int = 11, output_tokens: int = 7) -> None:
    import anthropic

    client = anthropic.Anthropic(api_key="test-key")
    fake = _fake_anthropic_message(input_tokens, output_tokens)
    with patch.object(client, "post", return_value=fake):
        client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )


def _mocked_openai_call() -> None:
    import openai

    client = openai.OpenAI(api_key="test-key")
    fake = _fake_openai_completion()
    with patch.object(client.chat.completions, "_post", return_value=fake):
        client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
        )


def _spans_with_content(capture_content: bool) -> InMemorySpanExporter:
    """Init the SDK with a given `capture_content` and tee spans into memory.

    Not the `spans` fixture (which fixes content off): the content tests need to
    control the flag, and `_reset_for_tests` clears `TRACELOOP_TRACE_CONTENT`
    between them so one run's setting can't leak into the next.
    """
    _reset_for_tests()
    init_observability(
        product="genai-content-tests",
        instrument_fastapi=False,
        capture_content=capture_content,
    )
    provider = _get_provider()
    assert provider is not None
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _model_span(exporter: InMemorySpanExporter, name_contains: str) -> ReadableSpan:
    matches = [
        s for s in exporter.get_finished_spans() if name_contains in s.name.lower()
    ]
    assert len(matches) == 1, (
        f"expected one span containing {name_contains!r}, "
        f"got {[s.name for s in exporter.get_finished_spans()]}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Step 3 — attribute-drift investigation (empirical, not assumed)
# ---------------------------------------------------------------------------


class TestAttributeNames:
    """Dump what the instrumentors actually put on the wire and pin it.

    This is the drift guard: if a future instrumentor version renames a token
    field, this test fails and conventions.md must be re-reconciled.
    """

    def test_mocked_anthropic_span_attribute_names(
        self, spans: InMemorySpanExporter
    ) -> None:
        _mocked_anthropic_call(input_tokens=11, output_tokens=7)

        span = _model_span(spans, "anthropic")
        attrs = dict(span.attributes)

        # Token counts: present, integer-valued, and named per conventions.md.
        assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == 11
        assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 7
        assert isinstance(attrs[GEN_AI_USAGE_INPUT_TOKENS], int)
        assert isinstance(attrs[GEN_AI_USAGE_OUTPUT_TOKENS], int)

        assert attrs[GEN_AI_REQUEST_MODEL] == "claude-3-5-haiku-20241022"

        # Drift on record: the system identity is emitted under
        # `gen_ai.provider.name`, NOT conventions.md's `gen_ai.system`. If this
        # ever flips back, update conventions.md's mapping table.
        assert attrs[GEN_AI_PROVIDER_NAME] == "anthropic"
        assert "gen_ai.system" not in attrs, (
            "instrumentor emitted gen_ai.system — the drift note in "
            "conventions.md is now stale; reconcile it"
        )

    def test_mocked_openai_span_attribute_names(
        self, spans: InMemorySpanExporter
    ) -> None:
        _mocked_openai_call()

        span = _model_span(spans, "openai")
        attrs = dict(span.attributes)

        assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == 9
        assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 5
        assert isinstance(attrs[GEN_AI_USAGE_INPUT_TOKENS], int)
        assert attrs[GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
        assert attrs[GEN_AI_PROVIDER_NAME] == "openai"


# ---------------------------------------------------------------------------
# Nesting — the headline acceptance criterion
# ---------------------------------------------------------------------------


class TestNesting:
    def test_model_span_nests_under_agent_and_tool(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def call_model() -> None:
            _mocked_anthropic_call()

        @trace_agent("token-counter")
        def run() -> None:
            call_model()

        run()

        by_name = {s.name: s for s in spans.get_finished_spans()}
        agent = by_name["agent token-counter"]
        tool = by_name["tool call_model"]
        model = _model_span(spans, "anthropic")

        # One trace, agent -> tool -> model.
        trace_ids = {s.context.trace_id for s in spans.get_finished_spans()}
        assert len(trace_ids) == 1, "model span split from its agent's trace"
        assert agent.parent is None, "the agent span is the trace root"
        assert tool.parent.span_id == agent.context.span_id
        assert model.parent is not None
        assert model.parent.span_id == tool.context.span_id

        # And the counts rode along.
        assert model.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 11
        assert model.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 7


# ---------------------------------------------------------------------------
# Graceful absence of the extras
# ---------------------------------------------------------------------------


class TestWithoutExtras:
    """A core install (no anthropic/openai extra) must init cleanly."""

    def test_enable_is_silent_when_no_instrumentor_importable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate the extras being absent via a monkeypatched import failure."""
        from opentelemetry.sdk.trace import TracerProvider

        real_import = builtins.__import__

        blocked = (
            "opentelemetry.instrumentation.anthropic",
            "opentelemetry.instrumentation.openai",
            "opentelemetry.instrumentation.google_generativeai",
            "opentelemetry.instrumentation.bedrock",
        )

        def no_instrumentors(name: str, *args: object, **kwargs: object):
            if name.startswith(blocked):
                raise ImportError(f"simulated missing extra: {name}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_instrumentors)

        # Must not raise, and must emit no model instrumentation.
        enable_genai_instrumentation(TracerProvider())

    def test_init_succeeds_with_extras_import_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`init_observability` still wires the other signals when extras fail
        to import — a missing GenAI extra is not a fatal error."""
        real_import = builtins.__import__

        def no_instrumentors(name: str, *args: object, **kwargs: object):
            if any(
                p in name
                for p in (
                    "instrumentation.anthropic",
                    "instrumentation.openai",
                    "instrumentation.google_generativeai",
                    "instrumentation.bedrock",
                )
            ):
                raise ImportError(f"simulated missing extra: {name}")
            return real_import(name, *args, **kwargs)

        _reset_for_tests()
        monkeypatch.setattr(builtins, "__import__", no_instrumentors)
        try:
            init_observability(product="no-genai-extra", instrument_fastapi=False)
            assert _get_provider() is not None, "traces must still be wired"
        finally:
            monkeypatch.undo()
            _reset_for_tests()


# ---------------------------------------------------------------------------
# Manual fallback
# ---------------------------------------------------------------------------


class TestRecordLLMUsage:
    def test_stamps_attributes_on_the_current_span(
        self, spans: InMemorySpanExporter
    ) -> None:
        @trace_tool
        def call_unsupported_provider() -> None:
            record_llm_usage(
                model="some-model-v2",
                input_tokens=123,
                output_tokens=45,
                system="acme-ai",
            )

        call_unsupported_provider()

        span = next(
            s
            for s in spans.get_finished_spans()
            if s.name == "tool call_unsupported_provider"
        )
        assert span.attributes[GEN_AI_PROVIDER_NAME] == "acme-ai"
        assert span.attributes[GEN_AI_REQUEST_MODEL] == "some-model-v2"
        assert span.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 123
        assert span.attributes[GEN_AI_USAGE_OUTPUT_TOKENS] == 45

    def test_default_system_is_other(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        def call() -> None:
            record_llm_usage(model="m", input_tokens=1, output_tokens=2)

        call()

        span = next(s for s in spans.get_finished_spans() if s.name == "tool call")
        assert span.attributes[GEN_AI_PROVIDER_NAME] == "other"

    def test_extra_attributes_are_stamped(self, spans: InMemorySpanExporter) -> None:
        @trace_tool
        def call() -> None:
            record_llm_usage(
                model="m",
                input_tokens=1,
                output_tokens=2,
                **{"gen_ai.usage.cache_read.input_tokens": 8},
            )

        call()

        span = next(s for s in spans.get_finished_spans() if s.name == "tool call")
        assert span.attributes["gen_ai.usage.cache_read.input_tokens"] == 8

    def test_no_recording_span_is_a_silent_no_op(self) -> None:
        """Outside any span, and with no init, it must not raise."""
        _reset_for_tests()
        try:
            record_llm_usage(model="m", input_tokens=1, output_tokens=2)
        finally:
            _reset_for_tests()


# ---------------------------------------------------------------------------
# Content capture — opt-in prompt/completion text (default OFF)
# ---------------------------------------------------------------------------


class TestContentCapture:
    """`capture_content` gates prompt/completion TEXT on model spans.

    Off by default because prompts carry customer data (conventions.md). The
    attribute names are pinned here empirically: `gen_ai.input.messages` /
    `gen_ai.output.messages` at the pinned instrumentor versions. Token counts
    are captured either way — the flag only gates the raw text.
    """

    def test_content_absent_by_default(self) -> None:
        exporter = _spans_with_content(capture_content=False)
        try:
            _mocked_anthropic_call(input_tokens=11, output_tokens=7)
            span = _model_span(exporter, "anthropic")
            attrs = dict(span.attributes)

            # No prompt/completion text.
            assert GEN_AI_INPUT_MESSAGES not in attrs
            assert GEN_AI_OUTPUT_MESSAGES not in attrs
            # Tokens still captured — the flag never touches usage.
            assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == 11
            assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 7
        finally:
            _reset_for_tests()

    def test_content_present_when_enabled(self) -> None:
        exporter = _spans_with_content(capture_content=True)
        try:
            _mocked_anthropic_call(input_tokens=11, output_tokens=7)
            span = _model_span(exporter, "anthropic")
            attrs = dict(span.attributes)

            # The prompt we sent and the completion text land on the span, under
            # the names conventions.md § Content capture documents.
            assert GEN_AI_INPUT_MESSAGES in attrs
            assert GEN_AI_OUTPUT_MESSAGES in attrs
            assert "hi" in attrs[GEN_AI_INPUT_MESSAGES]
            assert "ok" in attrs[GEN_AI_OUTPUT_MESSAGES]
            # Tokens unaffected.
            assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == 11
        finally:
            _reset_for_tests()

    def test_env_var_enables_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`INDRATRACE_CAPTURE_CONTENT` turns it on when the arg is unset."""
        monkeypatch.setenv("INDRATRACE_CAPTURE_CONTENT", "true")
        # capture_content=None → resolve from the env var.
        exporter = _spans_with_content(capture_content=None)  # type: ignore[arg-type]
        try:
            _mocked_anthropic_call()
            attrs = dict(_model_span(exporter, "anthropic").attributes)
            assert GEN_AI_INPUT_MESSAGES in attrs
        finally:
            _reset_for_tests()

    def test_explicit_arg_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Precedence: explicit `capture_content=False` beats a truthy env var."""
        monkeypatch.setenv("INDRATRACE_CAPTURE_CONTENT", "true")
        exporter = _spans_with_content(capture_content=False)
        try:
            _mocked_anthropic_call()
            attrs = dict(_model_span(exporter, "anthropic").attributes)
            assert GEN_AI_INPUT_MESSAGES not in attrs
        finally:
            _reset_for_tests()


# ---------------------------------------------------------------------------
# Gemini + Bedrock — extra wiring and graceful absence
# ---------------------------------------------------------------------------


class TestGeminiBedrockWiring:
    """The two new instrumentors: instrument/uninstrument cleanly when present.

    A mocked-transport token test for these providers would need a live client
    object per provider; per prompt 07 we cover the instrument/uninstrument
    wiring instead. The absent-extra silent-skip path is exercised by
    `TestWithoutExtras` (its blockers include both new modules) and by every
    core-install CI run, where the extras simply aren't there.
    """

    @staticmethod
    def _instrumentor(extra: str):
        """The instrumentor class for one row of `_INSTRUMENTORS`, or skip.

        Skips when the extra (or its provider SDK, which the instrumentor imports
        at module load) isn't installed — a core install is a valid environment.
        """
        from indratrace.genai import _INSTRUMENTORS

        module_path, class_name = next(
            (m, c) for e, m, c in _INSTRUMENTORS if e == extra
        )
        module = pytest.importorskip(module_path)
        return getattr(module, class_name)

    @pytest.mark.parametrize("extra", ["gemini", "bedrock"])
    def test_enable_then_uninstrument_is_clean(self, extra: str) -> None:
        """`init` instruments it against our provider; `_reset` unpatches it."""
        instrumentor_cls = self._instrumentor(extra)

        _reset_for_tests()
        init_observability(product=f"{extra}-wiring", instrument_fastapi=False)
        try:
            assert instrumentor_cls().is_instrumented_by_opentelemetry, (
                f"{extra} extra installed but init did not instrument it"
            )
        finally:
            _reset_for_tests()

        # `_reset_for_tests` calls `_uninstrument_genai`, which iterates every
        # row — so the provider client is unpatched afterwards.
        assert not instrumentor_cls().is_instrumented_by_opentelemetry, (
            f"{extra} instrumentor still patched after reset"
        )
