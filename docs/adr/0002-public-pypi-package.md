# ADR 0002 — Public PyPI package, Apache-2.0

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
The SDK contains no secrets — ingest key and endpoint are user-supplied
configuration. Long-term vision: anyone can `pip install indratrace`, subscribe
to the hosted platform, and get an ingest key (the Anthropic/OpenAI SDK model).

## Decision
- Package name `indratrace`, published publicly on PyPI (name reserved 2026-07-09
  with stub 0.0.1). License: Apache-2.0.
- The platform is a separate, closed-source product; ingest keys are only
  mintable there.
- Internal products install from public PyPI like any dependency.

## Alternatives considered
- **Private Azure Artifacts:** considered for the pre-1.0 churn phase; rejected
  as primary channel since the name was reservable now and the package carries
  no secrets. Nothing prevents publishing dev builds privately if needed.

## Consequences
- Public hygiene required: semver, CHANGELOG, README quickstart, no silent
  breaking changes.
- Endpoint must be first-class config (`INDRATRACE_ENDPOINT`), never hardcoded.
- Version numbers on PyPI are immutable; fixes ship as new versions.
