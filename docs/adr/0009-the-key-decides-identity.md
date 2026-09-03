# ADR 0009 — The key decides identity

- **Status:** Accepted (Rithin, 2026-09-03)
- **Date:** 2026-09-03
- **Amends:** ADR 0003 (thin OTel wrapper — the config surface it describes),
  and the § Resource attributes / § Transport sections of `docs/conventions.md`.
- **Platform counterpart:** platform P80; platform ADR 0010 §1 as amended
  (`ingest/stamp.py::STAMPED_ATTRS`). Read-only from this repo — two repos, two
  tasks.
- **Shipped in:** v1.0.0 (breaking).

## Context

Until 1.0 an SDK consumer configured their own identity:

```python
init_observability(product="compliance", env="prod", api_key="it_live_...")
```

`product` and `deployment.environment` were resolved in the SDK — from an
argument or from `INDRATRACE_PRODUCT` / `INDRATRACE_ENV` — stamped onto the
Resource, and shipped. `tenant.id` was a constant (`internal`). The endpoint was
a fourth thing to get right.

Two things made that untenable, and they arrived together.

**The platform stopped trusting the payload.** The authenticating ingest
gateway (platform ADR 0010 §1) already overwrote `tenant.id` from the API key —
the key is truth, the payload is a claim. At P80 that same rule was extended to
`product` and `deployment.environment`: the gateway now **drops** the client's
value for all three and **appends** the key's. The environment moved to the
product's registration (`products.env`); *product-as-label*, and the "discovered
product" flow it enabled, were withdrawn. So every `product=` an SDK sent was
already being discarded on arrival.

**A missing key failed silently.** With no key the SDK sent no auth header. The
gateway answered 401, the batch processor dropped the batch, and — because the
SDK never raises (ADR 0003) — the customer saw an empty dashboard and no reason
for it. The most common integration mistake produced the least informative
failure we had.

Between them: the SDK was carefully resolving three attributes nobody would
read, while failing to insist on the one value everything depended on.

## Decision

**The SDK carries credentials, never identity.**

1. **`init_observability(api_key)` is the whole public signature.** `api_key` is
   the only positional argument. Everything else — `service_name`,
   `service_version`, `instrument_http`, `log_level`, `capture_content`,
   `debug` — is keyword-only, optional, and documented under "Advanced".

2. **`product`, `env`, `endpoint` and `ingest_key` are removed**, as parameters
   and (for the first two, plus `INDRATRACE_KEY`) as env vars. Passing a removed
   parameter raises `IndraTraceConfigError` naming it and saying what to do
   instead; setting a removed env var is ignored with one `UserWarning`. A bare
   `TypeError` would tell a 0.x user nothing about why their argument vanished —
   the migration message *is* the feature.

3. **The key is required, and its absence is loud.** No `api_key` and no
   `INDRATRACE_API_KEY` raises `IndraTraceConfigError` (a `ValueError` subclass)
   at `init_observability`, with the text that gets the user unstuck: create a
   key under Products; it is shown once.

4. **Fail-fast at init only. Runtime posture is unchanged.** ADR 0003 stands:
   exports are async, batched, and never raise or block the host app. The one
   place a misconfiguration surfaces is the startup call — a line of code a
   developer is standing in front of, not a request path.

5. **No format validation of the key.** The SDK checks presence, never shape. A
   key that does not start with `it_` is still sent; the gateway is the single
   authority on validity, and a second authority here could only ever disagree
   with it — or reject a key format the platform introduces later.

6. **The SDK stops sending `product`, `deployment.environment` and `tenant.id`.**
   The Resource carries `service.name`, `service.version` and `telemetry.sdk.*`.
   The three stamped attributes are named once, in
   `config.GATEWAY_STAMPED_ATTRS`, and a test asserts the Resource contains none
   of them — the SDK-side twin of the platform's grep that `stamp.py` is their
   only writer.

7. **One endpoint constant, `http://localhost:8088`** — the dev gateway,
   replacing the dead collector port `:4318`. It becomes the production ingest
   hostname when that is decided (platform deployment arc phase 5).
   `INDRATRACE_ENDPOINT` survives as an **undocumented developer override** for
   running against a local stack, documented once in `CONTRIBUTING.md` and
   absent from the README and from conventions.md's customer-facing Transport
   section. A customer never learns it exists: they point at IndraTrace by
   holding an IndraTrace key, not by choosing a host.

## Consequences

- **This is a breaking change on a public PyPI package → v1.0.0.** Every 0.x
  integration passes at least `product=`, so every 0.x integration must be
  edited. That is the cost of the one-liner, paid once, with an error message at
  each call site telling the developer exactly what to write.
- **The SDK's config surface is now a credential and two service labels.** There
  is less to get wrong, and less to document: the README leads with the
  one-liner and a "Still waiting for your first span?" section that has only
  three steps, because there are only three things that can be wrong.
- **Discovered products are retired**, on the platform side (ADR 0006 §2, as
  amended by P80). Telemetry can no longer arrive under a name nobody
  registered, because the SDK no longer offers a way to name one.
- **`service.name` loses its default.** It used to fall back to `product`. It
  now falls back to OpenTelemetry's own resolution — `OTEL_SERVICE_NAME`, else
  `unknown_service` — which is more correct (it stops clobbering a value the app
  already set) but means a multi-deployable product should pass `service_name=`.
- **Bring-your-own-backend is withdrawn as a documented capability.** It was the
  `endpoint` parameter, and the parameter is gone. The SDK still emits plain
  OTLP and `INDRATRACE_ENDPOINT` still works, but it is no longer offered to
  customers; making it a supported story again is a future decision, not a side
  effect of this one.
- **The exception is public API.** `IndraTraceConfigError` is exported from
  `indratrace` and subclasses `ValueError`, so a pre-1.0 `except ValueError`
  guard around init keeps catching it.

## Release coordination

Released 2026-09-03; the platform pinned `<1.0` until its own call site migrated.
