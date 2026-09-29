"""init_observability(): resource stamping, idempotency, fail-silence."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import indratrace
from indratrace import IndraTraceConfigError, init_observability
from indratrace.config import (
    API_KEY_HEADER,
    ENV_API_KEY,
    ENV_ENDPOINT,
    ENV_ENV,
    ENV_KEY,
    ENV_PRODUCT,
    GATEWAY_STAMPED_ATTRS,
    REMOVED_PARAMS,
)
from indratrace.init import (
    _get_logger_provider,
    _get_meter_provider,
    _get_provider,
    _reset_for_tests,
)
from indratrace.version import __version__

from .conftest import (
    PRODUCTION_EXPORT_TIMEOUT_SECONDS,
    TEST_API_KEY,
    TIMEOUT_ATTR,
    sdk_warnings,
)
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
        # 1.0: the one exception `init_observability` is allowed to raise is
        # part of the public surface, so callers can catch it by name.
        assert issubclass(indratrace.IndraTraceConfigError, ValueError)
        assert set(indratrace.__all__) == {
            "IndraTraceConfigError",
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
        assert (
            init_observability(api_key=TEST_API_KEY, instrument_fastapi=False) is None
        )


class TestResourceOnSpans:
    def test_spans_carry_every_required_resource_attribute(self) -> None:
        init_observability(
            api_key=TEST_API_KEY,
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

        assert attrs["service.name"] == "compliance-api"
        assert attrs["service.version"] == "1.4.2"
        assert attrs["telemetry.sdk.wrapper"] == f"indratrace/{__version__}"

    def test_spans_carry_none_of_the_gateway_stamped_attributes(self) -> None:
        """The SDK-side twin of the platform's grep that `ingest/stamp.py` is the
        only writer of these three (P80). The gateway drops whatever the payload
        claims and appends the key's values, so sending them would be sending a
        claim we already know is discarded — and would let a customer *think*
        they had set it (ADR 0009).
        """
        init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)
        exporter = capture_spans()

        emit_span()

        (span,) = exporter.get_finished_spans()
        for attr in GATEWAY_STAMPED_ATTRS:
            assert attr not in span.resource.attributes, (
                f"the SDK must not send {attr!r} — the gateway stamps it"
            )

    def test_api_key_is_read_from_env(
        self, monkeypatch: pytest.MonkeyPatch, wire: list[_Capture]
    ) -> None:
        """`INDRATRACE_API_KEY` is the one supported env var, and it works."""
        monkeypatch.setenv(ENV_API_KEY, "it_test_from_env")

        init_observability(instrument_fastapi=False)
        _emit_and_flush_all_signals()

        assert _keys_by_path(wire) == dict.fromkeys(
            ALL_SIGNAL_PATHS, {"it_test_from_env"}
        )


class TestApiKeyIsRequired:
    """1.0 §2: no key is a loud, actionable failure — not a silent 401 stream."""

    def test_no_key_anywhere_raises_with_actionable_text(self) -> None:
        with pytest.raises(IndraTraceConfigError) as excinfo:
            init_observability(instrument_fastapi=False)

        message = str(excinfo.value)
        assert "No API key" in message
        assert ENV_API_KEY in message
        assert "api_key=" in message
        assert _get_provider() is None, "must not initialize without a key"

    def test_empty_string_is_no_key(self) -> None:
        with pytest.raises(IndraTraceConfigError, match="No API key"):
            init_observability("", instrument_fastapi=False)

    def test_empty_env_var_is_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_API_KEY, "")
        with pytest.raises(IndraTraceConfigError, match="No API key"):
            init_observability(instrument_fastapi=False)

    def test_the_error_is_a_value_error(self) -> None:
        """Subclassing ValueError keeps a pre-1.0 `except ValueError` guard working."""
        with pytest.raises(ValueError, match="No API key"):
            init_observability(instrument_fastapi=False)

    def test_a_key_of_any_shape_is_still_sent(self, wire: list[_Capture]) -> None:
        """The SDK checks presence, never format — the gateway is the one
        authority on whether a key is valid."""
        init_observability("not-an-it-prefixed-key", instrument_fastapi=False)
        _emit_and_flush_all_signals()

        assert _keys_by_path(wire) == dict.fromkeys(
            ALL_SIGNAL_PATHS, {"not-an-it-prefixed-key"}
        )

    def test_api_key_is_the_only_positional_argument(self) -> None:
        """The one-liner is `init_observability("it_live_...")`; everything else is
        keyword-only, so nothing can be passed by position and land in the key."""
        init_observability(TEST_API_KEY, instrument_fastapi=False)
        assert _get_provider() is not None

        with pytest.raises(TypeError):
            init_observability(TEST_API_KEY, "a-second-positional")


class TestRemovedParameters:
    """1.0 §1.2: each removed name explains itself instead of raising TypeError."""

    @pytest.mark.parametrize("name", sorted(REMOVED_PARAMS))
    def test_removed_parameter_raises_a_named_actionable_error(self, name: str) -> None:
        with pytest.raises(IndraTraceConfigError) as excinfo:
            init_observability(api_key=TEST_API_KEY, **{name: "x"})

        message = str(excinfo.value)
        assert f"`{name}`" in message, "the message must name the parameter"
        assert "removed in 1.0" in message
        assert "init_observability(api_key=" in message, "and say what to do"
        assert _get_provider() is None, "must not initialize on a removed param"

    def test_removed_parameter_raises_even_without_a_key(self) -> None:
        """A 0.x call site passes both mistakes at once; the removed name is the
        more useful of the two complaints, so it wins."""
        with pytest.raises(IndraTraceConfigError, match="`product` was removed"):
            init_observability(product="compliance")

    def test_removed_parameter_raises_even_after_a_successful_init(self) -> None:
        """Idempotency must not swallow a wrong call: a second, stale call site is
        exactly the one that still needs to be told."""
        init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)

        with pytest.raises(IndraTraceConfigError, match="`product` was removed"):
            init_observability(api_key=TEST_API_KEY, product="compliance")

    def test_an_unknown_kwarg_is_still_a_plain_type_error(self) -> None:
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            init_observability(api_key=TEST_API_KEY, produkt="typo")


class TestRemovedEnvVars:
    """1.0 §1.3: set one and it is ignored, with exactly one warning."""

    @pytest.mark.parametrize("name", [ENV_PRODUCT, ENV_ENV, ENV_KEY])
    def test_removed_env_var_warns_once_and_is_ignored(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(name, "whatever")

        with pytest.warns(UserWarning, match=f"{name} was removed in 1.0") as caught:
            init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)

        ours = [w for w in caught if name in str(w.message)]
        assert len(ours) == 1, f"expected exactly one warning for {name}"
        assert _get_provider() is not None, "the var is ignored, not fatal"

    def test_deprecated_key_env_var_no_longer_supplies_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`INDRATRACE_KEY` used to be honored. In 1.0 it only warns, so a process
        that relied on it gets the actionable missing-key error, not silent 401s."""
        monkeypatch.setenv(ENV_KEY, "it_test_old_alias")

        with pytest.warns(UserWarning, match="INDRATRACE_KEY was removed"):
            with pytest.raises(IndraTraceConfigError, match="No API key"):
                init_observability(instrument_fastapi=False)

    def test_no_warning_when_none_are_set(
        self, recwarn: pytest.WarningsRecorder
    ) -> None:
        init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)

        assert not [w for w in recwarn if "was removed in 1.0" in str(w.message)]

    def test_the_supported_env_var_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
    ) -> None:
        monkeypatch.setenv(ENV_API_KEY, "it_test_supported")

        init_observability(instrument_fastapi=False)

        assert not [w for w in recwarn if "was removed in 1.0" in str(w.message)]
        assert _get_provider() is not None


