# ADR 0007 — Log bridge never mutates the host app's logging; opt-in `log_level`

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
To ship a `logger.info(...)` line, the record must pass the stdlib root
logger's level (default WARNING). Code review of prompt 03 flagged that
`init_observability()` silently lowering root to INFO changes what the app's
OWN console/file handlers print — a hidden global side effect.

## Decision
The SDK attaches its export handler to root but **never changes the root
level by default**. An explicit `log_level=` parameter on
`init_observability()` opts in (and is the documented knob for bare scripts).
Apps already at INFO (basicConfig, uvicorn, gunicorn — all our products) ship
logs with zero configuration.

## Alternatives considered
- **Always lower root to INFO:** guarantees logs ship, but reconfigures the
  host app invisibly. Wrong altitude for a library; trust-killer publicly.
- **Never touch root, no knob:** purest, but a bare-script user following the
  README sees no logs and has no obvious remedy.

## Consequences
- product-spec's "logs flow automatically" carries one caveat: at the app's
  own configured level. README documents it.
- Extends the ADR 0003 principle into logging: the SDK observes the host app;
  it never reconfigures it.
- SDK/OTel internal loggers are excluded from export (feedback-loop guard).
