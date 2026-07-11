<h1 align="center">
  <img src="https://raw.githubusercontent.com/indrasol/indratrace-python-sdk/main/assets/indrabot-mascot.png" width="64" align="center" alt="Indrabot">
  IndraTrace SDK
</h1>

<p align="center">
  <a href="https://pypi.org/project/indratrace/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/indratrace.svg"></a>
  <a href="https://pypi.org/project/indratrace/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/indratrace.svg"></a>
  <a href="https://github.com/indrasol/indratrace-python-sdk/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/indrasol/indratrace-python-sdk/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/indrasol/indratrace-python-sdk/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
</p>

OpenTelemetry-native observability SDK for the IndraTrace platform — one-line
instrumentation for web apps and AI agents: traces, logs, metrics, and
model-call token usage.

```bash
pip install indratrace
```

Two words to know up front:

- A **span** is one timed step — a web request, a database call, one model call.
- A **trace** is the full story of one request — its spans stacked on a
  timeline, so you can see where the time went and what called what.

`init_observability()` ships traces, logs, and metrics; `trace_agent` /
`trace_tool` wrap your agents and tools; and model spans carry exact,
provider-reported token counts when the GenAI extras are installed. Token counts
are recorded raw — the SDK never computes cost; the platform derives it at query
time.

```python
import logging

from fastapi import FastAPI

from indratrace import init_observability, trace_agent, trace_tool

# Once, at app startup.
init_observability(product="my-app", env="prod", ingest_key="...")

app = FastAPI()  # every HTTP request becomes a span, automatically


@trace_tool  # a span per tool call
async def risk_score(vendor: str) -> int:
    logging.getLogger(__name__).info("scoring %s", vendor)  # ships with trace context
    return len(vendor)


@trace_agent("compliance-checker")  # a span wrapping the whole agent request
async def run(query: str) -> int:
    return await risk_score(query)
```

