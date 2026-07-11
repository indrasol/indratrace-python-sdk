"""End-to-end: instrumented app -> Collector -> ClickHouse row.

Covers all three signals — HTTP and agent/tool spans, logs carrying trace
context, and metrics — against the dev harness
(`docker compose -f dev/docker-compose.yml up -d`). Skipped automatically when
it isn't reachable, so `pytest` stays green without Docker.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from opentelemetry import trace

from indratrace import (
    current_trace_id,
    init_observability,
    record_feedback,
    session,
    trace_agent,
    trace_tool,
)
from indratrace.init import (
    _get_logger_provider,
    _get_meter_provider,
    _get_provider,
    _reset_for_tests,
)
from indratrace.version import __version__

OTLP_ENDPOINT = "http://localhost:4318"
CLICKHOUSE_URL = "http://localhost:8123"
# Throwaway harness credentials from dev/docker-compose.yml. The image confines
# the `default` user to loopback, so a host-side client needs its own user.
CLICKHOUSE_AUTH = ("otel", "otel")

# The collector batches with a 1s timeout, then inserts. Poll generously; CI
# machines are slow.
ROW_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5


def clickhouse_query(sql: str) -> str:
    """Run SQL over the ClickHouse HTTP interface; SQL goes in the body."""
    response = httpx.post(
        CLICKHOUSE_URL, content=sql, auth=CLICKHOUSE_AUTH, timeout=10.0
    )
    response.raise_for_status()
    return response.text.strip()


def harness_is_up() -> bool:
    try:
        if httpx.get(f"{CLICKHOUSE_URL}/ping", timeout=2.0).status_code != 200:
            return False
        # The collector has no health endpoint on 4318; an empty POST to the
        # traces path proves it is listening (it 400s on a bad body, which is
        # still a live server).
        httpx.post(f"{OTLP_ENDPOINT}/v1/traces", content=b"", timeout=2.0)
        return True
    except (httpx.HTTPError, OSError):
        return False


HARNESS_DOWN_REASON = (
    "dev harness not reachable "
    "(docker compose -f dev/docker-compose.yml up -d)"
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not harness_is_up(), reason=HARNESS_DOWN_REASON),
]


@pytest.fixture
def product() -> str:
    """A unique product per run, so we assert on *our* row, not a stale one."""
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


def wait_for_rows(sql: str) -> list[list[str]]:
    """Poll `sql` until it returns rows, or the deadline passes.

    The collector batches (1s) then inserts, so nothing is visible instantly.
    Returns TSV rows split into columns; empty when the deadline passed.
    """
    deadline = time.monotonic() + ROW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        raw = clickhouse_query(sql)
        if raw:
            return [line.split("\t") for line in raw.splitlines()]
        time.sleep(POLL_INTERVAL_SECONDS)
    return []


def wait_for_span(product: str, span_kind: str = "Server") -> list[str] | None:
    """Poll until the `span_kind` span for `product` lands, or time runs out.

    FastAPI emits several spans per request (the SERVER span plus INTERNAL
    `http send` children), so filter rather than taking the newest row.
    """
    sql = (
        "SELECT SpanName, SpanKind, "
        "ResourceAttributes['product'], "
        "ResourceAttributes['deployment.environment'], "
        "ResourceAttributes['tenant.id'], "
        "ResourceAttributes['service.name'], "
        "ResourceAttributes['service.version'], "
        "ResourceAttributes['telemetry.sdk.wrapper'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        f"AND SpanKind = '{span_kind}' "
        "ORDER BY Timestamp DESC LIMIT 1 FORMAT TSV"
    )

    deadline = time.monotonic() + ROW_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        rows = clickhouse_query(sql)
        if rows:
            return rows.split("\t")
        time.sleep(POLL_INTERVAL_SECONDS)
    return None


def test_fastapi_request_lands_in_clickhouse(product: str) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        service_name="itest-api",
        service_version="1.2.3",
        instrument_fastapi=False,
    )
    provider = _get_provider()
    assert provider is not None

    app = FastAPI()

    @app.get("/hello")
    def hello() -> dict:
        return {"ok": True}

    # Bind this app to the provider init_observability built, rather than the
    # process-global one. OTel freezes the global provider at the first
    # `set_tracer_provider`, so in a shared pytest process an earlier test's
    # provider would otherwise swallow these spans. Same wire path either way.
    FastAPIInstrumentor().instrument_app(app, tracer_provider=provider)

    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200

    # Push the batch out now instead of waiting on the batch timeout.
    assert provider.force_flush(timeout_millis=10_000), "span never left the SDK"

    row = wait_for_span(product)
    assert row is not None, (
        f"no span row for product={product} within {ROW_TIMEOUT_SECONDS}s"
    )

    (
        span_name,
        span_kind,
        row_product,
        environment,
        tenant_id,
        service_name,
        service_version,
        wrapper,
    ) = row

    assert row_product == product
    assert span_kind == "Server", f"expected an HTTP server span, got {span_kind}"
    assert "/hello" in span_name

    # The full resource contract from docs/conventions.md.
    assert environment == "dev"
    assert tenant_id == "internal"
    assert service_name == "itest-api"
    assert service_version == "1.2.3"
    assert wrapper == f"indratrace/{__version__}"


def test_unreachable_collector_does_not_break_requests(product: str) -> None:
    """ADR 0003: killing the Collector must not fail a single request."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    init_observability(
        product=product,
        endpoint="http://127.0.0.1:1",  # nothing listens here
        instrument_fastapi=False,
    )
    provider = _get_provider()
    assert provider is not None

    app = FastAPI()

    @app.get("/hello")
    def hello() -> dict:
        return {"ok": True}

    FastAPIInstrumentor().instrument_app(app, tracer_provider=provider)

    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/hello").status_code == 200

    provider.force_flush(timeout_millis=2_000)  # fails internally; never raises


