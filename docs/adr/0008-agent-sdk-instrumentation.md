# ADR 0008 — Claude Agent SDK auto-instrumentation via entrypoint wrapping + official hooks

- **Status:** Accepted
- **Date:** 2026-07-10

## Context

Apps built on Anthropic's **`claude-agent-sdk`** should get their whole agent
loop traced with zero decorators from the one `init_observability()` call — a
span per agent run, per tool/MCP call, and model token usage — nested, session-
aware, semconv-aligned. This is the SDK's differentiating feature (prompt 08).

Phase 1 investigated the pinned package `claude-agent-sdk==0.2.116` in a scratch
venv. Findings that drive the decision:

1. **Public surface.** Two entrypoints: the async generator `query(*, prompt,
   options, transport)` (one-shot / unidirectional) and the async-context-manager
   class `ClaudeSDKClient(options, transport)` (stateful — `connect()`,
   `query()`, `receive_response()`/`receive_messages()`, `disconnect()`). Both
   take a `ClaudeAgentOptions` dataclass. `query()` is a thin wrapper over
   `InternalClient().process_query(...)`.

2. **The agent loop runs in a subprocess CLI, NOT in-process.**
   `claude-agent-sdk` requires only `anyio`, `mcp`, `sniffio` — **not** the
   `anthropic` client. Its sole transport is `_internal/transport/subprocess_cli.py`,
   which spawns the `claude` Code CLI (`node`; v2.1.86 was on PATH during the
   investigation) and exchanges newline-delimited JSON. There is **no
   `import anthropic` anywhere in the package.** **Consequence for step 3:** our
   existing OpenLLMetry anthropic instrumentor (ADR 0005) patches the in-process
   `anthropic` client and therefore captures **nothing** here — the model calls
   happen in a different OS process. Token usage must be read off the Agent SDK's
   own message/result objects instead.

3. **Where the data lives.**
   - **Turn/model usage** is on the *message stream*. Each `AssistantMessage`
     carries `.usage` (a dict) and `.model`; the terminal `ResultMessage`
     carries the run totals: `.usage`, `.model_usage` (from CLI `modelUsage`),
     `.num_turns`, `.session_id`, `.duration_ms`, `.total_cost_usd`. The `usage`
     dict is passed through verbatim from the CLI/Anthropic API, so its keys are
     the Anthropic shape: `input_tokens`, `output_tokens`,
     `cache_read_input_tokens`, `cache_creation_input_tokens`.
   - **Tool calls** surface two ways: as `ToolUseBlock`/`ToolResultBlock` inside
     message content, *and* through the official **hooks** API.

4. **Official hooks exist and are the intended extension point.**
   `ClaudeAgentOptions.hooks` maps event names to `HookMatcher`s. The relevant
   events and their payloads (TypedDicts):
   - `PreToolUse` → `tool_name`, `tool_input`, `tool_use_id`, `session_id`,
     `agent_id`, `agent_type`
   - `PostToolUse` → `+ tool_response`
   - `PostToolUseFailure` → `+ error`
   - `SubagentStart` / `SubagentStop`, `Stop`, `PreCompact`, `Notification`, …

   Hooks are registered with the CLI at init; the CLI sends `hook_callback`
   control requests back over the wire and the Python callback runs in-process
   (`_internal/query.py`). MCP tools appear in `tool_name` under the well-known
   `mcp__<server>__<tool>` convention, so the server name is recoverable by
   parsing the prefix.

   **Testability caveat:** hook callbacks only fire when the real CLI subprocess
   runs. So the *tool-span* path is exercised by the integration/`genai` test,
   while the *agent-span + usage* path — driven by the message stream — is fully
   unit-testable by feeding mocked messages through the iterator wrapper (no CLI,
   no network, no API key).

## Decision

**Interception point: (a) official hooks for tool spans + (b) wrapping the
message iteration both entrypoints funnel through, patched as *methods*, for the
agent span and token usage.** A new module `src/indratrace/agent_sdk.py`,
enabled from `init_observability()` behind the same try-import guard as the GenAI
instrumentors — absent extra ⇒ silent skip, failure ⇒ fail-silent (ADR 0003).

> **⚠ Correction (post-implementation, 2026-07-10).** The first cut monkeypatched
> the module-level names `claude_agent_sdk.query` / `ClaudeSDKClient`. The live
> `genai` test caught that this **does not work for the common usage pattern**:
> `from claude_agent_sdk import query` binds the function object *by value* at
> import time, and real apps do that at module top — i.e. *before*
> `init_observability()` runs in `main()`. Rebinding the module attribute
> afterwards leaves that caller's `query` pointing at the original, untraced
> function, so no spans are produced. (This is the same reason OTel provider
> instrumentors patch client **methods**, not import-time references.)
>
> **The corrected interception patches methods, resolved at call time:**
> - `InternalClient.process_query` — the single seam every `query()` call funnels
>   through (verified: `query()` delegates to `InternalClient().process_query`).
> - `ClaudeSDKClient.__init__` (to merge tracing hooks into its options) and its
>   `receive_response` / `receive_messages` (each iteration is one run). The
>   client path does **not** touch `InternalClient`, so the two never
>   double-count.
>
> This means accepting one `_internal` dependency (`InternalClient.process_query`)
> — the very tier (c) the first cut avoided. The trade is deliberate: the public
> names cannot be instrumented robustly by monkeypatch (you cannot rebind names
> other modules already imported), so import-order robustness *requires* a
> call-time-resolved method seam. The fragility is bounded — the seam is imported
> and looked up defensively, so a rename in a future SDK version degrades to a
> silent skip (fail-silent), and the extra's floor is pinned tightly to the
> verified version (below).

