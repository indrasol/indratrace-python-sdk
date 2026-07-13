"""The loguru auto-bridge: a loguru-only app's logs reach the pipeline, zero-config.

Loguru bypasses stdlib logging entirely, so before 0.6.0 a loguru app shipped
*nothing* on the logs signal — silently. These tests pin the fix: severities,
trace correlation, the INFO+ threshold, no duplication against stdlib, and a
clean absent-loguru path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)

from indratrace import init_observability, trace_agent
from indratrace.init import _get_logger_provider, _reset_for_tests
from indratrace.logs import _stdlib_level, enable_loguru_bridge

loguru = pytest.importorskip("loguru", reason="the loguru bridge needs loguru")


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def loguru_logger() -> Iterator[Any]:
    """Loguru's `logger` singleton, with its default stderr sink removed.

    The default sink prints every record to the console, which would spray the
    test output. Removing it also proves the bridge does not depend on it. The
    app's own sinks are restored after each test so nothing leaks between them.
    """
    from loguru import logger

    logger.remove()  # drop loguru's default stderr sink
    try:
        yield logger
    finally:
        logger.remove()


def capture_logs() -> InMemoryLogRecordExporter:
    """Tee the logger provider init built into an in-memory exporter."""
    provider = _get_logger_provider()
    assert provider is not None, "init_observability() did not build a logger provider"

    exporter = InMemoryLogRecordExporter()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return exporter


def bodies(exporter: InMemoryLogRecordExporter) -> list[object]:
    return [record.log_record.body for record in exporter.get_finished_logs()]


def emitted(exporter: InMemoryLogRecordExporter, message: str) -> Any:
    matches = [
        record
        for record in exporter.get_finished_logs()
        if record.log_record.body == message
    ]
    assert len(matches) == 1, f"expected one {message!r} record, got {len(matches)}"
    return matches[0].log_record


class TestLoguruShipsWithNoConfiguration:
    """The acceptance criterion: nothing but `init_observability()`."""

    def test_loguru_info_ships_as_a_record(self, loguru_logger: Any) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.info("hello from loguru")

        assert emitted(exporter, "hello from loguru").severity_text == "INFO"

    @pytest.mark.parametrize(
        ("method", "severity"),
        [
            ("info", "INFO"),
            ("warning", "WARN"),
            ("error", "ERROR"),
            ("critical", "FATAL"),
        ],
    )
    def test_each_level_maps_to_the_right_severity(
        self, loguru_logger: Any, method: str, severity: str
    ) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        getattr(loguru_logger, method)(f"a {method} line")

        assert emitted(exporter, f"a {method} line").severity_text == severity

    def test_loguru_brace_formatting_is_preserved(self, loguru_logger: Any) -> None:
        """`logger.info("hi {}", name)` — loguru's own interpolation, not %-style."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.info("scored {} for {}", 42, "acme")

        assert "scored 42 for acme" in bodies(exporter)

    def test_exception_info_survives(self, loguru_logger: Any) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        try:
            raise ValueError("the cause")
        except ValueError:
            loguru_logger.exception("it broke")

        record = emitted(exporter, "it broke")
        assert record.severity_text == "ERROR"
        # The stack trace rides along in the record's attributes.
        attributes = record.attributes or {}
        assert attributes.get("exception.type") == "ValueError"
        assert "the cause" in str(attributes.get("exception.message", ""))


class TestTraceCorrelation:
    """The whole point of bridging logs at all: a log line links back to its trace."""

    def test_loguru_log_inside_a_span_carries_trace_context(
        self, loguru_logger: Any
    ) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        seen: dict[str, int] = {}

        @trace_agent("loguru-agent")
        def run() -> None:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            seen["trace_id"] = ctx.trace_id
            seen["span_id"] = ctx.span_id
            loguru_logger.info("inside the span")

        run()

        record = emitted(exporter, "inside the span")
        assert record.trace_id == seen["trace_id"]
        assert record.span_id == seen["span_id"]

    def test_loguru_log_outside_a_span_has_no_trace_context(
        self, loguru_logger: Any
    ) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.info("no span here")

        assert emitted(exporter, "no span here").trace_id == 0


