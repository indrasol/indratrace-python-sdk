"""Config resolution + the resource attribute contract (docs/conventions.md)."""

from __future__ import annotations

import pytest

from indratrace.config import (
    DEFAULT_ENDPOINT,
    DEFAULT_ENV,
    DEFAULT_TENANT_ID,
    INGEST_KEY_HEADER,
    ObsConfig,
    build_resource,
    resolve_config,
)
from indratrace.version import __version__

# Every attribute conventions.md marks Required.
REQUIRED_RESOURCE_ATTRS = (
    "service.name",
    "service.version",
    "product",
    "deployment.environment",
    "tenant.id",
    "telemetry.sdk.wrapper",
)


@pytest.fixture
def no_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the conftest test endpoint so real defaults are observable."""
    monkeypatch.delenv("INDRATRACE_ENDPOINT", raising=False)


class TestDefaults:
    def test_only_product_is_required(self, no_endpoint_env: None) -> None:
        cfg = resolve_config(product="compliance")

        assert cfg.product == "compliance"
        assert cfg.endpoint == DEFAULT_ENDPOINT
        assert cfg.env == DEFAULT_ENV
        assert cfg.tenant_id == DEFAULT_TENANT_ID
        assert cfg.ingest_key is None

    def test_service_name_defaults_to_product(self) -> None:
        assert resolve_config(product="compliance").service_name == "compliance"

    def test_missing_product_raises(self) -> None:
        with pytest.raises(ValueError, match="product is required"):
            resolve_config()


class TestPrecedence:
    """Explicit args > env vars > defaults."""

    def test_env_vars_beat_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDRATRACE_PRODUCT", "from-env")
        monkeypatch.setenv("INDRATRACE_ENV", "staging")
        monkeypatch.setenv("INDRATRACE_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("INDRATRACE_KEY", "env-key")

        cfg = resolve_config()

        assert cfg.product == "from-env"
        assert cfg.env == "staging"
        assert cfg.endpoint == "http://collector:4318"
        assert cfg.ingest_key == "env-key"

    def test_explicit_args_beat_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INDRATRACE_PRODUCT", "from-env")
        monkeypatch.setenv("INDRATRACE_ENV", "staging")
        monkeypatch.setenv("INDRATRACE_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("INDRATRACE_KEY", "env-key")

        cfg = resolve_config(
            product="from-arg",
            env="prod",
            endpoint="http://explicit:4318",
            ingest_key="arg-key",
        )

        assert cfg.product == "from-arg"
        assert cfg.env == "prod"
        assert cfg.endpoint == "http://explicit:4318"
        assert cfg.ingest_key == "arg-key"

    def test_empty_arg_falls_through_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string is absence, not an override."""
        monkeypatch.setenv("INDRATRACE_ENV", "staging")

        assert resolve_config(product="p", env="").env == "staging"

    def test_args_and_env_can_mix(
        self, monkeypatch: pytest.MonkeyPatch, no_endpoint_env: None
    ) -> None:
        monkeypatch.setenv("INDRATRACE_KEY", "env-key")

        cfg = resolve_config(product="p", env="prod")

        assert (cfg.product, cfg.env) == ("p", "prod")
        assert cfg.ingest_key == "env-key"  # only the key came from env
        assert cfg.endpoint == DEFAULT_ENDPOINT  # and this from defaults


class TestTransport:
    def test_traces_endpoint_appends_signal_path(self) -> None:
        cfg = resolve_config(product="p", endpoint="http://host:4318")
        assert cfg.traces_endpoint == "http://host:4318/v1/traces"

    def test_traces_endpoint_tolerates_trailing_slash(self) -> None:
        cfg = resolve_config(product="p", endpoint="http://host:4318/")
        assert cfg.traces_endpoint == "http://host:4318/v1/traces"

    def test_ingest_key_becomes_auth_header(self) -> None:
        cfg = resolve_config(product="p", ingest_key="secret")
        assert cfg.headers == {INGEST_KEY_HEADER: "secret"}

    def test_no_key_means_no_header(self) -> None:
        assert resolve_config(product="p").headers == {}

    def test_export_timeout_is_shorter_than_otel_default(self) -> None:
        """OTel defaults to 10s, which stalls shutdown when the collector is
        down. ADR 0003: drop, don't block."""
        assert resolve_config(product="p").export_timeout_seconds < 10.0


class TestResource:
    def test_carries_every_required_attribute(self) -> None:
        cfg = resolve_config(product="compliance")

        attrs = build_resource(cfg).attributes

        for attr in REQUIRED_RESOURCE_ATTRS:
            assert attr in attrs, f"conventions.md requires {attr!r}"

    def test_attribute_values_come_from_config(self) -> None:
        cfg = ObsConfig(
            product="compliance",
            env="prod",
            endpoint=DEFAULT_ENDPOINT,
            service_name="compliance-api",
            service_version="1.4.2",
            tenant_id="acme",
        )

        attrs = build_resource(cfg).attributes

        assert attrs["service.name"] == "compliance-api"
        assert attrs["service.version"] == "1.4.2"
        assert attrs["product"] == "compliance"
        assert attrs["deployment.environment"] == "prod"
        assert attrs["tenant.id"] == "acme"

    def test_wrapper_attribute_identifies_sdk_version(self) -> None:
        attrs = build_resource(resolve_config(product="p")).attributes
        assert attrs["telemetry.sdk.wrapper"] == f"indratrace/{__version__}"

    def test_otel_sdk_defaults_survive_the_merge(self) -> None:
        """Our attrs must not clobber the standard telemetry.sdk.* set."""
        attrs = build_resource(resolve_config(product="p")).attributes
        assert attrs["telemetry.sdk.language"] == "python"
        assert "telemetry.sdk.version" in attrs
