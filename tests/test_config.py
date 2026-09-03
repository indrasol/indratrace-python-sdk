"""Config resolution + the resource attribute contract (docs/conventions.md).

v1.0 (ADR 0009): the API key is the only configuration. `product`, `env` and
`endpoint` are gone as parameters and as env vars, and the three attributes the
gateway stamps from the key are gone from the wire.
"""

from __future__ import annotations

import pytest

from indratrace.config import (
    API_KEY_HEADER,
    DEFAULT_ENDPOINT,
    DEFAULT_SERVICE_VERSION,
    ENV_API_KEY,
    ENV_ENDPOINT,
    ENV_ENV,
    ENV_KEY,
    ENV_PRODUCT,
    GATEWAY_STAMPED_ATTRS,
    IndraTraceConfigError,
    ObsConfig,
    build_resource,
    resolve_config,
    warn_about_removed_env_vars,
)
from indratrace.version import __version__

from .conftest import TEST_API_KEY

# Every attribute conventions.md marks Required *of the SDK*. The three the
# gateway stamps (GATEWAY_STAMPED_ATTRS) are still required on a stored row —
# they are just no longer the SDK's to send, so they are asserted absent here
# and present on the platform side.
REQUIRED_RESOURCE_ATTRS = (
    "service.name",
    "service.version",
    "telemetry.sdk.wrapper",
)


@pytest.fixture
def no_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the conftest test endpoint so real defaults are observable."""
    monkeypatch.delenv(ENV_ENDPOINT, raising=False)


class TestDefaults:
    def test_only_the_api_key_is_required(self, no_endpoint_env: None) -> None:
        cfg = resolve_config(api_key=TEST_API_KEY)

        assert cfg.api_key == TEST_API_KEY
        assert cfg.endpoint == DEFAULT_ENDPOINT
        assert cfg.service_name is None
        assert cfg.service_version == DEFAULT_SERVICE_VERSION

    def test_default_endpoint_is_the_ingest_gateway(self) -> None:
        """The pre-gateway collector port (:4318) no longer terminates SDK
        traffic; :8088 is the dev gateway that authenticates the key."""
        assert DEFAULT_ENDPOINT == "http://localhost:8088"

    def test_missing_api_key_raises_actionably(self) -> None:
        with pytest.raises(IndraTraceConfigError) as excinfo:
            resolve_config()

        message = str(excinfo.value)
        assert "No API key" in message
        assert ENV_API_KEY in message
        assert "api_key=" in message

    def test_empty_api_key_is_no_key(self) -> None:
        with pytest.raises(IndraTraceConfigError, match="No API key"):
            resolve_config(api_key="")

    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(IndraTraceConfigError, ValueError)


class TestPrecedence:
    """One key, two sources: the explicit arg beats the env var."""

    def test_env_var_supplies_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_API_KEY, "env-key")

        assert resolve_config().api_key == "env-key"

    def test_explicit_arg_beats_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_API_KEY, "env-key")

        assert resolve_config(api_key="arg-key").api_key == "arg-key"

    def test_empty_arg_falls_through_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string is absence, not an override."""
        monkeypatch.setenv(ENV_API_KEY, "env-key")

        assert resolve_config(api_key="").api_key == "env-key"

    def test_key_format_is_never_validated(self) -> None:
        """§2.3: the gateway is the authority on validity. Checking the shape
        here would be a second authority, and would reject any key format the
        platform introduces later."""
        assert resolve_config(api_key="not-an-it-key").api_key == "not-an-it-key"


