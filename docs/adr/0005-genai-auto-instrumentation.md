# ADR 0005 — GenAI capture via existing auto-instrumentation libraries

- **Status:** Accepted
- **Date:** 2026-07-09

## Context
Token usage must be exact. Provider APIs (Anthropic, OpenAI, Gemini) return
exact usage counts in every response — the same numbers they bill on. The
question is who reads them off the response: our own client wrappers, or
existing OTel instrumentation libraries.

## Decision
Use existing OTel-ecosystem auto-instrumentation (OpenLLMetry's
`opentelemetry-instrumentation-anthropic` / `-openai`) enabled inside
`init_observability()`. They patch the provider clients and record
`gen_ai.usage.input_tokens` / `output_tokens` per the OTel GenAI semantic
conventions, including cache read/write token fields where available.
`genai.py` adds only a thin manual-capture fallback for unsupported providers.

## Alternatives considered
- **Hand-rolled client wrappers:** full control, no third-party deps — but we
  own streaming edge cases (usage arrives in the final chunk), provider API
  drift, and every new provider. Contradicts ADR 0003's wrap-don't-rebuild rule.

## Consequences
- Instrumentation deps are optional extras (`pip install indratrace[anthropic]`)
  so the core stays feather-light.
- Token counts stored raw as span attributes. Cost (USD) is NOT computed in the
  SDK — it is derived at query time in the platform from a price table, so
  price updates fix historical dashboards without reprocessing.
- Streaming caveat documented: OpenAI streams need
  `stream_options={"include_usage": True}` for usage to appear.
