"""HTTP server auto-instrumentation: FastAPI, Django, Flask.

Each framework has an official OTel instrumentor that emits a server span per
request (method, route, status, duration) following that instrumentor's own OTel
HTTP semconv — we consume those names, we do not fork them (docs/conventions.md).
Every instrumentor ships as an optional extra so the core stays OTel-only
(ADR 0003); an absent extra is a normal, silent outcome — not every product is a
web app, and no product is all three frameworks.

All three are handed OUR tracer provider explicitly, for the same reason the
GenAI instrumentors are (genai.py): the OTel *global* provider is frozen at the
first `set_tracer_provider` in a process, so a second init in the same process —
every test session, and reloading workers — would otherwise send HTTP spans to a
stale provider.

**The frameworks differ in *when* instrumentation must happen**, and that
difference is the whole reason this module has more than one function. It is not
cosmetic; get the placement wrong and you get zero spans with no error:

- **FastAPI** patches the `FastAPI` class, and its instrumentation also works on
  app instances created later. Init anywhere before your requests start.

- **Django** does not patch a class at all — it *inserts its middleware* into
  `settings.MIDDLEWARE`. Django reads that setting once, when the WSGI/ASGI
  application is built, and builds a frozen middleware chain from it. So
  `init_observability()` must run **before** `get_wsgi_application()` /
  `get_asgi_application()` — i.e. at the top of `wsgi.py` / `asgi.py` (and
  `manage.py` for `runserver`). Init after the app is built and the middleware
  is never in the chain: no HTTP spans, no error. Documented honestly in the
  README rather than pretended away.

- **Flask** replaces the `flask.Flask` class itself with an instrumented
  subclass. That only reaches code that looks the name up *after* init, so the
  ubiquitous `from flask import Flask` at the top of a module — which binds the
  original class **by value**, before `init_observability()` ever runs — is left
  uninstrumented. This is the same already-imported-class trap the FastAPI work
  hit. Verified against the pinned instrumentor. For it, `instrument_flask_app`
  is exported as public API: a one-line rescue that instruments an app instance
  directly, whatever the import order was.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("indratrace")


def _instrument_fastapi(tracer_provider: TracerProvider) -> tuple[bool, str]:
    """FastAPI HTTP server spans, if the `fastapi` extra is installed."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.debug(
            "indratrace: opentelemetry-instrumentation-fastapi not installed; "
            "skipping FastAPI HTTP auto-instrumentation"
        )
        return False, "extra not installed"

    FastAPIInstrumentor().instrument(tracer_provider=tracer_provider)
    logger.debug("indratrace: FastAPI auto-instrumentation enabled")
    return True, ""


def _instrument_django(tracer_provider: TracerProvider) -> tuple[bool, str]:
    """Django HTTP server spans, if the `django` extra is installed.

    Inserts the instrumentor's middleware into `settings.MIDDLEWARE`, so this
    only takes effect if it runs **before** Django builds its middleware chain
    (see the module docstring): init at the top of `wsgi.py` / `asgi.py` /
    `manage.py`. We cannot detect "too late" from here — Django gives no signal
    — so the placement requirement is documented rather than enforced.
    """
    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentor
    except ImportError:
        logger.debug(
            "indratrace: opentelemetry-instrumentation-django not installed; "
            "skipping Django HTTP auto-instrumentation"
        )
        return False, "extra not installed"

    DjangoInstrumentor().instrument(tracer_provider=tracer_provider)
    logger.debug("indratrace: Django auto-instrumentation enabled")
    return True, ""


def _instrument_flask(tracer_provider: TracerProvider) -> tuple[bool, str]:
    """Flask HTTP server spans, if the `flask` extra is installed.

    Swaps `flask.Flask` for an instrumented subclass, which covers every app
    constructed *after* this runs via a late lookup (`flask.Flask(...)`). An app
    whose module did `from flask import Flask` before init keeps the original
    class and needs `instrument_flask_app(app)` — see the module docstring.
    """
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
    except ImportError:
        logger.debug(
            "indratrace: opentelemetry-instrumentation-flask not installed; "
            "skipping Flask HTTP auto-instrumentation"
        )
        return False, "extra not installed"

    FlaskInstrumentor().instrument(tracer_provider=tracer_provider)
    logger.debug("indratrace: Flask auto-instrumentation enabled")
    return True, ""


