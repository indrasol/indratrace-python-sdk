# Contributing to IndraTrace

Thanks for helping improve IndraTrace! This is a thin OpenTelemetry wrapper —
the whole public surface is `init_observability`, `trace_agent`, `trace_tool`
(plus a few analytics helpers). Keeping it thin is the point, so most changes are
small and focused.

## Dev setup

You need Python ≥ 3.10. Clone, make a virtual environment, and install the
package editable with the `dev` extra (which pulls the test dependencies and every
provider SDK the unit tests exercise):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,fastapi,anthropic,openai,gemini,bedrock,claude-agent-sdk]"
```

## Running the tests

The suite is split by marker so the fast path needs nothing external:

```bash
# Offline — no Docker, no network, no API keys. This is what CI gates on.
pytest -m "not integration and not genai" -q
```

Two markers opt into heavier runs:

- **`integration`** — needs the local dev harness (an OTel Collector + ClickHouse
  in `dev/`) so telemetry can actually be delivered and queried back:

  ```bash
  docker compose -f dev/docker-compose.yml up -d --wait
  pytest -m integration -q
  docker compose -f dev/docker-compose.yml down -v
  ```

- **`genai`** — makes a real, tiny paid model call to verify token capture
  end-to-end. It needs `ANTHROPIC_API_KEY` and auto-skips otherwise, so it stays
  a **local ritual** and never runs in CI. Run it before releasing changes that
  touch the GenAI or Agent-SDK paths.

Keep the offline suite fast (under ~15s) — it shrinks the export timeout so a
refused connection doesn't cost real backoff on teardown.

## Style & linting

We use [ruff](https://docs.astral.sh/ruff/) for both lint and format, and type
hints everywhere (`py.typed` is shipped):

```bash
ruff check .
ruff format .
```

## What we ask of a PR

Before you open a pull request:

- **`ruff check .` is clean** and **the offline suite is green**
  (`pytest -m "not integration and not genai" -q`).
- If you touched the delivery/GenAI paths, run the `integration` (and, with a
  key, `genai`) suites locally and say so in the PR.
- **Conventional Commits** for messages — `feat:`, `fix:`, `docs:`, `test:`,
  `refactor:`, `chore:` — optionally scoped, e.g. `feat(agent-sdk): …`.
- Update the living docs you changed the meaning of: `docs/architecture.md`
  (module jobs + hard rules) and `docs/conventions.md` (the attribute contract —
  treat it as law). New design decisions get a new ADR under `docs/adr/`; don't
  edit past ADRs, supersede them.

## Design guardrails (please don't cross these)

The SDK is deliberately minimal. These rules come from the ADRs:

- **Fail silent.** SDK errors must never break or block the host app. If wiring
  fails, log one warning and run un-instrumented — never raise.
- **No policy in the SDK.** No redaction, sampling, or routing — that's the
  Collector's job.
- **OTel deps only in core.** Provider instrumentation ships as optional extras.
- **No cost math.** Raw token counts only; cost is derived downstream.

If a change needs to bend one of these, open an issue first so we can talk it
through.

Built by [Indrasol](https://indrasol.com).
