from iac_smith.models.intent import EnvironmentScope, InfrastructureIntent
from iac_smith.registry_modules import (
    discover_registry_candidates,
    fetch_module_contract,
    registry_modules_enabled,
    registry_namespaces,
    search_modules,
)

_SEARCH_PAYLOAD = {
    "modules": [
        {"namespace": "terraform-aws-modules", "name": "cloudfront", "provider": "aws"},
        {"namespace": "someone-else", "name": "cloudfront", "provider": "aws"},
        {"namespace": "terraform-aws-modules", "name": "cloudfront", "provider": "azurerm"},
        {"namespace": "terraform-aws-modules", "name": "s3-bucket", "provider": "aws"},
    ]
}

_DETAILS_PAYLOAD = {
    "version": "5.0.1",
    "description": "CloudFront distribution module",
    "root": {
        "inputs": [
            {"name": "comment", "type": "string", "default": "", "required": False},
            {"name": "origin", "type": "any", "required": True},
        ],
        "outputs": [
            {"name": "cloudfront_distribution_id"},
            {"name": "cloudfront_distribution_arn"},
        ],
    },
}


def _intent() -> InfrastructureIntent:
    return InfrastructureIntent(
        raw_request="Static website behind CloudFront",
        resource_type="cloudfront_distribution",
        environment_scope=EnvironmentScope.NON_PROD_ONLY,
        environments=["non-prod"],
        region="us-west-2",
    )


def test_enabled_by_default_and_disabled_with_zero():
    assert registry_modules_enabled(env={}) is True
    assert registry_modules_enabled(env={"IAC_SMITH_REGISTRY_MODULES": "0"}) is False


def test_namespaces_default_and_override():
    assert registry_namespaces(env={}) == ["terraform-aws-modules"]
    assert registry_namespaces(env={"IAC_SMITH_REGISTRY_NAMESPACES": "a, b,"}) == ["a", "b"]


def test_search_filters_namespace_and_provider(monkeypatch):
    urls = []

    def fake_get(url, timeout):
        urls.append(url)
        return _SEARCH_PAYLOAD

    monkeypatch.setattr("iac_smith.registry_modules._get_json", fake_get)
    sources = search_modules("cloudfront", namespace="terraform-aws-modules")

    assert sources == [
        "terraform-aws-modules/cloudfront/aws",
        "terraform-aws-modules/s3-bucket/aws",
    ]
    assert "q=cloudfront" in urls[0]


def test_fetch_contract_parses_inputs_and_outputs(monkeypatch):
    monkeypatch.setattr(
        "iac_smith.registry_modules._get_json", lambda url, timeout: _DETAILS_PAYLOAD
    )
    contract = fetch_module_contract("terraform-aws-modules/cloudfront/aws")

    assert contract is not None
    assert contract.version == "5.0.1"
    assert contract.required_inputs == ["origin"]
    assert contract.inputs["comment"].required is False
    assert "cloudfront_distribution_id" in contract.outputs


def test_fetch_contract_fail_soft(monkeypatch):
    monkeypatch.setattr("iac_smith.registry_modules._get_json", lambda url, timeout: None)
    assert fetch_module_contract("terraform-aws-modules/cloudfront/aws") is None


def test_discover_builds_query_from_intent_and_harvests_contracts(monkeypatch):
    queries = []

    def fake_search(query, *, namespace, provider="aws", limit=3, timeout=10.0):
        queries.append((query, namespace))
        return ["terraform-aws-modules/cloudfront/aws"]

    monkeypatch.setattr("iac_smith.registry_modules.search_modules", fake_search)
    monkeypatch.setattr(
        "iac_smith.registry_modules._get_json", lambda url, timeout: _DETAILS_PAYLOAD
    )
    candidates = discover_registry_candidates(_intent())

    assert queries == [("cloudfront distribution", "terraform-aws-modules")]
    assert [c.source for c in candidates] == ["terraform-aws-modules/cloudfront/aws"]
    assert candidates[0].version == "5.0.1"


def test_discover_returns_nothing_without_resource_type(monkeypatch):
    monkeypatch.setattr(
        "iac_smith.registry_modules.search_modules",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not search")),
    )
    intent = _intent().model_copy(update={"resource_type": ""})
    assert discover_registry_candidates(intent) == []
