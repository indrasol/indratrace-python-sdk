# Changelog

All notable changes to `indratrace` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
PyPI versions are immutable — fixes ship as new versions, never a re-upload.

## [Unreleased]

## [0.4.0] — 2026-07-10

**Public launch release**: debug diagnostics, Indrasol branding, and the standard
open-source community files. This is the version the repo goes public on.

### Added

- **`debug=True` diagnostics** — `init_observability(..., debug=True)` (or
  `INDRATRACE_DEBUG=1`; explicit arg wins). Turns the SDK's fail-*silent*
  behavior fail-*audible* without ever raising into the host app:
  - attaches a console handler to the `indratrace` logger at DEBUG — **only if
    that logger has no handlers of its own**, so an operator who already routes
    the SDK's diagnostics gets no duplicate line;
  - prints a **startup banner**: version, product, env, resolved endpoint,
    ingest-key/capture-content state, and one `enabled` / `skipped (reason)` line
    per optional integration (FastAPI, each GenAI provider, claude-agent-sdk) —
    so a `skipped (extra not installed)` line answers "why is my dashboard
    empty?" directly (the lesson of the 0.3.0 silent-failure follow-up);
  - makes **export outcomes visible** — an `export ok` at DEBUG, or a clear
    `export FAILED (…) — is the collector reachable?` WARNING — by wrapping the
    OTLP exporters' `export()` and emitting one startup connectivity probe.

  Off by default, so production consoles stay quiet; the debug lines go to the
  console only and are never shipped to the platform (the `indratrace` logger is
  filtered off the log-export path).

### Changed

- **Branding & docs** — the Indrasol/IndraTrace wordmark banner, the *Indrabot*
  mascot, and PyPI/Python/CI/license badges now head the README; a new
  **"Nothing showing up? Turn on debug"** section walks through reading the
  banner. Logo assets moved from `logo/` to `assets/`.
