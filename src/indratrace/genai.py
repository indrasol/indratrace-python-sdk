"""GenAI capture: enable OpenLLMetry instrumentors when installed; manual
fallback for unsupported providers.

The provider instrumentors (`opentelemetry-instrumentation-anthropic` /
`-openai`) patch the provider clients and emit a model span per call carrying
the exact, provider-reported token counts — the same numbers the provider
bills on (ADR 0005). They ship as optional extras so the core stays OTel-only
(ADR 0003); an absent extra is a normal, silent outcome.

Raw token counts only — no cost math (ADR 0005). Cost is derived at query time
in the platform from a price table, so a price change fixes historical
dashboards without reprocessing.

The instrumentors are always handed OUR tracer provider explicitly
(`instrument(tracer_provider=...)`). The OTel *global* provider is frozen at
the first `set_tracer_provider` in a process, so relying on it would send model
spans to a stale provider in any process that inits more than once — every test
session, and reloading workers (docs/architecture.md, "Testing notes").
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger("indratrace")

#: The env var the pinned OpenLLMetry instrumentors read to decide whether to
#: record prompt/completion text on model spans. Verified at the pinned
#: versions: `should_send_prompts()` in each instrumentor reads it and — this is
#: the trap — treats an UNSET value as ``"true"``. Left alone, the instrumentors
#: would capture customer prompts by default. So we always set it explicitly
#: from our own `capture_content` flag: `"true"` to opt in, `"false"` to keep
#: the SDK default (off). Setting it programmatically means a user flips one
#: boolean and never has to know this env var exists (docs/conventions.md
#: § Content capture).
_TRACELOOP_TRACE_CONTENT = "TRACELOOP_TRACE_CONTENT"

#: Attribute names the instrumentors land prompt/completion text under when
#: content capture is on, verified empirically at the pinned versions (see
#: tests/test_genai.py::TestContentCapture). They track the current OTel GenAI
#: semconv (`gen_ai.input.messages` / `gen_ai.output.messages`), not the older
#: `gen_ai.prompt.N` / `gen_ai.completion.N` forms. Recorded in
#: docs/conventions.md § Content capture.
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"

#: GenAI attributes the auto-instrumentors emit, verified empirically at the
#: pinned versions (see tests/test_genai.py::test_mocked_anthropic_span_attribute_names
#: and the drift note in docs/conventions.md § Model spans). `record_llm_usage`
#: stamps the SAME names, so a hand-instrumented provider is indistinguishable
#: from an auto-instrumented one on the wire.
#:
#: Token counts match conventions.md unchanged. The system-identity key has
#: drifted, though: conventions.md specifies `gen_ai.system`, but current
#: OpenLLMetry (tracking the OTel GenAI semconv rename) emits
#: `gen_ai.provider.name`. We stamp the drifted name to stay identical to the
#: auto path; the platform aliases the two at query time.
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

#: (extra name, module path, instrumentor class). Import is attempted lazily so
#: a product without the extra pays nothing and sees no error. Module/class
#: names verified against the pinned OpenLLMetry versions (all ship together at
#: the same release train). Note each instrumentor imports its provider SDK at
#: module load — `google.genai` for Gemini, `botocore` for Bedrock — so with the
#: extra installed but the provider SDK absent, the import raises `ImportError`
#: and is treated as the same normal, silent skip as an absent extra.
_INSTRUMENTORS: tuple[tuple[str, str, str], ...] = (
    ("anthropic", "opentelemetry.instrumentation.anthropic", "AnthropicInstrumentor"),
    ("openai", "opentelemetry.instrumentation.openai", "OpenAIInstrumentor"),
    (
        "gemini",
        "opentelemetry.instrumentation.google_generativeai",
        "GoogleGenerativeAiInstrumentor",
    ),
    ("bedrock", "opentelemetry.instrumentation.bedrock", "BedrockInstrumentor"),
)


def enable_genai_instrumentation(
    tracer_provider: TracerProvider, capture_content: bool = False
) -> list[tuple[str, bool, str]]:
    """Instrument every available GenAI provider against OUR tracer provider.

    For each supported provider: try to import its instrumentor (absent extra
    is a silent skip — not every product calls an LLM), then instrument it,
    passing `tracer_provider` explicitly so model spans land on the provider
    `init_observability` built rather than the frozen global.

    `capture_content` gates whether prompt/completion **text** is recorded on
    model spans. It maps to the instrumentors' `TRACELOOP_TRACE_CONTENT` env var,
    which we set here *before* instrumenting — the pinned instrumentors read it
    once via `should_send_prompts()` and, crucially, treat it as ``"true"`` when
    unset, so leaving it alone would capture customer prompts by default. We
    therefore always set it explicitly: our default (`False`) writes
    ``"false"``, keeping content off unless the caller opts in. It is off by
    default because prompts carry customer data (docs/conventions.md); the
    typical use is on in dev/staging, off in prod.

    Fail-silent per instrumentor (ADR 0003): any import or instrument failure is
    logged at debug and skipped, so one broken provider integration never denies
    the others — or the host app — their telemetry.

    Returns one `(provider, enabled, reason)` tuple per supported provider, so
    `init_observability` can render which GenAI providers came on and why the
    rest didn't in its debug banner. The reason is human-readable; on the
    enabled path it is empty.
    """
    # Set before any instrument() call: the instrumentors latch this via
    # `should_send_prompts()` on the first model call, and an unset value means
    # "on". Writing our resolved flag is what makes off-by-default true.
    os.environ[_TRACELOOP_TRACE_CONTENT] = "true" if capture_content else "false"

    statuses: list[tuple[str, bool, str]] = []
    for extra, module_path, class_name in _INSTRUMENTORS:
        try:
            module = __import__(module_path, fromlist=[class_name])
            instrumentor_cls = getattr(module, class_name)
        except ImportError:
            logger.debug(
                "indratrace: %s extra not installed; skipping GenAI "
                "instrumentation for it",
                extra,
            )
            statuses.append((extra, False, "extra not installed"))
            continue
        except Exception as exc:  # noqa: BLE001 — a broken import must not spread
            logger.debug(
                "indratrace: could not load the %s instrumentor; skipping",
                extra,
                exc_info=True,
            )
            statuses.append((extra, False, f"load failed: {exc}"))
            continue

        try:
            instrumentor_cls().instrument(tracer_provider=tracer_provider)
            logger.debug("indratrace: %s GenAI instrumentation enabled", extra)
            statuses.append((extra, True, ""))
        except Exception as exc:  # noqa: BLE001 — one provider must not sink the rest
            logger.debug(
                "indratrace: enabling %s GenAI instrumentation failed; skipping",
                extra,
                exc_info=True,
            )
            statuses.append((extra, False, f"instrument failed: {exc}"))
    return statuses


def _uninstrument_genai() -> None:
    """Undo `enable_genai_instrumentation`. Not public API — for `_reset_for_tests`.

    The instrumentors monkeypatch the provider clients process-wide, so a test
    that inits must be able to unpatch them, or the patch leaks into the next
    test. Absent extra / not-instrumented is a silent no-op.

    Also clears `TRACELOOP_TRACE_CONTENT`: `enable_genai_instrumentation` sets it
    process-wide, so leaving it set would leak a `capture_content=True` run's
    content-on state into the next init in the same process (every test session).
    """
    os.environ.pop(_TRACELOOP_TRACE_CONTENT, None)

    for _extra, module_path, class_name in _INSTRUMENTORS:
        try:
            module = __import__(module_path, fromlist=[class_name])
            instrumentor = getattr(module, class_name)()
            # Only unpatch what we patched — calling uninstrument on a clean
            # instrumentor logs a noisy "already uninstrumented" warning.
            if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                instrumentor.uninstrument()
        except Exception:  # noqa: BLE001 — unpatching a clean process
            continue


def record_llm_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    system: str = "other",
    **extra: Any,
) -> None:
    """Stamp canonical `gen_ai.*` usage attributes on the CURRENT span.

    A manual fallback for providers we do not auto-instrument: call it from
    inside your own model-call code (e.g. within a `@trace_tool`) with the
    counts the provider returned, and the resulting attributes are identical to
    what the auto-instrumentors emit — so the platform treats both the same.

    Args:
        model: the model identifier, e.g. ``"claude-3-5-haiku-latest"``.
        input_tokens: provider-reported prompt/input token count.
        output_tokens: provider-reported completion/output token count.
        system: the GenAI system/provider name, e.g. ``"anthropic"``,
            ``"openai"``. Stamped under ``gen_ai.provider.name`` to match what
            the auto-instrumentors emit (see the drift note above). Defaults to
            ``"other"``.
        **extra: additional `gen_ai.*` (or any) attributes to stamp verbatim —
            e.g. cache token fields the provider reports.

    Raw counts only — no cost (ADR 0005). Never raises into the caller
    (ADR 0003): with no active recording span it is a silent no-op, and any
    failure is swallowed at debug.
    """
    try:
        span = trace.get_current_span()
        if not span.is_recording():
            logger.debug(
                "indratrace: record_llm_usage called with no recording span; "
                "attributes dropped (is there an enclosing @trace_agent/@trace_tool?)"
            )
            return

        span.set_attribute(GEN_AI_PROVIDER_NAME, system)
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, int(input_tokens))
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, int(output_tokens))
        for key, value in extra.items():
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 — instrumentation must never break the app
        logger.debug("indratrace: record_llm_usage failed", exc_info=True)
