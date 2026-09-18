# Conventions — the attribute contract

*Living doc. **This is the real API between the SDK and the platform.** The two
repos are compatible if and only if they agree on this file. Change it only via
a new ADR, and mirror changes in the platform repo.*

## Resource attributes

A stored row still carries all six attributes below. What changed in SDK v1.0.0
(ADR 0009 / platform P80) is **who writes them**: three come from the SDK, and
three are stamped by the ingest gateway from the API key. The wire names are
unchanged — only their writer moved.

### Sent by the SDK (stamped on EVERY signal by `init_observability`)

| Attribute | Type | Required | Example | Notes |
|---|---|---|---|---|
| `service.name` | string | yes | `compliance-api` | OTel standard; the deployable's name. Optional to the SDK caller (`service_name=`); when unset, OpenTelemetry resolves it (`OTEL_SERVICE_NAME`, else `unknown_service`), so it is always present on the wire. |
| `service.version` | string | yes | `1.4.2` | Product's own version. Defaults to `0.0.0`. |
| `telemetry.sdk.wrapper` | string | yes | `indratrace/1.0.0` | Set automatically; identifies SDK version on the wire. |

### Stamped by the gateway — never sent by the SDK

**Amended at SDK v1.0.0** (platform P80; platform ADR 0010 §1 as amended). These
three rows used to live in the table above and were resolved from
`init_observability(product=..., env=...)` / `INDRATRACE_PRODUCT` /
`INDRATRACE_ENV` / a hardwired `internal`.

| Attribute | Type | Required | Example | Written by |
|---|---|---|---|---|
| `product` | string | yes | `compliance` | The gateway, from the API key's product. Lowercase, stable, from the platform Product Registry. |
| `deployment.environment` | string | yes | `prod` \| `staging` \| `dev` | The gateway, from that product's **registered** env (`products.env`). OTel standard key. |
| `tenant.id` | string | yes | `internal` | The gateway, from the key's org slug. Customer/tenant scoping. |

**Why the SDK sends none of them.** The gateway applies one rule to all three —
DROP the client's value, APPEND the key's (`ingest/stamp.py::STAMPED_ATTRS`) —
so a value the SDK put there would be discarded on arrival. Sending it would be
sending a claim we already know is ignored, and would let a customer believe
they had configured something they had not. The security property is stated once
and holds three times: **the payload is a claim, the key is truth.**

The SDK names these three in `config.GATEWAY_STAMPED_ATTRS` and a test asserts
the Resource contains none of them — the SDK-side twin of the platform's grep
that `stamp.py` is their only writer. **If the platform adds a fourth stamped
attribute, both lists move together.**

## Span conventions

- **Agent spans** (`trace_agent`): name = `agent <name>`, attributes
  `indratrace.span.kind = "agent"`, `agent.name`.
- **Tool spans** (`trace_tool`): name = `tool <function_name>`, attributes
  `indratrace.span.kind = "tool"`, `tool.name`. Exceptions recorded, status set
  to ERROR.
- **Step spans** (`trace_step`): name = `step <function_name>`, attributes
  `indratrace.span.kind = "step"`, `step.name`. Identical machinery to tool
  spans (exceptions recorded, status ERROR); the distinct kind exists so timing
  a plain non-AI function (a db query, a parser) isn't mislabeled a "tool".
