"""Claude Agent SDK auto-instrumentation — zero decorators (ADR 0008).

Apps built on Anthropic's ``claude-agent-sdk`` get their whole agent loop traced
from the single ``init_observability()`` call: a span per agent run, per turn,
and per tool/MCP call, plus model token usage — nested, session-aware, and
aligned with the same conventions as the rest of the SDK. This is the SDK's
differentiating feature.

**How (ADR 0008).** The Agent SDK runs the agent loop in a *subprocess* ``claude``
CLI, not in-process — it never imports the ``anthropic`` client — so the
OpenLLMetry anthropic instrumentor (ADR 0005) is blind to it. We therefore read
usage off the SDK's own message objects, and combine two interception layers:

1. **Wrap the message iteration** at the *method* both entrypoints funnel through
   — ``InternalClient.process_query`` (what ``query()`` calls) and the
   ``ClaudeSDKClient`` ``receive_*`` methods. We patch **methods**, not the
   module-level ``query`` reference, because ``from claude_agent_sdk import
   query`` captures the function *by value* at import time — usually *before*
   ``init_observability()`` runs — so rebinding the module attribute would leave
   that caller untraced. Method lookup happens at call time, so this intercepts
   regardless of import order (the discipline the OTel instrumentors use). An
   ``AssistantMessage`` is a turn (its ``.usage``/``.model`` give the turn's
   tokens); the terminal ``ResultMessage`` gives the run totals. Opening the
   agent span here lets us emit a ``turn`` child per assistant message, stamp
   usage, and close everything in a ``finally`` so an early-abandoned stream
   leaves no dangling span. This is the one place we touch an ``_internal`` seam;
   the extra's version floor is pinned tightly and the seam is looked up
   defensively so a rename degrades to a silent skip.

2. **Official hooks** for tools — ``PreToolUse`` opens a ``tool`` span,
   ``PostToolUse`` closes it, ``PostToolUseFailure`` marks it ERROR. Our hooks
   are *merged into* whatever the app already passed (never clobbered) and are
   observe-only (each returns an empty hook output). MCP tools appear as
   ``mcp__<server>__<tool>``; the server name is stamped as ``tool.mcp_server``.

Enabled behind the same try-import guard as the GenAI instrumentors: absent
extra ⇒ silent skip; any failure ⇒ fail-silent (ADR 0003), never touching the
host app's control flow. Raw token counts only, no cost math (ADR 0005).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("indratrace")

#: Instrumentation scope reported on every span this module emits — same as the
#: decorators', so agent-sdk spans share a scope with `@trace_agent` etc.
INSTRUMENTATION_SCOPE = "indratrace"

#: Span-kind marker (docs/conventions.md). New kinds `agent`/`turn` for this
#: framework; `tool` matches the decorator tool kind.
SPAN_KIND_ATTRIBUTE = "indratrace.span.kind"
AGENT_SPAN_KIND = "agent"
TURN_SPAN_KIND = "turn"
TOOL_SPAN_KIND = "tool"

#: Framework identity stamped on every agent-sdk span (docs/conventions.md), so
#: the platform can select exactly this feature's spans.
AGENT_FRAMEWORK_ATTRIBUTE = "agent.framework"
AGENT_FRAMEWORK = "claude-agent-sdk"

AGENT_NAME_ATTRIBUTE = "agent.name"
TOOL_NAME_ATTRIBUTE = "tool.name"
TOOL_MCP_SERVER_ATTRIBUTE = "tool.mcp_server"
SESSION_ID_ATTRIBUTE = "session.id"

#: Canonical gen_ai usage names (docs/conventions.md § Model spans). We stamp the
#: SAME names the GenAI instrumentors emit, read here from the Agent SDK message
#: objects rather than from an instrumentor. Raw counts only (ADR 0005).
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"

#: MCP tools surface under the Claude Code convention `mcp__<server>__<tool>`.
_MCP_PREFIX = "mcp__"

#: Guard so `enable`/`disable` are idempotent and `disable` only unpatches what
#: we patched (mirrors the GenAI instrumentors' `is_instrumented` guard). We hold
#: the original methods so `disable` restores them exactly (patching methods, not
#: the module-level `query` reference — see the entrypoint section note).
_patched = False
_orig_process_query: Any = None
_orig_client_init: Any = None
_orig_client_receive_response: Any = None
_orig_client_receive_messages: Any = None

#: Set by `enable_agent_sdk_instrumentation`; the provider our spans go to (the
#: one `init_observability` built, never the frozen global — architecture.md).
_tracer_provider: TracerProvider | None = None


def _get_tracer() -> trace.Tracer:
    """The tracer for agent-sdk spans — our provider, else the global fallback.

    Prefers the provider handed to `enable_agent_sdk_instrumentation` because
    OTel freezes the *global* provider at the first `set_tracer_provider`
    (architecture.md, "Testing notes"). Falls back to the global API, which
    yields non-recording spans when nothing was initialized.
    """
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(INSTRUMENTATION_SCOPE)
    return trace.get_tracer(INSTRUMENTATION_SCOPE)


# ---------------------------------------------------------------------------
# Token usage — read off the Agent SDK message objects (ADR 0008)
# ---------------------------------------------------------------------------

#: Map the Anthropic-shaped usage keys the CLI passes through verbatim onto our
#: canonical `gen_ai.usage.*` attribute names (docs/conventions.md).
_USAGE_KEY_MAP: tuple[tuple[str, str], ...] = (
    ("input_tokens", GEN_AI_USAGE_INPUT_TOKENS),
    ("output_tokens", GEN_AI_USAGE_OUTPUT_TOKENS),
    ("cache_read_input_tokens", GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS),
    ("cache_creation_input_tokens", GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS),
)


def _stamp_usage(span: Span, usage: dict[str, Any] | None) -> None:
    """Stamp canonical `gen_ai.usage.*` from a raw Agent SDK usage dict.

    The dict is passed through verbatim from the CLI/Anthropic API, so its keys
    are the Anthropic shape. Missing keys are simply skipped; a total is derived
    from input+output only when both are present (the provider doesn't always
    send one). Raw counts only — no cost (ADR 0005). Never raises (ADR 0003).
    """
    if not usage:
        return
    try:
        input_tokens: int | None = None
        output_tokens: int | None = None
        for raw_key, attr in _USAGE_KEY_MAP:
            value = usage.get(raw_key)
            if value is None:
                continue
            value = int(value)
            span.set_attribute(attr, value)
            if raw_key == "input_tokens":
                input_tokens = value
            elif raw_key == "output_tokens":
                output_tokens = value
        if input_tokens is not None and output_tokens is not None:
            span.set_attribute(
                GEN_AI_USAGE_TOTAL_TOKENS, input_tokens + output_tokens
            )
    except Exception:  # noqa: BLE001 — instrumentation must never break the app
        logger.debug("indratrace: agent-sdk usage stamping failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tool spans via official hooks (ADR 0008)
# ---------------------------------------------------------------------------


class _ToolSpans:
    """Open/close `tool` spans keyed by `tool_use_id`, driven by hook callbacks.

    One instance per agent run, parented under that run's agent span so tool
    spans nest correctly. The hooks are observe-only: each returns an empty
    output so it never affects permissioning or the SDK's control flow.

    Failure-silent throughout (ADR 0003): a hook must never raise back into the
    CLI dispatch loop, and a missing PostToolUse (e.g. an interrupted run) just
    leaves the span to be closed when the run ends.
    """

    def __init__(self, agent_context: Any) -> None:
        # The OTel context active when the agent span was current, so tool spans
        # parent under the agent span even though hooks fire from the SDK's own
        # task, where that span is not the current one.
        self._agent_context = agent_context
        self._open: dict[str, Span] = {}

    def _start(self, tool_name: str, tool_use_id: str) -> None:
        attributes: dict[str, Any] = {
            SPAN_KIND_ATTRIBUTE: TOOL_SPAN_KIND,
            TOOL_NAME_ATTRIBUTE: tool_name,
            AGENT_FRAMEWORK_ATTRIBUTE: AGENT_FRAMEWORK,
        }
        if tool_name.startswith(_MCP_PREFIX):
            # mcp__<server>__<tool> — recover the server segment.
            parts = tool_name[len(_MCP_PREFIX) :].split("__", 1)
            if parts and parts[0]:
                attributes[TOOL_MCP_SERVER_ATTRIBUTE] = parts[0]
        span = _get_tracer().start_span(
            f"tool {tool_name}",
            context=self._agent_context,
            attributes=attributes,
        )
        self._open[tool_use_id] = span

    def _finish(self, tool_use_id: str, error: str | None) -> None:
        span = self._open.pop(tool_use_id, None)
        if span is None:
            return
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, error))
        span.end()

    async def pre_tool_use(
        self, input_data: dict[str, Any], _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        try:
            self._start(
                str(input_data.get("tool_name", "unknown")),
                str(input_data.get("tool_use_id", "")),
            )
        except Exception:  # noqa: BLE001 — a hook must never break the run
            logger.debug("indratrace: agent-sdk PreToolUse hook failed", exc_info=True)
        return {}

    async def post_tool_use(
        self, input_data: dict[str, Any], _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        try:
            self._finish(str(input_data.get("tool_use_id", "")), error=None)
        except Exception:  # noqa: BLE001 — a hook must never break the run
            logger.debug("indratrace: agent-sdk PostToolUse hook failed", exc_info=True)
        return {}

    async def post_tool_use_failure(
        self, input_data: dict[str, Any], _tool_use_id: Any, _context: Any
    ) -> dict[str, Any]:
        try:
            self._finish(
                str(input_data.get("tool_use_id", "")),
                error=str(input_data.get("error", "tool failed")),
            )
        except Exception:  # noqa: BLE001 — a hook must never break the run
            logger.debug(
                "indratrace: agent-sdk PostToolUseFailure hook failed", exc_info=True
            )
        return {}

    def close_dangling(self) -> None:
        """End any tool spans still open when the run finishes (interrupted run)."""
        for span in self._open.values():
            try:
                span.end()
            except Exception:  # noqa: BLE001 — teardown never raises
                pass
        self._open.clear()


def _install_tracing_hooks(options: Any, tool_spans: _ToolSpans) -> Any:
    """Return `options` with our tracing hooks merged into its `hooks`.

    Non-destructive: whatever hooks the app configured are preserved; ours are
    appended as an extra matcher per event. We mutate the passed `options`
    in place (the SDK dataclass is mutable) — copying it risks dropping fields
    across SDK versions; appending a matcher is additive and reversible in
    effect (the app never sees our observe-only output).

    If the SDK's `HookMatcher` can't be imported/constructed we return `options`
    untouched — tool spans are a bonus, not a prerequisite (ADR 0003).
    """
    try:
        from claude_agent_sdk import HookMatcher
    except Exception:  # noqa: BLE001 — no hooks ⇒ no tool spans, still fine
        return options

    additions = {
        "PreToolUse": tool_spans.pre_tool_use,
        "PostToolUse": tool_spans.post_tool_use,
        "PostToolUseFailure": tool_spans.post_tool_use_failure,
    }

    hooks = dict(options.hooks) if getattr(options, "hooks", None) else {}
    for event, callback in additions.items():
        matcher = HookMatcher(matcher=None, hooks=[callback])
        hooks[event] = [*hooks.get(event, []), matcher]
    options.hooks = hooks
    return options


# ---------------------------------------------------------------------------
# Agent-run span + turn spans, wrapping the message iterator (ADR 0008)
# ---------------------------------------------------------------------------


def _agent_name(options: Any) -> str:
    """A stable, low-cardinality name for the agent span.

    The Agent SDK has no single "agent name"; use the configured model when set,
    else a constant. (The run/session id lands as `session.id`, not in the span
    name, to keep names groupable.)
    """
    model = getattr(options, "model", None)
    return str(model) if model else AGENT_FRAMEWORK


async def _traced_stream(
    inner: AsyncIterator[Any], options: Any, tool_spans: _ToolSpans, agent_span: Span
) -> AsyncIterator[Any]:
    """Wrap the SDK's message iterator: turn spans + usage, always closes.

    Opens a `turn` child span per `AssistantMessage` carrying that turn's model
    and token usage, stamps the run-total usage + session id from the terminal
    `ResultMessage` onto the agent span, and yields every message through
    unchanged. The `finally` ends the agent span and any dangling tool spans even
    when the consumer abandons iteration early (acceptance criterion) or the
    stream raises.
    """
    # Import lazily and defensively — a rename upstream degrades to "no turn
    # spans", not a crash (ADR 0003).
    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage
    except Exception:  # noqa: BLE001
        AssistantMessage = ResultMessage = ()  # type: ignore[assignment]

    try:
        async for message in inner:
            try:
                if AssistantMessage and isinstance(message, AssistantMessage):
                    _emit_turn_span(message, agent_span)
                elif ResultMessage and isinstance(message, ResultMessage):
                    _finalize_run(message, agent_span)
            except Exception:  # noqa: BLE001 — observing a message must not break it
                logger.debug(
                    "indratrace: agent-sdk message handling failed", exc_info=True
                )
            yield message
    finally:
        tool_spans.close_dangling()
        try:
            agent_span.end()
        except Exception:  # noqa: BLE001 — teardown never raises
            logger.debug("indratrace: agent span end failed", exc_info=True)


def _emit_turn_span(message: Any, agent_span: Span) -> None:
    """One `turn` child span per assistant message, with its model + usage."""
    context = trace.set_span_in_context(agent_span)
    model = getattr(message, "model", None)
    attributes: dict[str, Any] = {
        SPAN_KIND_ATTRIBUTE: TURN_SPAN_KIND,
        AGENT_FRAMEWORK_ATTRIBUTE: AGENT_FRAMEWORK,
    }
    if model:
        attributes[GEN_AI_RESPONSE_MODEL] = str(model)
        attributes[GEN_AI_REQUEST_MODEL] = str(model)
    span = _get_tracer().start_span("turn", context=context, attributes=attributes)
    try:
        _stamp_usage(span, getattr(message, "usage", None))
    finally:
        span.end()


def _finalize_run(message: Any, agent_span: Span) -> None:
    """Stamp run-total usage + session id onto the agent span from ResultMessage.

    `total_cost_usd` is deliberately NOT recorded — no cost math in the SDK
    (ADR 0005). ERROR status when the run itself errored.
    """
    session_id = getattr(message, "session_id", None)
    if session_id:
        agent_span.set_attribute(SESSION_ID_ATTRIBUTE, str(session_id))
    _stamp_usage(agent_span, getattr(message, "usage", None))
    if getattr(message, "is_error", False):
        agent_span.set_status(Status(StatusCode.ERROR, "agent run reported an error"))


def _start_agent_span(options: Any) -> Span:
    """Open the agent-run span (its context is left un-attached).

    We start (not enter-as-current) the span so we own its lifetime explicitly —
    the stream wrapper ends it in a `finally`. Tool/turn spans parent under it via
    an explicit context, so nesting holds without us mutating the current context
    on the caller's task.
    """
    return _get_tracer().start_span(
        f"agent {_agent_name(options)}",
        attributes={
            SPAN_KIND_ATTRIBUTE: AGENT_SPAN_KIND,
            AGENT_FRAMEWORK_ATTRIBUTE: AGENT_FRAMEWORK,
            AGENT_NAME_ATTRIBUTE: _agent_name(options),
        },
    )


def _ensure_options(options: Any) -> Any:
    """A usable `ClaudeAgentOptions` — the caller's, or a fresh default."""
    if options is not None:
        return options
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions()


