"""The startup preflight and export-failure surfacing (`preflight.py`).

One test per row of the diagnosis table, faking the transport or the status
and asserting the message names the right cause. Egress-blocked and 402 are
the two that matter most — they are the ones that used to be silence.

No test here reaches the network: `requests.post` is replaced for the probe,
and the export-health tests drive the tracker directly with a fake clock.
"""

from __future__ import annotations

import errno
import logging
import socket
import ssl
from collections.abc import Callable
from typing import Any

import pytest
import requests
import urllib3.exceptions

from indratrace import init_observability
from indratrace.config import (
    ENDPOINT_SOURCE_DEFAULT,
    ENDPOINT_SOURCE_ENV,
    ENV_PREFLIGHT,
    PREFLIGHT_OFF,
    PREFLIGHT_STRICT,
    PREFLIGHT_WARN,
    IndraTraceConfigError,
    ObsConfig,
    resolve_preflight_mode,
)
from indratrace.init import _observe_export, _reset_for_tests
from indratrace.preflight import (
    COLLECTOR_DOWN,
    CONTROL_PLANE_DOWN,
    DNS,
    EGRESS_BLOCKED,
    NO_CARD,
    NO_CARD_TITLE,
    NOT_GATEWAY,
    OK,
    RATE_LIMITED,
    REFUSED,
    REFUSED_LOCALHOST,
    STALLED,
    SUSPENDED,
    SUSPENDED_TITLE,
    TLS,
    UNAUTHORIZED,
    UNEXPECTED,
    Diagnosis,
    ExportHealth,
    diagnose_exception,
    diagnose_response,
    run_preflight,
)

#: Any string; the point of several tests is that it never appears in a log.
SECRET_KEY = "it_live_THIS_MUST_NEVER_BE_LOGGED"

CLOUD = "https://ingest.example.test"


def cfg(endpoint: str = CLOUD, source: str = ENDPOINT_SOURCE_ENV) -> ObsConfig:
    return ObsConfig(api_key=SECRET_KEY, endpoint=endpoint, endpoint_source=source)


class _Response:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def problem(status: int, title: str, detail: str = "") -> _Response:
    """The gateway's RFC 7807 shape (`ingest/app.py::_problem`)."""
    return _Response(status, {"title": title, "detail": detail, "status": status})


def _requests_error(inner: BaseException) -> requests.exceptions.ConnectionError:
    """What `requests` actually raises: its ConnectionError wrapping urllib3's
    MaxRetryError wrapping the socket-level cause."""
    retry = urllib3.exceptions.MaxRetryError(None, "/v1/traces", reason=inner)  # type: ignore[arg-type]
    return requests.exceptions.ConnectionError(retry)


@pytest.fixture
def post(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[dict[str, Any]]]:
    """Replace `requests.post` with a fake that returns/raises `outcome`, and
    hand back the list of calls so a test can inspect headers and timeouts."""
    calls: list[dict[str, Any]] = []

    def install(outcome: Any) -> list[dict[str, Any]]:
        def fake_post(url: str, **kwargs: Any) -> Any:
            calls.append({"url": url, **kwargs})
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(requests, "post", fake_post)
        return calls

    return install


# ─────────────────────────────────────────────────────────────────────────────
# The diagnosis table, row by row.
# ─────────────────────────────────────────────────────────────────────────────


