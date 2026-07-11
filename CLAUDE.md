# indratrace — Python SDK

Thin OpenTelemetry wrapper that lets any product plug into the IndraTrace
observability platform with one init call. Public API is exactly:
`init_observability`, `trace_agent`, `trace_tool`. Published publicly on PyPI
as `indratrace`. The platform (Collector/ClickHouse/FastAPI/Next.js UI) lives
in a separate repo (`indratrace-platform`) — never import from or depend on it.

## Stack
- Library: Python ≥3.9, OpenTelemetry SDK, `src/` layout, setuptools via
  `pyproject.toml`.
- GenAI capture: OpenLLMetry instrumentors as optional extras (ADR 0005).
- Dev harness: docker-compose (OTel Collector contrib + ClickHouse) in `dev/`.
- Tests: pytest; unit tests use in-memory exporters, integration tests use the
  harness.

## How to work in this repo
- **Read the relevant docs before changing code:**
  - Any code → `docs/architecture.md` (module jobs + hard rules) and
    `docs/conventions.md` (the attribute contract — treat it as law).
  - Any work → `docs/PROGRESS.md` for current state, `docs/adr/` for decisions.
- Tasks are specced in `docs/prompts/CLAUDE_CODE_PROMPT_NN.md`. Implement one
  at a time, in order.
- After finishing a task: update the relevant living doc, append a line to
  `docs/PROGRESS.md`, commit.

## Conventions
- Commit to `main` in small working increments. Conventional Commits.
- Version lives only in `src/indratrace/version.py`; pyproject reads it
  dynamically. PyPI versions are immutable — never reuse one.
- Secrets: never commit `.env`, tokens, or API keys. `.env.example` only.
- Style: ruff (lint + format). Type hints everywhere; `py.typed` shipped.

## Don't
- Don't add policy (redaction/sampling/routing) to the SDK — Collector's job.
- Don't let SDK errors propagate into the host app — fail silent, always.
- Don't add non-OTel runtime dependencies to core (provider instrumentors are
  optional extras).
- Don't compute cost in the SDK — raw token counts only.
- Don't touch `docs/adr/` retroactively — supersede with a new ADR instead.
