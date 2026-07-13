"""init_observability(): resource stamping, idempotency, fail-silence."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import indratrace
from indratrace import init_observability
from indratrace.init import _get_provider, _reset_for_tests
from indratrace.version import __version__

from .conftest import sdk_warnings
from .test_config import REQUIRED_RESOURCE_ATTRS


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    """Each test gets a fresh, un-initialized SDK."""
    _reset_for_tests()
    yield
    _reset_for_tests()


def capture_spans() -> InMemorySpanExporter:
    """Tee the provider init_observability built into an in-memory exporter.

    Reads the SDK's own provider rather than the global one: OTel permits
    `set_tracer_provider` only once per process, so after the first init in a
    test session the global is frozen to that first provider.
    """
    provider = _get_provider()
    assert provider is not None, "init_observability() did not build a provider"

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def emit_span(name: str = "unit-test-span") -> None:
    provider = _get_provider()
    assert provider is not None
    with provider.get_tracer("indratrace.tests").start_as_current_span(name):
        pass




class TestPublicApi:
    def test_exports_are_the_documented_surface(self) -> None:
        assert indratrace.__version__ == __version__
        # The documented surface (docs/product-spec.md): the three core calls,
        # the GenAI manual fallback `record_llm_usage` (ADR 0005), and the v0.2
        # product-analytics primitives — and nothing else.
        assert callable(indratrace.init_observability)
        assert callable(indratrace.trace_agent)
        assert callable(indratrace.trace_tool)
        assert callable(indratrace.trace_step)
        assert callable(indratrace.record_llm_usage)
        assert callable(indratrace.session)
        assert callable(indratrace.record_feedback)
        assert callable(indratrace.current_trace_id)
        # v0.6 escape hatches: `instrument_flask_app` for an app whose `Flask`
        # class was imported before init (web.py), `bridge_loguru` for an app
        # that reconfigured loguru after init (logs.py).
        assert callable(indratrace.instrument_flask_app)
        assert callable(indratrace.bridge_loguru)
        assert set(indratrace.__all__) == {
            "__version__",
            "bridge_loguru",
            "current_trace_id",
            "init_observability",
            "instrument_flask_app",
            "record_feedback",
            "record_llm_usage",
            "session",
            "trace_agent",
            "trace_step",
            "trace_tool",
        }

    def test_returns_none(self) -> None:
        assert init_observability(product="demo", instrument_fastapi=False) is None


class TestResourceOnSpans:
    def test_spans_carry_every_required_resource_attribute(self) -> None:
        init_observability(
            product="compliance",
            env="prod",
            service_name="compliance-api",
            service_version="1.4.2",
            instrument_fastapi=False,
        )
        exporter = capture_spans()

        emit_span()

        (span,) = exporter.get_finished_spans()
        attrs = span.resource.attributes
        for attr in REQUIRED_RESOURCE_ATTRS:
            assert attr in attrs, f"conventions.md requires {attr!r}"

        assert attrs["product"] == "compliance"
        assert attrs["deployment.environment"] == "prod"
        assert attrs["service.name"] == "compliance-api"
        assert attrs["service.version"] == "1.4.2"
        assert attrs["tenant.id"] == "internal"
        assert attrs["telemetry.sdk.wrapper"] == f"indratrace/{__version__}"

    def test_config_is_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDRATRACE_PRODUCT", "from-env")
        monkeypatch.setenv("INDRATRACE_ENV", "staging")

        init_observability(instrument_fastapi=False)
        exporter = capture_spans()

        emit_span()

        (span,) = exporter.get_finished_spans()
        assert span.resource.attributes["product"] == "from-env"
        assert span.resource.attributes["deployment.environment"] == "staging"


class TestApiKeyParameter:
    """`init_observability` accepts `api_key`; `ingest_key` is a deprecated
    alias that warns once but configures identically (v0.5.0 rename)."""

    def test_api_key_does_not_warn(self, recwarn: pytest.WarningsRecorder) -> None:
        init_observability(product="demo", api_key="k", instrument_fastapi=False)
        # Filter to *our* deprecation — third-party instrumentors emit their own
        # unrelated DeprecationWarnings when genai auto-instrumentation runs.
        ours = [w for w in recwarn if "ingest_key is deprecated" in str(w.message)]
        assert not ours

    def test_ingest_key_alias_warns_once(self) -> None:
        with pytest.warns(DeprecationWarning, match="ingest_key is deprecated"):
            init_observability(
                product="demo", ingest_key="k", instrument_fastapi=False
            )

    def test_both_names_produce_the_same_export_headers(self) -> None:
        from indratrace.config import API_KEY_HEADER

        init_observability(product="demo", api_key="secret", instrument_fastapi=False)
        via_new = _provider_export_headers()
        _reset_for_tests()

        with pytest.warns(DeprecationWarning):
            init_observability(
                product="demo", ingest_key="secret", instrument_fastapi=False
            )
        via_old = _provider_export_headers()

        assert via_new == via_old == {API_KEY_HEADER: "secret"}


def _provider_export_headers() -> dict[str, str]:
    """The auth headers the built span exporter will send, read back off the
    provider init_observability wired — the wire behavior the alias must match."""
    provider = _get_provider()
    assert provider is not None
    # Walk the batch processor to its OTLP exporter and read its headers.
    for processor in provider._active_span_processor._span_processors:
        exporter = getattr(processor, "span_exporter", None)
        headers = getattr(exporter, "_headers", None)
        if headers:
            # OTLP stores headers lowercased as a dict; return it verbatim.
            return dict(headers)
    return {}


class TestIdempotency:
    def test_second_call_is_a_noop(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        init_observability(product="first", instrument_fastapi=False)
        first_provider = _get_provider()

        init_observability(product="second", instrument_fastapi=False)

        assert _get_provider() is first_provider, "second call rebuilt the provider"
        assert any("already called" in r.getMessage() for r in sdk_log)

    def test_second_call_does_not_change_the_resource(self) -> None:
        init_observability(product="first", instrument_fastapi=False)
        init_observability(product="second", instrument_fastapi=False)
        exporter = capture_spans()

        emit_span()

        (span,) = exporter.get_finished_spans()
        assert span.resource.attributes["product"] == "first"


class TestFailSilent:
    """ADR 0003: SDK errors never propagate into the host app."""

    def test_bogus_endpoint_does_not_raise(self) -> None:
        init_observability(
            product="demo",
            endpoint="http://127.0.0.1:1",  # refuses instantly; nothing listens
            instrument_fastapi=False,
        )
        exporter = capture_spans()

        emit_span()  # export fails in the background; the caller never knows

        assert len(exporter.get_finished_spans()) == 1

    def test_dead_collector_does_not_stall_shutdown(
        self, production_export_timeout: float
    ) -> None:
        """OTel's 10s export timeout would otherwise hang process exit."""
        init_observability(
            product="demo",
            endpoint="http://127.0.0.1:1",
            instrument_fastapi=False,
        )
        emit_span()

        provider = _get_provider()
        assert provider is not None

        started = time.monotonic()
        provider.shutdown()
        elapsed = time.monotonic() - started

        assert elapsed < production_export_timeout + 2.0, (
            f"shutdown blocked for {elapsed:.1f}s against a dead collector"
        )

    def test_missing_product_warns_instead_of_raising(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        init_observability(instrument_fastapi=False)  # no product anywhere

        assert _get_provider() is None, "must not initialize on bad config"
        warnings = sdk_warnings(sdk_log)
        assert len(warnings) == 1, "exactly one warning, per the spec"
        assert "un-instrumented" in warnings[0].getMessage()

    def test_wiring_failure_warns_once_and_leaves_app_running(
        self, sdk_log: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("exporter exploded")

        monkeypatch.setattr("indratrace.init.OTLPSpanExporter", boom)

        init_observability(product="demo", instrument_fastapi=False)

        assert len(sdk_warnings(sdk_log)) == 1
        assert _get_provider() is None

    def test_missing_fastapi_extra_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, sdk_log: list[logging.LogRecord]
    ) -> None:
        """Not every product is a web app; absent extra must not warn."""
        import builtins

        real_import = builtins.__import__

        def no_fastapi_instrumentation(name: str, *args: object, **kwargs: object):
            if name == "opentelemetry.instrumentation.fastapi":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_fastapi_instrumentation)

        init_observability(product="demo", instrument_fastapi=True)

        assert _get_provider() is not None, "init must still succeed"
        assert sdk_warnings(sdk_log) == []


class TestFastApiInstrumentation:
    @staticmethod
    def _is_instrumented() -> bool:
        module = pytest.importorskip("opentelemetry.instrumentation.fastapi")
        return module.FastAPIInstrumentor().is_instrumented_by_opentelemetry

    def test_enabled_by_default(self) -> None:
        init_observability(product="demo")
        assert self._is_instrumented()

    def test_can_be_opted_out(self) -> None:
        init_observability(product="demo", instrument_fastapi=False)
        assert not self._is_instrumented()