class TestTransportRows:
    def test_dns_failure_names_the_hostname(self) -> None:
        exc = _requests_error(
            urllib3.exceptions.NameResolutionError(
                "ingest.example.test", None, socket.gaierror(8, "nodename nor servname")
            )
        )
        d = diagnose_exception(exc, cfg())
        assert d.cause == DNS
        assert "ingest.example.test" in d.message
        assert "does not resolve" in d.message

    def test_connect_timeout_is_egress_blocked(self) -> None:
        """THE row. A VNet-integrated host behind an NSG/UDR/firewall."""
        exc = _requests_error(
            urllib3.exceptions.ConnectTimeoutError(
                None, "Connection to ingest.example.test timed out. (connect timeout=2)"
            )
        )
        d = diagnose_exception(exc, cfg())
        assert d.cause == EGRESS_BLOCKED
        for word in ("egress", "firewall", "NSG", "UDR", "proxy", "443", "curl"):
            assert word in d.message, word
        assert "ingest.example.test" in d.message

    @pytest.mark.parametrize("err", [errno.EHOSTUNREACH, errno.ENETUNREACH])
    def test_unreachable_route_is_egress_blocked(self, err: int) -> None:
        exc = _requests_error(
            urllib3.exceptions.NewConnectionError(None, "failed")  # type: ignore[arg-type]
        )
        # Attach the real socket error as the cause, the way urllib3 does.
        exc.__cause__ = OSError(err, errno.errorcode[err])
        d = diagnose_exception(exc, cfg())
        assert d.cause == EGRESS_BLOCKED

    def test_no_route_to_host_text_is_egress_blocked(self) -> None:
        exc = requests.exceptions.ConnectionError("[Errno 65] No route to host")
        assert diagnose_exception(exc, cfg()).cause == EGRESS_BLOCKED

    def test_refused_on_localhost_from_the_default_says_the_var_was_never_set(
        self,
    ) -> None:
        exc = _requests_error(ConnectionRefusedError(61, "Connection refused"))
        d = diagnose_exception(
            exc, cfg("http://localhost:8088", ENDPOINT_SOURCE_DEFAULT)
        )
        assert d.cause == REFUSED_LOCALHOST
        assert "INDRATRACE_ENDPOINT was never set" in d.message
        assert "http://localhost:8088" in d.message

    def test_refused_on_localhost_from_the_env_var_says_nothing_listens(self) -> None:
        exc = _requests_error(ConnectionRefusedError(61, "Connection refused"))
        d = diagnose_exception(exc, cfg("http://127.0.0.1:1", ENDPOINT_SOURCE_ENV))
        assert d.cause == REFUSED_LOCALHOST
        assert "nothing is listening" in d.message
        assert "unset INDRATRACE_ENDPOINT" in d.message

    def test_refused_elsewhere_points_at_the_port(self) -> None:
        exc = _requests_error(ConnectionRefusedError(61, "Connection refused"))
        d = diagnose_exception(exc, cfg("https://ingest.example.test:8443"))
        assert d.cause == REFUSED
        assert "8443" in d.message

    def test_tls_failure_names_proxy_interception_and_the_ca_bundle(self) -> None:
        inner = ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        )
        exc = requests.exceptions.SSLError(
            urllib3.exceptions.MaxRetryError(None, "/", reason=inner)  # type: ignore[arg-type]
        )
        d = diagnose_exception(exc, cfg())
        assert d.cause == TLS
        assert "proxy" in d.message
        assert "REQUESTS_CA_BUNDLE" in d.message
        assert "Do not disable verification" in d.message

    def test_read_timeout_after_connect_is_stalled_not_egress(self) -> None:
        exc = requests.exceptions.ReadTimeout("Read timed out. (read timeout=2)")
        d = diagnose_exception(exc, cfg())
        assert d.cause == STALLED
        assert "received no response" in d.message


