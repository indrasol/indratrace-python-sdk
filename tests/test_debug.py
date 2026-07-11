"""The `debug=True` diagnostics flag (INDRATRACE_DEBUG).

Turning `debug` on must make the SDK's diagnostics *audible* — a startup banner,
per-integration enabled/skipped lines, and a clear export-failure line against a
dead collector — **without** weakening fail-silence: init still never raises, and
`debug=False` prints nothing at all. These tests pin exactly that contract, which
exists because a silent init once let a whole feature fail unnoticed (the prompt
08 story in docs/PROGRESS.md).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from indratrace import init_observability
from indratrace.config import ENV_DEBUG, resolve_debug
from indratrace.init import _reset_for_tests

# Nothing listens here; every export against it fails fast (conftest shrinks the
# export timeout for the offline suite, so the probe's flush is near-instant).
DEAD_ENDPOINT = "http://127.0.0.1:1"


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def console_lines(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records]


class TestResolveDebug:
    """Precedence: explicit arg > INDRATRACE_DEBUG > default (off)."""

    def test_off_by_default(self) -> None:
        assert resolve_debug() is False

    def test_explicit_true_wins(self) -> None:
        assert resolve_debug(True) is True

    def test_explicit_false_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEBUG, "true")
        assert resolve_debug(False) is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_env_truthy_values(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEBUG, raw)
        assert resolve_debug() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "maybe"])
    def test_env_falsey_values(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEBUG, raw)
        assert resolve_debug() is False


class TestOffByDefault:
    """The invariant that keeps a normal app's console clean."""

    def test_no_console_handler_attached_without_debug(self) -> None:
        before = list(logging.getLogger("indratrace").handlers)
        init_observability(
            product="quiet", endpoint=DEAD_ENDPOINT, instrument_fastapi=False
        )
        assert logging.getLogger("indratrace").handlers == before

    def test_debug_false_prints_nothing_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_observability(
            product="quiet", endpoint=DEAD_ENDPOINT, instrument_fastapi=False
        )
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestConsoleHandler:
    """debug=True attaches one StreamHandler to the `indratrace` logger."""

    def test_debug_attaches_a_console_handler(self) -> None:
        before = len(logging.getLogger("indratrace").handlers)
        init_observability(
            product="demo",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        assert len(logging.getLogger("indratrace").handlers) == before + 1

    def test_reset_detaches_the_debug_handler(self) -> None:
        before = len(logging.getLogger("indratrace").handlers)
        init_observability(
            product="demo",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        assert len(logging.getLogger("indratrace").handlers) == before + 1

        _reset_for_tests()
        assert len(logging.getLogger("indratrace").handlers) == before

    def test_debug_writes_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        init_observability(
            product="demo",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        # StreamHandler defaults to stderr; the banner must actually surface.
        assert "IndraTrace SDK" in capsys.readouterr().err

    def test_no_handler_duplication(self) -> None:
        """An operator who owns the `indratrace` logger gets no console dupe.

        If they attached their own handler to route our diagnostics into their
        logging setup, `debug=True` must not bolt a second one on top and
        double-print every line.
        """
        sdk_logger = logging.getLogger("indratrace")
        operator_handler = logging.StreamHandler(io.StringIO())
        sdk_logger.addHandler(operator_handler)
        before = len(sdk_logger.handlers)
        original_level = sdk_logger.level
        try:
            init_observability(
                product="demo",
                endpoint=DEAD_ENDPOINT,
                instrument_fastapi=False,
                debug=True,
            )
            # No handler added on top of theirs...
            assert len(sdk_logger.handlers) == before
            # ...but the logger is lowered so their handler actually sees DEBUG.
            assert sdk_logger.getEffectiveLevel() <= logging.DEBUG
        finally:
            sdk_logger.removeHandler(operator_handler)
            sdk_logger.setLevel(original_level)

    def test_operator_handler_receives_the_banner(self) -> None:
        """The flip side of no-duplication: their handler still gets the lines."""
        sdk_logger = logging.getLogger("indratrace")
        seen: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        operator_handler = Collect(level=logging.DEBUG)
        sdk_logger.addHandler(operator_handler)
        original_level = sdk_logger.level
        try:
            init_observability(
                product="demo",
                endpoint=DEAD_ENDPOINT,
                instrument_fastapi=False,
                debug=True,
            )
        finally:
            sdk_logger.removeHandler(operator_handler)
            sdk_logger.setLevel(original_level)

        assert any("IndraTrace SDK" in line for line in seen)


class TestBannerContent:
    """The banner is the operator's answer to 'what did init actually do?'."""

    def _banner(self, records: list[logging.LogRecord], **kwargs: object) -> str:
        init_observability(
            product="probe",
            env="staging",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
            **kwargs,
        )
        return "\n".join(console_lines(records))

    def test_banner_names_version_product_env_endpoint(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        from indratrace.version import __version__

        text = self._banner(sdk_log)
        assert f"IndraTrace SDK v{__version__}" in text
        assert "product=probe" in text
        assert "env=staging" in text
        assert DEAD_ENDPOINT in text

    def test_banner_reports_ingest_key_and_capture_content_state(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        text = self._banner(sdk_log, ingest_key="secret", capture_content=True)
        assert "ingest_key=set" in text
        assert "capture_content=on" in text

    def test_banner_reports_each_instrumentor_status(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        """FastAPI is off here, so the banner must say so with a reason; the
        GenAI providers are dev deps, so they show as enabled."""
        init_observability(
            product="probe",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        text = "\n".join(console_lines(sdk_log))
        # instrument_fastapi=False means the banner never adds a fastapi line;
        # the GenAI providers (dev deps in this env) come on.
        assert "genai[anthropic]: enabled" in text
        assert "claude-agent-sdk: enabled" in text

    def test_skipped_extra_shows_a_reason(
        self, sdk_log: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing extra reads as 'skipped (extra not installed)' — the exact
        line a user with an empty dashboard needs to see (prompt 08 lesson)."""
        import builtins

        real_import = builtins.__import__

        def no_anthropic(name: str, *args: object, **kwargs: object) -> object:
            if name == "opentelemetry.instrumentation.anthropic":
                raise ImportError("simulated missing extra")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_anthropic)
        init_observability(
            product="probe",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        text = "\n".join(console_lines(sdk_log))
        assert "genai[anthropic]: skipped (extra not installed)" in text


class TestAudibleExport:
    """Fail-silent becomes fail-*audible*: the drop is logged, not raised."""

    def test_dead_endpoint_logs_a_clear_export_failure(
        self, sdk_log: list[logging.LogRecord]
    ) -> None:
        init_observability(
            product="probe",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )
        failures = [
            record
            for record in sdk_log
            if record.levelno == logging.WARNING
            and "export FAILED" in record.getMessage()
        ]
        assert failures, "expected an audible export-failure line vs a dead endpoint"

    def test_init_never_raises_with_debug_on_and_a_dead_endpoint(self) -> None:
        """The whole point: audible, never loud. This must not raise."""
        init_observability(
            product="probe",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=True,
        )  # no exception == pass


class TestEnvVar:
    def test_indratrace_debug_env_turns_it_on(
        self, sdk_log: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEBUG, "1")
        init_observability(
            product="probe", endpoint=DEAD_ENDPOINT, instrument_fastapi=False
        )
        assert any("IndraTrace SDK" in line for line in console_lines(sdk_log))

    def test_explicit_false_beats_the_env_var(
        self, sdk_log: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_DEBUG, "1")
        init_observability(
            product="probe",
            endpoint=DEAD_ENDPOINT,
            instrument_fastapi=False,
            debug=False,
        )
        assert not any("IndraTrace SDK" in line for line in console_lines(sdk_log))
