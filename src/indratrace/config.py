"""Config resolution: the API key, and a small number of optional labels.

Responsibilities (docs/architecture.md):
- Resolve the API key from the `api_key` argument or `INDRATRACE_API_KEY`, and
  fail loudly when there is none.
- Build the OTel Resource carrying the attributes the SDK still owns
  (docs/conventions.md § Resource attributes).

**v1.0 — the key decides identity (ADR 0009).** `product`, `deployment.environment`
and `tenant.id` are no longer the SDK's to resolve or to send: the platform's
ingest gateway drops whatever the payload claims and stamps all three from the
API key (platform P80 / platform ADR 0010 §1 as amended). What is left here is a
credential and two labels about the *deployable* — `service.name`, `service.version`.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass

from opentelemetry.sdk.resources import Resource

from .version import __version__


class IndraTraceConfigError(ValueError):
    """The SDK was configured in a way that cannot possibly work.

    Raised **only** from `init_observability`, which is the one moment a
    developer is looking at the SDK. It does not weaken fail-silence: exports
    stay async, batched, and never raise or block at runtime (ADR 0003). What
    changes in 1.0 is that a *startup* mistake — no API key, or a parameter that
    no longer exists — stops being a silent stream of 401s nobody ever sees.

    A `ValueError` subclass so code that already guarded `init_observability`
    with `except ValueError` keeps catching it.
    """


#: Where the SDK ships OTLP when `INDRATRACE_ENDPOINT` is unset: the IndraTrace
#: **production** ingest gateway — the component that authenticates the API key
#: and stamps tenant/product/env (platform ADR 0010 §1).
#:
#: **Production, deliberately, and not the dev gateway.** This package is public
#: on PyPI. A dev default would route a stranger's telemetry into IndraTrace's
#: dev ClickHouse — and drop it silently every night, because dev is deallocated
#: 00:00–07:00 Central. IndraTrace's own engineers and self-hosted deployments
#: set `INDRATRACE_ENDPOINT` (a supported override since 1.1); a customer only
#: ever holds a key. One named constant, one place — there is no `endpoint`
#: parameter (see `REMOVED_PARAMS["endpoint"]` for why that stays true).
DEFAULT_ENDPOINT = "https://ingest.indratrace.com"

DEFAULT_SERVICE_VERSION = "0.0.0"

#: Seconds to spend on one export attempt (incl. OTel's internal retries).
#: OTel's own default is 10s, which makes a dead gateway stall `shutdown()`
#: — and therefore process exit — for seconds. ADR 0003 says drop, don't block.
DEFAULT_EXPORT_TIMEOUT_SECONDS = 3.0

#: The only supported env var for the key, and the only configuration a customer
#: ever sets. Everything else the platform derives from it.
ENV_API_KEY = "INDRATRACE_API_KEY"

#: **Supported override** (documented since 1.1; README § Configuration) for the
#: two callers who legitimately send somewhere other than `DEFAULT_ENDPOINT`:
#: self-hosted IndraTrace deployments, permanently, and IndraTrace's own dev
#: environment. It is an env var and not a parameter on purpose: which gateway a
#: deployment talks to is a property of the deployment, set where the rest of its
#: environment is provisioned — the same code runs against the cloud in one
#: place and a self-hosted gateway in another without a diff. A customer of the
#: hosted service never sets it.
ENV_ENDPOINT = "INDRATRACE_ENDPOINT"

#: Startup preflight mode (see `preflight.py`). `0`/`off`/`false`/`no` disables
#: the probe entirely (air-gapped deployments, unit tests); `strict` turns a
#: failed probe into `IndraTraceConfigError` (for CI); anything else — including
#: unset — is `warn`: log the diagnosis once and continue. See
#: `resolve_preflight_mode`.
ENV_PREFLIGHT = "INDRATRACE_PREFLIGHT"

#: Seconds the startup preflight waits to *connect*. Two seconds is long enough
#: for a cold TLS handshake to a far region and short enough that a dead
#: endpoint cannot delay a service boot meaningfully. A connect that does not
#: complete in this window is the egress-blocked signature.
PREFLIGHT_TIMEOUT_SECONDS = 2.0
#: Seconds the preflight waits for the *response* once connected. Longer than
#: connect: the gateway resolves the key against its control plane before
#: answering, which was measured at ~1.3s live, and a false "stalled" on a slow
#: lookup would send someone hunting a proxy that is not there.
PREFLIGHT_READ_TIMEOUT_SECONDS = 3.0

#: Opt-in prompt/completion content capture (default off). Truthy values:
#: 1/true/yes/on (case-insensitive). See `resolve_capture_content`.
ENV_CAPTURE_CONTENT = "INDRATRACE_CAPTURE_CONTENT"
#: Opt-in diagnostics (default off). When truthy, `init_observability` attaches a
#: console handler to the `indratrace` logger at DEBUG and logs a startup banner
#: plus export success/failure lines — turning fail-*silent* into fail-*audible*
#: without ever raising. Truthy: 1/true/yes/on. See `resolve_debug`.
ENV_DEBUG = "INDRATRACE_DEBUG"

# The three env vars 1.0 removed. Still *named* here — not to honor them, but so
# that a process which still sets one is told it is being ignored rather than
# silently getting different telemetry than it configured.
ENV_PRODUCT = "INDRATRACE_PRODUCT"
ENV_ENV = "INDRATRACE_ENV"
ENV_KEY = "INDRATRACE_KEY"  # the pre-1.0 deprecated alias for ENV_API_KEY

#: Env values that read as True. Anything else (incl. unset) is False.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: Env values that read as "off" for `INDRATRACE_PREFLIGHT`.
_FALSY = frozenset({"0", "false", "no", "off"})

#: Where `ObsConfig.endpoint` came from. The preflight's messages depend on it:
#: "nothing listens at localhost" means one thing when the *package default*
#: put you there and another when `INDRATRACE_ENDPOINT` did.
ENDPOINT_SOURCE_DEFAULT = "default"
ENDPOINT_SOURCE_ENV = "env"

#: Auth header carrying the API key (docs/conventions.md § Transport). The wire
#: header name is a fixed transport contract: it did not change with the
#: `ingest_key` → `api_key` rename (v0.5.0) and does not change in 1.0 either.
API_KEY_HEADER = "x-indratrace-key"

#: What every actionable message ends with. The key is created with the product,
#: and shown once — that is the sentence a stuck user actually needs.
_REGISTER_HINT = (
    "Register the product in your IndraTrace workspace, copy its key, and call "
    "init_observability(api_key=...)."
)

#: Raised when no key can be resolved. The failure this replaces was a silent
#: stream of 401s from the gateway that the customer never saw.
MISSING_API_KEY_MESSAGE = (
    "No API key. Set INDRATRACE_API_KEY or pass api_key=... — create one under "
    "Products in your IndraTrace workspace; the key is shown once when the "
    "product is created."
)

#: The parameters 1.0 removed from `init_observability`, and what to do instead.
#: `init_observability` swallows them into `**removed` purely so it can raise
#: *these* messages rather than a bare `TypeError` that explains nothing. Order
#: is the order they are checked in.
REMOVED_PARAMS: dict[str, str] = {
    "product": (
        "`product` was removed in 1.0 — the API key decides the product (and "
        "the environment). " + _REGISTER_HINT
    ),
    "env": (
        "`env` was removed in 1.0 — the API key decides the environment: a key "
        "belongs to one product in one environment, and the environment is "
        "chosen when the product is registered. " + _REGISTER_HINT
    ),
    "endpoint": (
        "`endpoint` was removed in 1.0 — the SDK ships to the IndraTrace ingest "
        "gateway, and the API key is what routes your telemetry once it lands. "
        "Drop the argument and call init_observability(api_key=...). Self-hosted "
        "or dev gateway? Set the INDRATRACE_ENDPOINT environment variable in that "
        "deployment instead — it is the supported override."
    ),
    "ingest_key": (
        "`ingest_key` was removed in 1.0 — it was the pre-0.5 name for "
        "`api_key`. Call init_observability(api_key=...)."
    ),
}

#: The env vars 1.0 removed. Setting one is **ignored**, with one warning each —
#: same wording idea as `REMOVED_PARAMS`, because it is the same mistake made in
#: the environment instead of in code.
REMOVED_ENV_VARS: dict[str, str] = {
    ENV_PRODUCT: (
        "INDRATRACE_PRODUCT was removed in 1.0 and is ignored — the API key "
        "decides the product (and the environment). " + _REGISTER_HINT
    ),
    ENV_ENV: (
        "INDRATRACE_ENV was removed in 1.0 and is ignored — the API key decides "
        "the environment: a key belongs to one product in one environment, and "
        "the environment is chosen when the product is registered. " + _REGISTER_HINT
    ),
    ENV_KEY: (
        "INDRATRACE_KEY was removed in 1.0 and is ignored — it was the "
        "deprecated alias for INDRATRACE_API_KEY. Set INDRATRACE_API_KEY "
        "instead."
    ),
}


@dataclass(frozen=True)
class ObsConfig:
    """Fully resolved configuration.

    Deliberately small: a credential, where to send, and two labels describing
    the deployable. Nothing here identifies the *customer's* product, tenant or
    environment — the gateway derives those from `api_key` (ADR 0009).
    """

    api_key: str
    endpoint: str = DEFAULT_ENDPOINT
    #: `ENDPOINT_SOURCE_DEFAULT` or `ENDPOINT_SOURCE_ENV` — which one supplied
    #: `endpoint`. Diagnostics only; never shapes transport.
    endpoint_source: str = ENDPOINT_SOURCE_DEFAULT
    #: `None` means "let OpenTelemetry decide" — its own default respects
    #: `OTEL_SERVICE_NAME`/`OTEL_RESOURCE_ATTRIBUTES` and otherwise yields
    #: `unknown_service`. Stamping a value of our own here would clobber that.
    service_name: str | None = None
    service_version: str = DEFAULT_SERVICE_VERSION
    export_timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_SECONDS

    @property
    def traces_endpoint(self) -> str:
        """OTLP/HTTP traces URL. `endpoint` is the base, per conventions.md."""
        return f"{self.endpoint.rstrip('/')}/v1/traces"

    @property
    def logs_endpoint(self) -> str:
        """OTLP/HTTP logs URL (docs/conventions.md § Transport)."""
        return f"{self.endpoint.rstrip('/')}/v1/logs"

    @property
    def metrics_endpoint(self) -> str:
        """OTLP/HTTP metrics URL (docs/conventions.md § Transport)."""
        return f"{self.endpoint.rstrip('/')}/v1/metrics"

    @property
    def headers(self) -> dict[str, str]:
        """Export headers. Always carries the key — 1.0 has no keyless mode."""
        return {API_KEY_HEADER: self.api_key}


def _first(*values: str | None) -> str | None:
    """First value that is neither None nor empty — encodes the precedence."""
    for value in values:
        if value:
            return value
    return None


def warn_about_removed_env_vars() -> None:
    """Warn once per removed `INDRATRACE_*` var that is set, then ignore it.

    A `UserWarning`, not a `DeprecationWarning`: these names are gone, not going,
    and `DeprecationWarning` is hidden by default outside `__main__` — which is
    exactly where a server process sets its environment. The whole point is that
    the operator finds out their variable stopped doing anything.

    "Set" means set to a non-empty value: an empty `INDRATRACE_PRODUCT` never
    configured anything, so warning about it would be noise (same emptiness rule
    as `_first`).
    """
    for name, message in REMOVED_ENV_VARS.items():
        if os.getenv(name):
            warnings.warn(message, UserWarning, stacklevel=3)


def _resolve_api_key(api_key: str | None) -> str:
    """`api_key` arg > `INDRATRACE_API_KEY` env > raise.

    Raises:
        IndraTraceConfigError: when neither source supplies a non-empty key. An
            empty string is "no key", not a key.
    """
    resolved = _first(api_key, os.getenv(ENV_API_KEY))
    if not resolved:
        raise IndraTraceConfigError(MISSING_API_KEY_MESSAGE)
    # Presence only — no format check. A key that does not start with `it_` is
    # still sent: the gateway is the authority on whether a key is valid, and a
    # second authority here could only ever disagree with it (and would reject
    # any future key format the platform introduces).
    return resolved


def resolve_config(
    api_key: str | None = None,
    service_name: str | None = None,
    service_version: str | None = None,
) -> ObsConfig:
    """Resolve config. The API key is required; everything else is optional.

    Raises:
        IndraTraceConfigError: if no API key is given and `INDRATRACE_API_KEY`
            is unset or empty.
    """
    endpoint_override = _first(os.getenv(ENV_ENDPOINT))
    return ObsConfig(
        api_key=_resolve_api_key(api_key),
        # Developer override only (see ENV_ENDPOINT); customers never set it.
        endpoint=endpoint_override or DEFAULT_ENDPOINT,
        endpoint_source=(
            ENDPOINT_SOURCE_ENV if endpoint_override else ENDPOINT_SOURCE_DEFAULT
        ),
        service_name=service_name or None,
        service_version=service_version or DEFAULT_SERVICE_VERSION,
        # Read at call time, not bound as a dataclass default, so the test
        # suite can shrink it and not pay a real export backoff per teardown.
        export_timeout_seconds=DEFAULT_EXPORT_TIMEOUT_SECONDS,
    )


def resolve_capture_content(capture_content: bool | None = None) -> bool:
    """Resolve prompt/completion content capture with the usual precedence.

    Explicit arg > `INDRATRACE_CAPTURE_CONTENT` env var > default (``False``).
    A separate resolver (not an `ObsConfig` field) because it does not shape
    transport or the resource — it only gates what the GenAI instrumentors
    record, and lives closest to where that flag is consumed (`genai.py`).

    Off by default: prompts carry customer data (docs/conventions.md § Content
    capture). The env value is truthy for ``1/true/yes/on`` (case-insensitive);
    anything else, including unset, is ``False``.
    """
    if capture_content is not None:
        return capture_content
    raw = os.getenv(ENV_CAPTURE_CONTENT)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def resolve_debug(debug: bool | None = None) -> bool:
    """Resolve the diagnostics flag with the usual precedence.

    Explicit arg > `INDRATRACE_DEBUG` env var > default (``False``). A separate
    resolver (not an `ObsConfig` field) because it shapes neither transport nor
    the resource — it only decides whether `init_observability` makes its
    diagnostics *audible*. The env value is truthy for ``1/true/yes/on``
    (case-insensitive); anything else, including unset, is ``False``.
    """
    if debug is not None:
        return debug
    raw = os.getenv(ENV_DEBUG)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


#: The three `INDRATRACE_PREFLIGHT` modes (see `resolve_preflight_mode`).
PREFLIGHT_OFF = "off"
PREFLIGHT_WARN = "warn"
PREFLIGHT_STRICT = "strict"


def resolve_preflight_mode() -> str:
    """How `init_observability` runs its startup preflight, from the env.

    `INDRATRACE_PREFLIGHT` unset or anything unrecognised → `warn` (the
    default: probe once, log the diagnosis, never raise). `0/false/no/off` →
    `off` (no network call at all — air-gapped hosts, unit tests). `strict` →
    a failed probe raises `IndraTraceConfigError`, for a CI job that wants a
    misconfigured service to fail its build rather than boot and drop data.

    Env-only, no argument: the mode is a property of *where* the process runs
    (a CI runner, an air-gapped box), not of the code, and this slice exists to
    keep configuration singular.
    """
    raw = (os.getenv(ENV_PREFLIGHT) or "").strip().lower()
    if raw in _FALSY:
        return PREFLIGHT_OFF
    if raw == PREFLIGHT_STRICT:
        return PREFLIGHT_STRICT
    return PREFLIGHT_WARN


#: The three the **gateway** owns and the SDK must never send (platform P80,
#: `ingest/stamp.py::STAMPED_ATTRS`). Named here so the test that asserts their
#: absence from the Resource reads from the same list the docstring cites — the
#: SDK-side twin of the platform's grep that `stamp.py` is their only writer.
GATEWAY_STAMPED_ATTRS = ("product", "deployment.environment", "tenant.id")


def build_resource(cfg: ObsConfig) -> Resource:
    """The Resource stamped on every signal (docs/conventions.md).

    Carries what the SDK legitimately knows: the deployable's name and version,
    plus `telemetry.sdk.*` (ours and OpenTelemetry's own — `Resource.create`
    merges over the SDK defaults, so the standard set survives).

    It carries **none** of `GATEWAY_STAMPED_ATTRS`. The gateway drops the
    client's values for those and appends the key's, so sending them would be
    sending a claim we already know is discarded (ADR 0009).
    """
    attributes: dict[str, str] = {
        "service.version": cfg.service_version,
        "telemetry.sdk.wrapper": f"indratrace/{__version__}",
    }
    if cfg.service_name:
        attributes["service.name"] = cfg.service_name
    return Resource.create(attributes)
