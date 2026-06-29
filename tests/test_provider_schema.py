import json
import subprocess

from iac_smith import provider_schema
from iac_smith.provider_schema import (
    build_schema_resolver,
    extract_required_providers,
    harvest_provider_schema,
)

_VERSIONS_TF = (
    "terraform {\n"
    '  required_version = ">= 1.5"\n'
    "  required_providers {\n"
    "    aws = {\n"
    '      source  = "hashicorp/aws"\n'
    '      version = "~> 5.0"\n'
    "    }\n"
    "  }\n"
    "}\n"
)


def _schema() -> dict:
    return {
        "format_version": "1.0",
        "provider_schemas": {
            "registry.terraform.io/hashicorp/aws": {
                "resource_schemas": {
                    "aws_s3_bucket": {"block": {"attributes": {"bucket": {"type": "string"}}}}
                }
            }
        },
    }


def test_extract_required_providers_reads_source_and_version():
    assert extract_required_providers([_VERSIONS_TF]) == {"hashicorp/aws": "~> 5.0"}


def test_extract_required_providers_dedupes_and_handles_multiple():
    multi = (
        "terraform {\n"
        "  required_providers {\n"
        '    aws    = { source = "hashicorp/aws", version = ">= 5.0" }\n'
        '    random = { source = "hashicorp/random" }\n'
        "  }\n"
        "}\n"
    )
    result = extract_required_providers([_VERSIONS_TF, multi])

    # A concrete version constraint wins over a bare/empty one for the same source.
    assert result["hashicorp/aws"] in {"~> 5.0", ">= 5.0"}
    assert result["hashicorp/random"] == ""


def test_extract_required_providers_empty_when_no_block():
    assert extract_required_providers(['resource "aws_s3_bucket" "b" {}']) == {}


def test_harvest_returns_empty_for_no_providers():
    assert harvest_provider_schema({}) == {}


def test_harvest_disabled_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("IAC_SMITH_SCHEMA_HARVEST", "0")
    called = False

    def _fake_run(*args, **kwargs):  # pragma: no cover - must never run
        nonlocal called
        called = True

    monkeypatch.setattr(provider_schema.subprocess, "run", _fake_run)

    assert harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path) == {}
    assert called is False


def test_harvest_runs_clean_init_then_schema_and_caches(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["terraform", "init"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_schema()), stderr="")

    monkeypatch.setattr(provider_schema.subprocess, "run", _fake_run)

    schema = harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path)

    assert schema == _schema()
    assert commands[0][:2] == ["terraform", "init"]
    assert commands[1] == ["terraform", "providers", "schema", "-json"]
    # Result is cached to disk.
    assert list(tmp_path.glob("schema-*.json"))


def test_harvest_uses_cache_without_invoking_terraform(monkeypatch, tmp_path):
    def _ok(cmd, **kwargs):
        if cmd[:2] == ["terraform", "init"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_schema()), stderr="")

    monkeypatch.setattr(provider_schema.subprocess, "run", _ok)
    harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("terraform should not run on a cache hit")

    monkeypatch.setattr(provider_schema.subprocess, "run", _boom)
    cached = harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path)

    assert cached == _schema()


def test_harvest_returns_empty_when_init_fails(monkeypatch, tmp_path):
    def _fail_init(cmd, **kwargs):
        if cmd[:2] == ["terraform", "init"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        raise AssertionError("schema should not run when init fails")

    monkeypatch.setattr(provider_schema.subprocess, "run", _fail_init)

    assert harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path) == {}


def test_harvest_returns_empty_when_terraform_missing(monkeypatch, tmp_path):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("terraform")

    monkeypatch.setattr(provider_schema.subprocess, "run", _missing)

    assert harvest_provider_schema({"hashicorp/aws": "~> 5.0"}, cache_dir=tmp_path) == {}


def test_build_schema_resolver_populates_provider_contracts(monkeypatch):
    monkeypatch.setattr(provider_schema, "harvest_provider_schema", lambda *a, **k: _schema())

    resolver = build_schema_resolver({"modules/s3/versions.tf": _VERSIONS_TF})

    assert "aws_s3_bucket" in resolver.provider_contracts


def test_build_schema_resolver_empty_when_harvest_fails(monkeypatch):
    monkeypatch.setattr(provider_schema, "harvest_provider_schema", lambda *a, **k: {})

    resolver = build_schema_resolver({"modules/s3/versions.tf": _VERSIONS_TF})

    assert resolver.provider_contracts == {}


def test_build_schema_resolver_only_reads_versions_tf(monkeypatch):
    # A required_providers block in a generated README/example must NOT reach the
    # harvest — a placeholder provider there would fail `init` and silently disable
    # the gate. Only real versions.tf files are authoritative.
    readme = (
        "# Module\n\n```hcl\nterraform {\n  required_providers {\n"
        '    bad = { source = "example/bad", version = "1.0.0" }\n'
        "  }\n}\n```\n"
    )
    captured: dict[str, str] = {}

    def _capture(requirements, **kwargs):
        captured.update(requirements)
        return _schema()

    monkeypatch.setattr(provider_schema, "harvest_provider_schema", _capture)

    build_schema_resolver({"modules/s3/versions.tf": _VERSIONS_TF, "modules/s3/README.md": readme})

    assert captured == {"hashicorp/aws": "~> 5.0"}