class TestExportThreshold:
    def test_debug_records_are_not_shipped(self, loguru_logger: Any) -> None:
        """Same INFO+ threshold as the stdlib bridge — DEBUG would be a firehose."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.debug("too chatty for the wire")
        loguru_logger.info("worth shipping")

        assert "too chatty for the wire" not in bodies(exporter)
        assert "worth shipping" in bodies(exporter)

    def test_custom_loguru_levels_map_by_number(self, loguru_logger: Any) -> None:
        """`SUCCESS` (25) has no stdlib name; it must not crash or be dropped."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.success("it worked")

        assert "it worked" in bodies(exporter)

    def test_trace_level_is_below_the_threshold(self, loguru_logger: Any) -> None:
        """Loguru's TRACE (5) is below DEBUG — nowhere near the export threshold."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.trace("far too chatty")

        assert bodies(exporter) == []


class TestNoDuplication:
    """An app using loguru *and* stdlib must not export anything twice."""

    @pytest.mark.usefixtures("app_logs_at_info")
    def test_loguru_and_stdlib_each_export_exactly_once(
        self, loguru_logger: Any
    ) -> None:
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.info("from loguru")
        logging.getLogger("product").info("from stdlib")

        # `emitted` asserts on exactly one match apiece.
        assert emitted(exporter, "from loguru").severity_text == "INFO"
        assert emitted(exporter, "from stdlib").severity_text == "INFO"
        assert len(bodies(exporter)) == 2

    def test_loguru_record_does_not_reach_the_apps_stdlib_handlers(
        self, loguru_logger: Any
    ) -> None:
        """We feed OUR handler directly, not the stdlib logging tree.

        Going through `logging.getLogger(...).handle(...)` would make the app's
        own console handler print the line a second time (loguru already printed
        it through its own sink).
        """
        init_observability(product="demo", instrument_http=False)
        capture_logs()

        seen: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        root = logging.getLogger()
        handler = Collect(level=logging.DEBUG)
        root.addHandler(handler)
        try:
            loguru_logger.info("loguru only")
        finally:
            root.removeHandler(handler)

        assert seen == [], "the loguru record leaked into the app's stdlib handlers"

    def test_reinit_does_not_double_export(self, loguru_logger: Any) -> None:
        """Idempotent across re-init: a reloading worker must not stack sinks."""
        init_observability(product="demo", instrument_http=False)
        _reset_for_tests()
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.info("just once")

        assert bodies(exporter).count("just once") == 1


class TestBridgeLoguru:
    """`bridge_loguru()` — the escape hatch for apps that configure loguru after init.

    `logger.remove()` (no argument) drops EVERY sink, ours included, so an app
    that sets loguru up after `init_observability()` silently unbridges itself.
    """

    def test_removing_all_sinks_unbridges(self, loguru_logger: Any) -> None:
        """The problem `bridge_loguru` exists to solve — pinned, so the README
        keeps telling the truth."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.remove()  # the idiomatic "drop the default stderr sink"
        loguru_logger.info("lost")

        assert bodies(exporter) == []

    def test_bridge_loguru_puts_it_back(self, loguru_logger: Any) -> None:
        from indratrace import bridge_loguru

        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.remove()
        assert bridge_loguru() is True

        loguru_logger.info("shipped again")

        assert "shipped again" in bodies(exporter)

    def test_info_survives_even_when_the_root_logger_is_at_warning(
        self, loguru_logger: Any
    ) -> None:
        """The trap that killed the first draft of the README snippet.

        Routing loguru through `logging.getLogger(...)` makes each record clear
        the ROOT logger's level first — which is WARNING in an app that never
        called `basicConfig`. INFO would vanish. Feeding our handler directly is
        what makes this work, so it is worth a test of its own.
        """
        from indratrace import bridge_loguru

        root = logging.getLogger()
        original = root.level
        try:
            root.setLevel(logging.WARNING)  # an app that never configured logging

            init_observability(product="demo", instrument_http=False)
            exporter = capture_logs()

            loguru_logger.remove()
            bridge_loguru()
            loguru_logger.info("INFO must not be eaten by the root level")

            assert "INFO must not be eaten by the root level" in bodies(exporter)
        finally:
            root.setLevel(original)

    def test_bridge_loguru_is_idempotent(self, loguru_logger: Any) -> None:
        from indratrace import bridge_loguru

        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        bridge_loguru()
        bridge_loguru()

        loguru_logger.info("exactly once")

        assert bodies(exporter).count("exactly once") == 1

    def test_bridge_loguru_without_init_is_false_not_an_exception(self) -> None:
        from indratrace import bridge_loguru

        assert bridge_loguru() is False


