"""init_observability(): provider + exporter + auto-instrumentation wiring.

All three signals — traces, logs, metrics (ADR 0006). Hard rules: idempotent,
fail-silent, no policy (see docs/architecture.md).
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterable
from typing import Any, Protocol

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .agent_sdk import (
    _disable_agent_sdk_instrumentation,
    enable_agent_sdk_instrumentation,
)
from .config import (
    ObsConfig,
    build_resource,
    resolve_capture_content,
    resolve_config,
    resolve_debug,
)
from .context import SessionSpanProcessor
from .genai import _uninstrument_genai, enable_genai_instrumentation
from .logs import _disable_loguru_bridge, enable_loguru_bridge
from .version import __version__
from .web import _uninstrument_http, enable_http_instrumentation

logger = logging.getLogger("indratrace")

#: Product name in banner + log lines. The company behind the SDK; handy when a
#: user greps their console and wants to know *what* is emitting these lines.
_PRODUCT_NAME = "IndraTrace"

#: Level at or above which stdlib log records ship to the platform. DEBUG would
#: be a firehose; INFO is what products already consider worth logging.
LOG_EXPORT_LEVEL = logging.INFO

#: Top-level logger names never shipped to the platform. Both emit *about* the
#: export path, so exporting them risks a loop that feeds itself: a failed
#: export logs an error, which becomes a record to export, which fails...
_EXPORT_EXCLUDED_LOGGERS = frozenset({"indratrace", "opentelemetry"})

# Module state guarding idempotency. `init_observability` is safe to call from
# an import-time module that gets imported twice, or from a reloading worker.
# A *failed* init leaves these unset, so the caller may retry.
_initialized = False
_provider: TracerProvider | None = None
_logger_provider: LoggerProvider | None = None
_meter_provider: MeterProvider | None = None
_log_handler: LoggingHandler | None = None
#: Root logger level before `log_level` overrode it, so tests can put it back.
_root_level_before: int | None = None
#: The console handler `debug=True` attached to the `indratrace` logger, if any,
#: so `_reset_for_tests` can detach it and restore the logger's prior level.
_debug_handler: logging.Handler | None = None
_debug_level_before: int | None = None


class _Shutdownable(Protocol):
    """The one thing all three providers have in common."""

    def shutdown(self) -> None: ...


class _ExcludeIndraTrace(logging.Filter):
    """Keep the SDK's own records off the log-export path.

    A failed export makes OTel log an error; exporting that error produces
    another record to export. Filtering here — rather than setting
    `propagate = False` on the `indratrace` logger — keeps the exclusion scoped
    to our handler, so an operator who attaches their own handler to that
    logger still sees the SDK's diagnostics.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        root = record.name.split(".", 1)[0]
        return root not in _EXPORT_EXCLUDED_LOGGERS


def _shutdown_quietly(providers: Iterable[_Shutdownable | None]) -> None:
    """Best-effort teardown. Used on the partial-init path and by tests.

    Each provider's shutdown flushes its exporter, bounded by the timeout we
    pinned in config — a dead collector must not stall this (ADR 0003).
    """
    for provider in providers:
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001 — teardown never raises
            logger.debug("indratrace: provider shutdown failed", exc_info=True)