This composes tier **(a) official hooks** (for individual tool start/stop/error,
which the message stream doesn't delimit as cleanly) with a method-level variant
of tier **(b)** (intercepting the entrypoints' shared funnel).

**What each layer produces:**

- **Agent span** (`kind="agent"`, `agent.framework="claude-agent-sdk"`): opened
  when the wrapped `process_query` (for `query()`) or `receive_*` (for the
  client) iterator is entered, closed when it is exhausted **or abandoned** (the
  wrapper is a generator with a `finally:` that ends the span, so an
  early-abandoned stream leaves no dangling span). Session id from the messages
  is stamped as `session.id` so it composes with the existing
  `SessionSpanProcessor`.

- **Tool spans** (`kind="tool"`, `tool.name`, `tool.mcp_server` when the name is
  `mcp__…`): opened on `PreToolUse`, closed on `PostToolUse`; `PostToolUseFailure`
  sets ERROR status. Our tracing hooks are *merged into* whatever `hooks` the app
  already passed in `ClaudeAgentOptions` (we never clobber the user's hooks), and
  each callback returns an empty hook output so it is observe-only and never
  alters permissioning or control flow.

- **Model usage** under the canonical `gen_ai.usage.*` names, captured from the
  message objects (not an instrumentor): per-turn from `AssistantMessage.usage`
  onto a `turn` child span, and the run total from `ResultMessage.usage` onto the
  agent span. Cache tokens map to
  `gen_ai.usage.cache_read.input_tokens` / `…cache_creation.input_tokens`. Raw
  counts only — no cost math (ADR 0005), even though `ResultMessage.total_cost_usd`
  is offered, we do not record it.

- **Turn spans** (`kind="turn"`): one child per `AssistantMessage`, since a turn
  boundary *is* observable (each assistant message is a turn), carrying that
  turn's model + usage.

## Supported version range

- Extra pinned `claude-agent-sdk>=0.2.116` (the investigated version; the floor
  is the first version we have verified the surface against). The wrapper touches
  the public message/block dataclasses and `HookMatcher` (stable contract) **plus
  one internal seam, `claude_agent_sdk._internal.client.InternalClient.process_query`**
  (see the correction above). Because that seam is private, the floor is treated
  as a tight pin: the offline unit tests drive the *real* `process_query` seam
  (not a stand-in), so a signature/location change there trips them immediately,
  and the version floor should be revisited on any Agent SDK bump.
- No upper cap pinned: the wrapper looks the seam up defensively and degrades to
  a silent skip if it is absent or moved (fail-silent), so a breaking upstream
  change loses agent-sdk spans but never breaks the host app or the other
  signals. The `ClaudeSDKClient` method patches touch only public class methods.

## What's captured / what isn't (yet)

**Captured:** agent-run span; per-turn spans with per-turn model + token usage;
run-total token usage; tool spans (incl. MCP, with server name) with ERROR status
on failure; session-id propagation onto all agent-sdk spans; correct nesting
under an enclosing `@trace_agent`/`@trace_step` or FastAPI span; clean close on
early-abandoned streams.

**Not captured (yet):** prompt/response **text** content capture for agent-sdk
runs is gated the same as GenAI content capture but implemented only if cheaply
available from the message objects; sub-subagent nesting beyond the first
`SubagentStart`/`Stop` pair is flattened onto the parent agent span; the CLI's
own internal server-tool calls (web search etc.) are recorded only insofar as
they surface as `ToolUseBlock`s. `total_cost_usd` is intentionally dropped
(ADR 0005 — no cost in the SDK).

## Alternatives considered

- **Reuse the anthropic instrumentor (ADR 0005).** Rejected on the Phase 1
  finding above: the Agent SDK never touches the in-process `anthropic` client,
  so the instrumentor is blind to it. Token usage has to come from the message
  objects regardless.
- **Monkeypatch the module-level `query` / `ClaudeSDKClient` names.** The first
  cut. Rejected after the live test proved it misses `from claude_agent_sdk
  import query` performed before init (the common case) — see the correction
  above. Monkeypatching `InternalClient.process_query` (an `_internal` seam) is
  the accepted cost of call-time-resolved, import-order-robust interception.
- **Hooks-only (no entrypoint wrap).** Hooks cover tools well but there is no
  hook that carries per-turn model usage, and the run-level `Stop` hook does not
  expose the `usage`/`model_usage` totals — those live only on the message
  stream. So the message-iterator wrap is necessary for the headline
  (token-usage) feature; hooks alone can't deliver it.

## Consequences

- New optional extra `claude-agent-sdk`; core stays OTel-only (ADR 0003).
- `conventions.md` gains `agent`/`turn` span kinds, `agent.framework`,
  `tool.mcp_server`, and records that agent-sdk model usage is read from message
  objects (not an instrumentor) under the same `gen_ai.usage.*` names.
- The tool-span path depends on the real CLI, so it is proven in the `genai`
  integration test; the agent/turn/usage path is proven offline with mocked
  messages.
