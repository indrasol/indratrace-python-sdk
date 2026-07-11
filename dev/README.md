# Dev harness

Throwaway local receiver for SDK development and CI (ADR 0004): OTel Collector
(contrib) + ClickHouse via docker-compose.

Not a platform copy: no key verification, no redaction, default `otel_*`
exporter tables. The SDK's correctness target is "emits correct OTLP".

## Quickstart

```bash
# 1. Start the stack (ClickHouse must go healthy before the collector starts)
docker compose -f dev/docker-compose.yml up -d
docker compose -f dev/docker-compose.yml ps

# 2. Install the smoke-test deps (once)
pip install -r dev/requirements.txt

# 3. Emit one span to localhost:4318
python dev/send_test_span.py

# 4. Prove it landed as a row (batch processor flushes within ~1s)
docker exec $(docker ps -qf name=clickhouse) clickhouse-client -q \
  "SELECT SpanName, ResourceAttributes['product'] FROM otel.otel_traces ORDER BY Timestamp DESC LIMIT 5"
# -> harness-smoke   harness-test
```

Teardown:

```bash
docker compose -f dev/docker-compose.yml down      # keep data volume
docker compose -f dev/docker-compose.yml down -v   # wipe data volume
```

Both are idempotent — `down` then `up` works, with or without `-v`. The
ClickHouse exporter's `create_schema: true` is a no-op against an existing
schema.

## Ports

| Port | What |
|---|---|
| 4318 | OTLP/HTTP — what the SDK targets (`INDRATRACE_ENDPOINT`) |
| 4317 | OTLP/gRPC |
| 8123 | ClickHouse HTTP (`/ping` backs the healthcheck) |
| 9000 | ClickHouse native — the collector's exporter uses this |

## Poking at the data

```bash
CH=$(docker ps -qf name=clickhouse)

docker exec $CH clickhouse-client -q "SHOW TABLES FROM otel"
docker exec $CH clickhouse-client -q \
  "SELECT ResourceAttributes FROM otel.otel_traces ORDER BY Timestamp DESC LIMIT 1 FORMAT Vertical"

# Telemetry is also echoed by the `debug` exporter:
docker compose -f dev/docker-compose.yml logs -f otel-collector
```

## Two things that will bite you

**ClickHouse users.** With only `CLICKHOUSE_DB` set, the 24.8 image confines the
`default` user to loopback (`127.0.0.1`, `::1`). The collector connects from
another container, so it gets rejected — and ClickHouse reports this as
`Authentication failed: password is incorrect`, which is misleading; it's a
network ACL, not a password. Hence the dedicated `otel` user in
`docker-compose.yml`. Those credentials are throwaway, not secrets.

`clickhouse-client` run via `docker exec` *is* on loopback, so it connects as
`default` with no password — which is why the query above needs no `--user`.

**The exporter DSN.** Use `endpoint: tcp://clickhouse:9000` plus a separate
`database: otel` field. Putting `?database=otel` in the DSN makes ClickHouse
24.8 fail the exporter's bootstrap with `code: 115, Unknown setting 'database'`.

## Versions

Pinned deliberately; bump together and re-run the smoke test.

- `clickhouse/clickhouse-server:24.8`
- `otel/opentelemetry-collector-contrib:0.111.0`
- `opentelemetry-sdk` / `opentelemetry-exporter-otlp-proto-http` — see
  `requirements.txt`
