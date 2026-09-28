"""Startup preflight and export-failure surfacing: say *which* thing is wrong.

The failure this module exists for is silence. OpenTelemetry's batch processors
run exports on background threads and swallow the outcome by design (ADR 0003:
never raise into the host app). So a service behind a firewall that blocks
outbound 443, a process whose `INDRATRACE_ENDPOINT` was never set, a key with a
stray newline pasted into it, an organisation with no card on file — all of them
look identical from the outside: the service runs, nothing arrives, and the
engineer blames the key.

Two mechanisms, one diagnosis table:

- **Preflight** (`run_preflight`): `init_observability` makes ONE short,
  authenticated request to the gateway before returning, and maps the outcome
  to a named cause. Non-fatal by default, ~2s, once, skippable with
  `INDRATRACE_PREFLIGHT=0`, never logs the key.
- **Export health** (`ExportHealth`): every exporter's `export()` reports its
  outcome here. After `FAILURE_THRESHOLD` consecutive failures one ERROR carries
  the same diagnosis; after that, at most one line per `REPORT_INTERVAL_SECONDS`.
  Recovery is one INFO line.

Both read the gateway's *existing* responses (`ingest/app.py` in the platform
repo): the RFC 7807 `problem+json` body's `title` distinguishes the two 402s,
and the 401 detail is deliberately generic there (key-enumeration defence), so
nothing here depends on it.

The diagnosis never contains the API key. Every message is built from the
configured endpoint, the HTTP status, and the gateway's own `title`/`detail` —
the key is sent in a header and never read back into a string.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .config import (
    API_KEY_HEADER,
    ENDPOINT_SOURCE_ENV,
    ENV_ENDPOINT,
    PREFLIGHT_READ_TIMEOUT_SECONDS,
    PREFLIGHT_TIMEOUT_SECONDS,
    ObsConfig,
)
from .version import __version__

logger = logging.getLogger("indratrace")

#: Consecutive failed export batches before the first ERROR is logged. One
#: failure is a blip (a rolling restart on the gateway); three in a row, with
#: OTel's own retries inside each, is a real outage or a misconfiguration.
FAILURE_THRESHOLD = 3

#: After the first ERROR, re-log at most this often (seconds). Five minutes:
#: the dev collector is deallocated 00:00–07:00 Central every night, so an
#: uncapped per-batch error would fill a deployed service's logs for seven
#: hours and page someone. 84 lines a night is a signal; 5,000 is a page.
REPORT_INTERVAL_SECONDS = 300.0

#: Hosts that mean "this machine". A refused connection to one of these is a
#: configuration error, not a network one.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: The two 402 titles the gateway can send, verbatim from `ingest/app.py`
#: (`NO_CARD_TITLE`, `SUSPENDED_TITLE`). Same status, same shape, different
#: cause — the title is the only thing that tells them apart.
NO_CARD_TITLE = "no card on file"
SUSPENDED_TITLE = "account suspended"

#: Causes, as stable slugs. Tests and the export-health tracker key on these;
#: the message is what a human reads.
OK = "ok"
DNS = "dns"
EGRESS_BLOCKED = "egress-blocked"
REFUSED_LOCALHOST = "refused-localhost"
REFUSED = "refused"
STALLED = "stalled"
TLS = "tls"
UNAUTHORIZED = "unauthorized"
NO_CARD = "no-card"
SUSPENDED = "suspended"
NOT_GATEWAY = "not-gateway"
RATE_LIMITED = "rate-limited"
COLLECTOR_DOWN = "collector-down"
CONTROL_PLANE_DOWN = "control-plane-down"
UNEXPECTED = "unexpected"

#: Causes that are the *caller's* to fix. The rest are on our side or transient.
MISCONFIGURATION = frozenset(
    {
        DNS,
        EGRESS_BLOCKED,
        REFUSED_LOCALHOST,
        REFUSED,
        TLS,
        UNAUTHORIZED,
        NO_CARD,
        SUSPENDED,
        NOT_GATEWAY,
    }
)


@dataclass(frozen=True)
class Diagnosis:
    """One named cause and the paragraph an engineer acts on."""

    cause: str
    message: str
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.cause == OK

    @property
    def level(self) -> int:
        """ERROR for something the caller must change; WARNING for the rest."""
        return logging.ERROR if self.cause in MISCONFIGURATION else logging.WARNING


# ─────────────────────────────────────────────────────────────────────────────
# Message building. One function per cause so each paragraph is reviewable on
# its own, and so a test can assert on the cause rather than on prose.
# ─────────────────────────────────────────────────────────────────────────────


def _host(endpoint: str) -> str:
    return urlsplit(endpoint).hostname or endpoint


def _port(endpoint: str) -> int:
    parts = urlsplit(endpoint)
    if parts.port:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _where(cfg: ObsConfig) -> str:
    """ "…as set by INDRATRACE_ENDPOINT" or "…the package default"."""
    if cfg.endpoint_source == ENDPOINT_SOURCE_ENV:
        return f"as set by {ENV_ENDPOINT}"
    return "the package default"


def _curl(cfg: ObsConfig) -> str:
    """The reachability check from docs/ENVIRONMENTS.md, against `/health`."""
    base = cfg.endpoint.rstrip("/")
    return f"curl -sS -o /dev/null -w '%{{http_code}}\\n' {base}/health"


def _dropped() -> str:
    return "Telemetry is being dropped until this is fixed."


def _msg_dns(cfg: ObsConfig) -> str:
    return (
        f"IndraTrace: the ingest hostname {_host(cfg.endpoint)!r} does not resolve "
        f"(endpoint {cfg.endpoint}, {_where(cfg)}). Check the hostname for a typo; "
        f"if it is right, this host's DNS cannot see it — a private DNS zone or a "
        f"resolver that only answers for internal names. {_dropped()}"
    )


def _msg_egress(cfg: ObsConfig) -> str:
    host, port = _host(cfg.endpoint), _port(cfg.endpoint)
    return (
        f"IndraTrace: could not open a connection to {host}:{port} within "
        f"{PREFLIGHT_TIMEOUT_SECONDS:g}s — outbound egress from this host is "
        f"blocked. A firewall, NSG, UDR or proxy in front of this service must "
        f"allow outbound HTTPS on port {port} to {host}. VNet-integrated Container "
        f"Apps, App Service and VMs behind a firewall or a route table do not have "
        f"this by default; plain ones do. If this host egresses through an HTTP "
        f"proxy, set HTTPS_PROXY. Confirm from this machine with: {_curl(cfg)} — "
        f"anything other than an HTTP status (a hang, 'no route to host') is an "
        f"egress problem, not an IndraTrace problem. {_dropped()}"
    )


def _msg_refused_localhost(cfg: ObsConfig) -> str:
    if cfg.endpoint_source == ENDPOINT_SOURCE_ENV:
        return (
            f"IndraTrace: nothing is listening at {cfg.endpoint}. {ENV_ENDPOINT} "
            f"points this process at localhost, and no ingest gateway is running "
            f"there. If you meant to send to IndraTrace, unset {ENV_ENDPOINT} so "
            f"the SDK uses the IndraTrace ingest endpoint; if you run a "
            f"self-hosted or local gateway, start it first. {_dropped()}"
        )
    return (
        f"IndraTrace: nothing is listening at {cfg.endpoint}, which is the "
        f"package's placeholder default — {ENV_ENDPOINT} was never set in this "
        f"process. Set {ENV_ENDPOINT} to your IndraTrace ingest endpoint (the "
        f"value shown next to your key when it was minted). {_dropped()}"
    )


def _msg_refused(cfg: ObsConfig) -> str:
    host, port = _host(cfg.endpoint), _port(cfg.endpoint)
    return (
        f"IndraTrace: {host} refused the connection on port {port} ({cfg.endpoint}, "
        f"{_where(cfg)}). The host is reachable but nothing is listening on that "
        f"port — check the port and scheme in the endpoint URL. {_dropped()}"
    )


def _msg_stalled(cfg: ObsConfig) -> str:
    host = _host(cfg.endpoint)
    return (
        f"IndraTrace: connected to {host} but received no response within "
        f"{PREFLIGHT_READ_TIMEOUT_SECONDS:g}s ({cfg.endpoint}, {_where(cfg)}). A proxy "
        f"or firewall is accepting the connection and dropping the request; check "
        f"for an egress proxy that only allows an allow-list, or a TLS-inspecting "
        f"appliance. {_dropped()}"
    )


def _msg_tls(cfg: ObsConfig) -> str:
    host = _host(cfg.endpoint)
    return (
        f"IndraTrace: TLS verification failed for {host} ({cfg.endpoint}). The "
        f"certificate this host was shown is not trusted by this Python — almost "
        f"always an intercepting corporate proxy re-signing outbound traffic. "
        f"Either point REQUESTS_CA_BUNDLE (or OTEL_EXPORTER_OTLP_CERTIFICATE) at "
        f"your organisation's CA bundle, or exempt {host} from interception. Do "
        f"not disable verification. {_dropped()}"
    )


def _msg_unauthorized(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the ingest gateway at {_host(cfg.endpoint)} rejected the API "
        f"key (HTTP 401{_quoted(detail)}). This is the key, not the network — the "
        f"gateway was reached. Check INDRATRACE_API_KEY in the *running* process "
        f"for stray whitespace or a trailing newline from copy-paste, that it is "
        f"the full value shown once when the key was minted, and that the key has "
        f"not been revoked or expired. {_dropped()}"
    )


def _msg_no_card(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the key is valid, but its organisation is not an internal "
        f"one and has no card on file, so its telemetry is refused (HTTP 402"
        f"{_quoted(detail)}). The usual cause is a workspace created with a "
        f"personal, non-work email address, which is not recognised as internal. "
        f"Save a card at Settings > Usage & billing, or have the workspace "
        f"re-created with a work email. {_dropped()}"
    )


def _msg_suspended(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the key is valid, but its organisation is suspended for an "
        f"unpaid invoice (HTTP 402{_quoted(detail)}). Pay at Settings > Usage & "
        f"billing to resume sending. {_dropped()}"
    )


def _msg_not_gateway(cfg: ObsConfig, status: int) -> str:
    return (
        f"IndraTrace: {cfg.endpoint} ({_where(cfg)}) answered HTTP {status} — "
        f"something is listening there, but it is not the IndraTrace ingest "
        f"gateway. The endpoint must be the gateway's base URL with no path; the "
        f"SDK appends /v1/traces itself. {_dropped()}"
    )


def _msg_rate_limited(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the gateway is rate-limiting this key or organisation (HTTP "
        f"429{_quoted(detail)}). Configuration is fine; the SDK backs off and "
        f"retries. If this persists, this service is sending faster than the "
        f"organisation's ingest quota."
    )


def _msg_collector_down(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the gateway at {_host(cfg.endpoint)} accepted the key but "
        f"cannot reach the collector behind it (HTTP 502{_quoted(detail)}). "
        f"Nothing to fix on your side. On the dev environment the collector is "
        f"deallocated 00:00–07:00 Central every day, and telemetry sent in that "
        f"window is dropped, not queued."
    )


def _msg_control_plane_down(cfg: ObsConfig, detail: str) -> str:
    return (
        f"IndraTrace: the gateway at {_host(cfg.endpoint)} could not validate the "
        f"key because its control plane is unavailable (HTTP 503{_quoted(detail)}). "
        f"Nothing to fix on your side; the SDK retries."
    )


def _msg_unexpected(cfg: ObsConfig, status: int, title: str, detail: str) -> str:
    what = title or "no problem body"
    return (
        f"IndraTrace: the ingest gateway at {_host(cfg.endpoint)} answered HTTP "
        f"{status} ({what}{_quoted(detail)}) to the startup preflight. This is not "
        f"one of the outcomes the SDK knows how to explain; check the gateway's "
        f"logs or contact support with this line."
    )


def _quoted(detail: str) -> str:
    return f": {detail}" if detail else ""


# ─────────────────────────────────────────────────────────────────────────────
# Classification.
# ─────────────────────────────────────────────────────────────────────────────


def _chain(exc: BaseException) -> list[BaseException]:
    """`exc` and everything it wraps: `__cause__`, `__context__`, urllib's
    `.reason`, and `.args` — requests/urllib3 nest the socket error three deep
    and each layer wraps it a different way."""
    seen: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if any(current is s for s in seen):
            continue
        seen.append(current)
        for attr in ("__cause__", "__context__", "reason"):
            nested = getattr(current, attr, None)
            if isinstance(nested, BaseException):
                stack.append(nested)
        for arg in getattr(current, "args", ()) or ():
            if isinstance(arg, BaseException):
                stack.append(arg)
    return seen


def _classify_exception(exc: BaseException, cfg: ObsConfig) -> Diagnosis:
    """Map a transport exception to a cause. Types first, then message text as
    a fallback (urllib3 sometimes flattens the errno into a string)."""
    chain = _chain(exc)
    text = " | ".join(str(e) for e in chain).lower()
    names = " ".join(type(e).__name__ for e in chain)

    # DNS. `socket.gaierror` is the truth; urllib3 ≥2 also names it.
    if any(isinstance(e, socket.gaierror) for e in chain) or (
        "nameresolutionerror" in names.lower()
        or "name or service not known" in text
        or "nodename nor servname" in text
        or "temporary failure in name resolution" in text
        or "getaddrinfo failed" in text
    ):
        return Diagnosis(DNS, _msg_dns(cfg))

    # TLS. Certificate verification specifically; any SSLError otherwise.
    if any(isinstance(e, ssl.SSLError) for e in chain) or (
        "sslerror" in names.lower()
        or "certificate_verify_failed" in text
        or "ssl:" in text
    ):
        return Diagnosis(TLS, _msg_tls(cfg))

    # Refused: something answered "no" at the TCP layer, so the host is
    # reachable. On loopback that means the endpoint is wrong, not the network.
    if any(isinstance(e, ConnectionRefusedError) for e in chain) or (
        "connection refused" in text or "econnrefused" in text
    ):
        if _host(cfg.endpoint) in _LOOPBACK_HOSTS:
            return Diagnosis(REFUSED_LOCALHOST, _msg_refused_localhost(cfg))
        return Diagnosis(REFUSED, _msg_refused(cfg))

    # Egress blocked: a connect that never completes, or a route that does not
    # exist. This is the one the firewall/NSG/UDR produces.
    unreachable_errnos = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT}
    if (
        any(
            isinstance(e, OSError) and getattr(e, "errno", None) in unreachable_errnos
            for e in chain
        )
        or "connecttimeout" in names.lower()
        or "no route to host" in text
        or "network is unreachable" in text
        or "connect timeout" in text
        or "timed out. (connect timeout" in text
    ):
        return Diagnosis(EGRESS_BLOCKED, _msg_egress(cfg))

    # Timed out *after* connecting: a proxy or appliance swallowing the request.
    if any(isinstance(e, (TimeoutError, socket.timeout)) for e in chain) or (
        "readtimeout" in names.lower() or "timed out" in text
    ):
        return Diagnosis(STALLED, _msg_stalled(cfg))

    # A generic connection error with no recognisable errno. Treat as egress:
    # it is the most likely cause and the message tells the reader how to
    # confirm it with curl.
    if any(isinstance(e, ConnectionError) for e in chain) or "connection" in text:
        return Diagnosis(EGRESS_BLOCKED, _msg_egress(cfg))

    return Diagnosis(
        UNEXPECTED,
        f"IndraTrace: the startup preflight to {cfg.endpoint} failed with "
        f"{type(exc).__name__}: {exc}. {_dropped()}",
    )


def _classify_status(status: int, title: str, detail: str, cfg: ObsConfig) -> Diagnosis:
    """Map an HTTP status (+ the gateway's problem body) to a cause.

    Every branch here is a response `ingest/app.py::_handle` can actually
    produce for an empty, authenticated POST — read from the source, not from
    a summary of it — plus 404/405 for "this is not the gateway at all".
    """
    if 200 <= status < 300:
        return Diagnosis(OK, "", status)
    if status == 401:
        return Diagnosis(UNAUTHORIZED, _msg_unauthorized(cfg, detail), status)
    if status == 402:
        if title == SUSPENDED_TITLE:
            return Diagnosis(SUSPENDED, _msg_suspended(cfg, detail), status)
        # `NO_CARD_TITLE`, or a 402 with a body we cannot read: the gateway has
        # exactly one other 402, and this is it.
        return Diagnosis(NO_CARD, _msg_no_card(cfg, detail), status)
    if status in (404, 405):
        return Diagnosis(NOT_GATEWAY, _msg_not_gateway(cfg, status), status)
    if status == 429:
        return Diagnosis(RATE_LIMITED, _msg_rate_limited(cfg, detail), status)
    if status == 502:
        return Diagnosis(COLLECTOR_DOWN, _msg_collector_down(cfg, detail), status)
    if status == 503:
        return Diagnosis(
            CONTROL_PLANE_DOWN, _msg_control_plane_down(cfg, detail), status
        )
    return Diagnosis(UNEXPECTED, _msg_unexpected(cfg, status, title, detail), status)


def _problem_fields(response: Any) -> tuple[str, str]:
    """`(title, detail)` from an RFC 7807 body, or empty strings.

    Only the two string fields the gateway writes are read, and both are
    truncated: the body is the server's, not ours, and it ends up in a log
    line. Never raises.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON body is not a failure
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    title = body.get("title")
    detail = body.get("detail")
    return (
        str(title)[:200] if isinstance(title, str) else "",
        str(detail)[:400] if isinstance(detail, str) else "",
    )


def diagnose_response(response: Any, cfg: ObsConfig) -> Diagnosis:
    """Diagnosis for an HTTP response object (anything with `status_code`)."""
    status = int(getattr(response, "status_code", 0) or 0)
    title, detail = _problem_fields(response) if status >= 400 else ("", "")
    return _classify_status(status, title, detail, cfg)


def diagnose_exception(exc: BaseException, cfg: ObsConfig) -> Diagnosis:
    """Diagnosis for a transport exception."""
    return _classify_exception(exc, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# The startup preflight.
# ─────────────────────────────────────────────────────────────────────────────


def run_preflight(cfg: ObsConfig) -> Diagnosis:
    """One short, authenticated request to the gateway; the outcome, named.

    An **empty** OTLP/protobuf traces export — zero bytes is a valid, empty
    `ExportTraceServiceRequest`, so the gateway runs the full path (auth →
    quota → billing door → stamp → forward) and the collector accepts nothing.
    A `GET /health` would prove only reachability: the 401 and 402 rows of the
    table are only reachable through an authenticated POST, and those two are
    the rows that matter most.

    Goes through `requests` (already a dependency: the OTLP/HTTP exporter is
    built on it), so it honours the same `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE` and
    `OTEL_EXPORTER_OTLP_CERTIFICATE` settings the real exports will — a probe
    that took a different route could disagree with the thing it is probing.

    Never raises. Never logs. The key goes out in a header and does not come
    back into any string this returns.
    """
    try:
        import requests
    except Exception as exc:  # noqa: BLE001 — cannot happen with the exporter installed
        return Diagnosis(
            UNEXPECTED,
            "IndraTrace: startup preflight skipped — `requests` is unavailable "
            f"({exc}).",
        )

    verify: bool | str = os.environ.get("OTEL_EXPORTER_OTLP_CERTIFICATE") or True
    headers = {
        API_KEY_HEADER: cfg.api_key,
        "Content-Type": "application/x-protobuf",
        "User-Agent": f"indratrace-preflight/{__version__}",
    }
    try:
        response = requests.post(
            cfg.traces_endpoint,
            data=b"",
            headers=headers,
            timeout=(PREFLIGHT_TIMEOUT_SECONDS, PREFLIGHT_READ_TIMEOUT_SECONDS),
            verify=verify,
            allow_redirects=False,
        )
    except Exception as exc:  # noqa: BLE001 — every transport failure is a diagnosis
        return diagnose_exception(exc, cfg)
    return diagnose_response(response, cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Export-failure surfacing.
# ─────────────────────────────────────────────────────────────────────────────


class ExportHealth:
    """Counts consecutive export failures across all three exporters and logs
    the diagnosis once, then at most once per interval, then recovery once.

    One instance per init, shared by traces, logs and metrics: they all point
    at one host with one key, so there is one diagnosis, and a per-signal
    tracker would log it three times.

    Thread-safe: each exporter runs on its own background thread.
    """

    def __init__(
        self,
        cfg: ObsConfig,
        *,
        threshold: int = FAILURE_THRESHOLD,
        interval: float = REPORT_INTERVAL_SECONDS,
        clock: Any = time.monotonic,
        log: logging.Logger = logger,
    ) -> None:
        self._cfg = cfg
        self._threshold = threshold
        self._interval = interval
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._consecutive = 0
        self._since_report = 0
        self._last_report_at: float | None = None
        self._reported = False

    def record_failure(
        self,
        signal: str,
        *,
        status: int | None = None,
        response: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        """One failed `export()`. Pass whichever of the three the wrapper saw."""
        with self._lock:
            self._consecutive += 1
            self._since_report += 1
            if self._consecutive < self._threshold:
                return
            now = self._clock()
            if self._reported and (
                self._last_report_at is not None
                and now - self._last_report_at < self._interval
            ):
                return
            diagnosis = self._diagnose(status, response, exc)
            dropped = self._since_report
            self._since_report = 0
            self._last_report_at = now
            first = not self._reported
            self._reported = True
        if first:
            self._log.log(
                diagnosis.level,
                "indratrace: %d consecutive export batches failed (last: %s). %s "
                "Further failures are reported at most every %d minutes.",
                dropped,
                signal,
                diagnosis.message or f"HTTP {diagnosis.status}",
                int(self._interval // 60) or 1,
            )
        else:
            self._log.log(
                diagnosis.level,
                "indratrace: exports still failing — %d more batches dropped "
                "(last: %s). %s",
                dropped,
                signal,
                diagnosis.message or f"HTTP {diagnosis.status}",
            )

    def record_success(self, signal: str) -> None:
        """One successful `export()`. Logs recovery only if we had reported."""
        with self._lock:
            failed = self._consecutive
            reported = self._reported
            self._consecutive = 0
            self._since_report = 0
            self._reported = False
            self._last_report_at = None
        if reported:
            self._log.info(
                "indratrace: exports recovered (%s) after %d failed batches",
                signal,
                failed,
            )

    def _diagnose(
        self, status: int | None, response: Any, exc: BaseException | None
    ) -> Diagnosis:
        if exc is not None:
            return diagnose_exception(exc, self._cfg)
        if response is not None and getattr(response, "status_code", None):
            return diagnose_response(response, self._cfg)
        if status is not None:
            return _classify_status(status, "", "", self._cfg)
        # No exception and no response reached us: OTel's retry loop exhausted
        # its budget without a single answer. That is the egress signature.
        return Diagnosis(EGRESS_BLOCKED, _msg_egress(self._cfg))
