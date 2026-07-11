"""Shared fixtures: keep the ambient environment — and the network — out."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from indratrace.config import (
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    ENV_ENDPOINT,
    ENV_ENV,
    ENV_KEY,
    ENV_PRODUCT,
)

_INDRATRACE_ENV_VARS = (ENV_ENDPOINT, ENV_KEY, ENV_PRODUCT, ENV_ENV)

#: Bound at import, before any fixture shrinks the module global.
PRODUCTION_EXPORT_TIMEOUT_SECONDS = DEFAULT_EXPORT_TIMEOUT_SECONDS

# Nothing listens here, so exports fail. Unit tests assert on in-memory signals,
# never on delivery.
UNREACHABLE_ENDPOINT = "http://127.0.0.1:1"

# A refused connection does *not* fail instantly: OTel's OTLP exporter retries
# with backoff until the export timeout is spent, and provider shutdown drains
# the queue. At the production 3s that costs seconds of teardown per test, on
# three providers. Tests that care about the real budget restore it themselves
# (see `production_export_timeout`).
TEST_EXPORT_TIMEOUT_SECONDS = 0.05

TIMEOUT_ATTR = "indratrace.config.DEFAULT_EXPORT_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Unset INDRATRACE_* so a developer's real env can't change results."""
    for var in _INDRATRACE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    # Integration tests target the real harness and set their own endpoint.
    if request.node.get_closest_marker("integration") is not None:
        return

    monkeypatch.setenv(ENV_ENDPOINT, UNREACHABLE_ENDPOINT)
    monkeypatch.setattr(TIMEOUT_ATTR, TEST_EXPORT_TIMEOUT_SECONDS)


@pytest.fixture
def sdk_log() -> Iterator[list[logging.LogRecord]]:
    """Records the `indratrace` logger emits, captured at the source.

    `caplog` reads through the root logger, but a successful init sets
    `indratrace.propagate = False` (it must not feed its own diagnostics back
    into the log exporter). So attach directly to the SDK's logger.
    """
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sdk_logger = logging.getLogger("indratrace")
    handler = Collect(level=logging.DEBUG)
    original_level = sdk_logger.level
    sdk_logger.addHandler(handler)
    sdk_logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        sdk_logger.removeHandler(handler)
        sdk_logger.setLevel(original_level)


def sdk_warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Warnings out of `records`, which `sdk_log` already scoped to the
    `indratrace` logger — OpenTelemetry's own warnings (e.g. "Overriding of
    current TracerProvider is not allowed", on the second init in a session)
    would otherwise be miscounted as ours."""
    return [record for record in records if record.levelno == logging.WARNING]


@pytest.fixture
def app_logs_at_info() -> Iterator[None]:
    """A host app that configured logging at INFO — the common case.

    The SDK deliberately does not lower the root logger's level, so a test that
    wants its INFO records bridged has to ask for it, exactly as a real app
    does via `basicConfig(level=INFO)` (or uvicorn/gunicorn).
    """
    root = logging.getLogger()
    original = root.level
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.setLevel(original)


@pytest.fixture
def production_export_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    """Undo the suite-wide shrink, for tests asserting on the real budget.

    Runs after the autouse `clean_env`, so it wins.
    """
    monkeypatch.setattr(TIMEOUT_ATTR, PRODUCTION_EXPORT_TIMEOUT_SECONDS)
    return PRODUCTION_EXPORT_TIMEOUT_SECONDS
