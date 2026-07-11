"""Smoke test for the dev harness: emit one OTLP span at localhost:4318.

Deliberately does NOT import indratrace — this proves the harness works on its
own, so a failing integration test can't be blamed on the SDK. Resource
attributes follow the contract in docs/conventions.md.
"""

from __future__ import annotations

import os
import sys

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

ENDPOINT = os.getenv("INDRATRACE_ENDPOINT", "http://localhost:4318")

resource = Resource.create(
    {
        "service.name": "harness-smoke-test",
        "service.version": "0.0.0",
        "product": "harness-test",
        "deployment.environment": "dev",
        "tenant.id": "internal",
        "telemetry.sdk.wrapper": "indratrace/0.0.1",
    }
)

provider = TracerProvider(resource=resource)
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{ENDPOINT}/v1/traces"))
)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("indratrace.dev.harness")

with tracer.start_as_current_span("harness-smoke") as span:
    span.set_attribute("indratrace.span.kind", "agent")
    span.set_attribute("agent.name", "harness")

if not provider.force_flush(timeout_millis=10_000):
    print(f"FAIL: could not flush span to {ENDPOINT}", file=sys.stderr)
    sys.exit(1)

provider.shutdown()
print(f"OK: sent span 'harness-smoke' to {ENDPOINT}/v1/traces")