class TestStatusRows:
    """Every branch is a response `ingest/app.py::_handle` can produce."""

    def test_2xx_is_silent(self) -> None:
        d = diagnose_response(_Response(200), cfg())
        assert d.ok and d.message == ""

    def test_401_is_the_key_not_the_network(self) -> None:
        d = diagnose_response(
            problem(401, "unauthorized", "unknown or revoked key"), cfg()
        )
        assert d.cause == UNAUTHORIZED
        assert "HTTP 401" in d.message
        assert "unknown or revoked key" in d.message
        assert "not the network" in d.message
        assert "whitespace" in d.message and "newline" in d.message
        assert "INDRATRACE_API_KEY" in d.message

    def test_402_no_card_says_so_and_names_the_personal_email_cause(self) -> None:
        """THE other row. Verbatim title from `ingest/app.py::NO_CARD_TITLE`."""
        detail = (
            "save a card at Settings > Usage & billing to start sending - nothing "
            "is charged until the month ends"
        )
        d = diagnose_response(problem(402, NO_CARD_TITLE, detail), cfg())
        assert d.cause == NO_CARD
        assert "not an internal" in d.message
        assert "no card on file" in d.message
        assert "non-work email" in d.message or "personal" in d.message
        assert detail in d.message

    def test_402_suspended_is_told_apart_by_title(self) -> None:
        d = diagnose_response(problem(402, SUSPENDED_TITLE, "pay to resume"), cfg())
        assert d.cause == SUSPENDED
        assert "suspended" in d.message and "unpaid invoice" in d.message

    def test_402_with_an_unreadable_body_is_still_no_card(self) -> None:
        # The gateway has exactly two 402s; without a title, the common one.
        assert diagnose_response(_Response(402), cfg()).cause == NO_CARD

    @pytest.mark.parametrize("status", [404, 405])
    def test_404_405_means_not_the_gateway_and_shows_the_url(self, status: int) -> None:
        d = diagnose_response(_Response(status), cfg("https://example.test/api"))
        assert d.cause == NOT_GATEWAY
        assert "https://example.test/api" in d.message
        assert f"HTTP {status}" in d.message

    def test_429_is_not_a_misconfiguration(self) -> None:
        d = diagnose_response(problem(429, "rate limit exceeded", "slow down"), cfg())
        assert d.cause == RATE_LIMITED
        assert d.level == logging.WARNING

    def test_502_is_the_collector_and_mentions_the_dev_window(self) -> None:
        d = diagnose_response(problem(502, "collector unreachable", "x"), cfg())
        assert d.cause == COLLECTOR_DOWN
        assert "accepted the key" in d.message
        assert "00:00" in d.message and "Central" in d.message
        assert d.level == logging.WARNING

    def test_503_is_the_control_plane(self) -> None:
        d = diagnose_response(problem(503, "control plane unavailable", "x"), cfg())
        assert d.cause == CONTROL_PLANE_DOWN
        assert d.level == logging.WARNING

    def test_anything_else_is_named_as_unexpected_with_the_status(self) -> None:
        d = diagnose_response(problem(500, "boom", "detail"), cfg())
        assert d.cause == UNEXPECTED
        assert "HTTP 500" in d.message and "boom" in d.message

    def test_misconfigurations_log_at_error(self) -> None:
        for status in (401, 402, 404):
            assert diagnose_response(_Response(status), cfg()).level == logging.ERROR


# ─────────────────────────────────────────────────────────────────────────────
# The probe itself.
# ─────────────────────────────────────────────────────────────────────────────


class TestRunPreflight:
    def test_one_empty_authenticated_post_to_the_traces_url(self, post: Any) -> None:
        calls = post(_Response(200))
        d = run_preflight(cfg())
        assert d.ok
        assert len(calls) == 1
        call = calls[0]
        assert call["url"] == CLOUD + "/v1/traces"
        assert call["data"] == b""
        assert call["headers"]["x-indratrace-key"] == SECRET_KEY
        assert call["headers"]["Content-Type"] == "application/x-protobuf"

    def test_the_timeout_is_about_two_seconds(self, post: Any) -> None:
        calls = post(_Response(200))
        run_preflight(cfg())
        connect, read = calls[0]["timeout"]
        assert 1.0 <= connect <= 3.0 and 1.0 <= read <= 3.0

    def test_a_transport_exception_becomes_a_diagnosis_not_a_raise(
        self, post: Any
    ) -> None:
        post(_requests_error(ConnectionRefusedError(61, "Connection refused")))
        d = run_preflight(cfg("http://localhost:8088", ENDPOINT_SOURCE_DEFAULT))
        assert d.cause == REFUSED_LOCALHOST

    def test_honours_the_otel_certificate_setting(
        self, post: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_CERTIFICATE", "/etc/ssl/corp.pem")
        calls = post(_Response(200))
        run_preflight(cfg())
        assert calls[0]["verify"] == "/etc/ssl/corp.pem"


# ─────────────────────────────────────────────────────────────────────────────
# Modes, wired through init_observability.
# ─────────────────────────────────────────────────────────────────────────────


class TestMode:
    def test_default_is_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_PREFLIGHT, raising=False)
        assert resolve_preflight_mode() == PREFLIGHT_WARN

    @pytest.mark.parametrize("raw", ["0", "off", "false", "no", " OFF "])
    def test_off_values(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, raw)
        assert resolve_preflight_mode() == PREFLIGHT_OFF

    def test_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "strict")
        assert resolve_preflight_mode() == PREFLIGHT_STRICT

    def test_unknown_values_fall_back_to_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "loud")
        assert resolve_preflight_mode() == PREFLIGHT_WARN