def _audible_export(exporter: Any, signal: str) -> Any:
    """Wrap an OTLP exporter's `export()` so each attempt logs its outcome.

    The exporters are async and batched, so a delivery failure would otherwise
    surface only inside OpenTelemetry's own logger — invisible to a user who
    turned `debug=True` on the `indratrace` logger. This wrapper makes the
    outcome *audible* under our logger instead: an ``export ok`` line at DEBUG on
    success, and a clear ``export FAILED`` WARNING with the reason on failure.
    Behavior is unchanged (it returns the real result and re-raises nothing new)
    — it only narrates, which is the whole point of debug mode (ADR 0003:
    fail-silent becomes fail-audible, never fail-loud into the app).

    Only used when `debug` is on; a normal init leaves the exporter untouched so
    the hot export path pays nothing.
    """
    real_export = exporter.export

    def export(*args: Any, **kwargs: Any) -> Any:
        try:
            result = real_export(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — narrate, never change behavior
            logger.warning(
                "indratrace: %s export FAILED: %s", signal, exc, exc_info=True
            )
            raise
        # The three result enums all name their success member SUCCESS; anything
        # else is a drop (dead collector, 4xx, timeout).
        if getattr(result, "name", None) == "SUCCESS":
            logger.debug("indratrace: %s export ok", signal)
        else:
            logger.warning(
                "indratrace: %s export FAILED (%s) — is the collector reachable "
                "at the configured endpoint?",
                signal,
                getattr(result, "name", result),
            )
        return result

    exporter.export = export
    return exporter


def _build_tracer_provider(
    cfg: ObsConfig, resource: Resource, debug: bool = False
) -> TracerProvider:
    """Spans over OTLP/HTTP, batched in the background."""
    provider = TracerProvider(resource=resource)
    # Stamp session.id/user.id from baggage onto every span at start (see
    # context.py). Added before the exporter so the attributes are present by
    # the time the span ends and is batched out. `on_start` only, so it costs
    # nothing on the export path.
    provider.add_span_processor(SessionSpanProcessor())
    exporter = OTLPSpanExporter(
        endpoint=cfg.traces_endpoint,
        headers=cfg.headers,
        timeout=cfg.export_timeout_seconds,
    )
    if debug:
        _audible_export(exporter, "traces")
    # Batched + async: a dead collector drops spans, it never blocks a request
    # or raises into the caller.
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _build_logger_provider(
    cfg: ObsConfig, resource: Resource, debug: bool = False
) -> LoggerProvider:
    """Log records over OTLP/HTTP, batched in the background, same resource."""
    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(
        endpoint=cfg.logs_endpoint,
        headers=cfg.headers,
        timeout=cfg.export_timeout_seconds,
    )
    if debug:
        _audible_export(exporter, "logs")
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    return provider


def _build_meter_provider(
    cfg: ObsConfig, resource: Resource, debug: bool = False
) -> MeterProvider:
    """Metrics over OTLP/HTTP on a periodic reader, same resource.

    v0.1 ships no custom-metric API (ADR 0006); this provider exists so
    auto-instrumentation metrics — request count, latency — have somewhere to
    go. The reader's export interval is OTel's default (60s).
    """
    exporter = OTLPMetricExporter(
        endpoint=cfg.metrics_endpoint,
        headers=cfg.headers,
        timeout=cfg.export_timeout_seconds,
    )
    if debug:
        _audible_export(exporter, "metrics")
    reader = PeriodicExportingMetricReader(
        exporter,
        # Bound each periodic export. Note this does *not* bound `shutdown()`,
        # which uses its own `timeout_millis` (30s by default) — what keeps
        # process exit fast against a dead collector is the exporter's own
        # `timeout` above. Don't drop that one. (ADR 0003.)
        export_timeout_millis=cfg.export_timeout_seconds * 1000,
    )
    return MeterProvider(resource=resource, metric_readers=[reader])


def _attach_log_handler(
    provider: LoggerProvider, log_level: int | str | None
) -> tuple[LoggingHandler, int | None]:
    """Bridge stdlib `logging` into OTel, so `logger.info(...)` ships as a record.

    The handler goes on the **root** logger: products log through their own
    named loggers, whose records propagate up to root. A record emitted inside
    a span picks up that span's trace context automatically — which is what
    links a log line to its trace.

    The root logger's **level** changes only when the caller explicitly asks,
    via `log_level`. That level gates records before any handler sees them, so
    lowering it would make every *other* root handler — the app's console, its
    log file — start emitting records the app had deliberately suppressed. The
    SDK observes the host app; it does not silently reconfigure it. An app that
    already runs at INFO (the common case: `basicConfig`, uvicorn, gunicorn)
    ships its INFO records with no argument at all.

    The handler drops the SDK's own records (`_ExcludeIndraTrace`): a failing
    export makes OTel log an error, and turning that error into another record
    to export is a loop that feeds itself. Filtering on the handler keeps the
    exclusion scoped to the export path, so an operator's own handler on the
    `indratrace` logger still sees those diagnostics.

    Returns the handler, plus the root level as it was before `log_level`
    replaced it (`None` when we left it alone) so teardown can restore it.
    """
    with warnings.catch_warnings():
        # The SDK's own handler is deprecated in favour of one that lives in
        # `opentelemetry-instrumentation-logging` — a dependency core may not
        # take (ADR 0003: OTel deps only). Under `python -W error`, common in
        # CI, this DeprecationWarning would otherwise raise and cost the host
        # app *all three* signals.
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        handler = LoggingHandler(level=LOG_EXPORT_LEVEL, logger_provider=provider)

    handler.addFilter(_ExcludeIndraTrace())

    root = logging.getLogger()
    root.addHandler(handler)

    root_level_before: int | None = None
    if log_level is not None:
        root_level_before = root.level
        root.setLevel(log_level)

    return handler, root_level_before


#: Prefix on the debug console handler's lines, so a user who turned debug on
#: can tell IndraTrace's diagnostics apart from their own app's output at a
#: glance. `%(levelname)s` keeps the export-failure lines visibly WARNING/ERROR.
_DEBUG_LOG_FORMAT = "indratrace [%(levelname)s] %(message)s"


def _enable_debug_logging() -> tuple[logging.Handler | None, int | None]:
    """Make the SDK's diagnostics *audible* on the console (`debug=True`).

    Attaches a `StreamHandler` at DEBUG to the `indratrace` logger and lowers
    that logger to DEBUG so the banner + export lines actually surface. This is
    the whole point of the flag: fail-silent stays fail-silent for the host app,
    but its failures become visible to the operator who asked to see them.

    **Only if the `indratrace` logger has no handlers of its own** — an operator
    who already attached one (to route diagnostics into their own logging setup)
    gets no duplicate console line from us. Returns `(handler, level_before)`;
    both `None` when we attached nothing, so teardown can be exact.

    Note this handler goes on the `indratrace` logger directly, not root, and
    that logger's records are filtered *off* the OTLP log-export path
    (`_ExcludeIndraTrace`) — so turning debug on never ships the SDK's own noise
    to the platform, it only prints it locally.
    """
    sdk_logger = logging.getLogger("indratrace")
    if sdk_logger.handlers:
        # Someone owns this logger's output already; don't double-log.
        level_before = sdk_logger.level
        if sdk_logger.level > logging.DEBUG or sdk_logger.level == logging.NOTSET:
            sdk_logger.setLevel(logging.DEBUG)
            return None, level_before
        return None, None

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_DEBUG_LOG_FORMAT))
    sdk_logger.addHandler(handler)

    level_before = sdk_logger.level
    sdk_logger.setLevel(logging.DEBUG)
    return handler, level_before


