# Product spec — indratrace SDK v0.1

*Living doc.*

## What this is

The integration surface of the IndraTrace observability platform: a Python
package that lets any product plug into central tracing with one init call.
First consumers: Indrasol's own products and the platform itself as product #0
(dogfood). Long-term: public package, anyone can subscribe to the hosted
platform for a key (see ADR 0002).

## The entire developer experience (v0.1)

```python
from indratrace import init_observability, trace_agent, trace_tool

# once, at app startup
init_observability(
    product="compliance",
    env="prod",
    ingest_key="obs_live_...",          # from the platform's Product Registry
    # endpoint defaults to INDRATRACE_ENDPOINT env var
)

@trace_agent("compliance-checker")      # wraps a whole agent request
async def run(query): ...

@trace_tool                             # wraps each tool the agent calls
async def risk_score(vendor): ...
```

That's it. HTTP spans, model-call spans with exact token counts, and basic
metrics flow automatically after `init_observability()`. Existing `logging`
calls ship too when the app is at INFO (basicConfig/uvicorn/gunicorn); an app
that never configured logging opts in with `init_observability(..., log_level="INFO")`
rather than have the SDK silently change what its own handlers print.

## v0.1 acceptance (definition of done for the release)

1. A FastAPI app with the snippet above shows: HTTP spans, agent→tool span
   trees, model spans with correct `gen_ai.usage.*` for Anthropic + OpenAI
   (streaming included), stdlib logs with trace context, request metrics — all
   verified landing in the dev harness ClickHouse.
2. SDK survives a dead Collector endpoint with zero impact on the host app.
3. All resource attributes per conventions.md present on every signal.
4. `pip install indratrace` from PyPI gets 0.1.0; extras:
   `indratrace[fastapi,anthropic,openai]`.
5. Docs: README quickstart + CHANGELOG.

## v0.2 surface — product analytics, content capture, more providers, `trace_step`

The public API grows from five names to nine —
`init_observability`, `trace_agent`, `trace_tool`, `trace_step`,
`record_llm_usage`, `session`, `record_feedback`, `current_trace_id`:

```python
from indratrace import session, record_feedback, current_trace_id, trace_step

with session(session_id="conversation-42", user_id="u-1001"):
    answer = run(query)          # every span here carries session.id + user.id

tid = current_trace_id()         # capture at answer time…
record_feedback(1, comment="👍", trace_id=tid)   # …score it later, out of band

@trace_step                      # time a plain non-AI function
def parse(raw): ...
```

Part 1 — product analytics (prompt 06):

- **`session(session_id=, user_id=)`** — context manager or imperative handle
  (`.detach()`/`.close()` for middleware). Every span started while active —
  agent/tool, FastAPI HTTP, GenAI model, feedback — carries `session.id` /
  `user.id` (baggage + a span processor; works across async/threads). Session
  and user ids are span attributes, never metric labels (conventions.md).
- **`record_feedback(score, comment=, trace_id=)`** — a `feedback` span
  (`indratrace.span.kind="feedback"`, `feedback.score`, optional
  `feedback.comment`, `feedback.trace_id`) joinable to the original trace.
- **`current_trace_id()`** — the current trace id as hex, for products to store
  alongside an answer and pass to `record_feedback` later.

Part 2 — content, providers, timing (prompt 07):

- **`capture_content`** — `init_observability(..., capture_content=False)` /
  `INDRATRACE_CAPTURE_CONTENT`. On records prompt/completion text on model spans
  (`gen_ai.input.messages` / `gen_ai.output.messages`); **off by default**
  because prompts carry customer data. Token counts are captured either way.
- **Gemini + Bedrock extras** — `indratrace[gemini]` / `indratrace[bedrock]`
  join `anthropic` / `openai` for auto-instrumented model spans with exact
  token counts.
- **`@trace_step`** — neutral sibling of `@trace_tool` (`step <func>`,
  `indratrace.span.kind="step"`, `step.name`) for timing db queries, parsers,
  and other non-AI work — including *inside a slow REST endpoint*, since a plain
  FastAPI service is auto-traced from the one init line with no per-route code.

Housekeeping across the increment: Apache-2.0 `LICENSE` at repo root (ships in
the sdist); `[project.urls]` gains `Repository` + `Changelog`; GitHub Actions
bumped to current majors; README plain-language pass (defines *span*/*trace*, a
"Tracing a regular REST API" section, "Bring your own backend", content-capture,
and a configuration table), verified against the built wheel.

## Explicitly out of scope (v0.1)

- TypeScript/browser SDK (blocked on the public-ingest-key design decision).
- Custom metrics API, sampling config, redaction (platform's job).
- Cost computation (query-time, platform side).
- The observability agent (platform, later).
