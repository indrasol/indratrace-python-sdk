"""Session/user context + feedback API (product-analytics primitives).

Two things live here, both built on OpenTelemetry primitives so they compose
with everything else the SDK emits — decorator spans, FastAPI HTTP spans, and
the GenAI model spans alike.

**Session/user context.** ``session(session_id=..., user_id=...)`` puts the two
ids into OTel *baggage* — a key/value bag that rides the OTel context across
sync boundaries and, because that context is a `contextvars.ContextVar`, across
``async``/``await`` and thread boundaries too. A `SpanProcessor` we register in
``init_observability`` reads that baggage in ``on_start`` and stamps
``session.id`` / ``user.id`` onto **every** span started while the context is
active — no per-decorator plumbing, so auto-instrumented spans get the
attributes just as ours do. This is the standard OTel pattern (baggage +
processor), not a bespoke mechanism.

**Feedback API.** ``record_feedback(score, comment=None, trace_id=None)`` emits
a short ``feedback`` span linking a thumbs-up/down (or any numeric score) back
to a trace. ``current_trace_id()`` lets a product capture the id at answer time
so it can pass the score in minutes later, out of band.

Fail-silent throughout (ADR 0003): nothing here raises into the host app, and
everything is a well-behaved no-op when ``init_observability`` never ran.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import baggage, context, trace
from opentelemetry.sdk.trace import SpanProcessor

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import Span

logger = logging.getLogger("indratrace")

#: Baggage keys the session context carries, and the span-attribute names the
#: processor copies them onto. Same string for both by design: the platform
#: reads them off spans as `session.id` / `user.id` (docs/conventions.md).
#: Session and user ids are span attributes, NEVER metric labels — they are
#: unbounded-cardinality values (conventions.md § Naming).
SESSION_ID_KEY = "session.id"
USER_ID_KEY = "user.id"

#: Feedback span shape (docs/conventions.md § Feedback span).
FEEDBACK_SPAN_NAME = "feedback"
FEEDBACK_SPAN_KIND = "feedback"
SPAN_KIND_ATTRIBUTE = "indratrace.span.kind"
FEEDBACK_SCORE_ATTRIBUTE = "feedback.score"
FEEDBACK_COMMENT_ATTRIBUTE = "feedback.comment"
FEEDBACK_TRACE_ID_ATTRIBUTE = "feedback.trace_id"

#: Instrumentation scope reported on the feedback span. Matches the decorators'.
INSTRUMENTATION_SCOPE = "indratrace"


class SessionSpanProcessor(SpanProcessor):
    """Copies session/user baggage onto every span at start.

    Registered on the SDK's `TracerProvider` in `init_observability`. Its only
    job is `on_start`: read the two baggage keys off the parent context and, if
    present, stamp them as span attributes. Because it runs for *every* span the
    provider starts — decorator spans, FastAPI HTTP spans, GenAI model spans —
    the attributes appear uniformly without any per-span code.

    Subclasses the SDK's `SpanProcessor` base so it inherits no-op defaults for
    the hooks it doesn't need (`on_end`, `shutdown`, `force_flush`, and internal
    ones like `_on_ending` that newer SDK versions call). We override only
    `on_start`.
    """

    def on_start(
        self, span: Span, parent_context: Context | None = None
    ) -> None:
        try:
            session_id = baggage.get_baggage(SESSION_ID_KEY, parent_context)
            if session_id is not None:
                span.set_attribute(SESSION_ID_KEY, str(session_id))

            user_id = baggage.get_baggage(USER_ID_KEY, parent_context)
            if user_id is not None:
                span.set_attribute(USER_ID_KEY, str(user_id))
        except Exception:  # noqa: BLE001 — a processor must never break a span
            logger.debug(
                "indratrace: SessionSpanProcessor.on_start failed", exc_info=True
            )


def _apply_session_baggage(
    session_id: str | None, user_id: str | None
) -> object:
    """Attach a new OTel context carrying the ids as baggage; return its token.

    Each `set_baggage` returns a *new* context layered over the current one, so
    a `session(...)` nested inside another only overrides the keys it sets —
    an inner `session(user_id="u2")` keeps the outer `session.id` intact.
    """
    ctx = context.get_current()
    if session_id is not None:
        ctx = baggage.set_baggage(SESSION_ID_KEY, session_id, context=ctx)
    if user_id is not None:
        ctx = baggage.set_baggage(USER_ID_KEY, user_id, context=ctx)
    return context.attach(ctx)


class _SessionScope:
    """Handle returned by `session(...)`, usable as a context manager or handle.

    As a context manager (`with session(...):`) the baggage is attached on
    `__enter__` and detached on `__exit__` — the common case. Middleware that
    cannot bracket a `with` (it enters on request-in, exits on request-out, in
    separate callbacks) uses the imperative form: call `session(...)` to attach
    now and keep the returned handle, then call `handle.detach()` (or
    `handle.close()`) later to restore the previous context. The token is bound
    to the calling context, so detach must run in the same task/thread.
    """

    def __init__(self, session_id: str | None, user_id: str | None) -> None:
        self._session_id = session_id
        self._user_id = user_id
        # Attaching eagerly (not on `__enter__`) is what makes the imperative
        # form work: `token = session(...)` is already active before any `with`.
        self._token: object | None = _apply_session_baggage(session_id, user_id)

    def detach(self) -> None:
        """Restore the context that was current before this scope attached.

        Idempotent and fail-silent: calling it twice, or after the context has
        already moved on, logs at debug and does nothing (ADR 0003).
        """
        if self._token is None:
            return
        try:
            context.detach(self._token)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — detach must never break the app
            logger.debug("indratrace: session detach failed", exc_info=True)
        finally:
            self._token = None

    #: Alias so middleware can treat the handle like any closeable resource.
    close = detach

    def __enter__(self) -> _SessionScope:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.detach()


def session(
    session_id: str | None = None, user_id: str | None = None
) -> _SessionScope:
    """Tag every span started in this scope with `session.id` / `user.id`.

    Two forms, one call:

    - **Context manager** (the common case)::

          with session(session_id="conv-42", user_id="u1"):
              await agent(query)   # every span here carries both ids

    - **Imperative handle** (for middleware that can't bracket a `with` — it
      attaches on request-in and detaches on request-out in separate
      callbacks)::

          handle = session(session_id=request.headers["x-session"])
          try:
              ...  # dispatch the request
          finally:
              handle.detach()   # or handle.close()

    Works across `async`/`await` and threads: the ids live in OTel baggage,
    which rides the `contextvars`-backed OTel context. Spans from FastAPI
    auto-instrumentation and the GenAI instrumentors pick them up too — a
    processor stamps them at span start, so this is not limited to the SDK's
    own decorators.

    Passing only one id sets only that attribute. Nesting overrides per-key:
    an inner `session(user_id=...)` keeps the outer `session.id`. Safe to call
    before `init_observability` (or if it failed): baggage is set regardless,
    and the processor that reads it simply isn't there, so the call is inert.
    """
    return _SessionScope(session_id, user_id)


def current_trace_id() -> str | None:
    """The current trace id as a 32-char lowercase hex string, or `None`.

    Returns `None` when called outside any recording span — no active trace to
    identify. Products capture this at answer time so they can attach feedback
    to the trace minutes later, out of band::

        answer = run(query)
        return {"answer": answer, "trace_id": current_trace_id()}
        ...
        record_feedback(1, trace_id=stored_trace_id)

    Never raises (ADR 0003): any failure resolves to `None`.
    """
    try:
        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return trace.format_trace_id(span_context.trace_id)
    except Exception:  # noqa: BLE001 — instrumentation must never break the app
        logger.debug("indratrace: current_trace_id failed", exc_info=True)
        return None


def record_feedback(
    score: int | float,
    comment: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Emit a `feedback` span tying a thumbs-up/down (or any score) to a trace.

    Args:
        score: numeric feedback. Convention: `1` = positive, `0` or `-1` =
            negative; any numeric scale (e.g. a 1–5 rating) works — the platform
            reads `feedback.score` verbatim (docs/conventions.md § Feedback).
        comment: optional free-text note stamped as `feedback.comment`.
        trace_id: the trace this feedback is about, as a 32-char hex string
            (what `current_trace_id()` returns). If omitted, the current
            trace's id is used when inside one; if there is no current trace
            either, the span is still emitted with no `feedback.trace_id` (a
            debug note is logged) so the score is never silently dropped.

    Typical flow: the product stored `current_trace_id()` alongside the answer,
    then calls `record_feedback(1, trace_id=stored_id)` when the user clicks 👍
    minutes later. If the call happens inside `session(...)`, the feedback span
    carries `session.id` / `user.id` too (the processor stamps them).

    Fail-silent (ADR 0003): with no initialized SDK the span is non-recording
    and this is effectively a no-op; any failure is swallowed at debug and never
    reaches the caller.
    """
    try:
        # Resolve the trace_id linkage: explicit arg wins, else the ambient
        # trace, else none (still emit — losing the score is worse than a span
        # with no linkage).
        linked_trace_id = trace_id
        if linked_trace_id is None:
            linked_trace_id = current_trace_id()
        if linked_trace_id is None:
            logger.debug(
                "indratrace: record_feedback has no trace_id and no ambient "
                "trace; emitting an unlinked feedback span"
            )

        attributes: dict[str, object] = {
            SPAN_KIND_ATTRIBUTE: FEEDBACK_SPAN_KIND,
            FEEDBACK_SCORE_ATTRIBUTE: score,
        }
        if comment is not None:
            attributes[FEEDBACK_COMMENT_ATTRIBUTE] = comment
        if linked_trace_id is not None:
            attributes[FEEDBACK_TRACE_ID_ATTRIBUTE] = linked_trace_id

        tracer = _get_tracer()
        # A standalone span, not nested under any ambient span: feedback is its
        # own event, often recorded long after the original request ended.
        with tracer.start_as_current_span(
            FEEDBACK_SPAN_NAME,
            attributes=attributes,
        ):
            pass
    except Exception:  # noqa: BLE001 — instrumentation must never break the app
        logger.debug("indratrace: record_feedback failed", exc_info=True)


def _get_tracer() -> trace.Tracer:
    """The tracer to use, resolved lazily — mirrors `agent._get_tracer`.

    Prefers the provider `init_observability` built over the global, which OTel
    freezes at the first `set_tracer_provider` in a process (architecture.md,
    "Testing notes"). Falls back to the global API, which hands out
    non-recording spans when nothing was initialized.
    """
    from . import init as _init

    provider = _init._get_provider()
    if provider is not None:
        return provider.get_tracer(INSTRUMENTATION_SCOPE)
    return trace.get_tracer(INSTRUMENTATION_SCOPE)
