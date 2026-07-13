"""HTTP server spans for Django and Flask, and the extras' fail-silent wiring.

FastAPI already had an extra; 0.6.0 adds Django and Flask. Each framework is
driven as a *real minimal app* (a real request through a real test client), not a
mocked instrumentor — the only way to catch the two silent-zero-span traps these
frameworks have, both of which are about *placement*:

- Django instruments by inserting middleware into `settings.MIDDLEWARE`, which
  Django reads once when the WSGI/ASGI app is built.
- Flask instruments by replacing the `flask.Flask` class, which misses a name
  bound by `from flask import Flask` before init.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from indratrace import init_observability, instrument_flask_app
from indratrace.init import _get_provider, _reset_for_tests
from indratrace.web import enable_http_instrumentation


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def capture_spans() -> InMemorySpanExporter:
    """Tee the tracer provider init built into an in-memory exporter.

    OTel freezes the *global* provider at the first setter call, so tests read
    the SDK's own provider rather than `trace.get_tracer_provider()`.
    """
    provider = _get_provider()
    assert provider is not None, "init_observability() did not build a tracer provider"

    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def server_spans(exporter: InMemorySpanExporter) -> list[Any]:
    """Spans with an HTTP route — i.e. the framework's server spans, not ours."""
    return [
        span
        for span in exporter.get_finished_spans()
        if "http.route" in (span.attributes or {})
    ]


# --------------------------------------------------------------------------- #
# Flask
# --------------------------------------------------------------------------- #

flask = pytest.importorskip("flask", reason="the flask extra needs flask")


class TestFlask:
    def test_a_request_becomes_a_server_span(self) -> None:
        init_observability(product="flask-demo")
        exporter = capture_spans()

        # Looked up on the module at call time, so it picks up the instrumented
        # class the instrumentor swapped in — the no-extra-line path.
        app = flask.Flask("demo")

        @app.route("/orders/<order_id>")
        def get_order(order_id: str) -> str:
            return f"order {order_id}"

        response = app.test_client().get("/orders/42")
        assert response.status_code == 200

        spans = server_spans(exporter)
        assert len(spans) == 1, "one request should make exactly one server span"
        assert spans[0].attributes["http.route"] == "/orders/<order_id>"

    def test_span_records_the_status_code(self) -> None:
        init_observability(product="flask-demo")
        exporter = capture_spans()

        app = flask.Flask("demo")

        @app.route("/missing")
        def missing() -> tuple[str, int]:
            return "nope", 404

        app.test_client().get("/missing")

        attributes = server_spans(exporter)[0].attributes
        status = attributes.get("http.status_code") or attributes.get(
            "http.response.status_code"
        )
        assert status == 404

    def test_a_preimported_flask_class_is_the_silent_trap(self) -> None:
        """The caveat that `instrument_flask_app` exists to rescue.

        `from flask import Flask` binds the ORIGINAL class by value. The
        instrumentor swaps `flask.Flask`, which that name no longer points at —
        so the app is never instrumented, and nothing says so. Pinning the trap
        here means the README's claim about it stays true.
        """
        preimported_flask = flask.Flask  # captured before init, by value

        init_observability(product="flask-demo")
        exporter = capture_spans()

        app = preimported_flask("trapped")

        @app.route("/trap")
        def trap() -> str:
            return "ok"

        app.test_client().get("/trap")

        assert server_spans(exporter) == [], (
            "if this now produces a span, the instrumentor changed how it patches "
            "and instrument_flask_app / the README caveat should be revisited"
        )

    def test_instrument_flask_app_rescues_a_preimported_app(self) -> None:
        preimported_flask = flask.Flask

        init_observability(product="flask-demo")
        exporter = capture_spans()

        app = preimported_flask("rescued")

        @app.route("/rescued")
        def rescued() -> str:
            return "ok"

        assert instrument_flask_app(app) is True

        app.test_client().get("/rescued")

        spans = server_spans(exporter)
        assert len(spans) == 1
        assert spans[0].attributes["http.route"] == "/rescued"

    def test_instrument_flask_app_is_safe_to_call_twice(self) -> None:
        init_observability(product="flask-demo")
        exporter = capture_spans()

        app = flask.Flask("twice")

        @app.route("/twice")
        def twice() -> str:
            return "ok"

        instrument_flask_app(app)
        instrument_flask_app(app)  # no-op, and must not double-span

        app.test_client().get("/twice")

        assert len(server_spans(exporter)) == 1

    def test_instrument_flask_app_never_raises(self) -> None:
        """Fail-silent (ADR 0003): a missing span must not take the app down."""
        init_observability(product="flask-demo")

        assert instrument_flask_app(object()) is False  # not a Flask app at all


# --------------------------------------------------------------------------- #
# Django
# --------------------------------------------------------------------------- #

django = pytest.importorskip("django", reason="the django extra needs django")


def hello(request: Any) -> Any:
    from django.http import HttpResponse

    return HttpResponse("ok")


# Django settings are configured at **import time**, deliberately — not inside a
# fixture. `settings.configure()` is one-shot per process, and the Django
# instrumentor itself calls a bare `settings.configure()` when
# DJANGO_SETTINGS_MODULE is unset (verified in its source). So the first
# `init_observability()` in the session — from *any* test in this file, Flask's
# included — would otherwise burn that one shot on empty settings, leaving this
# module's Django tests with no ROOT_URLCONF and an AttributeError. Configuring
# here, at collection, wins the race by construction.
#
# This module is its own ROOT_URLCONF, so `urlpatterns` must be a module global.
from django.conf import settings as _django_settings  # noqa: E402
from django.urls import path  # noqa: E402