- **Model spans**: emitted by the GenAI instrumentors
  (`opentelemetry-instrumentation-anthropic` / `-openai`) per the OTel GenAI
  semantic conventions. Span name is `anthropic.chat` / `openai.chat`.
  Token counts — `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.usage.total_tokens`, plus `gen_ai.usage.cache_read.input_tokens` /
  `gen_ai.usage.cache_creation.input_tokens` where the provider reports them —
  match this contract exactly. Also present: `gen_ai.request.model`,
  `gen_ai.response.model`. Raw counts only — no cost values.

  > **⚠ Attribute drift — system identity (`gen_ai.system` → `gen_ai.provider.name`).**
  > This contract specified `gen_ai.system` for the provider identity. The
  > current instrumentors (verified empirically against the pinned versions —
  > see `tests/test_genai.py::TestAttributeNames`) emit **`gen_ai.provider.name`**
  > instead, tracking the OTel GenAI-semconv rename. Token attribute names did
  > **not** drift, so the SDK does not rewrite anything — it records the wire
  > truth here and the manual fallback (`record_llm_usage`) stamps the same
  > drifted name, so hand- and auto-instrumented spans are identical. **The
  > platform must alias the two at query time** until this contract and the
  > platform repo are reconciled. Flagged for product-owner review.
  >
  > | Canonical (this doc, pre-drift) | Actually on the wire (pinned instrumentors) |
  > |---|---|
  > | `gen_ai.system` | `gen_ai.provider.name` |
  > | `gen_ai.request.model` | `gen_ai.request.model` *(unchanged)* |
  > | `gen_ai.usage.input_tokens` | `gen_ai.usage.input_tokens` *(unchanged)* |
  > | `gen_ai.usage.output_tokens` | `gen_ai.usage.output_tokens` *(unchanged)* |

### Content capture (prompt & completion text)

Off by default — prompts carry customer data. Enabled per process via
`init_observability(..., capture_content=True)` or `INDRATRACE_CAPTURE_CONTENT`
(truthy: `1/true/yes/on`; explicit arg wins). When **on**, the model span also
carries the request/response **text**, under these names (verified empirically
at the pinned instrumentor versions — `tests/test_genai.py::TestContentCapture`):

| Attribute | Type | Present when | Holds |
|---|---|---|---|
| `gen_ai.input.messages` | string (JSON) | `capture_content` on | the prompt/messages sent to the model |
| `gen_ai.output.messages` | string (JSON) | `capture_content` on | the model's completion text |

Both track the current OTel GenAI semconv (the newer `gen_ai.*.messages` form,
not the older `gen_ai.prompt.N` / `gen_ai.completion.N`). Token counts above are
captured **regardless** of this flag — it gates only the raw text.

> **⚠ Mechanism.** The instrumentors read `TRACELOOP_TRACE_CONTENT` and treat it
> as *on* when unset. The SDK therefore sets that env var explicitly from the
> resolved `capture_content` flag (`"false"` by default) so the platform default
> is off — a user flips one boolean and never touches the env var. This is the
> only place the SDK writes a `TRACELOOP_*` var; it is not policy (ADR 0003),
> just off-by-default plumbing for an upstream default we disagree with.

### Claude Agent SDK spans (ADR 0008)

Auto-instrumentation of Anthropic's `claude-agent-sdk` — enabled from
`init_observability()` with the `claude-agent-sdk` extra, zero decorators. The
Agent SDK runs its agent loop in a *subprocess* CLI (it never uses the in-process
`anthropic` client), so token usage is read off the SDK's **message objects**,
not from a GenAI instrumentor — but it lands under the **same** `gen_ai.usage.*`
names, so the platform treats it identically.