The `import logging` above is just Python's built-in logging — nothing
IndraTrace-specific, and not required. Whatever your app already logs ships
automatically (see [Configuration](#configuration)); the line is there to show a
log call picking up its span's trace context.

Both decorators work on sync and async functions. They are transparent: a tool
that raises gets its span marked `ERROR` with the exception recorded, and the
exception then propagates to your code unchanged.

## Tracing a regular REST API

You don't need to be building an AI agent. For a plain FastAPI service, the one
init line is the whole setup — every endpoint is reported as a span, with its
status and duration, no per-route code:

```python
from fastapi import FastAPI

from indratrace import init_observability

init_observability(product="orders-api", env="prod", ingest_key="...")

app = FastAPI()


@app.get("/orders/{order_id}")            # this endpoint is now a span automatically
def get_order(order_id: str) -> dict:
    return {"id": order_id}
```

When one endpoint is slow and you want to see *inside* it — which query, which
parser ate the time — wrap that piece in `@trace_step`. It adds a child span
under the request so the timeline shows the breakdown:

```python
from indratrace import trace_step


@trace_step                               # a span named "step load_order"
def load_order(order_id: str) -> dict:
    ...                                   # e.g. a database query
    return {"id": order_id}
```

`@trace_step` is the neutral sibling of `@trace_tool`: same behavior, but for
timing ordinary functions (database queries, parsers, validation) where calling
them a "tool" would be misleading. Bare or called (`@trace_step()`), sync or
async, exceptions recorded and re-raised unchanged.

Install the extras you need — FastAPI for HTTP auto-instrumentation, and
`anthropic` / `openai` / `gemini` / `bedrock` for model spans with token usage:

```bash
pip install "indratrace[fastapi,anthropic,openai,gemini,bedrock]"
```

## Claude Agent SDK — zero instrumentation

If your app is built on Anthropic's
[`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/), the single
`init_observability()` call traces the **whole agent loop** — with no decorators
anywhere. Install the extra, init once, and every `query()` / `ClaudeSDKClient`
run produces:

- an **agent** span for the run,
- a **turn** span for each step of the loop, each with that turn's model and
  exact token usage (input, output, and cache tokens),
- a **tool** span for every tool the agent calls — including MCP tools, tagged
  with the MCP server name — with an error status if the tool failed.

They arrive as one nested trace, so you can see exactly what the agent did, how
many turns it took, which tools it called, and how many tokens each step spent.

```bash
pip install "indratrace[claude-agent-sdk]"
```

```python
from claude_agent_sdk import query, ClaudeAgentOptions

from indratrace import init_observability

init_observability(product="my-agent", ingest_key="...")   # once, at startup

# No decorators. This whole run is traced — agent → turns → tools → tokens.
async for message in query(prompt="Summarize today's incidents and file a ticket"):
    print(message)
```

That's it — there is nothing else to add. It works the same with the stateful
`ClaudeSDKClient` (each `receive_response()` is one traced run), it nests inside a
`@trace_agent` if you have one, it picks up `session(...)` context, and an
early-abandoned stream never leaves a span dangling. Token counts are stored raw;
the SDK never computes cost.

> Under the hood the Agent SDK runs the agent loop in a subprocess CLI, so the
> usage is read straight off the SDK's own messages — but it lands under the same
> `gen_ai.usage.*` names as any other model span.

## Token usage from model calls

With the `anthropic`, `openai`, `gemini`, or `bedrock` extra installed, every
provider call made after `init_observability()` produces a **model span**
carrying the exact, provider-reported token counts (`gen_ai.usage.input_tokens` /
`gen_ai.usage.output_tokens`), nested under whatever agent/tool span is active.
No wrapper, no config — just call the provider as you already do:

```python
import anthropic

from indratrace import init_observability, trace_agent, trace_tool

init_observability(product="my-app", ingest_key="...")
client = anthropic.Anthropic()


@trace_tool
def summarize(doc: str) -> str:
    msg = client.messages.create(               # model span with token counts,
        model="claude-haiku-4-5",               # a child of this tool span
        max_tokens=256,
        messages=[{"role": "user", "content": doc}],
    )
    return msg.content[0].text


@trace_agent("summarizer")
def run(doc: str) -> str:
    return summarize(doc)
```

Streaming calls are captured too — usage lands on the span from the final
streamed event.

For a provider the SDK does not auto-instrument, stamp the counts yourself from
inside a span with `record_llm_usage`:

```python
from indratrace import record_llm_usage

record_llm_usage(
    model="some-model-v2",
    input_tokens=resp.usage.input,
    output_tokens=resp.usage.output,
    system="acme-ai",
)
```

Token counts are stored raw — the SDK never computes cost; the platform derives
it at query time from a price table.

### Capturing prompt & completion text

By default, model spans carry token counts but **not** the prompt or completion
text — because prompts often contain customer data. Turn the text on when you
want to see exactly what was sent and returned (the usual case in dev and
staging, off in production):

```python
init_observability(product="my-app", ingest_key="...", capture_content=True)
```

Or set `INDRATRACE_CAPTURE_CONTENT=true` in the environment (an explicit
`capture_content=` argument wins over it). When on, the prompt lands on the model
span under `gen_ai.input.messages` and the completion under
`gen_ai.output.messages`. This flag only gates the text — token counts are
captured either way.

## Session &amp; user context

Wrap a conversation in `session(...)` and **every** span started inside it —
your agent/tool spans, the FastAPI HTTP span, and the GenAI model spans —
carries `session.id` and/or `user.id`. No per-call wiring: the ids ride OTel
baggage and a span processor stamps them at span start.

```python
from indratrace import session

with session(session_id="conversation-42", user_id="u-1001"):
    answer = run(query)          # every span here is tagged with both ids
```

It works across `async`/`await` and threads, and nests — an inner
`session(user_id=...)` overrides only `user.id` and keeps the outer
`session.id`. For middleware that can't bracket a `with` (it tags on
request-in and untags on request-out, in separate callbacks), call it
imperatively and keep the handle:

```python
handle = session(session_id=request.headers["x-session-id"])
try:
    ...                          # dispatch the request
finally:
    handle.detach()             # or handle.close(); restores the prior context
```

## Feedback (👍 / 👎)

Tie a user's thumbs-up/down back to the trace that produced the answer.
Capture the trace id at answer time with `current_trace_id()`, hand it back to
the caller, and record the score whenever the user reacts — often minutes
later, out of band:

```python
from indratrace import current_trace_id, record_feedback

@trace_agent("assistant")
def answer(query: str) -> dict:
    text = run(query)
    return {"answer": text, "trace_id": current_trace_id()}  # store this id

# later, when the user clicks 👍
record_feedback(1, comment="spot on", trace_id=stored_trace_id)
```

`score` is any number — the convention is `1` for positive, `0`/`-1` for
negative, but any scale (e.g. 1–5) works. If you omit `trace_id`, the current
trace's id is used when you're inside one. `record_feedback` emits a short
`feedback` span carrying `feedback.score`, the optional `feedback.comment`, and
`feedback.trace_id`, which the platform joins back to the original trace. Called
inside `session(...)`, the feedback span carries the session/user ids too.

## Bring your own backend

The SDK emits **standard OTLP over HTTP** — nothing IndraTrace-specific on the
wire. Point `endpoint=` (or `INDRATRACE_ENDPOINT`) at any OTLP receiver and the
telemetry flows there, no ingest key required outside the IndraTrace platform:

```python
# Your own OpenTelemetry Collector, Jaeger, Grafana (Tempo/Alloy), SigNoz, …
init_observability(product="my-app", endpoint="http://otel-collector:4318")
```

```bash
export INDRATRACE_ENDPOINT="http://localhost:4318"   # e.g. a local Jaeger all-in-one
```

The `x-indratrace-key` header is only sent when you set a key (`ingest_key=` /
`INDRATRACE_KEY`), which the hosted IndraTrace platform uses to authenticate
ingest. Your own collector doesn't need it — leave it unset.

## Configuration

Resolution order is **explicit arg > env var > default**:

| Parameter (`init_observability(...)`) | Env var | Default |
|---|---|---|
| `product` | `INDRATRACE_PRODUCT` | *required* — raises/warns if unset |
| `env` | `INDRATRACE_ENV` | `dev` |
| `ingest_key` | `INDRATRACE_KEY` | *none* (no auth header sent) |
| `endpoint` | `INDRATRACE_ENDPOINT` | `http://localhost:4318` |
| `capture_content` | `INDRATRACE_CAPTURE_CONTENT` | `false` (token counts only, no prompt/completion text) |
| `debug` | `INDRATRACE_DEBUG` | `false` (no diagnostics; see below) |

Your existing `logging` calls ship automatically once your app is at INFO — the
usual case under `basicConfig(level=INFO)`, uvicorn, or gunicorn. The SDK does
**not** change your root logger's level on its own; if your app never
configured logging (so it sits at the stdlib default of WARNING), pass
`log_level="INFO"` to opt in:

```python
init_observability(product="my-app", ingest_key="...", log_level="INFO")
```

The SDK never raises into your app: if the collector is unreachable or the
config is wrong, it logs one warning and runs un-instrumented. The decorators
hold to that too — they run your function even when `init_observability()` was
never called.

## Nothing showing up? Turn on debug

Because the SDK is **fail-silent** — it never raises or blocks your app — a
misconfiguration (wrong endpoint, missing extra, unreachable collector) can leave
your dashboard empty with no obvious clue why. Pass `debug=True` to make those
failures *audible*:

```python
init_observability(product="my-app", ingest_key="...", debug=True)
```

or set `INDRATRACE_DEBUG=1` in the environment. It prints a startup banner and
turns silent drops into visible log lines — **without** changing behavior; your
app still never sees an exception from the SDK.

```
indratrace [INFO] indratrace initialized: product=my-app env=dev endpoint=http://localhost:4318
indratrace [DEBUG] IndraTrace SDK v0.4.0 initialized
indratrace [DEBUG]   product=my-app env=dev service=my-app
indratrace [DEBUG]   endpoint=http://localhost:4318 (traces=http://localhost:4318/v1/traces)
indratrace [DEBUG]   ingest_key=set capture_content=off
indratrace [DEBUG]   signals: traces + logs + metrics (OTLP/HTTP, batched)
indratrace [DEBUG]   genai[anthropic]: enabled
indratrace [DEBUG]   claude-agent-sdk: skipped (extra not installed)
indratrace [WARNING] indratrace: traces export FAILED (FAILURE) — is the collector reachable at the configured endpoint?
```

Read it top to bottom:

- The **endpoint** line tells you where telemetry is being sent — the most common
  fix is a wrong host/port here.
- Each **integration** line says `enabled` or `skipped (reason)`. `skipped (extra
  not installed)` means you need the extra, e.g. `pip install "indratrace[anthropic]"`.
- An **`export FAILED`** line means the SDK built fine but the collector didn't
  accept the data — check that it's running and reachable at the endpoint above.

The debug lines go to your console only (they're never shipped to the platform),
and `debug` defaults to off, so production stays quiet. Turn it off once you've
found the problem.

## Contributing & community

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, the test harness, and PR
  expectations.
- [`SECURITY.md`](SECURITY.md) — how to report a vulnerability privately.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — the Contributor Covenant we follow.
- Found a bug or want a feature? Open an
  [issue](https://github.com/indrasol/indratrace-python-sdk/issues/new/choose).

Built by [Indrasol](https://indrasol.com).