# ---------------------------------------------------------------------------
# Entrypoint interception — patch METHODS, not module-level references (ADR 0008)
# ---------------------------------------------------------------------------
#
# Why methods, not the `query` function reference: a monkeypatch of the
# module-level `claude_agent_sdk.query` is captured *by value* the moment a
# caller does `from claude_agent_sdk import query` — which real apps put at
# module top, i.e. *before* `init_observability()` runs in `main()`. That
# caller's `query` name then points at the original, unpatched function forever,
# and the run is never traced. The public `query()` funnels every call through
# `InternalClient.process_query`, and `ClaudeSDKClient` exposes its stream via
# `receive_messages`/`receive_response`; those are **methods**, resolved on the
# class at call time, so patching them intercepts regardless of how or when the
# caller imported the entrypoint — the same discipline the OTel provider
# instrumentors use (they patch client methods, not import-time references).
# This is the one place we touch an `_internal` seam; the version floor is
# pinned tightly and both attributes are looked up defensively so a rename
# degrades to a silent skip (fail-silent, ADR 0003).


def _wrap_process_query(orig_process_query: Any) -> Any:
    """Trace `InternalClient.process_query` — the funnel for every `query()` call.

    Receives the bound-method args `(self, prompt, options, transport=None)`.
    Installs our tracing hooks into `options` (mutated in place — the SDK
    dataclass is mutable and this is the object that reaches the CLI), opens the
    agent span, and wraps the returned async generator so turns/usage are stamped
    and the span always closes (incl. early abandon). A setup failure degrades to
    the untouched original (ADR 0003).
    """

    async def process_query(
        self: Any,
        prompt: Any,
        options: Any,
        transport: Any = None,
    ) -> AsyncIterator[Any]:
        try:
            options = _ensure_options(options)
            agent_span = _start_agent_span(options)
            agent_context = trace.set_span_in_context(agent_span)
            tool_spans = _ToolSpans(agent_context)
            options = _install_tracing_hooks(options, tool_spans)
        except Exception:  # noqa: BLE001 — never block the real query
            logger.debug(
                "indratrace: agent-sdk process_query wrap setup failed", exc_info=True
            )
            async for message in orig_process_query(self, prompt, options, transport):
                yield message
            return

        inner = orig_process_query(self, prompt, options, transport)
        async for message in _traced_stream(inner, options, tool_spans, agent_span):
            yield message

    return process_query


