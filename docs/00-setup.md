# Setup — prerequisites & environment

*Living doc. [now] = needed before the next task. [later] = needed at
release/deploy time.*

## Accounts (browser)

- **[done]** PyPI account + 2FA + project-scoped token for `indratrace`
  (0.0.1 stub published 2026-07-09). Token lives in your password manager —
  never in chat, prompts, or commits.
- **[now]** GitHub repo `indrasol/indratrace-python-sdk` — create it empty (no
  README/license/gitignore, we have them locally), then push this folder.
- **[done]** PyPI *trusted publishing*: PyPI → project → Publishing → GitHub
  publisher (`indrasol/indratrace-python-sdk`, workflow `release.yml`,
  environment `pypi`). Kills the need for tokens entirely — `release.yml` uses
  OIDC (`id-token: write`) and `pypa/gh-action-pypi-publish`, zero secrets in
  the repo. Confirm this publisher is registered on PyPI *before* tagging.
- **[done]** Anthropic + OpenAI API keys for GenAI integration tests (task 04) —
  live `pytest -m genai` verified real token capture (see PROGRESS 2026-07-10).
  Stays a local pre-release ritual; CI never runs the `genai` marker (paid key).

## Tools (your machine)

- **[done]** Python 3.11 via pyenv; venv workflow.
- **[now]** **Docker Desktop** — required for the dev harness (task 01).
  Install from docker.com, then verify: `docker --version` and
  `docker compose version`.
- **[now]** Git configured for the indrasol org.

## Environment variables (dev)

Copy `.env.example` → `.env` (gitignored). For the local harness:

```
INDRATRACE_ENDPOINT=http://localhost:4318
INDRATRACE_KEY=dev-local            # harness doesn't verify keys (ADR 0004)
```

## Critical path right now

1. ~~Docker Desktop~~ ✓ · ~~GitHub repo + push~~ ✓ · ~~Prompt 01 (dev harness)~~ ✓
2. ~~Prompt 02 (config + init_observability, traces)~~ ✓
3. ~~Prompt 03 (decorators + logs/metrics)~~ ✓ · ~~Prompt 04 (GenAI capture)~~ ✓
4. ~~Prompt 05 (CI + trusted-publishing release)~~ ✓ — CI green on `main`.
5. **Release:** confirm the PyPI trusted-publishing publisher is registered,
   then tag `v0.1.0` and push the tag. `release.yml` builds, guards
   tag↔version, and publishes to PyPI. Tagging is a human act — do it yourself.