- **Agent-run spans**: name = `agent <model-or-framework>`, attributes
  `indratrace.span.kind = "agent"`, `agent.name`,
  `agent.framework = "claude-agent-sdk"`. Carries the **run-total** token usage
  (from the terminal `ResultMessage`) under the `gen_ai.usage.*` names, plus
  `session.id` (from the run's session id). ERROR status when the run errored.
- **Turn spans**: name = `turn`, attributes `indratrace.span.kind = "turn"`,
  `agent.framework`, `gen_ai.request.model` / `gen_ai.response.model`. One child
  per assistant turn, carrying that **turn's** `gen_ai.usage.*` token counts
  (incl. `cache_read.input_tokens` / `cache_creation.input_tokens` when present).
- **Tool spans**: name = `tool <name>`, attributes `indratrace.span.kind = "tool"`,
  `tool.name`, `agent.framework`, and `tool.mcp_server` when the tool is an MCP
  tool (name `mcp__<server>__<tool>` → server segment). Driven by the SDK's
  official `PreToolUse`/`PostToolUse`/`PostToolUseFailure` hooks; ERROR status on
  a tool failure. All nest under the enclosing agent-run span.

Cost is **never** recorded even though `ResultMessage.total_cost_usd` is offered —
raw counts only (ADR 0005). `agent.framework` selects exactly this feature's
spans at query time.

- **HTTP spans**: whatever the framework's official OTel instrumentor emits —
  `FastAPIInstrumentor`, `DjangoInstrumentor`, or `FlaskInstrumentor` (v0.6.0),
  each following the OTel HTTP semconv. **Consume, don't fork**: the SDK adds no
  attributes of its own to these spans and renames nothing, so the platform reads
  the instrumentors' names (`http.route`, `http.request.method`, and the status
  code under whichever name that instrumentor's semconv mode emits). Don't
  customize. (Session/user ids still land on them, via the span processor — that
  is stamped at span start for *every* span, not an HTTP-specific rewrite.)

- **Feedback spans** (`record_feedback`): name = `feedback`, attributes
  `indratrace.span.kind = "feedback"`, `feedback.score` (int/float — `1` =
  positive, `0`/`-1` = negative, or any numeric scale; recorded verbatim, no
  cost/normalization in the SDK), optional `feedback.comment` (string), and
  `feedback.trace_id` (32-char lowercase hex) linking the feedback to the trace
  it is about. `feedback.trace_id` is the explicit `trace_id` argument when
  given, else the current trace's id when emitted inside one; if neither exists
  the span is still emitted with `feedback.trace_id` absent (the score is never
  silently dropped). The platform joins the feedback row to the original trace
  on `feedback.trace_id`.

## Session / user context (span attributes)

| Attribute | Type | On | Example | Notes |
|---|---|---|---|---|
| `session.id` | string | every span started inside `session(...)` | `conversation-42` | Groups all spans of one conversation. |
| `user.id` | string | every span started inside `session(...)` | `u-1001` | The end user the request is for. |

Set by wrapping work in `session(session_id=..., user_id=...)`: the ids ride
OTel **baggage** and a `SessionSpanProcessor` (registered by
`init_observability`) copies them onto **every** span at `on_start` — decorator
spans, FastAPI HTTP spans, GenAI model spans, and feedback spans alike. Either
id may be set independently; nesting overrides per key. Propagation is
`contextvars`-backed, so it holds across `async`/`await` and threads.

> **⚠ Cardinality.** `session.id` and `user.id` are **span attributes only,
> NEVER metric labels or resource attributes** — they are unbounded values and
> would explode metric cardinality (same discipline as trace ids / raw prompts,
> see § Naming). The SDK only ever stamps them on spans.

## Transport

**Amended at SDK v1.0.0** (ADR 0009): the destination is the authenticating
ingest gateway, and it is not configurable by a customer. The auth header is
unchanged.

- OTLP over HTTP (`/v1/traces`, `/v1/logs`, `/v1/metrics`) to the IndraTrace
  **ingest gateway** — one constant, `config.DEFAULT_ENDPOINT`, since 1.1 the
  production hostname `https://ingest.indratrace.com`. The pre-gateway collector
  port `:4318` no longer terminates SDK traffic. There is no `endpoint`
  parameter: the key routes the telemetry once it lands. `INDRATRACE_ENDPOINT`
  (env only) is the supported override for self-hosted deployments and
  IndraTrace's dev environment — the gateway's base URL, no path.
- Startup preflight (1.1): one authenticated `POST /v1/traces` with an empty
  body, so the gateway's real 401/402 are what the SDK reads. Diagnoses are
  built from the endpoint, the status and the gateway's RFC 7807 `title`/
  `detail`; the key never appears in one.
- Auth header: `x-indratrace-key: <api_key>`, on **all three** signal exporters.
  The header name is a fixed transport contract. The SDK parameter/env that
  supplies it is `api_key` / `INDRATRACE_API_KEY`; the key is **required**, so
  there is no headerless export. (`ingest_key` / `INDRATRACE_KEY` were removed
  in 1.0.)
- Batch export; on failure, retry per OTel defaults, then drop. Never block.

## Naming

- PyPI package: `indratrace`. Import: `indratrace`. Env var prefix: `INDRATRACE_`.
- Products must NOT put unbounded values (user IDs, trace IDs, raw prompts) in
  metric labels or resource attributes — cardinality discipline from day one.