urlpatterns = [path("hello/", hello)]

if not _django_settings.configured:
    _django_settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test-only-not-a-secret",
        MIDDLEWARE=[],
        DATABASES={},
        INSTALLED_APPS=[],
    )
    django.setup()


@pytest.fixture
def django_app() -> Iterator[Any]:
    """A Django project with an empty middleware chain — the surface the
    instrumentor mutates. Reset per test so one test's middleware never leaks."""
    _django_settings.MIDDLEWARE = []
    try:
        yield _django_settings
    finally:
        _django_settings.MIDDLEWARE = []


class TestDjango:
    def test_init_inserts_the_otel_middleware(self, django_app: Any) -> None:
        """Django's whole instrumentation mechanism, in one assertion."""
        assert django_app.MIDDLEWARE == []

        init_observability(product="django-demo")

        assert any(
            "opentelemetry" in middleware for middleware in django_app.MIDDLEWARE
        ), "the instrumentor did not insert its middleware"

    def test_a_request_becomes_a_server_span(self, django_app: Any) -> None:
        from django.test import Client

        # init BEFORE the request, so the middleware is in the chain Django builds.
        init_observability(product="django-demo")
        exporter = capture_spans()

        response = Client().get("/hello/")
        assert response.status_code == 200

        spans = server_spans(exporter)
        assert len(spans) == 1, "one request should make exactly one server span"
        assert spans[0].attributes["http.route"] == "hello/"

    def test_span_records_the_status_code(self, django_app: Any) -> None:
        from django.test import Client

        init_observability(product="django-demo")
        exporter = capture_spans()

        Client().get("/hello/")

        attributes = server_spans(exporter)[0].attributes
        status = attributes.get("http.status_code") or attributes.get(
            "http.response.status_code"
        )
        assert status == 200

    def test_reset_removes_the_middleware(self, django_app: Any) -> None:
        """Otherwise the middleware leaks into every later test in the session."""
        init_observability(product="django-demo")
        assert django_app.MIDDLEWARE != []

        _reset_for_tests()

        assert not any(
            "opentelemetry" in middleware for middleware in django_app.MIDDLEWARE
        )


# --------------------------------------------------------------------------- #
# Wiring: extras, statuses, fail-silence
# --------------------------------------------------------------------------- #


class TestWiring:
    def test_every_framework_reports_a_status(self) -> None:
        """One `(framework, enabled, reason)` per framework, for the debug banner."""
        init_observability(product="demo", instrument_http=False)

        statuses = enable_http_instrumentation(_get_provider())

        assert {name for name, _enabled, _reason in statuses} == {
            "fastapi",
            "django",
            "flask",
        }

    def test_an_absent_extra_is_a_silent_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core installs stay dependency-clean: no extra, no error, no cost."""
        import builtins

        real_import = builtins.__import__

        def no_web_instrumentors(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("opentelemetry.instrumentation.") and name.rsplit(
                ".", 1
            )[-1] in ("fastapi", "django", "flask"):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_web_instrumentors)

        init_observability(product="demo", instrument_http=False)
        statuses = enable_http_instrumentation(_get_provider())

        assert statuses == [
            ("fastapi", False, "extra not installed"),
            ("django", False, "extra not installed"),
            ("flask", False, "extra not installed"),
        ]

    def test_one_broken_framework_does_not_sink_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-silent per framework: a broken Django must not cost Flask its spans."""
        from opentelemetry.instrumentation.django import DjangoInstrumentor

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("django exploded")

        monkeypatch.setattr(DjangoInstrumentor, "_instrument", boom)

        init_observability(product="demo", instrument_http=False)
        statuses = {
            name: enabled
            for name, enabled, _reason in enable_http_instrumentation(_get_provider())
        }

        assert statuses["django"] is False
        assert statuses["flask"] is True, "flask must still come up"
        assert statuses["fastapi"] is True, "fastapi must still come up"

    def test_instrument_http_false_instruments_nothing(self, django_app: Any) -> None:
        init_observability(product="demo", instrument_http=False)

        assert django_app.MIDDLEWARE == [], "instrument_http=False still instrumented"

    def test_the_deprecated_instrument_fastapi_alias_still_gates_everything(
        self, django_app: Any
    ) -> None:
        """Pre-0.6.0 callers passed `instrument_fastapi=False` to keep HTTP off."""
        init_observability(product="demo", instrument_fastapi=False)

        assert django_app.MIDDLEWARE == [], (
            "the deprecated alias no longer disables HTTP instrumentation"
        )

    def test_http_spans_go_to_our_provider_not_the_frozen_global(self) -> None:
        """A second init in a process must not send spans to a stale provider."""
        init_observability(product="first", instrument_http=False)
        _reset_for_tests()
        init_observability(product="second")  # the global is already frozen
        exporter = capture_spans()

        app = flask.Flask("second")

        @app.route("/second")
        def second() -> str:
            return "ok"

        app.test_client().get("/second")

        assert len(server_spans(exporter)) == 1, (
            "HTTP spans landed on the stale global provider, not ours"
        )