def _banner_lines(
    cfg: ObsConfig,
    http_statuses: list[tuple[str, bool, str]],
    loguru_status: tuple[bool, str],
    genai_statuses: list[tuple[str, bool, str]],
    agent_sdk_status: tuple[bool, str] | None,
    capture_content: bool,
) -> list[str]:
    """The `debug=True` startup banner, one list entry per line.

    Reports the resolved identity (version, product, env, endpoint) and, for
    every optional integration, whether it turned on and — when it didn't — the
    reason (extra not installed, load/instrument failure). That last part is the
    lesson of the prompt-08 silent failure: a user staring at an empty dashboard
    needs to see *"claude-agent-sdk: skipped (extra not installed)"* to know why.
    """

    def status(enabled: bool, reason: str) -> str:
        return "enabled" if enabled else f"skipped ({reason or 'unavailable'})"

    lines = [
        f"{_PRODUCT_NAME} SDK v{__version__} initialized",
        f"  product={cfg.product} env={cfg.env} service={cfg.service_name}",
        f"  endpoint={cfg.endpoint} (traces={cfg.traces_endpoint})",
        f"  api_key={'set' if cfg.api_key else 'unset (no auth header)'} "
        f"capture_content={'on' if capture_content else 'off'}",
        "  signals: traces + logs + metrics (OTLP/HTTP, batched)",
    ]
    for framework, enabled, reason in http_statuses:
        lines.append(f"  http[{framework}]: {status(enabled, reason)}")
    lines.append(f"  loguru: {status(*loguru_status)}")
    for provider, enabled, reason in genai_statuses:
        lines.append(f"  genai[{provider}]: {status(enabled, reason)}")
    if agent_sdk_status is not None:
        lines.append(f"  claude-agent-sdk: {status(*agent_sdk_status)}")
    return lines