def _wrap_client_init(orig_init: Any) -> Any:
    """Wrap `ClaudeSDKClient.__init__` to merge tracing hooks into its options.

    Hooks must be registered with the CLI at connect time, so they have to be in
    the options the client is constructed with. We attach the per-client
    `_ToolSpans` used by the receive wrappers to re-parent tool spans per run.
    """

    def __init__(self: Any, options: Any = None, transport: Any = None) -> None:
        self._indratrace_tool_spans = None
        try:
            options = _ensure_options(options)
            tool_spans = _ToolSpans(None)
            self._indratrace_tool_spans = tool_spans
            options = _install_tracing_hooks(options, tool_spans)
        except Exception:  # noqa: BLE001 — never block construction
            logger.debug(
                "indratrace: agent-sdk client hook install failed", exc_info=True
            )
        orig_init(self, options=options, transport=transport)

    return __init__


def _wrap_client_receive(orig_receive: Any) -> Any:
    """Wrap a client `receive_*` method so each stream is one traced agent run.

    A client can issue several queries over one connection, so each
    `receive_response`/`receive_messages` iteration is its own run: open a fresh
    agent span, re-parent the client's tool spans onto it, and close in the
    stream's `finally`. Degrades to the untouched stream on any setup failure.
    """

    def receive(self: Any) -> AsyncIterator[Any]:
        inner = orig_receive(self)
        try:
            agent_span = _start_agent_span(getattr(self, "options", None))
            agent_context = trace.set_span_in_context(agent_span)
            tool_spans = getattr(self, "_indratrace_tool_spans", None) or _ToolSpans(
                agent_context
            )
            tool_spans._agent_context = agent_context
        except Exception:  # noqa: BLE001 — degrade to the untouched stream
            logger.debug(
                "indratrace: agent-sdk client stream wrap failed", exc_info=True
            )
            return inner
        return _traced_stream(inner, None, tool_spans, agent_span)

    return receive


