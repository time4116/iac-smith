import pytest


@pytest.fixture(autouse=True)
def _no_registry_network(monkeypatch):
    """Unit tests must never reach the Terraform Registry; candidate discovery
    is exercised with injected contracts instead."""
    monkeypatch.setenv("IAC_SMITH_REGISTRY_MODULES", "0")
