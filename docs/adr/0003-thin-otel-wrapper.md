# ADR 0003 — Thin OpenTelemetry wrapper; OTLP is the only wire contract

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
We need products to plug in with one line, but we must not rebuild
instrumentation (OTel already does it) or invent a wire protocol.

## Decision
The SDK is a thin (~300–400 line) opinionated wrapper around the OpenTelemetry
Python SDK. Public API is exactly three calls: `init_observability`,
`trace_agent`, `trace_tool`. It emits standard OTLP to a configurable endpoint
with the ingest key as an auth header.

## Alternatives considered
- **Custom client + custom protocol:** total control, but loses the OTel
  ecosystem (collectors, SIEMs, backends) and becomes a maintenance tarpit.
- **Raw OTel with a how-to doc:** no code to maintain, but every product
  re-implements config, tagging drifts, and the one-line-plug goal dies.

## Consequences
- Policy (redaction, sampling, routing) lives in the Collector, NOT the SDK —
  policy changes never require product redeploys.
- SDK must fail silent: if the Collector is unreachable, products must never
  slow down or error (batch export, drop on full queue, never block requests).
- Dependencies limited to OTel packages only. No platform code, ever.