- **Community files** — `CONTRIBUTING.md` (dev setup, test markers, PR
  expectations, conventional commits), `SECURITY.md` (private disclosure to
  security@indrasol.com), `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), and
  GitHub issue templates for bug reports (which ask for SDK version, Python
  version, and `debug=True` output) and feature requests.
- **Release tooling** — `scripts/make_public_copy.sh` produces the pruned public
  copy (strips `docs/prompts/`, `docs/PROGRESS.md`, `.env`, caches; no `.git`),
  prints a manual-review checklist, and is idempotent. It does not git-init or
  push — that's the human's step.

## [0.3.0] — 2026-07-10

Zero-decorator **Claude Agent SDK** auto-instrumentation — the differentiating
feature. An app built on Anthropic's `claude-agent-sdk` gets its whole agent loop
traced from the single `init_observability()` call: a span per agent run, per
turn, and per tool/MCP call, with exact model token usage — no decorators.

### Added

- **Claude Agent SDK auto-instrumentation** (new module `agent_sdk.py`, ADR
  0008). With the `claude-agent-sdk` extra installed, `init_observability()`
  wraps the SDK's public `query` / `ClaudeSDKClient` entrypoints (against the
  SDK's own tracer provider) and emits:
  - an **`agent`** span per run (`indratrace.span.kind="agent"`,
    `agent.framework="claude-agent-sdk"`, `agent.name`), carrying the run-total
    `gen_ai.usage.*` token counts and `session.id`;
  - a **`turn`** child span per assistant message, with that turn's model and
    `gen_ai.usage.*` counts (incl. cache read/creation tokens);
  - a **`tool`** span per tool call via the SDK's official
    `PreToolUse`/`PostToolUse`/`PostToolUseFailure` hooks, with `tool.name`,
    `tool.mcp_server` for MCP tools (`mcp__<server>__<tool>`), and ERROR status
    on failure.

  **Why a new mechanism, not the anthropic instrumentor:** the Agent SDK runs
  its agent loop in a *subprocess* CLI and never touches the in-process
  `anthropic` client, so the GenAI instrumentor (ADR 0005) is blind to it. Token
  usage is read off the SDK's message objects instead — but under the **same**
  `gen_ai.usage.*` names, so the platform treats it identically. Raw counts only;
  `ResultMessage.total_cost_usd` is deliberately dropped (ADR 0005).

  Composes with everything else: session context propagates onto agent-sdk
  spans, and an agent-sdk run inside a `@trace_agent` nests correctly in one
  trace. Streams close cleanly on early abandon (no dangling spans). Optional
  extra `claude-agent-sdk = ["claude-agent-sdk>=0.2.116"]`; absent extra is a
  silent skip and any failure is fail-silent (ADR 0003), never touching the host
  app's control flow.

## [0.2.0] — 2026-07-10

Product-analytics surface (session/user context + feedback) plus content
capture, two more GenAI providers, and a neutral timing decorator. The public
API grows from five names to nine: adds `session`, `record_feedback`,
`current_trace_id`, and `trace_step`.

### Added

- **`session(session_id=None, user_id=None)`** — tags every span started in its
  scope with `session.id` / `user.id`. Usable as a context manager
  (`with session(...):`) or as an imperative handle for middleware (returns a
  scope with `.detach()`/`.close()`). Built on OTel baggage + a
  `SessionSpanProcessor` registered in `init_observability`, so the ids land on
  **every** span — decorator, FastAPI HTTP, GenAI model, and feedback spans —
  and propagate across `async`/`await` and threads. Nesting overrides per key.
  No-op without init (ADR 0003).
- **`record_feedback(score, comment=None, trace_id=None)`** — emits a `feedback`
  span (`indratrace.span.kind="feedback"`, `feedback.score`, optional
  `feedback.comment`, `feedback.trace_id`) tying a thumbs-up/down (or any
  numeric score) to a trace. Linkage is the explicit `trace_id`, else the
  current trace's id, else none (the span is still emitted). Carries
  session/user context when inside `session(...)`.
- **`current_trace_id() -> str | None`** — the current trace id as 32-char hex,
  for products to capture at answer time and pass to `record_feedback` later.
- **`@trace_step`** — the neutral sibling of `@trace_tool`: a span named
  `step <function_name>` with `indratrace.span.kind="step"` and `step.name`,
  for timing any non-AI function (a database query, a parser) without
  mislabeling it a "tool". Bare or called, sync or async; nests under the
  active agent/tool/HTTP span. No-op without init.
- **Opt-in content capture** — `init_observability(..., capture_content=False)`
  (or `INDRATRACE_CAPTURE_CONTENT`, arg wins). When on, GenAI model spans carry
  the prompt under `gen_ai.input.messages` and the completion under
  `gen_ai.output.messages`. **Off by default** — prompts carry customer data;
  typical use is on in dev/staging, off in prod. Token counts are captured
  regardless of the flag. (Implemented by setting the instrumentors'
  `TRACELOOP_TRACE_CONTENT`, which defaults to *on* when unset — so the SDK sets
  it explicitly to keep off the default.)
- **Gemini + Bedrock GenAI extras** — `indratrace[gemini]` and
  `indratrace[bedrock]` enable the OpenLLMetry Google-Generative-AI and Bedrock
  instrumentors, alongside the existing `anthropic` / `openai`. Absent extra is
  a silent skip, same as before.
- Convention: `session.id` / `user.id` are span attributes only, never metric
  labels; the `step` span shape and the content-capture attribute names
  (`docs/conventions.md`).

### Housekeeping

- **`LICENSE`** — full Apache-2.0 text at the repo root (copyright "Indrasol"),
  shipped in the sdist and wheel metadata.
- **`[project.urls]`** — added `Repository` and `Changelog` (GitHub URLs)
  alongside `Homepage`.
- **CI** — GitHub Actions bumped to current majors (`checkout@v7`,
  `setup-python@v6`, `upload-artifact@v7`, `download-artifact@v8`), clearing the
  Node.js 20 deprecation warnings; the offline unit job now also installs the
  `gemini` / `bedrock` extras.
- **README** — plain-language pass (defines *span* / *trace* up front, a
  "Tracing a regular REST API" section, and a note that the `logging` import in
  examples is Python's built-in logging, not required); new **Session & user
  context**, **Feedback**, **Bring your own backend** (SDK emits standard OTLP;
  point `endpoint=`/`INDRATRACE_ENDPOINT` at any OTLP receiver), **Capturing
  prompt & completion text**, and **Configuration** (parameter / env var /
  default table) sections. Every snippet verified against the built wheel.

## [0.1.0] — 2026-07-10

First functional release: OpenTelemetry-native observability for web apps and
AI agents behind a three-call public API.

### Added

- **`init_observability(...)`** — one call, at startup, wires all three signals
  (traces, logs, metrics) over OTLP/HTTP to an IndraTrace-compatible collector,
  authenticated with the `x-indratrace-key` header. Config precedence is
  explicit args > `INDRATRACE_*` env vars > defaults
  (`INDRATRACE_ENDPOINT`, `INDRATRACE_KEY`, `INDRATRACE_PRODUCT`,
  `INDRATRACE_ENV`). Idempotent and fail-silent: bad config or an unreachable
  collector logs one warning and leaves the host app un-instrumented — it never
  raises and never blocks (ADR 0003).
  - **Traces** — batched OTLP span exporter; FastAPI HTTP server spans via
    optional `fastapi` extra.
  - **Logs** — stdlib `logging` bridged into OTel, so existing `logger.info(...)`
    calls ship as structured records carrying their span's trace context.
    Opt-in via `log_level=` when the host app never configured logging; the SDK
    does not lower the root logger's level on its own.
  - **Metrics** — periodic OTLP metric reader over the auto-instrumentation
    metrics (request count/duration). No custom-metric API in 0.1.
- **`@trace_agent(name)` / `@trace_tool`** — parent-per-agent and child-per-tool
  spans, sync and async. Transparent: an exception is recorded on the span, the
  status set to `ERROR`, then re-raised unchanged. No-op (non-recording span)
  when `init_observability()` never ran.
- **GenAI auto-instrumentation** — optional `anthropic` / `openai` extras enable
  the OpenLLMetry instrumentors against the SDK's own tracer provider, so model
  spans carry exact, provider-reported token counts
  (`gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`, plus cache
  counts where reported) and nest under the enclosing agent/tool span. Streaming
  calls captured too (usage from the final streamed event).
- **`record_llm_usage(model, input_tokens, output_tokens, system=..., **extra)`**
  — manual fallback stamping the same canonical `gen_ai.*` usage attributes on
  the current span for providers the SDK does not auto-instrument.
- Ships `py.typed`; supports Python 3.10–3.13. Apache-2.0.

### Notes

- **No cost math, no policy in the SDK.** Raw token counts only; redaction,
  sampling, and routing are the platform/Collector's job (ADR 0003 / 0005).
- **Attribute drift.** The current instrumentors emit the provider identity
  under `gen_ai.provider.name`, not the `gen_ai.system` that
  `docs/conventions.md` originally specified (token names are unchanged). The
  SDK records the wire truth rather than rewriting it, and `record_llm_usage`
  stamps the same name so hand- and auto-instrumented spans match; the platform
  aliases the two at query time. See `docs/conventions.md`.

## [0.0.1] — 2026-07-09

Reserved-name stub. Published only to claim the `indratrace` name on PyPI and
prove the package installs; it carries no functional SDK.

[Unreleased]: https://github.com/indrasol/indratrace-python-sdk/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/indrasol/indratrace-python-sdk/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/indrasol/indratrace-python-sdk/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/indrasol/indratrace-python-sdk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/indrasol/indratrace-python-sdk/releases/tag/v0.1.0
[0.0.1]: https://github.com/indrasol/indratrace-python-sdk/releases/tag/v0.0.1
