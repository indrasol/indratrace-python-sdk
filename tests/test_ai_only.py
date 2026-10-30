"""AI-only by default (1.2, ADR 0010): what is sent, what is not, and how to opt
back in.

The rest of the suite pins 1.1 behaviour by passing `ai_only=False` wherever it
tests HTTP spans, the log bridge or metrics, so that whole suite doubles as the
parity check. This file covers the new defaults, the explicit opt-ins, the one
warning, and the contract that an LLM span with no web request around it is
still a span the platform reads.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from indratrace import init_observability, record_feedback, trace_agent, trace_tool
from indratrace.agent import SPAN_KIND_ATTRIBUTE
from indratrace.genai import (
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
)
from indratrace.init import (
    HTTP_IGNORED_WARNING,
    _get_log_handler,
    _get_logger_provider,
    _get_meter_provider,
    _get_provider,
    _reset_for_tests,
)

from .conftest import sdk_warnings
from .test_genai import _mocked_anthropic_call

CONVENTIONS = Path(__file__).resolve().parents[1] / "docs" / "conventions.md"


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def capture_spans() -> InMemorySpanExporter:
    provider = _get_provider()
    assert provider is not None, "init_observability() did not build a provider"
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _fastapi_is_instrumented() -> bool:
    module = pytest.importorskip("opentelemetry.instrumentation.fastapi")
    return module.FastAPIInstrumentor().is_instrumented_by_opentelemetry


def _console(records: list[logging.LogRecord]) -> str:
    return "\n".join(record.getMessage() for record in records)


class TestDefaults:
    def test_traces_only(self) -> None:
        init_observability(api_key="it_test_ai_only")

        assert _get_provider() is not None
        assert _get_logger_provider() is None, "no log bridge unless logs=True"
        assert _get_meter_provider() is None, "no app metrics under ai_only"
        assert _get_log_handler() is None

    def test_http_is_not_instrumented(self) -> None:
        init_observability(api_key="it_test_ai_only")
        assert not _fastapi_is_instrumented()

    def test_root_logger_gets_no_export_handler(self) -> None:
        before = list(logging.getLogger().handlers)
        init_observability(api_key="it_test_ai_only")
        assert logging.getLogger().handlers == before

    def test_loguru_is_not_bridged(self) -> None:
        pytest.importorskip("loguru")
        from indratrace import logs

        init_observability(api_key="it_test_ai_only")
        assert logs._sink_id is None

    def test_agent_tool_and_feedback_spans_still_ship(self) -> None:
        init_observability(api_key="it_test_ai_only")
        exporter = capture_spans()

        @trace_tool
        def lookup(q: str) -> str:
            return q

        @trace_agent("support")
        def run(q: str) -> str:
            return lookup(q)

        run("hi")
        record_feedback(1)

        kinds = sorted(
            str(s.attributes.get(SPAN_KIND_ATTRIBUTE))
            for s in exporter.get_finished_spans()
        )
        assert kinds == ["agent", "feedback", "tool"]

    def test_model_spans_still_ship(self) -> None:
        pytest.importorskip("anthropic")
        init_observability(api_key="it_test_ai_only")
        exporter = capture_spans()

        _mocked_anthropic_call(input_tokens=11, output_tokens=7)

        (span,) = exporter.get_finished_spans()
        assert span.attributes[GEN_AI_USAGE_INPUT_TOKENS] == 11

    def test_banner_names_traces_only(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_ai_only", debug=True)
        text = _console(sdk_log)

        assert "signals: traces (OTLP/HTTP, batched)" in text
        assert "loguru: skipped (logs off; pass logs=True)" in text
        assert "http[" not in text


class TestOptIns:
    def test_logs_true_bridges_logs_and_still_no_metrics(
        self, app_logs_at_info: None
    ) -> None:
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )

        init_observability(api_key="it_test_ai_only", logs=True)
        provider = _get_logger_provider()
        assert provider is not None
        assert _get_meter_provider() is None

        exporter = InMemoryLogRecordExporter()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        logging.getLogger("product").info("shipped on request")
        provider.force_flush()

        bodies = [r.log_record.body for r in exporter.get_finished_logs()]
        assert bodies == ["shipped on request"]

    def test_explicit_instrument_http_still_instruments(self) -> None:
        init_observability(api_key="it_test_ai_only", instrument_http=True)
        assert _fastapi_is_instrumented()

    def test_logs_false_overrides_ai_only_false(self) -> None:
        init_observability(api_key="it_test_ai_only", ai_only=False, logs=False)
        assert _get_logger_provider() is None
        assert _get_meter_provider() is not None


class TestAiOnlyFalseIsOnePointOne:
    """The 1.1 call — no arguments past the key — plus `ai_only=False`."""

    def test_all_three_signals_and_http(self) -> None:
        init_observability(api_key="it_test_compat", ai_only=False)

        assert _get_provider() is not None
        assert _get_logger_provider() is not None
        assert _get_meter_provider() is not None
        assert _get_log_handler() in logging.getLogger().handlers
        assert _fastapi_is_instrumented()

    def test_loguru_is_bridged(self) -> None:
        pytest.importorskip("loguru")
        from indratrace import logs

        init_observability(api_key="it_test_compat", ai_only=False)
        assert logs._sink_id is not None

    def test_banner_names_all_three(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_compat", ai_only=False, debug=True)
        assert "signals: traces + logs + metrics (OTLP/HTTP, batched)" in _console(
            sdk_log
        )

    def test_instrument_fastapi_alias_still_opts_out(self) -> None:
        init_observability(
            api_key="it_test_compat", ai_only=False, instrument_fastapi=False
        )
        assert not _fastapi_is_instrumented()

    def test_no_warning(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_compat", ai_only=False)
        assert sdk_warnings(sdk_log) == []


class TestHttpIgnoredWarning:
    def test_explicit_http_warns_once(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_ai_only", instrument_http=True)
        init_observability(api_key="it_test_ai_only", instrument_http=True)

        warnings = [r.getMessage() for r in sdk_warnings(sdk_log)]
        assert warnings == [HTTP_IGNORED_WARNING]

    def test_the_old_alias_warns_too(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_ai_only", instrument_fastapi=True)
        warnings = [r.getMessage() for r in sdk_warnings(sdk_log)]
        assert warnings == [HTTP_IGNORED_WARNING]

    def test_defaults_do_not_warn(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_ai_only")
        assert sdk_warnings(sdk_log) == []

    def test_the_warning_never_carries_the_key(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        init_observability(api_key="it_test_secret_key", instrument_http=True)
        assert "it_test_secret_key" not in _console(sdk_log)


class TestLlmSpanAsRunRoot:
    """With HTTP spans off, a model call made while serving a web request has
    no server span above it: it becomes its trace's root. The contract in
    docs/conventions.md (§ Run roots) says such a span is read on its own —
    by its `gen_ai.*` attributes, or, for a tool, by `indratrace.span.kind` —
    so these tests pin both halves: the span IS a root, and it carries the
    documented attributes a reader keys on."""

    @pytest.fixture
    def app(self) -> object:
        flask = pytest.importorskip("flask")
        pytest.importorskip("anthropic")
        init_observability(api_key="it_test_ai_only")

        app = flask.Flask("ai_only_contract")

        @trace_tool
        def lookup(q: str) -> str:
            return q

        @app.route("/chat")
        def chat() -> str:
            _mocked_anthropic_call(input_tokens=11, output_tokens=7)
            return "ok"

        @app.route("/tool")
        def tool() -> str:
            return lookup("x")

        return app

    @staticmethod
    def _documented(attribute: str) -> bool:
        return f"`{attribute}`" in CONVENTIONS.read_text(encoding="utf-8")

    def test_the_model_span_is_a_root_with_its_usage(self, app: object) -> None:
        exporter = capture_spans()
        assert app.test_client().get("/chat").status_code == 200  # type: ignore[attr-defined]

        spans: tuple[ReadableSpan, ...] = exporter.get_finished_spans()
        assert len(spans) == 1, "no HTTP server span under ai_only"
        (span,) = spans
        assert span.parent is None, "the model span is its trace's root"

        attrs = dict(span.attributes)
        for key in (
            GEN_AI_USAGE_INPUT_TOKENS,
            GEN_AI_USAGE_OUTPUT_TOKENS,
            GEN_AI_REQUEST_MODEL,
        ):
            assert key in attrs, f"model span lost {key}"
            assert self._documented(key), f"{key} is not in conventions.md"
        assert any(k.startswith("gen_ai.") for k in attrs)

    def test_a_tool_span_is_a_root_with_its_kind(self, app: object) -> None:
        exporter = capture_spans()
        assert app.test_client().get("/tool").status_code == 200  # type: ignore[attr-defined]

        (span,) = exporter.get_finished_spans()
        assert span.parent is None
        assert span.attributes[SPAN_KIND_ATTRIBUTE] == "tool"
        assert self._documented(SPAN_KIND_ATTRIBUTE)

    def test_conventions_documents_run_roots(self) -> None:
        assert "### Run roots" in CONVENTIONS.read_text(encoding="utf-8")