class TestInitIntegration:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Any:
        _reset_for_tests()
        yield
        _reset_for_tests()

    def test_non_fatal_by_default_and_logged_once(
        self,
        post: Any,
        sdk_log: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "warn")
        post(problem(402, NO_CARD_TITLE, "save a card"))
        init_observability(api_key=SECRET_KEY, instrument_http=False)  # no raise
        errors = [r for r in sdk_log if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "no card on file" in errors[0].getMessage()

    def test_strict_raises_the_same_diagnosis(
        self, post: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "strict")
        post(problem(401, "unauthorized", "unknown or revoked key"))
        with pytest.raises(IndraTraceConfigError, match="HTTP 401"):
            init_observability(api_key=SECRET_KEY, instrument_http=False)

    def test_strict_is_a_value_error_like_the_other_config_errors(
        self, post: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "strict")
        post(problem(401, "unauthorized", "x"))
        with pytest.raises(ValueError):
            init_observability(api_key=SECRET_KEY, instrument_http=False)

    def test_off_makes_no_network_call_at_all(
        self, post: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "0")
        calls = post(AssertionError("preflight must not call the network"))
        init_observability(api_key=SECRET_KEY, instrument_http=False)
        assert calls == []

    def test_success_is_silent(
        self,
        post: Any,
        sdk_log: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "warn")
        post(_Response(200))
        init_observability(api_key=SECRET_KEY, instrument_http=False)
        assert not [r for r in sdk_log if r.levelno >= logging.WARNING]

    def test_a_crashing_probe_never_breaks_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "warn")
        monkeypatch.setattr(
            "indratrace.init.run_preflight",
            lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        init_observability(api_key=SECRET_KEY, instrument_http=False)  # no raise

    @pytest.mark.parametrize(
        "outcome",
        [
            problem(401, "unauthorized", "unknown or revoked key"),
            problem(402, NO_CARD_TITLE, "save a card"),
            _requests_error(ConnectionRefusedError(61, "Connection refused")),
            _requests_error(
                urllib3.exceptions.ConnectTimeoutError(None, "connect timeout=2")
            ),
        ],
    )
    def test_the_key_never_appears_in_any_log_line(
        self,
        outcome: Any,
        post: Any,
        sdk_log: list[logging.LogRecord],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "warn")
        post(outcome)
        init_observability(api_key=SECRET_KEY, instrument_http=False, debug=True)
        for record in sdk_log:
            assert SECRET_KEY not in record.getMessage()
            assert SECRET_KEY not in str(record.exc_text or "")
        captured = capsys.readouterr()
        assert SECRET_KEY not in captured.out + captured.err

    def test_strict_error_text_never_contains_the_key(
        self, post: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_PREFLIGHT, "strict")
        post(problem(401, "unauthorized", "unknown or revoked key"))
        with pytest.raises(IndraTraceConfigError) as info:
            init_observability(api_key=SECRET_KEY, instrument_http=False)
        assert SECRET_KEY not in str(info.value)


# ─────────────────────────────────────────────────────────────────────────────
# Export-failure surfacing.
# ─────────────────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def tracked() -> tuple[ExportHealth, _Collect, _Clock]:
    log = logging.getLogger("indratrace.test.health")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    handler = _Collect()
    log.handlers = [handler]
    clock = _Clock()
    health = ExportHealth(cfg(), threshold=3, interval=300.0, clock=clock, log=log)
    return health, handler, clock


def _errors(handler: _Collect) -> list[str]:
    return [r.getMessage() for r in handler.records if r.levelno >= logging.WARNING]


def _infos(handler: _Collect) -> list[str]:
    return [r.getMessage() for r in handler.records if r.levelno == logging.INFO]


class TestExportHealth:
    def test_n_consecutive_failures_log_exactly_one_error(self, tracked: Any) -> None:
        health, handler, _ = tracked
        for _ in range(3):
            health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        assert len(_errors(handler)) == 1
        assert "3 consecutive" in _errors(handler)[0]
        assert "HTTP 401" in _errors(handler)[0]

    def test_fewer_than_n_logs_nothing(self, tracked: Any) -> None:
        health, handler, _ = tracked
        health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        health.record_failure("logs", response=problem(401, "unauthorized", "x"))
        assert _errors(handler) == []

    def test_further_failures_inside_the_interval_log_nothing(
        self, tracked: Any
    ) -> None:
        health, handler, clock = tracked
        for _ in range(3):
            health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        clock.now += 299.0
        for _ in range(50):
            health.record_failure("metrics", response=problem(401, "unauthorized", "x"))
        assert len(_errors(handler)) == 1

    def test_after_the_interval_one_more_line_with_the_dropped_count(
        self, tracked: Any
    ) -> None:
        health, handler, clock = tracked
        for _ in range(3):
            health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        for _ in range(10):
            health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        clock.now += 300.0
        health.record_failure("traces", response=problem(401, "unauthorized", "x"))
        lines = _errors(handler)
        assert len(lines) == 2
        assert "still failing" in lines[1] and "11 more batches" in lines[1]

    def test_recovery_logs_one_info_line_and_resets(self, tracked: Any) -> None:
        health, handler, clock = tracked
        for _ in range(4):
            health.record_failure("traces", response=problem(502, "collector", "x"))
        health.record_success("traces")
        assert len(_infos(handler)) == 1
        assert "recovered" in _infos(handler)[0] and "4 failed" in _infos(handler)[0]
        # A fresh run of failures reports again from scratch — but not before N.
        health.record_failure("traces", response=problem(502, "collector", "x"))
        assert len(_errors(handler)) == 1

    def test_recovery_without_a_report_is_silent(self, tracked: Any) -> None:
        health, handler, _ = tracked
        health.record_failure("traces", response=problem(502, "collector", "x"))
        health.record_success("traces")
        assert _infos(handler) == [] and _errors(handler) == []

    def test_the_failure_carries_the_same_diagnosis_as_the_preflight(
        self, tracked: Any
    ) -> None:
        health, handler, _ = tracked
        exc = _requests_error(
            urllib3.exceptions.ConnectTimeoutError(None, "connect timeout=3")
        )
        for _ in range(3):
            health.record_failure("traces", exc=exc)
        assert "egress" in _errors(handler)[0]
        assert "firewall" in _errors(handler)[0]

    def test_no_response_and_no_exception_reads_as_egress(self, tracked: Any) -> None:
        # OTel's retry loop can exhaust its budget without ever handing us a
        # response object; that silence is the egress signature.
        health, handler, _ = tracked
        for _ in range(3):
            health.record_failure("traces")
        assert "egress" in _errors(handler)[0]

    def test_the_key_never_appears(self, tracked: Any) -> None:
        health, handler, _ = tracked
        for _ in range(3):
            health.record_failure(
                "traces",
                response=problem(401, "unauthorized", "unknown or revoked key"),
            )
        for record in handler.records:
            assert SECRET_KEY not in record.getMessage()


class TestObserveExportFeedsHealth:
    """`_observe_export` is the seam: the real exporters' `export()` outcomes
    reach the tracker whether or not `debug` is on."""

    class _Result:
        def __init__(self, name: str) -> None:
            self.name = name

    def _exporter(self, outcomes: list[Any]) -> Any:
        results = self

        class _Exporter:
            def _export(self, *args: Any, **kwargs: Any) -> Any:
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            def export(self, *args: Any, **kwargs: Any) -> Any:
                try:
                    response = self._export(b"")
                except Exception:  # noqa: BLE001 — mirrors the real exporter
                    return results._Result("FAILURE")
                return results._Result("SUCCESS" if response.ok else "FAILURE")

        return _Exporter()

    def test_failures_then_success(self, tracked: Any) -> None:
        health, handler, _ = tracked
        exporter = _observe_export(
            self._exporter(
                [
                    problem(402, NO_CARD_TITLE, "save a card"),
                    problem(402, NO_CARD_TITLE, "save a card"),
                    problem(402, NO_CARD_TITLE, "save a card"),
                    _Response(200),
                ]
            ),
            "traces",
            health,
        )
        for _ in range(4):
            exporter.export([])
        assert len(_errors(handler)) == 1
        assert "no card on file" in _errors(handler)[0]
        assert len(_infos(handler)) == 1

    def test_without_debug_no_per_batch_narration(self, tracked: Any) -> None:
        health, handler, _ = tracked
        exporter = _observe_export(self._exporter([_Response(200)]), "traces", health)
        exporter.export([])
        assert handler.records == []

    def test_a_diagnosis_is_a_frozen_value(self) -> None:
        d = Diagnosis(OK, "")
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            d.cause = DNS  # type: ignore[misc]
