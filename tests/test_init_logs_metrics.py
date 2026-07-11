"""Logs + metrics wiring: trace correlation, the resource contract, fail-silence."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

from indratrace import init_observability, trace_agent
from indratrace.config import ObsConfig
from indratrace.init import (
    _get_logger_provider,
    _get_meter_provider,
    _get_provider,
    _reset_for_tests,
)
from indratrace.version import __version__

from .conftest import sdk_warnings
from .test_config import REQUIRED_RESOURCE_ATTRS

DEAD_ENDPOINT = "http://127.0.0.1:1"  # refuses instantly; nothing listens


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def capture_logs() -> InMemoryLogRecordExporter:
    """Tee the logger provider init built into an in-memory exporter.

    Same reason as `capture_spans` in test_init.py: OTel freezes each global
    provider at the first setter call, so tests read the SDK's own provider.
    """
    provider = _get_logger_provider()
    assert provider is not None, "init_observability() did not build a logger provider"

    exporter = InMemoryLogRecordExporter()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return exporter


def bodies(exporter: InMemoryLogRecordExporter) -> list[object]:
    return [record.log_record.body for record in exporter.get_finished_logs()]


def emitted(exporter: InMemoryLogRecordExporter, message: str) -> ReadableLogRecord:
    matches = [
        record
        for record in exporter.get_finished_logs()
        if record.log_record.body == message
    ]
    assert len(matches) == 1, f"expected one {message!r} record, got {len(matches)}"
    return matches[0]


def meter_resource(provider: MeterProvider) -> Resource:
    """The resource a MeterProvider was built with.

    `TracerProvider` and `LoggerProvider` expose `.resource`; `MeterProvider`
    does not, so reach into its SDK config. Private, but the alternative is
    keeping a copy of the resource in `init` purely so tests can read it back.
    """
    return provider._sdk_config.resource


class TestEndpoints:
    def test_signal_paths(self) -> None:
        cfg = ObsConfig(
            product="p",
            env="dev",
            endpoint="https://collector.example.com:4318/",  # trailing slash tolerated
            service_name="s",
            service_version="1",
        )
        assert cfg.traces_endpoint == "https://collector.example.com:4318/v1/traces"
        assert cfg.logs_endpoint == "https://collector.example.com:4318/v1/logs"
        assert cfg.metrics_endpoint == "https://collector.example.com:4318/v1/metrics"


class TestProvidersAreBuilt:
    def test_all_three_signals_wired(self) -> None:
        init_observability(product="demo", instrument_fastapi=False)

        assert _get_provider() is not None
        assert _get_logger_provider() is not None
        assert _get_meter_provider() is not None


@pytest.mark.usefixtures("app_logs_at_info")
class TestLogBridge:
    """These assume a host app that configured logging at INFO.

    The SDK no longer lowers the root level itself, so an app that suppressed
    INFO ships nothing — which is the point of `test_root_logger_level_is_left_alone`.
    """

    def test_stdlib_log_ships_as_a_record(self) -> None:
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        logging.getLogger("some.product.module").info("hello from the product")

        record = emitted(exporter, "hello from the product").log_record
        assert record.severity_text == "INFO"

    def test_log_inside_a_span_carries_trace_context(self) -> None:
        """The whole point of the bridge: a log line links back to its trace."""
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        seen: dict[str, int] = {}

        @trace_agent("logger")
        def run() -> None:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            seen["trace_id"] = ctx.trace_id
            seen["span_id"] = ctx.span_id
            logging.getLogger("product").info("inside the span")

        run()

        record = emitted(exporter, "inside the span").log_record
        assert record.trace_id == seen["trace_id"]
        assert record.span_id == seen["span_id"]

    def test_log_outside_a_span_has_no_trace_context(self) -> None:
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        logging.getLogger("product").info("no span here")

        assert emitted(exporter, "no span here").log_record.trace_id == 0

    def test_debug_records_are_not_shipped(self) -> None:
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        product = logging.getLogger("chatty.product")
        product.setLevel(logging.DEBUG)
        try:
            product.debug("too chatty for the wire")
            product.info("worth shipping")
        finally:
            product.setLevel(logging.NOTSET)  # don't leak the level to other tests

        assert "too chatty for the wire" not in bodies(exporter)
        assert "worth shipping" in bodies(exporter)

    @pytest.mark.parametrize("noisy_logger", ["indratrace", "opentelemetry"])
    def test_export_path_logs_are_not_shipped(self, noisy_logger: str) -> None:
        """Shipping these would feed a loop: a failed export logs an error,
        which becomes another record to export, which fails..."""
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        logging.getLogger(noisy_logger).warning("export failed")
        logging.getLogger(f"{noisy_logger}.exporter.otlp").error("export failed again")

        assert bodies(exporter) == []

    def test_the_sdk_logger_still_reaches_the_apps_own_handlers(self) -> None:
        """We filter on our handler, not by cutting `indratrace` off from root.

        An operator who wants the SDK's diagnostics must still get them.
        """
        init_observability(product="demo", instrument_fastapi=False)

        seen: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        root = logging.getLogger()
        handler = Collect(level=logging.WARNING)
        root.addHandler(handler)
        try:
            logging.getLogger("indratrace").warning("export failed")
        finally:
            root.removeHandler(handler)

        assert seen == ["export failed"]
        assert logging.getLogger("indratrace").propagate is True

    def test_reset_detaches_the_handler(self) -> None:
        """Otherwise every test in the session accumulates another handler."""
        before = len(logging.getLogger().handlers)

        init_observability(product="demo", instrument_fastapi=False)
        assert len(logging.getLogger().handlers) == before + 1

        _reset_for_tests()
        assert len(logging.getLogger().handlers) == before

class TestRootLoggerLevel:
    """`log_level` is opt-in: without it the SDK never reconfigures logging."""

    def test_root_level_is_left_alone_by_default(self) -> None:
        """Lowering root would make the app's *own* console and file handlers
        start emitting records it had deliberately suppressed."""
        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.WARNING)
            init_observability(product="demo", instrument_fastapi=False)
            assert root.level == logging.WARNING, "init changed the app's log level"
        finally:
            root.setLevel(original)

    def test_quiet_app_ships_nothing_below_warning_by_default(self) -> None:
        """The documented consequence of not touching root."""
        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.WARNING)
            init_observability(product="demo", instrument_fastapi=False)
            exporter = capture_logs()

            # A fresh logger name with no level of its own, so it inherits root.
            logging.getLogger("quiet.app.module").info("suppressed by the level")

            assert bodies(exporter) == []
        finally:
            root.setLevel(original)

    def test_log_level_opts_into_lowering_root(self) -> None:
        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.WARNING)
            init_observability(
                product="demo", instrument_fastapi=False, log_level="INFO"
            )
            exporter = capture_logs()

            logging.getLogger("product").info("explicitly requested")

            assert root.level == logging.INFO
            assert "explicitly requested" in bodies(exporter)
        finally:
            root.setLevel(original)

    def test_reset_restores_the_root_level_it_changed(self) -> None:
        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.WARNING)
            init_observability(
                product="demo", instrument_fastapi=False, log_level=logging.DEBUG
            )
            assert root.level == logging.DEBUG

            _reset_for_tests()
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original)

    @pytest.mark.usefixtures("app_logs_at_info")
    def test_an_app_configured_at_info_ships_its_logs_with_no_argument(self) -> None:
        """The common case — `basicConfig(level=INFO)`, uvicorn, gunicorn."""
        init_observability(product="demo", instrument_fastapi=False)
        exporter = capture_logs()

        logging.getLogger("product").info("shipped")

        assert "shipped" in bodies(exporter)


@pytest.mark.usefixtures("app_logs_at_info")
class TestResourceOnLogsAndMetrics:
    def test_logs_carry_every_required_resource_attribute(self) -> None:
        init_observability(
            product="compliance",
            env="prod",
            service_name="compliance-api",
            service_version="1.4.2",
            instrument_fastapi=False,
        )
        exporter = capture_logs()

        logging.getLogger("product").info("check the resource")

        resource = emitted(exporter, "check the resource").resource
        for attr in REQUIRED_RESOURCE_ATTRS:
            assert attr in resource.attributes, f"conventions.md requires {attr!r}"
        assert resource.attributes["product"] == "compliance"
        wrapper = resource.attributes["telemetry.sdk.wrapper"]
        assert wrapper == f"indratrace/{__version__}"

    def test_metrics_carry_every_required_resource_attribute(self) -> None:
        """Assert on exported metric data, not just the provider's resource."""
        init_observability(product="compliance", instrument_fastapi=False)

        provider = _get_meter_provider()
        assert provider is not None

        # The SDK's own reader is the OTLP one, which exports on a 60s timer
        # and cannot be drained. Feed its resource to a reader we can read.
        reader = InMemoryMetricReader()
        probe = MeterProvider(
            resource=meter_resource(provider), metric_readers=[reader]
        )
        probe.get_meter("indratrace.tests").create_counter("probe").add(1)

        metrics_data = reader.get_metrics_data()
        assert metrics_data is not None
        resource = metrics_data.resource_metrics[0].resource
        for attr in REQUIRED_RESOURCE_ATTRS:
            assert attr in resource.attributes, f"conventions.md requires {attr!r}"
        assert resource.attributes["product"] == "compliance"

        probe.shutdown()

    def test_all_three_providers_share_one_resource(self) -> None:
        init_observability(product="shared", instrument_fastapi=False)

        tracer_provider = _get_provider()
        logger_provider = _get_logger_provider()
        meter_provider = _get_meter_provider()
        assert tracer_provider and logger_provider and meter_provider

        products = {
            tracer_provider.resource.attributes["product"],
            logger_provider.resource.attributes["product"],
            meter_resource(meter_provider).attributes["product"],
        }
        assert products == {"shared"}


