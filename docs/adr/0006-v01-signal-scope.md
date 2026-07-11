# ADR 0006 — v0.1 emits all three signals; traces built first

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
OTel defines three signals: traces (per-request span trees, incl. agent/tool/
token detail), logs (structured events linkable via trace_id), and metrics
(pre-aggregated series for KPI tiles). The platform plan calls for all three.

## Decision
SDK v0.1.0 — the version that onboards product #1 — ships all three signals.
Build order inside v0.1: traces → logs → metrics, so the first end-to-end win
(span visible in ClickHouse) lands as early as possible.

## Alternatives considered
- **Traces-only v0.1:** fastest path, and request rate / error % / p95 are
  derivable from spans anyway. Rejected by product owner: first onboarded
  product should get the full plan's surface immediately.

## Consequences
- Three provider setups (tracer, logger, meter) wired in `init.py`.
- Logs bridge attaches trace context so log lines link to traces.
- Metrics kept minimal in v0.1 (request counters, latency histogram via
  auto-instrumentation) — no custom-metric public API until demand exists.