def instrument_flask_app(app: Any) -> bool:
    """Instrument one Flask **app instance**, whatever the import order was.

    `init_observability()` already instruments Flask, and for most apps that is
    enough. But the instrumentor works by replacing the `flask.Flask` *class*,
    so an app built from a name that was imported before init —

        from flask import Flask          # binds the ORIGINAL class, by value
        ...
        init_observability(product="my-app")
        app = Flask(__name__)            # still the original ⇒ no HTTP spans

    — never becomes instrumented, silently. If your HTTP spans are missing, this
    is almost always why. Hand the app here and it is instrumented directly:

        from flask import Flask
        from indratrace import init_observability, instrument_flask_app

        init_observability(product="my-app")
        app = Flask(__name__)
        instrument_flask_app(app)        # now every request is a span

    (Constructing the app as `flask.Flask(__name__)`, i.e. looking the name up
    on the module at call time, also works and needs no extra line.)

    Safe to call twice — the instrumentor no-ops on an already-instrumented app.
    Returns True if the app is instrumented, False if it couldn't be (the `flask`
    extra isn't installed, or the instrumentor refused). Never raises into the
    caller (ADR 0003): a missing HTTP span must not take the app down with it.
    """
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
    except ImportError:
        logger.debug(
            "indratrace: instrument_flask_app() called but the flask extra is not "
            "installed; install indratrace[flask]"
        )
        return False

    try:
        from .init import _get_provider

        # Our provider, not the frozen global — same reason as everywhere else.
        # `None` (init never ran / failed) lets the instrumentor fall back to the
        # global, which is the best available answer in that case.
        FlaskInstrumentor().instrument_app(app, tracer_provider=_get_provider())
    except Exception:  # noqa: BLE001 — HTTP spans are a bonus, never a cost
        logger.debug("indratrace: instrument_flask_app failed", exc_info=True)
        return False

    logger.debug("indratrace: Flask app instrumented directly")
    return True


#: (label, instrument function). The label is what the debug banner prints, and
#: is also the extra's name — so a `skipped (extra not installed)` line tells the
#: user exactly what to `pip install`.
_HTTP_INSTRUMENTORS: tuple[tuple[str, Any], ...] = (
    ("fastapi", _instrument_fastapi),
    ("django", _instrument_django),
    ("flask", _instrument_flask),
)


def enable_http_instrumentation(
    tracer_provider: TracerProvider,
) -> list[tuple[str, bool, str]]:
    """Instrument every web framework whose extra is installed.

    Fail-silent per framework (ADR 0003): one framework's broken instrumentor
    must never deny the others — or the host app — their telemetry. Returns one
    `(framework, enabled, reason)` tuple per supported framework for the debug
    banner, exactly like `enable_genai_instrumentation`.
    """
    statuses: list[tuple[str, bool, str]] = []
    for name, instrument in _HTTP_INSTRUMENTORS:
        try:
            enabled, reason = instrument(tracer_provider)
            statuses.append((name, enabled, reason))
        except Exception as exc:  # noqa: BLE001 — one framework must not sink the rest
            logger.warning(
                "indratrace: %s auto-instrumentation failed; other signals are "
                "unaffected",
                name,
                exc_info=True,
            )
            statuses.append((name, False, f"instrument failed: {exc}"))
    return statuses


def _uninstrument_http() -> None:
    """Undo `enable_http_instrumentation`. Not public API — for `_reset_for_tests`.

    The instrumentors patch classes and Django settings process-wide, so a test
    that inits must be able to unpatch them or the patch leaks into the next
    test. Absent extra / not-instrumented is a silent no-op.
    """
    for module_path, class_name in (
        ("opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
        ("opentelemetry.instrumentation.django", "DjangoInstrumentor"),
        ("opentelemetry.instrumentation.flask", "FlaskInstrumentor"),
    ):
        try:
            module = __import__(module_path, fromlist=[class_name])
            instrumentor = getattr(module, class_name)()
            # Only unpatch what we patched — uninstrumenting a clean instrumentor
            # logs a noisy "already uninstrumented" warning.
            if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                instrumentor.uninstrument()
        except Exception:  # noqa: BLE001 — unpatching a clean process
            continue
