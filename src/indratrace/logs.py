"""Loguru auto-bridge: make a loguru app's logs reach the pipeline, zero-config.

The stdlib log bridge in `init.py` attaches an OTel `LoggingHandler` to the
**root** stdlib logger, which is why any app using `logging` ships its records
for free. Loguru does not go through stdlib logging at all — it owns its own
`logger` object and dispatches straight to its own sinks — so a loguru-only app
was, until 0.6.0, invisible on the logs signal: it emitted nothing, with no
error to explain why.

This module closes that gap. At `init_observability`, if `loguru` is importable
we add one sink that turns each loguru record into a stdlib `logging.LogRecord`
and hands it to the OTel handler `init` already built.

**Straight to our handler, not through `logging`.** The obvious implementation —
`logging.getLogger(record.name).handle(...)` — would re-enter the stdlib logging
tree, so the record would also visit the app's *own* root handlers and get
printed a second time (loguru already printed it to stderr through its default
sink). Feeding our handler directly keeps the bridge invisible to everything but
the export path: the app's console output is untouched, and the record is
exported exactly once. It also means an app running *both* loguru and stdlib
logging never double-exports — the two paths stay disjoint, each record travels
exactly one of them.

Same INFO+ threshold as the stdlib bridge (`LOG_EXPORT_LEVEL`), applied by the
handler itself, and the same `_ExcludeIndraTrace` filter rides along on that
handler — so if the SDK ever logs through loguru, its own export diagnostics
still can't loop back into the export path.

Absent loguru: nothing is imported and nothing is paid — the import is attempted
lazily inside the function, and an `ImportError` is a normal, silent outcome.
Fail-silent throughout (ADR 0003): a broken bridge must never cost the host app
its other signals, let alone raise into it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggingHandler

logger = logging.getLogger("indratrace")

#: Loguru's `record["name"]` for a record emitted from this package — i.e. the
#: SDK's own diagnostics, should they ever be routed through loguru. Records
#: whose module path starts with this never reach the export path: a failed
#: export logs an error, and exporting that error is a loop that feeds itself.
#: (The stdlib bridge's `_ExcludeIndraTrace` filter also runs, on the handler;
#: this is the same guarantee enforced at the sink, before the record is even
#: built.)
_EXCLUDED_RECORD_PREFIXES = ("indratrace", "opentelemetry")

#: The handle loguru returns from `logger.add()`, kept so the sink can be removed
#: again — both by `_reset_for_tests` and to keep re-init idempotent. `None` when
#: no sink is installed (loguru absent, or init never ran).
_sink_id: int | None = None


def _stdlib_level(record: Any) -> int:
    """The stdlib level number for a loguru record's level.

    Loguru's built-in levels line up with stdlib's by name (DEBUG/INFO/WARNING/
    ERROR/CRITICAL), so resolving by name keeps `severity_text` on the exported
    record correct. But loguru also ships levels stdlib has never heard of —
    `SUCCESS` (25), `TRACE` (5) — and users can register their own; for those,
    `getLevelName` hands back the string `"Level 25"` rather than an int, so we
    fall back to loguru's own numeric level, which is already on the same 0–50
    scale. Either way the number is what the handler's INFO+ threshold gates on.
    """
    level = record["level"]
    by_name = logging.getLevelName(level.name)
    return by_name if isinstance(by_name, int) else int(level.no)


def _to_log_record(record: Any) -> logging.LogRecord:
    """Rebuild one loguru record as the stdlib `LogRecord` the OTel handler wants.

    Message text is taken already-formatted (`record["message"]`) and passed with
    empty `args`, so loguru's `{}`-style interpolation — `logger.info("hi {}",
    name)` — is preserved without stdlib trying to re-apply `%`-formatting to it.
    Exception info is converted from loguru's `(type, value, traceback)` record
    tuple into stdlib's `exc_info` triple, which is what makes `logger.exception`
    land on the exported record with its stack trace intact.
    """
    exception = record["exception"]
    return logging.LogRecord(
        name=record["name"] or "loguru",
        level=_stdlib_level(record),
        pathname=str(record["file"].path),
        lineno=record["line"],
        msg=record["message"],
        args=(),
        exc_info=(
            (exception.type, exception.value, exception.traceback)
            if exception
            else None
        ),
        func=record["function"],
    )


def enable_loguru_bridge(handler: LoggingHandler) -> tuple[bool, str]:
    """Forward loguru records into `handler` (the OTel log-export handler).

    Idempotent: an existing sink from a previous init is removed first, so a
    re-initializing worker never ends up exporting each record twice.

    Returns `(enabled, reason)` for the debug banner — `reason` is empty when on,
    and explains the skip when off (loguru not installed, or the sink failed to
    attach). Never raises (ADR 0003).
    """
    global _sink_id

    try:
        from loguru import logger as loguru_logger
    except ImportError:
        logger.debug("indratrace: loguru not installed; skipping the loguru bridge")
        return False, "loguru not installed"

    try:
        _remove_sink(loguru_logger)

        def sink(message: Any) -> None:
            """Called by loguru for every record at or above our threshold."""
            try:
                record = message.record
                root = str(record["name"] or "").split(".", 1)[0]
                if root in _EXCLUDED_RECORD_PREFIXES:
                    return
                # `handle()` — not `emit()` — so the handler's level and its
                # `_ExcludeIndraTrace` filter both still apply, exactly as they
                # do for a stdlib record.
                handler.handle(_to_log_record(record))
            except Exception:  # noqa: BLE001 — a log line must never break the app
                logger.debug("indratrace: loguru bridge failed", exc_info=True)

        # `level` mirrors the stdlib bridge's INFO+ export threshold, so loguru
        # DEBUG lines stay local (a firehose otherwise) and the two paths agree
        # on what "worth shipping" means. The handler re-checks it anyway.
        _sink_id = loguru_logger.add(
            sink,
            level=logging.getLevelName(handler.level),
            format="{message}",
        )
    except Exception as exc:  # noqa: BLE001 — the bridge is a bonus, never a cost
        logger.debug("indratrace: could not add the loguru sink", exc_info=True)
        return False, f"sink failed: {exc}"

    logger.debug("indratrace: loguru bridge enabled")
    return True, ""


def bridge_loguru() -> bool:
    """Re-attach the loguru bridge after your own loguru reconfiguration.

    `init_observability()` already installs the bridge, so most apps never need
    this. It exists for one specific case: loguru's `logger.remove()` — called
    with no argument, the idiomatic way to drop loguru's default stderr sink —
    removes **every** sink, ours included. An app that configures loguru *after*
    init therefore silently unbridges itself:

        init_observability(product="my-app")
        logger.remove()              # drops OUR sink too
        logger.add("app.log")        # ...and now nothing reaches IndraTrace
        bridge_loguru()              # ← put it back

    Returns True if the bridge is attached, False if it couldn't be (loguru not
    installed, or `init_observability()` never ran / failed — there is no handler
    to bridge into). Never raises into the caller (ADR 0003).
    """
    from .init import _get_log_handler

    handler = _get_log_handler()
    if handler is None:
        logger.debug(
            "indratrace: bridge_loguru() called before a successful "
            "init_observability(); nothing to bridge into"
        )
        return False

    enabled, _reason = enable_loguru_bridge(handler)
    return enabled


def _remove_sink(loguru_logger: Any) -> None:
    """Drop the sink we added, if any. Tolerates loguru having dropped it already."""
    global _sink_id

    if _sink_id is None:
        return
    try:
        loguru_logger.remove(_sink_id)
    except Exception:  # noqa: BLE001 — already removed / loguru reset by the app
        pass
    _sink_id = None


def _disable_loguru_bridge() -> None:
    """Undo `enable_loguru_bridge`. Not public API — for `_reset_for_tests`.

    Loguru's `logger` is a process-wide singleton, so a test that inits must be
    able to take its sink back off, or the sink leaks into the next test and
    exports into a torn-down provider.
    """
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return
    _remove_sink(loguru_logger)
