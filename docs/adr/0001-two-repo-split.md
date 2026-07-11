# ADR 0001 — Two repos: SDK and platform

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
IndraTrace has two deliverables with fundamentally different lifecycles: a Python
library that products install and upgrade on their own schedule, and a platform
(Collector, ClickHouse, FastAPI, Next.js) that we deploy and control centrally.

## Decision
Two repositories:
- `indratrace-python-sdk` (this repo) → https://github.com/indrasol/indratrace-python-sdk
- `indratrace-platform` → https://github.com/indrasol/indratrace-platform

## Alternatives considered
- **Single monorepo:** simpler at first, but platform dependencies bleed into the
  library, versioning couples, and the SDK can't be opened publicly without
  exposing platform internals.

## Consequences
- SDK versions independently (semver, PyPI); platform deploys continuously.
- The compatibility contract between them is NOT code — it is OTLP plus the
  attribute conventions in `docs/conventions.md`. That doc is the real API.
- The SDK repo must be self-testing (see ADR 0004).
