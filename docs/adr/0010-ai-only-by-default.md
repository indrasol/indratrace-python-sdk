# ADR 0010 — AI-only by default

- **Status:** Proposed (pending maintainer review)
- **Date:** 2026-10-30
- **Supersedes in part:** ADR 0006 (v0.1 emits all three signals) — as a
  *default*. ADR 0007 (the log bridge never mutates the host app's logging)
  still holds whenever the bridge is on.
- **Amends:** `docs/conventions.md` § Span conventions (new "What is sent" and
  "Run roots"), § Transport (which exporters carry the key).
- **Shipped in:** v1.2.0 (minor: defaults change, nothing public is removed).

## Context

IndraTrace's product is observability for AI applications: model calls, their
token usage and cost, agent runs and their tools, and user feedback. The
platform's ingest keeps exactly those signals and drops the rest — HTTP server
spans, database and framework spans, application log records and application
metrics are accepted and discarded (a workspace can ask support to keep
everything, which is not the default).

Since ADR 0006 the SDK has sent all three OpenTelemetry signals by default:
every HTTP request became a server span, every INFO+ log record was exported,
and request metrics were pushed every 60 s. For a typical web app calling a
model, most of those bytes are the HTTP and log traffic the platform then
drops. The customer pays the egress and CPU for telemetry nobody reads, and
the SDK's own README promises a "full trace" view the product no longer has.

## Decision

`init_observability()` gains `ai_only: bool = True` and `logs: bool | None =
None`. With the default:

| Signal | 1.1 default | 1.2 default (`ai_only=True`) |
|---|---|---|
| Model spans (GenAI instrumentors, `record_llm_usage`) | on | **on** |
| Agent / turn / tool / step spans (`trace_agent`, `trace_tool`, `trace_step`, Claude Agent SDK, MCP tools) | on | **on** |
| Feedback spans (`record_feedback`) | on | **on** |
| HTTP server spans (FastAPI / Django / Flask) | on | **off** — `instrument_http=True` opts in |
| stdlib `logging` + `loguru` bridge (logs signal) | on | **off** — `logs=True` opts in |
| Metrics signal | on | **off** |

- `instrument_http` and `logs` default to `None`, which means "follow
  `ai_only`". An explicit value always wins.
- `ai_only=False` with nothing else passed restores 1.1 behaviour exactly: the
  same providers, the same instrumentation, the same exporters.
- `instrument_http=True` (or the old `instrument_fastapi=True`) under
  `ai_only=True` still instruments, and logs **one** WARNING at init that the
  platform ignores those spans unless the workspace keeps all signals.
- When a signal is off its provider is **not built**: no logger or meter
  provider, no exporter, no background thread, and the OTel globals stay the
  no-op defaults. Nothing is batched and dropped locally.
- No attribute name, span name or span kind changes. No new dependency.

### Run roots

With HTTP spans off, a model call made while serving a request has no server
span above it, so it becomes its trace's root; likewise a `trace_tool` outside
any `trace_agent`. Both are complete on their own — a model span carries its
`gen_ai.*` usage, a tool span its `indratrace.span.kind` — and
`docs/conventions.md` § Run roots now says a consumer must read them without a
parent. `trace_agent` and Claude Agent SDK run spans are unchanged.

## Alternatives considered

- **Keep sending everything; let the platform drop it.** Already true, and the
  reason for this ADR: it costs customers bandwidth for nothing.
- **Drop non-AI spans in the SDK with a filtering span processor.** That is
  policy in the SDK (ADR 0003 forbids it), and it still pays the cost of
  creating every HTTP span. Not instrumenting is cheaper and needs no filter.
- **A major version (2.0).** Nothing public is removed or renamed; every 1.1
  call still runs. The change is to defaults, and the one-line way back
  (`ai_only=False`) is documented. A minor version, called out at the top of
  the CHANGELOG.

## Consequences

- New installs send only what IndraTrace uses. Existing installs on 1.1 keep
  working unchanged; the platform ignores what it does not need.
- An app upgrading from 1.1 loses HTTP spans, exported logs and metrics unless
  it passes `ai_only=False` (or the per-signal opt-ins). CHANGELOG and README
  lead with this.
- `log_level` only matters when the log bridge is on.
- The debug banner's `signals:` line reports the signals actually wired, and the
  loguru line says why it is off.
- Tests: `tests/test_ai_only.py` covers the defaults, the opt-ins, the warning,
  and the run-root contract; the rest of the suite passes `ai_only=False`
  wherever it tests HTTP, logs or metrics, so it doubles as the 1.1 parity check.