def enable_agent_sdk_instrumentation(
    tracer_provider: TracerProvider,
) -> tuple[bool, str]:
    """Patch the Agent SDK to trace every agent run.

    Called from `init_observability` behind a try-import guard: if
    `claude-agent-sdk` isn't installed (the extra is absent) this is a silent
    skip — not every product uses it. `tracer_provider` is OUR provider, so
    agent-sdk spans land alongside the decorator/GenAI spans in one trace (the
    global is frozen after the first init — architecture.md).

    Patches **methods** (`InternalClient.process_query`, `ClaudeSDKClient`'s
    `__init__`/`receive_response`/`receive_messages`), not the module-level
    `query` reference, so instrumentation survives `from claude_agent_sdk import
    query` performed before init (see the section note above).

    Idempotent: a second call is a no-op. Fail-silent (ADR 0003): any error
    leaves the SDK unpatched and the host app unaffected.

    Returns `(enabled, reason)` so `init_observability`'s debug banner can report
    whether the agent-sdk feature came on and, if not, why. `reason` is empty on
    the enabled path.
    """
    global _patched, _orig_process_query, _orig_client_init, _tracer_provider
    global _orig_client_receive_response, _orig_client_receive_messages

    if _patched:
        return True, ""

    try:
        import claude_agent_sdk
        from claude_agent_sdk._internal.client import InternalClient
    except ImportError:
        logger.debug(
            "indratrace: claude-agent-sdk extra not installed (or its internal "
            "client moved); skipping agent-sdk instrumentation"
        )
        return False, "extra not installed"

    try:
        _tracer_provider = tracer_provider
        client_cls = claude_agent_sdk.ClaudeSDKClient

        _orig_process_query = InternalClient.process_query
        _orig_client_init = client_cls.__init__
        _orig_client_receive_response = client_cls.receive_response
        _orig_client_receive_messages = client_cls.receive_messages

        InternalClient.process_query = _wrap_process_query(_orig_process_query)
        client_cls.__init__ = _wrap_client_init(_orig_client_init)
        client_cls.receive_response = _wrap_client_receive(
            _orig_client_receive_response
        )
        client_cls.receive_messages = _wrap_client_receive(
            _orig_client_receive_messages
        )

        _patched = True
        logger.debug("indratrace: claude-agent-sdk instrumentation enabled")
        return True, ""
    except Exception as exc:  # noqa: BLE001 — a broken wrap must not sink init
        logger.debug(
            "indratrace: enabling claude-agent-sdk instrumentation failed; skipping",
            exc_info=True,
        )
        _disable_agent_sdk_instrumentation()
        return False, f"instrument failed: {exc}"