class _Capture(NamedTuple):
    """One request the fake gateway received."""

    path: str
    headers: dict[str, str]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Capture]]:
    """A throwaway local OTLP/HTTP receiver; yields the requests it saw.

    Asserting on what actually crosses the wire keeps these tests independent
    of OpenTelemetry's private exporter attributes, which change between
    releases (`_headers` is gone in 1.45).
    """
    seen: list[_Capture] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append(
                _Capture(self.path, {k.lower(): v for k, v in self.headers.items()})
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(ENV_ENDPOINT, f"http://127.0.0.1:{server.server_address[1]}")
    # conftest shrinks the export timeout for unreachable endpoints; a live
    # local server needs a realistic one or a slow CI runner times out.
    monkeypatch.setattr(TIMEOUT_ATTR, PRODUCTION_EXPORT_TIMEOUT_SECONDS)
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _emit_and_flush_all_signals() -> None:
    """One span, one log record and one metric point, then flush all three."""
    tracer_provider = _get_provider()
    logger_provider = _get_logger_provider()
    meter_provider = _get_meter_provider()
    assert tracer_provider is not None
    assert logger_provider is not None
    assert meter_provider is not None

    with tracer_provider.get_tracer("indratrace.tests").start_as_current_span("s"):
        pass
    # WARNING passes the stdlib root default, so no log_level is needed.
    logging.getLogger("tests.wire").warning("wire test record")
    meter_provider.get_meter("indratrace.tests").create_counter("wire").add(1)

    tracer_provider.force_flush()
    logger_provider.force_flush()
    meter_provider.force_flush()


def _keys_by_path(seen: list[_Capture]) -> dict[str, set[str | None]]:
    """Every `x-indratrace-key` value the receiver saw, per OTLP path."""
    by_path: dict[str, set[str | None]] = {}
    for capture in seen:
        by_path.setdefault(capture.path, set()).add(capture.headers.get(API_KEY_HEADER))
    return by_path


ALL_SIGNAL_PATHS = {"/v1/traces", "/v1/logs", "/v1/metrics"}


class TestAuthHeaderOnEveryExporter:
    """conventions.md § Transport: `x-indratrace-key` authenticates ingest.

    Asserted on all three exporters, not just spans: the gateway rejects an
    unauthenticated batch with a 401 the app never sees, so a logs- or
    metrics-only regression would be invisible until a dashboard was empty.
    """

    def test_all_three_exporters_carry_the_key(self, wire: list[_Capture]) -> None:
        init_observability(api_key="it_test_secret", instrument_fastapi=False)
        _emit_and_flush_all_signals()

        # Every request, on every signal, carried exactly this key.
        assert _keys_by_path(wire) == dict.fromkeys(
            ALL_SIGNAL_PATHS, {"it_test_secret"}
        )


class TestReadmeOneLiner:
    """The README leads with a one-liner; execute it so the two cannot drift.

    Pinning the exact source string means a signature change breaks the *call*
    and a README rewrite breaks the *match* — either way someone is told.
    """

    #: Copied verbatim from README.md's quickstart.
    ONE_LINER = 'init_observability(api_key="it_live_...")'

    def test_the_readme_still_leads_with_it(self) -> None:
        readme = Path(__file__).resolve().parents[1] / "README.md"

        assert self.ONE_LINER in readme.read_text(encoding="utf-8"), (
            f"README.md no longer contains {self.ONE_LINER!r}"
        )

    def test_it_runs_against_a_fake_key(self) -> None:
        """Exercised against the suite's fake key and dead endpoint (conftest):
        the SDK never validates the key's format, and never needs delivery."""
        source = f"from indratrace import init_observability\n{self.ONE_LINER}\n"

        exec(compile(source, "<README.md>", "exec"), {})  # noqa: S102

        assert _get_provider() is not None


class TestIdempotency:
    def test_second_call_is_a_noop(self, sdk_log: list[logging.LogRecord]) -> None:
        init_observability(api_key="it_test_first", instrument_fastapi=False)
        first_provider = _get_provider()

        init_observability(api_key="it_test_second", instrument_fastapi=False)

        assert _get_provider() is first_provider, "second call rebuilt the provider"
        assert any("already called" in r.getMessage() for r in sdk_log)

    def test_second_call_does_not_change_the_resource(self) -> None:
        init_observability(
            api_key=TEST_API_KEY, service_name="first", instrument_fastapi=False
        )
        init_observability(
            api_key=TEST_API_KEY, service_name="second", instrument_fastapi=False
        )
        exporter = capture_spans()

        emit_span()

        (span,) = exporter.get_finished_spans()
        assert span.resource.attributes["service.name"] == "first"


class TestFailSilent:
    """ADR 0003: SDK errors never propagate into the host app."""

    def test_bogus_endpoint_does_not_raise(self) -> None:
        init_observability(
            api_key="it_test_demo",
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
            api_key="it_test_demo",
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

    def test_wiring_failure_warns_once_and_leaves_app_running(
        self, sdk_log: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("exporter exploded")

        monkeypatch.setattr("indratrace.init.OTLPSpanExporter", boom)

        init_observability(api_key="it_test_demo", instrument_fastapi=False)

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

        init_observability(api_key="it_test_demo", instrument_fastapi=True)

        assert _get_provider() is not None, "init must still succeed"
        assert sdk_warnings(sdk_log) == []


class TestFastApiInstrumentation:
    @staticmethod
    def _is_instrumented() -> bool:
        module = pytest.importorskip("opentelemetry.instrumentation.fastapi")
        return module.FastAPIInstrumentor().is_instrumented_by_opentelemetry

    def test_enabled_by_default(self) -> None:
        init_observability(api_key="it_test_demo")
        assert self._is_instrumented()

    def test_can_be_opted_out(self) -> None:
        init_observability(api_key="it_test_demo", instrument_fastapi=False)
        assert not self._is_instrumented()


class TestPlaintextEndpoint:
    def test_remote_http_endpoint_warns_and_still_initializes(
        self, monkeypatch: pytest.MonkeyPatch, sdk_log: list[logging.LogRecord]
    ) -> None:
        monkeypatch.setenv(ENV_ENDPOINT, "http://gateway.example.invalid:4318")

        init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)

        warnings = sdk_warnings(sdk_log)
        assert len(warnings) == 1
        assert "unencrypted" in warnings[0].getMessage()
        assert TEST_API_KEY not in warnings[0].getMessage()
        assert _get_provider() is not None, "a warning, never a block"

    def test_loopback_http_endpoint_does_not_warn(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        init_observability(api_key=TEST_API_KEY, instrument_fastapi=False)

        assert sdk_warnings(sdk_log) == []