def test_agent_and_tool_spans_form_a_tree(product: str) -> None:
    """Agent -> tool -> nested tool, with a failing tool marked ERROR.

    The decorators resolve the tracer from the SDK's own provider, so unlike
    the FastAPI test they need no `instrument_app` wiring to dodge the frozen
    global provider.
    """
    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
    )
    provider = _get_provider()
    assert provider is not None

    @trace_tool
    def failing_tool() -> None:
        raise RuntimeError("vendor lookup exploded")

    @trace_tool
    def risk_score(vendor: str) -> int:
        # A nested tool call: proves the parent chain is more than one deep.
        try:
            failing_tool()
        except RuntimeError:
            pass
        return len(vendor)

    @trace_agent("compliance-checker")
    def run(vendor: str) -> int:
        return risk_score(vendor)

    assert run("acme") == 4

    assert provider.force_flush(timeout_millis=10_000), "spans never left the SDK"

    rows = wait_for_rows(
        "SELECT SpanName, SpanId, ParentSpanId, TraceId, StatusCode, "
        "SpanAttributes['indratrace.span.kind'], SpanAttributes['agent.name'], "
        "SpanAttributes['tool.name'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "FORMAT TSV"
    )
    assert len(rows) == 3, f"expected agent + 2 tool spans, got {len(rows)}"

    spans = {row[0]: row for row in rows}
    agent = spans["agent compliance-checker"]
    tool = spans["tool risk_score"]
    failing = spans["tool failing_tool"]

    # One trace, and the parent chain agent -> risk_score -> failing_tool.
    assert len({row[3] for row in rows}) == 1, "spans split across traces"
    assert agent[2] == "", "the agent span is the trace root"
    assert tool[2] == agent[1]
    assert failing[2] == tool[1]

    # Attributes per docs/conventions.md.
    assert agent[5] == "agent"
    assert agent[6] == "compliance-checker"
    assert tool[5] == "tool"
    assert tool[7] == "risk_score"

    # The raised exception marks only the tool that raised.
    assert failing[4] == "Error"
    assert tool[4] == "Unset", "a handled tool error must not taint its caller"
    assert agent[4] == "Unset"