def _disable_agent_sdk_instrumentation() -> None:
    """Undo `enable_agent_sdk_instrumentation`. Not public — for `_reset_for_tests`.

    Restores the original methods so a test that re-inits (or a reloading worker)
    doesn't stack wrappers. Absent extra / never-patched is a silent no-op.
    """
    global _patched, _orig_process_query, _orig_client_init, _tracer_provider
    global _orig_client_receive_response, _orig_client_receive_messages

    if not _patched:
        _tracer_provider = None
        return

    try:
        import claude_agent_sdk
        from claude_agent_sdk._internal.client import InternalClient

        client_cls = claude_agent_sdk.ClaudeSDKClient
        if _orig_process_query is not None:
            InternalClient.process_query = _orig_process_query
        if _orig_client_init is not None:
            client_cls.__init__ = _orig_client_init
        if _orig_client_receive_response is not None:
            client_cls.receive_response = _orig_client_receive_response
        if _orig_client_receive_messages is not None:
            client_cls.receive_messages = _orig_client_receive_messages
    except Exception:  # noqa: BLE001 — unpatching a clean process
        pass
    finally:
        _patched = False
        _orig_process_query = None
        _orig_client_init = None
        _orig_client_receive_response = None
        _orig_client_receive_messages = None
        _tracer_provider = None
