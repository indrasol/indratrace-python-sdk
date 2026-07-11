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
        assert set(indratrace.__all__) == {
            "__version__",
            "current_trace_id",
            "init_observability",
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