class TestFailSilentAcrossAllSignals:
    """ADR 0003, now with three exporters that can each fail."""

    def test_bogus_endpoint_does_not_raise(self) -> None:
        init_observability(
            product="demo", endpoint=DEAD_ENDPOINT, instrument_fastapi=False
        )

        assert _get_provider() is not None
        # Every signal exercised against a dead collector; the caller never knows.
        logging.getLogger("product").info("into the void")
        meter = _get_meter_provider().get_meter("t")
        meter.create_counter("void").add(1)

    @pytest.mark.parametrize(
        "exporter_name",
        ["OTLPSpanExporter", "OTLPLogExporter", "OTLPMetricExporter"],
    )
    def test_any_exporter_failure_warns_once_and_leaves_app_running(
        self,
        exporter_name: str,
        sdk_log: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError(f"{exporter_name} exploded")

        monkeypatch.setattr(f"indratrace.init.{exporter_name}", boom)

        init_observability(product="demo", instrument_fastapi=False)

        assert len(sdk_warnings(sdk_log)) == 1, "exactly one warning, per the spec"
        # A partial init leaves nothing marked initialized, so a retry is possible.
        assert _get_provider() is None
        assert _get_logger_provider() is None
        assert _get_meter_provider() is None

    def test_dead_collector_does_not_stall_shutdown_of_any_signal(
        self, production_export_timeout: float
    ) -> None:
        """Regression: OTel's 10s default timeout, now on three exporters.

        Shutdown drains all three serially, so an exporter that ignored our
        pinned timeout would hang process exit for 30s.
        """
        init_observability(
            product="demo", endpoint=DEAD_ENDPOINT, instrument_fastapi=False
        )

        # Queue something on each signal, so every exporter has work to flush.
        trace_agent("shutdown")(lambda: None)()
        logging.getLogger("product").info("queued, never delivered")
        _get_meter_provider().get_meter("t").create_counter("queued").add(1)

        started = time.monotonic()
        _reset_for_tests()  # shuts down tracer, logger, and meter providers
        elapsed = time.monotonic() - started

        budget = 3 * production_export_timeout + 2.0
        assert elapsed < budget, (
            f"shutdown blocked for {elapsed:.1f}s against a dead collector"
        )
