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

```python
from indratrace import init_observability

init_observability(api_key="it_live_...")
```

That is the whole integration. **The API key is the only thing you configure** —
it identifies your product, its environment and your workspace, all decided when
you register the product and copy its key. There is nothing else to set, and no
endpoint to point at.

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

# Once, at app startup. The key is the whole configuration.
init_observability(api_key="it_live_...")

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

## What you get, by framework

**If your app uses Python's standard logging, your logs are already in
IndraTrace — don't use `print()`.** That is true with no extra installed and no
configuration: the one `init_observability()` call bridges `logging` into the
pipeline, and every record emitted inside a span carries that span's trace
context. As of 0.6.0 the same is true of [Loguru](https://github.com/Delgan/loguru).

Everything else — HTTP server spans, model spans, agent spans — comes from the
extras in the table below.

| Your app uses | Logs | HTTP server spans | Model spans (tokens) | Agent spans |
|---|---|---|---|---|
| **`logging`** (stdlib) | ✅ automatic | — | — | — |
| **Loguru** | ✅ automatic *(0.6.0+)* | — | — | — |
| **`print()`** | ❌ **not captured** — switch to `logging`/loguru | — | — | — |
| **FastAPI** | ✅ automatic | `indratrace[fastapi]` | — | — |
| **Django** | ✅ automatic | `indratrace[django]` ¹ | — | — |
| **Flask** | ✅ automatic | `indratrace[flask]` ² | — | — |
| **Anthropic** | ✅ automatic | — | `indratrace[anthropic]` | — |
| **OpenAI** | ✅ automatic | — | `indratrace[openai]` | — |
| **Gemini** | ✅ automatic | — | `indratrace[gemini]` | — |
| **Bedrock** | ✅ automatic | — | `indratrace[bedrock]` | — |
| **Claude Agent SDK** | ✅ automatic | — | ✅ *(per turn)* | `indratrace[claude-agent-sdk]` — zero decorators |
| **Any other agent/tool code** | ✅ automatic | — | `record_llm_usage(...)` | `@trace_agent` / `@trace_tool` / `@trace_step` |

Install exactly what you use — extras are additive, and core stays
dependency-clean (OpenTelemetry only):

```bash
pip install "indratrace[fastapi,anthropic]"        # a FastAPI app calling Claude
pip install "indratrace[django]"                   # a Django app
pip install "indratrace[flask,openai]"             # a Flask app calling OpenAI
```

¹ **Django:** `init_observability()` must run **before** Django builds its
application object — it works by adding middleware. See
[Django](#django).
² **Flask:** if your module does `from flask import Flask`, add one line —
`instrument_flask_app(app)`. See [Flask](#flask).

Missing an extra is never an error — the SDK skips it silently. If a signal you
expected is missing, [turn on debug](#turning-on-debug): the
startup banner prints `enabled` or `skipped (extra not installed)` for every
integration above.

## Tracing a regular REST API

You don't need to be building an AI agent. For a plain FastAPI service, the one
init line is the whole setup — every endpoint is reported as a span, with its
status and duration, no per-route code:

```python
from fastapi import FastAPI

from indratrace import init_observability

init_observability(api_key="it_live_...")

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

## Django

```bash
pip install "indratrace[django]"
```

Every request becomes a server span, with no per-view code. **Where you call
`init_observability()` matters.** The instrumentation works by adding middleware,
and Django reads its middleware list once, when it builds the application object
— so init has to happen *before* that. In practice: put it at the top of
`wsgi.py` (and `asgi.py`, and `manage.py` if you use `runserver`), above the
`get_wsgi_application()` call.

```python
# myproject/wsgi.py
import os

from django.core.wsgi import get_wsgi_application

from indratrace import init_observability

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")

init_observability(api_key="it_live_...")            # BEFORE ↓

application = get_wsgi_application()
```

Call it *after* `get_wsgi_application()` and you get logs and model spans, but no
HTTP spans — and no error saying so. The reason: `get_wsgi_application()` reads
`settings.MIDDLEWARE` and freezes a middleware chain from it, so a middleware
added later is simply never in the chain your server actually runs. If your HTTP
spans are missing, check this first.

> A wrinkle worth knowing if you write tests: Django's test `Client` builds a
> fresh handler per request, so it re-reads `settings.MIDDLEWARE` every time and
> will happily produce spans even when init ran too late. Only a real WSGI/ASGI
> server exposes the mistake — so trust your staging environment here, not a
> passing test.

## Flask

```bash
pip install "indratrace[flask]"
```

Every request becomes a server span. **One caveat, and it bites the most common
import style.** The instrumentation works by replacing the `flask.Flask` class,
so an app built from a `Flask` name that was imported *before* `init_observability()`
ran is left uninstrumented — silently. Since `from flask import Flask` sits at the
top of the file and init runs below it, that is the usual case. Add one line:

```python
from flask import Flask

from indratrace import init_observability, instrument_flask_app

init_observability(api_key="it_live_...")

app = Flask(__name__)
instrument_flask_app(app)          # ← now every request is a span


@app.get("/orders/<order_id>")
def get_order(order_id: str):
    return {"id": order_id}
```

`instrument_flask_app(app)` is safe to call twice, never raises, and is a no-op
if the `flask` extra isn't installed. (If you construct the app as
`flask.Flask(__name__)` — looking the name up on the module instead of importing
the class — the extra line isn't needed. The explicit call works either way, so
when in doubt, keep it.)

## Loguru

Nothing to install and nothing to configure — as of **0.6.0**, if
[loguru](https://github.com/Delgan/loguru) is importable, `init_observability()`
bridges it automatically:

```python
from loguru import logger

from indratrace import init_observability

init_observability(api_key="it_live_...")

logger.info("this ships to IndraTrace")          # INFO and above
logger.exception("so does this, with its stack trace")
```

Records at **INFO and above** are exported (DEBUG stays local — it would be a
firehose), severities are preserved, and a line logged inside a span carries that
span's trace context, exactly like a stdlib one. Your own loguru sinks are
untouched: console output looks the same as it always did, and an app that uses
loguru *and* stdlib `logging` gets each record exported exactly once.

### If you configure loguru after init

`logger.remove()` — the idiomatic way to drop loguru's default stderr sink —
takes **every** sink with it, including ours. So an app that reconfigures loguru
*after* `init_observability()` silently unbridges itself. Put the bridge back
with `bridge_loguru()`:

```python
from loguru import logger

from indratrace import bridge_loguru, init_observability

init_observability(api_key="it_live_...")

logger.remove()                    # your own setup — drops our sink too
logger.add("app.log", level="INFO")

bridge_loguru()                    # ← put the bridge back; logs ship again
```

Call it any time after init; it's idempotent, so calling it twice does not
double-export. It returns `False` (and does nothing) if loguru isn't installed
or `init_observability()` never ran.

The simplest way to avoid the whole issue is to configure loguru **before**
`init_observability()`, in which case there is nothing to re-add.

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

init_observability(api_key="it_live_...")   # once, at startup

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

init_observability(api_key="it_live_...")
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
init_observability(api_key="it_live_...", capture_content=True)
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

## Configuration

There is one required setting, and it is the API key:

```python
init_observability(api_key="it_live_...")
```

```bash
export INDRATRACE_API_KEY="it_live_..."   # or set it in the environment
```

An explicit `api_key=` wins over the environment. If neither is set,
`init_observability()` raises `IndraTraceConfigError` and says so — it will not
quietly run un-instrumented, because a missing key is the one mistake you cannot
see from the outside.

**Where the key comes from:** register the product in your IndraTrace workspace,
choose its environment there, and copy the key it shows you. The key is shown
**once**, at creation. A key belongs to one product in one environment — a
staging deploy and a production deploy of the same product hold different keys,
and that is the only difference between them.

Everything else is optional and keyword-only:

| Parameter (`init_observability(...)`) | Env var | Default |
|---|---|---|
| `api_key` | `INDRATRACE_API_KEY` | **required** — raises if unset |
| `service_name` | — | whatever OpenTelemetry resolves (`OTEL_SERVICE_NAME`, else `unknown_service`) |
| `service_version` | — | `0.0.0` |
| `capture_content` | `INDRATRACE_CAPTURE_CONTENT` | `false` (token counts only, no prompt/completion text) |
| `log_level` | — | *none* — the SDK does not change your root logger |
| `debug` | `INDRATRACE_DEBUG` | `false` (no diagnostics; see below) |

`service_name` is for one product with several deployables — an API and a
worker, say. It labels the deployable; it does not name your product, which the
key already does.

`init_observability()` also takes `instrument_http=False` to turn off web-framework
auto-instrumentation entirely (it's on by default, and an absent extra is already
a no-op). Before 0.6.0 this argument was called `instrument_fastapi`; the old name
still works and now gates all three frameworks.

> **Upgrading from 0.6?** `product`, `env`, `endpoint` and `ingest_key` were
> removed in 1.0 — the key decides all of them. Passing one raises an error that
> names it and tells you what to write instead, and `INDRATRACE_PRODUCT` /
> `INDRATRACE_ENV` / `INDRATRACE_KEY` are ignored with a warning. See the
> [CHANGELOG](https://github.com/indrasol/indratrace-python-sdk/blob/main/CHANGELOG.md#100--2026-09-03) for a before/after snippet.

Your existing `logging` calls ship automatically once your app is at INFO — the
usual case under `basicConfig(level=INFO)`, uvicorn, or gunicorn. The SDK does
**not** change your root logger's level on its own; if your app never
configured logging (so it sits at the stdlib default of WARNING), pass
`log_level="INFO"` to opt in:

```python
init_observability(api_key="it_live_...", log_level="INFO")
```

Loguru needs none of this: its own level gates its records, and the bridge takes
everything at INFO and above regardless of the stdlib root level (see
[Loguru](#loguru)).

The SDK never raises into your app **after** it is wired: if IndraTrace is
unreachable or rejects the key, it logs and drops, never blocks a request and
never raises. The decorators hold to that too — they run your function even when
`init_observability()` was never called.

The one exception is `init_observability()` itself, which raises
`IndraTraceConfigError` when there is no API key (or when you pass a parameter
that 1.0 removed). That is a startup mistake, on a line you are looking at — and
letting it through would only turn into telemetry silently rejected in
production, which is the failure this replaced.

## Still waiting for your first span?

You called `init_observability()`, your app is serving traffic, and the dashboard
is empty. There are only three things it can be — work down the list.

**1. Is `INDRATRACE_API_KEY` actually set in the process that is running?**

```bash
python -c "import os; print(os.getenv('INDRATRACE_API_KEY') or 'NOT SET')"
```

Run that the same way you run your app — same shell, same container, same
systemd unit. A `.env` file your process manager never loads, or a key exported
in your terminal but not in the container, is the single most common cause. If
the key is missing entirely, `init_observability()` raises rather than starting
un-instrumented — so if your app started at all, a key was found *somewhere*.
The question is whether it is the right one.

**2. Is it the key for the product you expect?**

A key belongs to **one product in one environment**. Your telemetry is arriving
under whichever product that key names — so a staging key in a production deploy
means the data is landing in staging, not nowhere. Open Products in your
IndraTrace workspace and check the environment you are looking at matches the
key the process is holding. (Keys are shown once at creation; if you cannot tell
which one a deploy has, rotate it and set the new one deliberately.)

**3. Turn on debug and read the export lines.**

```bash
export INDRATRACE_DEBUG=1
```

or pass `debug=True`. It prints a startup banner and turns silent drops into
visible log lines (details below). Two lines answer almost everything:

- `export ok` — the telemetry left your process and was accepted. If the
  dashboard is still empty, you are looking at the wrong product or environment;
  go back to step 2.
- `export FAILED` — it did not, and the line names the HTTP status the gateway
  answered with. A `401` means the key was rejected (revoked, or a typo in the
  value); *no* HTTP response at all means the gateway was not reachable from
  where your app runs; any other status means it was reached and refused the
  batch.

If all three check out and you still see nothing, the remaining possibility is
placement, not configuration: `init_observability()` ran too late for
[Django](#django) or after your `Flask` app object was built —
see [Flask](#flask). The banner cannot detect either.

## Turning on debug

Because the SDK is **fail-silent** — it never raises or blocks your app once it
is wired — a missing extra or an unreachable gateway can leave your dashboard
empty with no obvious clue why. Pass `debug=True` to make those failures
*audible*:

```python
init_observability(api_key="it_live_...", debug=True)
```

or set `INDRATRACE_DEBUG=1` in the environment. It prints a startup banner and
turns silent drops into visible log lines — **without** changing behavior; your
app still never sees an exception from the SDK.

```
indratrace [INFO] indratrace initialized: service=my-app endpoint=http://localhost:8088
indratrace [DEBUG] IndraTrace SDK v1.0.0 initialized
indratrace [DEBUG]   service=my-app version=1.4.2
indratrace [DEBUG]   endpoint=http://localhost:8088 (traces=http://localhost:8088/v1/traces)
indratrace [DEBUG]   api_key=set capture_content=off
indratrace [DEBUG]   identity: product, deployment.environment and tenant.id are stamped by IndraTrace from your API key
indratrace [DEBUG]   signals: traces + logs + metrics (OTLP/HTTP, batched)
indratrace [DEBUG]   http[fastapi]: skipped (extra not installed)
indratrace [DEBUG]   http[django]: enabled
indratrace [DEBUG]   http[flask]: skipped (extra not installed)
indratrace [DEBUG]   loguru: enabled
indratrace [DEBUG]   genai[anthropic]: enabled
indratrace [DEBUG]   claude-agent-sdk: skipped (extra not installed)
indratrace [WARNING] indratrace: traces export FAILED (FAILURE) — HTTP 401: the gateway rejected your API key. Check the value of INDRATRACE_API_KEY in the process that is running; keys are shown once when the product is created.
```

Read it top to bottom:

- The **identity** line is there because the banner reports no product and no
  environment — deliberately. Your key decides both, and IndraTrace stamps them
  when the telemetry lands, so there is nothing here for the SDK to report.
- Each **integration** line says `enabled` or `skipped (reason)`. `skipped (extra
  not installed)` means you need the extra, e.g. `pip install "indratrace[anthropic]"`.
  The `http[…]`, `loguru`, `genai[…]`, and `claude-agent-sdk` lines cover every
  row of the [support matrix](#what-you-get-by-framework).
- An **`export FAILED`** line means the SDK built fine but IndraTrace didn't
  accept the data. It carries the HTTP status the gateway answered with: `401`
  is a rejected key (the sample above), any other status means the gateway was
  reached and refused the batch, and *no* HTTP response at all means it was not
  reachable from where your app runs.

One thing the banner **cannot** tell you: whether `init_observability()` ran
early enough. `http[django]: enabled` means the middleware was installed, but if
you called init *after* `get_wsgi_application()` it went into a chain Django had
already built, and you'll still see no HTTP spans — see [Django](#django).
Likewise `http[flask]: enabled` doesn't guarantee *your* app object was caught;
see [Flask](#flask).

The debug lines go to your console only (they're never shipped to the platform),
and `debug` defaults to off, so production stays quiet. Turn it off once you've
found the problem.

## Contributing & community

- [`CONTRIBUTING.md`](https://github.com/indrasol/indratrace-python-sdk/blob/main/CONTRIBUTING.md) — dev setup, the test harness, and PR
  expectations.
- [`SECURITY.md`](https://github.com/indrasol/indratrace-python-sdk/blob/main/SECURITY.md) — how to report a vulnerability privately.
- [`CODE_OF_CONDUCT.md`](https://github.com/indrasol/indratrace-python-sdk/blob/main/CODE_OF_CONDUCT.md) — the Contributor Covenant we follow.
- Found a bug or want a feature? Open an
  [issue](https://github.com/indrasol/indratrace-python-sdk/issues/new/choose).

Built by [Indrasol](https://indrasol.com).