class TestNoExportLoop:
    def test_the_sdks_own_loguru_records_are_not_shipped(
        self, loguru_logger: Any
    ) -> None:
        """Shipping these would feed a loop: a failed export logs an error, which
        becomes another record to export, which fails..."""
        init_observability(product="demo", instrument_http=False)
        exporter = capture_logs()

        loguru_logger.bind().patch(
            lambda record: record.update(name="indratrace.exporter")
        ).warning("export failed")

        assert bodies(exporter) == []


class TestFailSilent:
    def test_absent_loguru_is_a_silent_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No loguru installed: zero cost, zero error, and init still succeeds."""
        import builtins

        real_import = builtins.__import__

        def no_loguru(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "loguru":
                raise ImportError("No module named 'loguru'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_loguru)

        enabled, reason = enable_loguru_bridge(logging.Handler())

        assert enabled is False
        assert reason == "loguru not installed"

    def test_init_survives_a_broken_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing bridge must not cost the app its other signals (ADR 0003)."""

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("loguru exploded")

        monkeypatch.setattr("indratrace.init.enable_loguru_bridge", boom)

        init_observability(product="demo", instrument_http=False)

        # Traces/logs/metrics all still wired despite the bridge blowing up.
        from indratrace.init import _get_meter_provider, _get_provider

        assert _get_provider() is not None
        assert _get_logger_provider() is not None
        assert _get_meter_provider() is not None

    def test_a_sink_failure_never_raises_into_the_app(
        self, loguru_logger: Any
    ) -> None:
        """A log call is not a place to discover an SDK bug."""
        init_observability(product="demo", instrument_http=False)

        provider = _get_logger_provider()
        assert provider is not None

        # Break the export path underneath the sink.
        class Exploding(SimpleLogRecordProcessor):
            def emit(self, log_data: object) -> None:
                raise RuntimeError("processor exploded")

        provider.add_log_record_processor(Exploding(InMemoryLogRecordExporter()))

        loguru_logger.info("the app keeps running")  # must not raise


class TestLevelMapping:
    """`_stdlib_level` in isolation — the one piece of real logic in the bridge."""

    @pytest.mark.parametrize(
        ("loguru_name", "loguru_no", "expected"),
        [
            ("DEBUG", 10, logging.DEBUG),
            ("INFO", 20, logging.INFO),
            ("WARNING", 30, logging.WARNING),
            ("ERROR", 40, logging.ERROR),
            ("CRITICAL", 50, logging.CRITICAL),
            ("SUCCESS", 25, 25),  # no stdlib name — falls back to loguru's number
            ("TRACE", 5, 5),
            ("MY_CUSTOM", 33, 33),
        ],
    )
    def test_levels_resolve(
        self, loguru_name: str, loguru_no: int, expected: int
    ) -> None:
        class Level:
            name = loguru_name
            no = loguru_no

        assert _stdlib_level({"level": Level()}) == expected
