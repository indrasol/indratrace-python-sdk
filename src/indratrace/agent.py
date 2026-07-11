"""@trace_agent / @trace_tool decorators (sync + async).

Span naming and attributes per docs/conventions.md:

- agent span: name ``agent <name>``, ``indratrace.span.kind="agent"``, ``agent.name``
- tool span:  name ``tool <func>``,  ``indratrace.span.kind="tool"``,  ``tool.name``
- step span:  name ``step <func>``,  ``indratrace.span.kind="step"``,  ``step.name``

Two hard rules shape everything here (docs/architecture.md):

1. **Transparent.** The decorators observe; they never change what the wrapped
   function returns or raises. An exception is recorded on the span, the status
   is set to ERROR, and then it is re-raised unchanged.
2. **Never raise from instrumentation.** If the tracer cannot be obtained or a
   span cannot be started, the wrapped function still runs. A decorated app
   works — un-instrumented — when `init_observability` was never called or
   failed.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any, TypeVar, cast, overload

from opentelemetry import trace

logger = logging.getLogger("indratrace")

#: Attribute marking which of our span kinds this is (docs/conventions.md).
SPAN_KIND_ATTRIBUTE = "indratrace.span.kind"
AGENT_SPAN_KIND = "agent"
TOOL_SPAN_KIND = "tool"
STEP_SPAN_KIND = "step"

#: Instrumentation scope reported on every span the decorators emit.
INSTRUMENTATION_SCOPE = "indratrace"

F = TypeVar("F", bound=Callable[..., Any])


def _get_tracer() -> trace.Tracer:
    """The tracer to use for this call, resolved lazily.

    Prefers the provider `init_observability` built, because OTel freezes the
    *global* provider at the first `set_tracer_provider` in a process — a later
    init's provider is never consulted through the global (architecture.md,
    "Testing notes"). Falls back to the global API, which hands out
    non-recording spans when nothing was ever initialized.
    """
    from . import init as _init

    provider = _init._get_provider()
    if provider is not None:
        return provider.get_tracer(INSTRUMENTATION_SCOPE)
    return trace.get_tracer(INSTRUMENTATION_SCOPE)


@contextmanager
def _span(name: str, attributes: dict[str, str]):
    """Open a span around the body, or just run the body if that isn't possible.

    OTel's `start_as_current_span` already implements rule 1: on an exception it
    records the event, sets status ERROR, and re-raises unchanged — and it
    correctly skips `BaseException` (`GeneratorExit`, `KeyboardInterrupt`,
    `CancelledError` are not errors). We rely on that rather than reimplement it.

    Rule 2 is this function's own job: a failure in the tracing machinery must
    not reach the app, so the body still runs, un-instrumented.
    """
    try:
        cm = _get_tracer().start_as_current_span(name, attributes=attributes)
    except Exception:  # noqa: BLE001 — instrumentation must never break the app
        logger.debug("indratrace: could not start span %r", name, exc_info=True)
        yield
        return

    with cm:
        yield


def _instrument(func: F, span_name: str, attributes: dict[str, str]) -> F:
    """Wrap `func` (sync or async) so each call opens `span_name`."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _span(span_name, attributes):
                return await cast(Callable[..., Awaitable[Any]], func)(*args, **kwargs)

        return cast(F, async_wrapper)

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with _span(span_name, attributes):
            return func(*args, **kwargs)

    return cast(F, sync_wrapper)


def trace_agent(name: str) -> Callable[[F], F]:
    """Wrap a whole agent request in a span named ``agent <name>``.

        @trace_agent("compliance-checker")
        async def run(query): ...

    Works on sync and async functions. Exceptions are recorded on the span and
    re-raised unchanged.
    """

    def decorator(func: F) -> F:
        return _instrument(
            func,
            span_name=f"agent {name}",
            attributes={SPAN_KIND_ATTRIBUTE: AGENT_SPAN_KIND, "agent.name": name},
        )

    return decorator


@overload
def trace_tool(func: F) -> F: ...


@overload
def trace_tool(func: None = None) -> Callable[[F], F]: ...


def trace_tool(func: F | None = None) -> F | Callable[[F], F]:
    """Wrap one tool call in a span named ``tool <function_name>``.

    Usable bare or called — `@trace_tool` and `@trace_tool()` are equivalent.
    Works on sync and async functions. Exceptions are recorded on the span and
    re-raised unchanged.
    """

    def decorator(target: F) -> F:
        tool_name = target.__name__
        return _instrument(
            target,
            span_name=f"tool {tool_name}",
            attributes={SPAN_KIND_ATTRIBUTE: TOOL_SPAN_KIND, "tool.name": tool_name},
        )

    if func is None:  # @trace_tool()
        return decorator
    return decorator(func)  # @trace_tool


@overload
def trace_step(func: F) -> F: ...


@overload
def trace_step(func: None = None) -> Callable[[F], F]: ...


def trace_step(func: F | None = None) -> F | Callable[[F], F]:
    """Wrap any non-AI function in a span named ``step <function_name>``.

    The neutral sibling of `trace_tool`: same machinery, but for timing plain
    work — a database query, a parser, a validation pass — where calling it a
    "tool" would mislabel it. Produces `indratrace.span.kind="step"` and
    `step.name`, and nests under whatever agent/tool/HTTP span is active.

    Usable bare or called — `@trace_step` and `@trace_step()` are equivalent.
    Works on sync and async functions. Exceptions are recorded on the span and
    re-raised unchanged.
    """

    def decorator(target: F) -> F:
        step_name = target.__name__
        return _instrument(
            target,
            span_name=f"step {step_name}",
            attributes={SPAN_KIND_ATTRIBUTE: STEP_SPAN_KIND, "step.name": step_name},
        )

    if func is None:  # @trace_step()
        return decorator
    return decorator(func)  # @trace_step
