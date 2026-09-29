# ADR 0004 — Local test harness lives in the SDK repo

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
An observability SDK can't be tested against nothing — it needs an OTLP
receiver and a store to assert rows landed. The platform's real
ClickHouse/Collector deployment is part of the IndraTrace backend (closed
source), which is not available to SDK contributors.

## Decision
This repo carries a throwaway dev harness in `dev/`: a docker-compose file with
an OTel Collector (contrib image, ClickHouse exporter) + ClickHouse. Clone →
`docker compose up` → run tests. CI uses the same harness.

## Alternatives considered
- **Use the backend's deployment for tests:** keeps all ClickHouse config in
  one place, but SDK development and CI would require access to, and booting
  (a growing part of), the closed-source backend. A repo should prove itself
  correct in isolation — this is what Sentry/Datadog/OTel SDK repos do.

## Consequences
- The harness is a dumb OTLP receiver, NOT a platform copy. No key
  verification, no redaction, default exporter schema. Keep it minimal.
- The IndraTrace backend separately owns the real deployment (custom schema,
  TTLs, auth, infrastructure). Divergence between harness and platform schema
  is fine — the SDK's correctness target is "emits correct OTLP", not "matches platform
  tables".
