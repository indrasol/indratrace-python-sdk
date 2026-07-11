# ADR 0004 — Local test harness lives in the SDK repo

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
An observability SDK can't be tested against nothing — it needs an OTLP
receiver and a store to assert rows landed. The platform's real
ClickHouse/Collector deployment lives in the platform repo and on Azure.

## Decision
This repo carries a throwaway dev harness in `dev/`: a docker-compose file with
an OTel Collector (contrib image, ClickHouse exporter) + ClickHouse. Clone →
`docker compose up` → run tests. CI uses the same harness.

## Alternatives considered
- **Harness in the platform repo:** keeps all ClickHouse config in one place,
  but SDK development and CI would require cloning and booting (a growing part
  of) the platform. A repo should prove itself correct in isolation — this is
  what Sentry/Datadog/OTel SDK repos do.

## Consequences
- The harness is a dumb OTLP receiver, NOT a platform copy. No key
  verification, no redaction, default exporter schema. Keep it minimal.
- The platform repo separately owns the real deployment (custom schema, TTLs,
  auth, Azure infra). Divergence between harness and platform schema is fine —
  the SDK's correctness target is "emits correct OTLP", not "matches platform
  tables".