class TestRemovedEnvVars:
    """`INDRATRACE_PRODUCT` / `_ENV` / `_KEY`: warn once each, then ignore."""

    @pytest.mark.parametrize("name", [ENV_PRODUCT, ENV_ENV, ENV_KEY])
    def test_one_warning_each(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(name, "whatever")

        with pytest.warns(UserWarning, match=f"{name} was removed in 1.0") as caught:
            warn_about_removed_env_vars()

        assert len([w for w in caught if name in str(w.message)]) == 1

    def test_the_warning_says_what_to_do(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PRODUCT, "compliance")

        with pytest.warns(UserWarning) as caught:
            warn_about_removed_env_vars()

        message = str(caught[0].message)
        assert "ignored" in message
        assert "the API key decides the product" in message
        assert "init_observability(api_key=" in message

    def test_a_user_warning_not_a_deprecation_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DeprecationWarning is hidden by default outside `__main__` — which is
        exactly where a server sets its environment. The operator has to find
        out their variable stopped doing anything."""
        monkeypatch.setenv(ENV_ENV, "prod")

        with pytest.warns(UserWarning) as caught:
            warn_about_removed_env_vars()

        assert not issubclass(caught[0].category, DeprecationWarning)

    def test_deprecated_key_env_var_no_longer_supplies_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`INDRATRACE_KEY` used to be honored with a DeprecationWarning."""
        monkeypatch.setenv(ENV_KEY, "old-alias-key")

        with pytest.raises(IndraTraceConfigError, match="No API key"):
            resolve_config()

    def test_nothing_set_warns_nothing(self, recwarn: pytest.WarningsRecorder) -> None:
        warn_about_removed_env_vars()

        assert not [w for w in recwarn if "was removed in 1.0" in str(w.message)]

    def test_an_empty_value_is_not_in_use(
        self, monkeypatch: pytest.MonkeyPatch, recwarn: pytest.WarningsRecorder
    ) -> None:
        """An empty INDRATRACE_PRODUCT never configured anything, so warning
        about it would be noise (same emptiness rule as the key)."""
        monkeypatch.setenv(ENV_PRODUCT, "")

        warn_about_removed_env_vars()

        assert not [w for w in recwarn if "was removed in 1.0" in str(w.message)]


class TestTransport:
    def test_traces_endpoint_appends_signal_path(self) -> None:
        cfg = ObsConfig(api_key="k", endpoint="http://host:8088")
        assert cfg.traces_endpoint == "http://host:8088/v1/traces"

    def test_traces_endpoint_tolerates_trailing_slash(self) -> None:
        cfg = ObsConfig(api_key="k", endpoint="http://host:8088/")
        assert cfg.traces_endpoint == "http://host:8088/v1/traces"

    def test_api_key_becomes_auth_header(self) -> None:
        cfg = resolve_config(api_key="secret")
        assert cfg.headers == {API_KEY_HEADER: "secret"}

    def test_the_auth_header_is_always_sent(self) -> None:
        """1.0 has no keyless mode, so there is no headerless export left —
        the silent-401 failure mode is gone by construction."""
        assert resolve_config(api_key=TEST_API_KEY).headers

    def test_endpoint_env_var_still_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`INDRATRACE_ENDPOINT` survives as an IndraTrace-developer override for
        running against a local stack (CONTRIBUTING.md). It is deliberately
        undocumented for customers — there is no `endpoint` parameter at all."""
        monkeypatch.setenv(ENV_ENDPOINT, "http://localhost:9999")

        assert resolve_config(api_key=TEST_API_KEY).endpoint == "http://localhost:9999"

    def test_export_timeout_is_shorter_than_otel_default(self) -> None:
        """OTel defaults to 10s, which stalls shutdown when the gateway is
        down. ADR 0003: drop, don't block."""
        assert resolve_config(api_key=TEST_API_KEY).export_timeout_seconds < 10.0


class TestResource:
    def test_carries_every_required_attribute(self) -> None:
        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes

        for attr in REQUIRED_RESOURCE_ATTRS:
            assert attr in attrs, f"conventions.md requires {attr!r}"

    def test_carries_none_of_the_gateway_stamped_attributes(self) -> None:
        """§4.2 — the SDK-side twin of the platform's grep that `stamp.py` is the
        only writer of these three (P80). The gateway drops the client's values
        and appends the key's, so sending them claims something we know is
        discarded."""
        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes

        for attr in GATEWAY_STAMPED_ATTRS:
            assert attr not in attrs, f"the SDK must not send {attr!r}"

    def test_gateway_stamped_attrs_are_the_three_from_the_platform_contract(
        self,
    ) -> None:
        """Pinned against `indratrace-app/ingest/stamp.py::STAMPED_ATTRS`. If the
        platform adds a fourth, this list — and the resource — must follow."""
        assert set(GATEWAY_STAMPED_ATTRS) == {
            "product",
            "deployment.environment",
            "tenant.id",
        }

    def test_attribute_values_come_from_config(self) -> None:
        cfg = ObsConfig(
            api_key="k",
            service_name="compliance-api",
            service_version="1.4.2",
        )

        attrs = build_resource(cfg).attributes

        assert attrs["service.name"] == "compliance-api"
        assert attrs["service.version"] == "1.4.2"

    def test_service_name_defaults_to_opentelemetrys_own(self) -> None:
        """Unset means "let OTel decide" rather than a value of ours: OTel's
        default honors OTEL_SERVICE_NAME, which stamping over would clobber."""
        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes

        assert attrs["service.name"] == "unknown_service"

    def test_service_name_respects_otel_service_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-otel-env")

        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes

        assert attrs["service.name"] == "from-otel-env"

    def test_explicit_service_name_beats_otel_service_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "from-otel-env")

        cfg = resolve_config(api_key=TEST_API_KEY, service_name="explicit")

        assert build_resource(cfg).attributes["service.name"] == "explicit"

    def test_wrapper_attribute_identifies_sdk_version(self) -> None:
        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes
        assert attrs["telemetry.sdk.wrapper"] == f"indratrace/{__version__}"

    def test_otel_sdk_defaults_survive_the_merge(self) -> None:
        """Our attrs must not clobber the standard telemetry.sdk.* set."""
        attrs = build_resource(resolve_config(api_key=TEST_API_KEY)).attributes
        assert attrs["telemetry.sdk.language"] == "python"
        assert "telemetry.sdk.version" in attrs