def _debug_connectivity_probe(tracer_provider: TracerProvider) -> None:
    """Emit one startup span and flush it, so debug reports reachability *now*.

    The exporters are batched, so with no telemetry emitted a debug run would
    otherwise print the banner and then say nothing about whether the collector
    is actually reachable — the user is back to guessing. This emits a single
    `indratrace.startup` span and `force_flush`es the tracer provider, which
    drives the wrapped exporter synchronously and thus logs the `export ok` /
    `export FAILED` line during init. That failure line, against a dead
    endpoint, is the acceptance criterion this whole flag exists to satisfy.

    Fail-silent: any error here is swallowed at debug — the probe is a
    diagnostic courtesy, never a prerequisite for a working init.
    """
    try:
        tracer = tracer_provider.get_tracer("indratrace")
        with tracer.start_as_current_span("indratrace.startup"):
            pass
        # Bounded by the exporter timeout we pinned (cfg.export_timeout_seconds),
        # so a dead collector reports its failure fast rather than stalling init.
        tracer_provider.force_flush()
    except Exception:  # noqa: BLE001 — a probe must never break init
        logger.debug("indratrace: debug connectivity probe failed", exc_info=True)


def init_observability(
    product: str | None = None,
    env: str | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    service_name: str | None = None,
    service_version: str | None = None,
    instrument_http: bool = True,
    log_level: int | str | None = None,
    capture_content: bool | None = None,
    debug: bool | None = None,
    ingest_key: str | None = None,
    instrument_fastapi: bool | None = None,
) -> None:
    """Wire OpenTelemetry to ship telemetry to IndraTrace. Call once, at startup.

    Sets up all three signals: traces, logs (stdlib `logging` — and `loguru` —
    bridged into OTel, carrying trace context), and metrics. Config precedence is
    explicit args > `INDRATRACE_*` env vars > defaults (see docs/conventions.md).
    Everything exports over OTLP/HTTP from background batchers, authenticated
    with the `x-indratrace-key` header.

    Args:
        api_key: The IndraTrace API key. When set, it is sent on every export as
            the `x-indratrace-key` header; leave it unset (the default) and no
            auth header is sent. Resolves from the `INDRATRACE_API_KEY` env var
            when omitted.
        ingest_key: **Deprecated** alias for `api_key` (renamed in v0.5.0), still
            accepted for backward compatibility. Passing it — or the
            `INDRATRACE_KEY` env var — emits a single `DeprecationWarning`. If
            both are given, `api_key` wins and the warning still fires.
        instrument_http: Whether to auto-instrument the web frameworks whose
            extras are installed — FastAPI, Django, and Flask — so every HTTP
            request becomes a server span. On by default; an absent extra is a
            silent skip, so this costs nothing if your app is not a web app. Two
            placement caveats that produce *silent* zero-span outcomes: **Django**
            works by inserting middleware into `settings.MIDDLEWARE`, so init must
            run before `get_wsgi_application()` (top of `wsgi.py`/`asgi.py`); and
            **Flask** works by replacing the `flask.Flask` class, so an app built
            from a `from flask import Flask` name bound before init needs
            `instrument_flask_app(app)`. Both are covered in the README.
        instrument_fastapi: **Deprecated** alias for `instrument_http` (renamed in
            v0.6.0, when Django and Flask joined FastAPI). Still honored — it
            gates all three frameworks, not just FastAPI. If both are given,
            `instrument_http` wins.
        log_level: If set, the root logger's level is lowered to this so that
            records at or above it reach the export path. Leave it `None` (the
            default) and the SDK will not touch your logging config: records
            already passing your root logger's level still ship. Pass e.g.
            `"INFO"` if your app never configured logging and would otherwise
            emit nothing below WARNING. Note this also affects your app's own
            console and file handlers — it is the stdlib root level.
        capture_content: Whether to record prompt and completion **text** on
            GenAI model spans. Off by default because prompts carry customer
            data — the typical use is on in dev/staging, off in prod. Leave it
            `None` (the default) to resolve from `INDRATRACE_CAPTURE_CONTENT`
            (truthy: `1/true/yes/on`), else off. Token counts are captured
            regardless of this flag; it gates only the raw text.
        debug: Turn on SDK diagnostics. When `True`, a console handler is
            attached to the `indratrace` logger at DEBUG (only if that logger
            has no handlers of its own — no double-logging), a startup banner is
            printed (version, product, env, endpoint, and which optional
            integrations turned on or were skipped and why), and export
            success/failure lines become visible. This does **not** weaken
            fail-silence: failures still never raise into your app — they just
            become *audible*. Leave it `None` (the default) to resolve from
            `INDRATRACE_DEBUG` (truthy: `1/true/yes/on`), else off. Use it when
            nothing is showing up in your dashboard and you want to see why.

    This never raises and never blocks the host app (ADR 0003). If wiring
    fails — bad config, unreachable collector, missing dependency — it logs a
    single warning and leaves the app un-instrumented. Calling it twice is a
    no-op.
    """
    global _initialized, _provider, _logger_provider, _meter_provider
    global _log_handler, _root_level_before
    global _debug_handler, _debug_level_before

    if _initialized:
        logger.debug("init_observability() already called; ignoring")
        return

    # `instrument_fastapi` is the pre-0.6.0 name, from when FastAPI was the only
    # web framework we instrumented. It now gates all three; the new name wins if
    # both are passed. No DeprecationWarning: unlike `ingest_key`, the old name
    # was almost always passed as `False` by tests/scripts to *disable* HTTP
    # instrumentation, and warning at them buys the user nothing.
    if instrument_fastapi is not None and instrument_http:
        instrument_http = instrument_fastapi

    # Resolve + wire debug *first*, before anything that can fail: a missing
    # `product` raises inside resolve_config, and the operator who asked for
    # diagnostics should still see *that* failure narrated on the console.
    debug_on = resolve_debug(debug)
    if debug_on:
        _debug_handler, _debug_level_before = _enable_debug_logging()

    # Track what got built so a failure part-way through doesn't strand
    # background exporter threads owned by nothing.
    built: list[_Shutdownable] = []
    try:
        cfg = resolve_config(
            product=product,
            env=env,
            api_key=api_key,
            ingest_key=ingest_key,
            endpoint=endpoint,
            service_name=service_name,
            service_version=service_version,
        )

        # One Resource for all three providers. Built once, not per provider:
        # `Resource.create` runs the OTel detectors, and a detector that varies
        # per call (`service.instance.id` is one) would stamp traces, logs, and
        # metrics with different identities and break correlation.
        resource = build_resource(cfg)

        tracer_provider = _build_tracer_provider(cfg, resource, debug=debug_on)
        built.append(tracer_provider)
        logger_provider = _build_logger_provider(cfg, resource, debug=debug_on)
        built.append(logger_provider)
        meter_provider = _build_meter_provider(cfg, resource, debug=debug_on)
        built.append(meter_provider)

        # Everything that can fail happens *before* the globals are published.
        # `set_*_provider` is once-per-process and cannot be taken back, so
        # publishing first would leave the process pinned to providers we then
        # shut down on the failure path — and a retry could never replace them.
        log_handler, root_level_before = _attach_log_handler(logger_provider, log_level)

        trace.set_tracer_provider(tracer_provider)
        _logs.set_logger_provider(logger_provider)
        metrics.set_meter_provider(meter_provider)

        # Past this point the globals are frozen, so a failure here must not
        # unwind the signals: HTTP instrumentation is a bonus, not a
        # prerequisite. Absent extra is already handled inside. Each integration
        # returns its enabled/skipped(reason) status for the debug banner.
        #
        # FastAPI, Django, and Flask, each behind its own extra and each handed
        # OUR tracer provider (web.py). Django's must land before its middleware
        # chain is built and Flask's misses pre-imported `Flask` names — both
        # documented there and in the README, neither detectable from here.
        http_statuses: list[tuple[str, bool, str]] = []
        if instrument_http:
            try:
                http_statuses = enable_http_instrumentation(tracer_provider)
            except Exception:  # noqa: BLE001 — HTTP spans are optional
                logger.warning(
                    "indratrace: HTTP auto-instrumentation failed; "
                    "other signals are unaffected",
                    exc_info=True,
                )

        # Loguru: bridge its records into the OTel log handler we just attached,
        # so a loguru-only app's logs reach the pipeline with no configuration at
        # all (loguru bypasses stdlib logging entirely, so without this it emits
        # nothing). Absent loguru is a silent skip inside; fail-silent, and
        # idempotent across re-init.
        loguru_status: tuple[bool, str] = (False, "loguru not installed")
        try:
            loguru_status = enable_loguru_bridge(log_handler)
        except Exception as exc:  # noqa: BLE001 — the log bridge is a bonus
            loguru_status = (False, f"bridge failed: {exc}")
            logger.warning(
                "indratrace: loguru bridge failed; other signals are unaffected",
                exc_info=True,
            )

        # GenAI instrumentors, handed OUR provider (not the frozen global) so
        # model spans nest under the same trace as the agent/tool spans. Also a
        # bonus, and fail-silent per instrumentor inside — a missing extra or a
        # broken provider integration must not cost the app its other signals.
        genai_statuses: list[tuple[str, bool, str]] = []
        try:
            genai_statuses = enable_genai_instrumentation(
                tracer_provider,
                capture_content=resolve_capture_content(capture_content),
            )
        except Exception:  # noqa: BLE001 — model spans are optional
            logger.warning(
                "indratrace: GenAI auto-instrumentation failed; "
                "other signals are unaffected",
                exc_info=True,
            )

        # Claude Agent SDK auto-instrumentation (ADR 0008), handed OUR provider
        # so its agent/turn/tool spans nest in the same trace. Absent extra is a
        # silent skip inside; also a bonus, so a failure here must not unwind the
        # signals — the whole agent-loop feature is optional.
        agent_sdk_status: tuple[bool, str] | None = None
        try:
            agent_sdk_status = enable_agent_sdk_instrumentation(tracer_provider)
        except Exception as exc:  # noqa: BLE001 — agent-sdk spans are optional
            agent_sdk_status = (False, f"instrument failed: {exc}")
            logger.warning(
                "indratrace: Claude Agent SDK auto-instrumentation failed; "
                "other signals are unaffected",
                exc_info=True,
            )

        _provider = tracer_provider
        _logger_provider = logger_provider
        _meter_provider = meter_provider
        _log_handler = log_handler
        _root_level_before = root_level_before
        _initialized = True

        # Init success, with the resolved endpoint — the single most useful line
        # for "is it even pointed at the right place?". At INFO so it surfaces
        # under debug (and to any operator handler on the `indratrace` logger)
        # without a full banner.
        logger.info(
            "indratrace initialized: product=%s env=%s endpoint=%s",
            cfg.product,
            cfg.env,
            cfg.endpoint,
        )
        if debug_on:
            for line in _banner_lines(
                cfg,
                http_statuses,
                loguru_status,
                genai_statuses,
                agent_sdk_status,
                resolve_capture_content(capture_content),
            ):
                logger.debug(line)
            _debug_connectivity_probe(tracer_provider)
    except Exception:  # noqa: BLE001 — fail-silent is the whole point (ADR 0003)
        _shutdown_quietly(built)
        logger.warning(
            "indratrace: observability setup failed; the app will run "
            "un-instrumented",
            exc_info=True,
        )


