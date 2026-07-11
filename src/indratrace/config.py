"""Config resolution: explicit args > env vars > defaults.

Responsibilities (docs/architecture.md):
- Resolve endpoint/key/product/env from args or INDRATRACE_* env vars.
- Build the OTel Resource carrying the required attributes (docs/conventions.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from opentelemetry.sdk.resources import Resource

from .version import __version__

DEFAULT_ENDPOINT = "http://localhost:4318"
DEFAULT_ENV = "dev"
DEFAULT_TENANT_ID = "internal"
DEFAULT_SERVICE_VERSION = "0.0.0"

#: Seconds to spend on one export attempt (incl. OTel's internal retries).
#: OTel's own default is 10s, which makes a dead collector stall `shutdown()`
#: — and therefore process exit — for seconds. ADR 0003 says drop, don't block.
DEFAULT_EXPORT_TIMEOUT_SECONDS = 3.0

ENV_ENDPOINT = "INDRATRACE_ENDPOINT"
ENV_KEY = "INDRATRACE_KEY"
ENV_PRODUCT = "INDRATRACE_PRODUCT"
ENV_ENV = "INDRATRACE_ENV"
#: Opt-in prompt/completion content capture (default off). Truthy values:
#: 1/true/yes/on (case-insensitive). See `resolve_capture_content`.
ENV_CAPTURE_CONTENT = "INDRATRACE_CAPTURE_CONTENT"
#: Opt-in diagnostics (default off). When truthy, `init_observability` attaches a
#: console handler to the `indratrace` logger at DEBUG and logs a startup banner
#: plus export success/failure lines — turning fail-*silent* into fail-*audible*
#: without ever raising. Truthy: 1/true/yes/on. See `resolve_debug`.
ENV_DEBUG = "INDRATRACE_DEBUG"

#: Env values that read as True. Anything else (incl. unset) is False.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Auth header carrying the ingest key (docs/conventions.md § Transport).
INGEST_KEY_HEADER = "x-indratrace-key"


@dataclass(frozen=True)
class ObsConfig:
    """Fully resolved configuration. Every field has a value by construction."""

    product: str
    env: str
    endpoint: str
    service_name: str
    service_version: str
    tenant_id: str = DEFAULT_TENANT_ID
    ingest_key: str | None = None
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
        """Export headers. Empty when no ingest key is configured."""
        if not self.ingest_key:
            return {}
        return {INGEST_KEY_HEADER: self.ingest_key}


def _first(*values: str | None) -> str | None:
    """First value that is neither None nor empty — encodes the precedence."""
    for value in values:
        if value:
            return value
    return None


def resolve_config(
    product: str | None = None,
    env: str | None = None,
    ingest_key: str | None = None,
    endpoint: str | None = None,
    service_name: str | None = None,
    service_version: str | None = None,
    tenant_id: str | None = None,
) -> ObsConfig:
    """Resolve config with precedence: explicit args > env vars > defaults.

    Raises:
        ValueError: if `product` resolves to nothing. It is required by the
            attribute contract and there is no sane default for it.
    """
    resolved_product = _first(product, os.getenv(ENV_PRODUCT))
    if not resolved_product:
        raise ValueError(
            "product is required: pass product= to init_observability() "
            f"or set {ENV_PRODUCT}"
        )

    return ObsConfig(
        product=resolved_product,
        env=_first(env, os.getenv(ENV_ENV)) or DEFAULT_ENV,
        endpoint=_first(endpoint, os.getenv(ENV_ENDPOINT)) or DEFAULT_ENDPOINT,
        # The deployable's name; defaults to the product it belongs to.
        service_name=service_name or resolved_product,
        service_version=service_version or DEFAULT_SERVICE_VERSION,
        tenant_id=tenant_id or DEFAULT_TENANT_ID,
        ingest_key=_first(ingest_key, os.getenv(ENV_KEY)),
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


def build_resource(cfg: ObsConfig) -> Resource:
    """The Resource stamped on every signal (docs/conventions.md).

    `Resource.create` merges these over the OTel SDK defaults, so the standard
    `telemetry.sdk.*` attributes survive alongside our `telemetry.sdk.wrapper`.
    """
    return Resource.create(
        {
            "service.name": cfg.service_name,
            "service.version": cfg.service_version,
            "product": cfg.product,
            "deployment.environment": cfg.env,
            "tenant.id": cfg.tenant_id,
            "telemetry.sdk.wrapper": f"indratrace/{__version__}",
        }
    )