def test_log_inside_a_span_lands_with_the_same_trace_id(product: str) -> None:
    """A stdlib log line inside an agent span links to that trace.

    `log_level="INFO"` opts this app into shipping INFO — the SDK does not
    lower the root level on its own (see TestRootLoggerLevel).
    """
    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
        log_level="INFO",
    )
    tracer_provider = _get_provider()
    logger_provider = _get_logger_provider()
    assert tracer_provider is not None and logger_provider is not None

    message = f"audit complete for {product}"
    seen: dict[str, str] = {}

    @trace_agent("logger")
    def run() -> None:
        span_context = trace.get_current_span().get_span_context()
        seen["trace_id"] = format(span_context.trace_id, "032x")
        logging.getLogger("product.audit").info(message)

    run()

    assert tracer_provider.force_flush(timeout_millis=10_000)
    assert logger_provider.force_flush(timeout_millis=10_000)

    rows = wait_for_rows(
        "SELECT Body, TraceId, SeverityText, "
        "ResourceAttributes['product'], ResourceAttributes['tenant.id'] "
        "FROM otel.otel_logs "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "FORMAT TSV"
    )
    assert rows, f"no log row for product={product} within {ROW_TIMEOUT_SECONDS}s"

    (body, trace_id, severity, row_product, tenant_id) = rows[0]
    assert body == message
    assert trace_id == seen["trace_id"], "the log did not link to its span's trace"
    assert severity == "INFO"
    assert row_product == product
    assert tenant_id == "internal"  # the resource contract holds on logs too


def test_metrics_land_with_the_product_resource_attribute(product: str) -> None:
    """v0.1 has no custom-metric API; assert the meter provider's wire path."""
    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
    )
    meter_provider = _get_meter_provider()
    assert meter_provider is not None

    meter_provider.get_meter("indratrace.itest").create_counter("itest.requests").add(1)

    # The periodic reader exports on a 60s timer; push it now.
    assert meter_provider.force_flush(timeout_millis=10_000)

    rows = wait_for_rows(
        "SELECT MetricName, Value, "
        "ResourceAttributes['product'], ResourceAttributes['deployment.environment'] "
        "FROM otel.otel_metrics_sum "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "FORMAT TSV"
    )
    assert rows, f"no metric row for product={product} within {ROW_TIMEOUT_SECONDS}s"

    (metric_name, value, row_product, environment) = rows[0]
    assert metric_name == "itest.requests"
    assert float(value) == 1.0
    assert row_product == product
    assert environment == "dev"


def test_session_and_feedback_land_and_join(product: str) -> None:
    """A session-wrapped agent+tool flow, then feedback keyed on its trace.

    Asserts every span of the request carries the same `session.id`/`user.id`,
    and that a `record_feedback` emitted afterwards carries `feedback.trace_id`
    equal to the request's trace — the join the platform relies on.
    """
    init_observability(
        product=product,
        env="dev",
        endpoint=OTLP_ENDPOINT,
        ingest_key="dev-local",
        instrument_fastapi=False,
    )
    provider = _get_provider()
    assert provider is not None

    captured: dict[str, str] = {}

    @trace_tool
    def risk_score(vendor: str) -> int:
        return len(vendor)

    @trace_agent("compliance-checker")
    def run(vendor: str) -> int:
        captured["trace_id"] = current_trace_id() or ""
        return risk_score(vendor)

    with session(session_id="conversation-42", user_id="u-1001"):
        assert run("acme") == 4

    # The product stored the trace id at answer time and scores it out of band.
    record_feedback(1, comment="spot on", trace_id=captured["trace_id"])

    assert provider.force_flush(timeout_millis=10_000), "spans never left the SDK"

    rows = wait_for_rows(
        "SELECT SpanName, TraceId, "
        "SpanAttributes['session.id'], SpanAttributes['user.id'], "
        "SpanAttributes['indratrace.span.kind'], "
        "SpanAttributes['feedback.score'], SpanAttributes['feedback.trace_id'] "
        "FROM otel.otel_traces "
        f"WHERE ResourceAttributes['product'] = '{product}' "
        "FORMAT TSV"
    )
    # agent + tool + feedback = 3 spans.
    assert len(rows) == 3, f"expected agent + tool + feedback, got {len(rows)}"

    spans = {row[0]: row for row in rows}
    agent = spans["agent compliance-checker"]
    tool = spans["tool risk_score"]
    feedback = spans["feedback"]

    request_trace_id = agent[1]
    assert request_trace_id == captured["trace_id"], "current_trace_id() mismatch"

    # Every span of the request shares the session/user ids.
    for span in (agent, tool):
        assert span[2] == "conversation-42", f"{span[0]} missing session.id"
        assert span[3] == "u-1001", f"{span[0]} missing user.id"

    # The agent and tool are one trace; feedback is its own trace, joined by
    # feedback.trace_id back to the request.
    assert agent[1] == tool[1], "agent and tool split across traces"
    assert feedback[4] == "feedback"
    assert feedback[5] == "1"
    assert feedback[6] == request_trace_id, "feedback did not join to the request trace"