def _get_provider() -> TracerProvider | None:
    """The tracer provider this SDK built, or None if init failed/never ran.

    Not public API. Tests need it because OTel allows each *global* provider to
    be set only once per process, so `trace.get_tracer_provider()` goes stale
    after the first init in a test session. Same for the two accessors below.
    """
    return _provider


def _get_logger_provider() -> LoggerProvider | None:
    """The logger provider this SDK built, or None. Not public API."""
    return _logger_provider


def _get_meter_provider() -> MeterProvider | None:
    """The meter provider this SDK built, or None. Not public API."""
    return _meter_provider


def _get_log_handler() -> LoggingHandler | None:
    """The OTel log handler this SDK attached to root, or None. Not public API.

    `bridge_loguru()` needs it to re-attach the loguru sink after an app's own
    `logger.remove()` dropped it.
    """
    return _log_handler


def _reset_for_tests() -> None:
    """Tear down module state so a test can init again. Not public API."""
    global _initialized, _provider, _logger_provider, _meter_provider
    global _log_handler, _root_level_before
    global _debug_handler, _debug_level_before

    root = logging.getLogger()
    if _log_handler is not None:
        root.removeHandler(_log_handler)
    if _root_level_before is not None:
        root.setLevel(_root_level_before)

    # Undo the debug console handler on the `indratrace` logger, else every
    # debug-mode test in a session leaves another StreamHandler behind (and the
    # logger pinned at DEBUG).
    sdk_logger = logging.getLogger("indratrace")
    if _debug_handler is not None:
        sdk_logger.removeHandler(_debug_handler)
    if _debug_level_before is not None:
        sdk_logger.setLevel(_debug_level_before)

    # Take the loguru sink off before the providers go: loguru's `logger` is a
    # process-wide singleton, so a leaked sink would keep feeding records into a
    # handler whose provider is being torn down.
    _disable_loguru_bridge()

    _shutdown_quietly((_provider, _logger_provider, _meter_provider))

    _uninstrument_http()
    _uninstrument_genai()
    _disable_agent_sdk_instrumentation()

    _initialized = False
    _provider = None
    _logger_provider = None
    _meter_provider = None
    _log_handler = None
    _root_level_before = None
    _debug_handler = None
    _debug_level_before = None
