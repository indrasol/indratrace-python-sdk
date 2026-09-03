"""Integration-suite fixtures: point the SDK at the local dev harness.

1.0 removed the `endpoint` parameter — a customer never chooses a host, they
hold a key. `INDRATRACE_ENDPOINT` survives as the IndraTrace-developer override
for exactly this case (running against a local stack; see CONTRIBUTING.md), so
the integration suite sets it here, once, instead of at every call site.

The root `conftest.clean_env` deliberately leaves integration tests alone after
unsetting the ambient vars, which is what makes this the right place.
"""

from __future__ import annotations

import pytest

from indratrace.config import ENV_ENDPOINT

#: The dev harness's OTLP receiver (dev/docker-compose.yml). This is the plain
#: Collector, not the platform's ingest gateway: nothing here validates the key
#: or stamps identity, which is precisely why these tests can assert that the
#: SDK sends no product/env/tenant of its own.
OTLP_ENDPOINT = "http://localhost:4318"

#: Any non-empty string: the harness Collector accepts the header and ignores it
#: (the gateway is what would validate it), and the SDK never checks the format.
HARNESS_API_KEY = "it_test_harness"


@pytest.fixture(autouse=True)
def harness_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ENDPOINT, OTLP_ENDPOINT)
