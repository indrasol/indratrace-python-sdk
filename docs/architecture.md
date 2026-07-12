# Architecture — indratrace SDK

*Living doc. Describes the current state of this repo. For the full platform
picture, see the platform repo (`indratrace-platform`).*

## Where this SDK sits

```
Product code (FastAPI app + AI agents)
        │  imports indratrace
        ▼
indratrace SDK  ──  thin wrapper over OpenTelemetry Python SDK
        │  OTLP/HTTP (4318), header: x-indratrace-key: <api_key>
        ▼
OTel Collector  →  ClickHouse          ← the platform (other repo)
```

The SDK **produces** telemetry. Everything after the wire (verify, redact,
sample, store, query, render) is the platform's job. The SDK never talks to a
database and contains zero policy.

## Module layout (`src/indratrace/`)

| Module | Job |
|---|---|
| `__init__.py` | Public API surface: `init_observability`, `trace_agent`, `trace_tool`, `trace_step`, `session`, `record_feedback`, `current_trace_id`, `record_llm_usage`, `__version__`. Nothing else is public. Names land here as they are implemented. |
| `config.py` | `ObsConfig` + `resolve_config()`: explicit args > env vars (`INDRATRACE_ENDPOINT`, `INDRATRACE_KEY`, `INDRATRACE_PRODUCT`, `INDRATRACE_ENV`) > defaults. `build_resource(cfg)` builds the OTel `Resource` with the required attributes (see conventions.md). Also owns the export timeout (see "Never block", below). |
| `init.py` | `init_observability()`: wires tracer/logger/meter providers, OTLP exporters with batch processors, FastAPI auto-instrumentation, GenAI auto-instrumentation (if extras installed). Idempotent; fail-silent. **Today: traces + logs + metrics + FastAPI + GenAI.** Also owns the **`debug`** flag (`INDRATRACE_DEBUG`): when on, attaches a console handler to the `indratrace` logger at DEBUG (only if it has none — no double-logging), lowers that logger to DEBUG, logs a startup banner (version/product/env/endpoint + each instrumentation enabled/skipped-with-reason), wraps the OTLP exporters so each export's success/failure is *audible*, and fires one startup connectivity probe. Diagnostics only — it never weakens fail-silence (below). |
| `agent.py` | `@trace_agent(name)` — parent span per agent request. `@trace_tool` — child span per tool call (duration, status, exception recording). `@trace_step` — same machinery, `step <func>` span for timing plain non-AI functions without mislabeling them a "tool". All support sync + async. *Built.* |
| `genai.py` | Enables OpenLLMetry instrumentors (anthropic / openai / gemini / bedrock) when present, each handed OUR tracer provider (never the frozen global); optional prompt/completion content capture gated by `capture_content` (maps to the instrumentors' `TRACELOOP_TRACE_CONTENT`, which the SDK sets explicitly so the platform default is off); thin manual-capture fallback (`record_llm_usage(model, input_tokens, output_tokens, ...)`) stamping canonical `gen_ai.*` usage on the current span for unsupported providers. *Built.* |
| `agent_sdk.py` | Claude Agent SDK auto-instrumentation (ADR 0008) — the differentiating feature. Patches the **methods** the entrypoints funnel through (`InternalClient.process_query` for `query()`, and `ClaudeSDKClient`'s `__init__`/`receive_*`), against OUR provider, to open an `agent` span per run and a `turn` child per assistant message, reading token usage off the message objects (the loop runs in a subprocess CLI, so the anthropic instrumentor is blind to it). Methods not the module-level `query` reference, so `from claude_agent_sdk import query` before init is still traced (ADR 0008 correction). Merges observe-only `PreToolUse`/`PostToolUse`/`PostToolUseFailure` hooks non-destructively into the caller's options for `tool` spans (incl. `tool.mcp_server`). Streams close on early abandon via a `finally`. Optional extra; absent (or the seam moved) = silent skip; fail-silent. *Built.* |
| `context.py` | Product-analytics primitives. `session(session_id=, user_id=)` — context manager **and** imperative handle (returns a `_SessionScope` with `detach()`/`close()`) — puts the two ids into OTel baggage; a `SessionSpanProcessor` (registered in `init_observability`, `on_start` only) copies them onto every span, so auto-instrumented spans get them too. `record_feedback(score, comment=, trace_id=)` emits a `feedback` span linkable to a trace; `current_trace_id()` returns the current trace id (hex) so products can capture it at answer time. Fail-silent, no-op without init. *Built.* |
| `version.py` | Single source of the version string. |

## Signal wiring (v0.1)

All three providers are built from the same `Resource`, so the attribute
contract holds identically on every signal.

- **Traces** — OTLP span exporter, batch processor. HTTP server spans come free
  from `FastAPIInstrumentor`; agent/tool spans from decorators; model spans from
  GenAI instrumentors carrying exact provider-reported token counts, nested
  under the enclosing agent/tool spans (they share the SDK's tracer provider).
  A **`SessionSpanProcessor`** (context.py) is registered ahead of the batch
  processor: in `on_start` it reads `session.id`/`user.id` from baggage and
  stamps them as attributes, so every span started inside `session(...)` —
  including auto-instrumented ones — carries them. `on_start`-only, so it adds
  nothing to the export path. *Built.*
- **Logs** — `LoggerProvider` + `BatchLogRecordProcessor` + OTLP log exporter.
  An OTel `LoggingHandler` is attached to the **root** stdlib logger, so
  existing `logger.info(...)` calls ship as structured records; those emitted
  inside a span carry its trace context, which is what links a log line to its
  trace. The handler carries a filter (`_ExcludeIndraTrace`) dropping records
  from the `indratrace` and `opentelemetry` logger trees: both log *about* the
  export path, so shipping them makes a failing export log an error that
  becomes another record to export — a loop. Filtering on the handler (rather
  than `propagate = False`) keeps that scoped, so an operator's own handler on
  the `indratrace` logger still sees the diagnostics. **`init_observability`
  does not change the root logger's level** unless the caller passes
  `log_level=` — an app already at INFO (basicConfig/uvicorn/gunicorn) ships
  its records with no argument; a quiet app opts in explicitly rather than
  having its own console/file handlers silently start emitting suppressed
  INFO. *Built.*
- **Metrics** — `MeterProvider` + `PeriodicExportingMetricReader` over an OTLP
  metric exporter. v0.1 relies on auto-instrumentation metrics (request
  count/duration); no custom-metric API. *Built.*

### The decorators (`agent.py`)

`@trace_agent(name)` / `@trace_tool` resolve their tracer **lazily, per call**,
preferring `init._get_provider()` over the global (see "Testing notes"). Two
rules govern them:

1. **Transparent.** An exception is recorded on the span, status set to ERROR,
   then re-raised unchanged. The decorators never alter control flow.
2. **Never raise from instrumentation.** If no provider exists — init never ran,
   or failed — the call still runs, against a non-recording span.

## Hard rules (from ADRs — do not violate)

1. **Fail silent** (ADR 0003): SDK errors must never break or block the host
   app. Export is async/batched; queue overflow drops data, never blocks.
   `init_observability` catches everything (including bad config) and logs one
   warning; a failure part-way through shuts down whatever it already built, so
   no exporter thread is left running unowned. Note the OTLP exporter's own
   default timeout is **10s**, which stalls `shutdown()` — and process exit —
   when the collector is down; we pass an explicit
   `DEFAULT_EXPORT_TIMEOUT_SECONDS` to all three exporters instead, and pin the
   metric reader's `export_timeout_millis` to it as well. Don't remove them:
   shutdown drains the three providers serially, so an unpinned exporter costs
   10s of process exit each. **`debug=True` makes failures *audible*, not loud:**
   it prints the banner and turns silent export drops into visible WARNING lines
   on the `indratrace` logger, but the host app still never sees an exception —
   the diagnostics are console-only and are filtered off the OTLP log-export
   path, so debug mode must never become a way for an SDK failure to surface in
   the app's control flow.
2. **No policy in the SDK** (ADR 0003): no redaction, sampling, or routing
   logic here. Collector's job.
3. **OTel deps only** (ADR 0003); provider instrumentation as optional extras
   (ADR 0005).
4. **No cost math in the SDK** (ADR 0005): raw token counts only.
5. **Attribute contract is law** (conventions.md): every signal carries
   `product`, `deployment.environment`, `tenant.id`, `service.version`.

## Dev harness (`dev/`)

Throwaway receiver for local dev + CI (ADR 0004): docker-compose running
`otel/opentelemetry-collector-contrib` (ClickHouse exporter, default `otel_*`
tables) + `clickhouse/clickhouse-server`. Not a platform copy — no auth, no
redaction. Integration tests send telemetry, then query ClickHouse to assert
arrival.

## Testing notes

- `pytest -m "not integration"` needs no Docker; integration tests skip
  themselves when the harness isn't reachable. Query ClickHouse over HTTP
  (8123) as `otel:otel` — the image confines `default` to loopback.
- **OTel allows each `set_*_provider` once per process.** After the first
  `init_observability`, the *global* tracer/logger/meter providers are frozen,
  so a later test's providers are built but never consulted by
  `FastAPIInstrumentor`. Tests read the SDK's own providers via
  `init._get_provider()` / `_get_logger_provider()` / `_get_meter_provider()`,
  and the integration test binds its app with
  `instrument_app(app, tracer_provider=...)` rather than relying on the global.
  `init._reset_for_tests()` cannot undo the freeze. The decorators sidestep this
  entirely: they resolve the tracer from the SDK's provider on every call.
- **A refused connection is not free.** The OTLP exporter retries with backoff
  until its timeout is spent, and shutdown drains the queue — so at the
  production 3s, every offline test that emitted a signal paid seconds of
  teardown across three providers. `conftest.py` shrinks
  `DEFAULT_EXPORT_TIMEOUT_SECONDS` for the offline suite; the two
  shutdown-stall regression tests opt back into the real value via the
  `production_export_timeout` fixture. Keep the offline suite under ~15s.
- **`sdk_log` fixture** captures the `indratrace` logger's records directly
  (the export-path exclusion is a *handler filter*, so `propagate` stays true
  and `caplog` still works — but the fixture is the robust way to assert on the
  SDK's own diagnostics regardless).
- **`log_level` is opt-in.** Log-bridge tests that assert on shipped INFO
  records use the `app_logs_at_info` fixture (or set root themselves), because
  the SDK no longer lowers the root level on the caller's behalf.
- **`debug` tests read the SDK logger, not stdout.** `test_debug.py` mostly
  asserts on records captured via the `sdk_log` fixture (the `indratrace`
  logger), which is robust regardless of where the console handler writes; a
  couple assert on `capsys.err` to prove the `StreamHandler` actually surfaces
  the banner. The **no-duplication** contract has two halves both pinned here:
  with an operator handler already on the `indratrace` logger, `debug=True` adds
  no second handler *and* still lowers the logger so the operator's handler sees
  DEBUG. The **export-failure** line is proven against the unreachable endpoint
  (the offline suite's shrunk timeout keeps the startup probe's flush fast), and
  a companion test asserts init still doesn't raise with debug on — audible,
  never loud (ADR 0003).
- **GenAI instrumentors are handed our provider, and mocked at the transport.**
  `enable_genai_instrumentation` always calls `.instrument(tracer_provider=...)`
  with the SDK's own provider — the global is frozen after the first init, so
  relying on it would send model spans to a stale provider (same freeze that
  bites `FastAPIInstrumentor`). Unit tests make no network call: they patch the
  provider client's low-level `post`/`_post` so the instrumentor's wrapper still
  runs and emits a span (patching `create()` would replace the wrapped method
  and emit nothing). The instrumentors monkeypatch the client class
  process-wide, so `_reset_for_tests` uninstruments them (guarded by
  `is_instrumented_by_opentelemetry` to avoid a noisy double-uninstrument). The
  provider SDKs (`anthropic`, `openai`) are dev-only test deps — the runtime
  extras pull only the instrumentors (ADR 0003/0005).
- **Attribute drift lives in a test, not a rewriter.** The instrumentors emit
  the provider identity under `gen_ai.provider.name`, not the `gen_ai.system`
  conventions.md originally specified (token names are unchanged). Rather than
  build a rewriting layer, `tests/test_genai.py::TestAttributeNames` pins the
  actual wire names and conventions.md records the mapping; the platform aliases
  at query time. If a future instrumentor bump changes a name, that test fails.
- **Content capture is empirically pinned too.** `TestContentCapture` mocks an
  Anthropic call under `capture_content=True`/`False` and asserts the prompt /
  completion land under `gen_ai.input.messages` / `gen_ai.output.messages` (on)
  or are absent (off), and that tokens are captured either way. It also proves
  the config precedence (explicit arg > `INDRATRACE_CAPTURE_CONTENT` > off). The
  trap it guards: the instrumentors' `TRACELOOP_TRACE_CONTENT` defaults to *on*
  when unset, so the SDK must set it explicitly — `enable_genai_instrumentation`
  writes it before instrumenting, and `_uninstrument_genai` clears it so a
  content-on run can't leak into the next init in the same process.
- **Agent SDK: the message path is mocked, the hook path is live-only.** The
  Claude Agent SDK runs its agent loop in a *subprocess* `claude` CLI (ADR 0008),
  so `test_agent_sdk.py` never spawns it. The **agent/turn/usage** path is fully
  offline: the real init patches `InternalClient.process_query`, the unit tests
  swap its *inner* for a fake yielding constructed
  `AssistantMessage`/`ResultMessage` objects and then call the genuine
  `claude_agent_sdk.query(...)` — so the tests exercise the actual production
  seam (which also guards the import-order fix), and the span tree +
  `gen_ai.usage.*` counts are asserted from in-memory spans (incl. the
  early-abandoned-stream close and nesting under `@trace_agent`). The **tool-span**
  path can't fire offline — hook callbacks are dispatched by the CLI over the
  control channel — so the unit tests call the tracing hook callbacks directly
  with the CLI-shaped payload (asserting shape, MCP-server parsing, ERROR
  status), and the *live* CLI-dispatched path is proven in
  `tests/integration/test_agent_sdk_live.py` (marked `genai`; also skips without
  the `claude` CLI on PATH). That live test polls ClickHouse until an **agent**
  row is present, not just any row: the agent span ends *last* (it wraps the run)
  so it flushes in a later OTLP batch than the turn/tool spans — a first-row poll
  would race it. `_reset_for_tests` restores the original methods so wrappers
  don't stack across a session.
- **Gemini / Bedrock: wiring, not mocked tokens.** Both instrumentors import
  their provider SDK (`google.genai`, `botocore`) at module load, so an absent
  provider SDK is the same silent `ImportError` skip as an absent extra.
  `TestGeminiBedrockWiring` `importorskip`s each and asserts init instruments it
  and `_reset_for_tests` unpatches it; a mocked-transport token test would need
  a live client per provider, and the anthropic mock already proves the
  token+content path end to end. The `dev` extra pulls `google-genai` / `boto3`
  so these run offline; on a core install they skip.
