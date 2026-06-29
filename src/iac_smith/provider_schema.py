"""Authoritative provider-schema harvesting for generation-time contract checks.

This is the real resolver behind the otherwise-inert ``ContractResolver`` hook in
:mod:`iac_smith.blackboard`. It exists so the deterministic contract gate
(``validate_generated_contracts``) has something to check against *before*
Terraform ever runs — catching hallucinated arguments and resource types at
generation time instead of waiting for a runtime ``terraform validate`` failure
and hoping the repair model applies the feedback.

Two design decisions matter:

1. **Schema is sourced from a clean provider install, not the module under
   generation.** The runtime harvest (``runtime_validation._harvest_module_contracts``)
   runs ``terraform providers schema -json`` inside the generated module — which
   fails to load exactly when the module is most broken (e.g. an invalid resource
   type), returning ``{}`` precisely when the backstop is needed. Here we declare
   only the *providers* the run requires in a throwaway directory, init that, and
   read the schema there. The schema is therefore always available regardless of
   how broken the generated module is.

2. **Generic across providers.** Nothing is keyed to AWS or any resource catalog.
   The providers are whatever the generated ``versions.tf`` files declare, and the
   schema is whatever those providers expose.

The harvest is best-effort: any failure (no terraform binary, no network, parse
error) degrades to an empty resolver, which makes the contract gate a no-op pass —
exactly the pre-existing behaviour. It never breaks a generation run.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from iac_smith.blackboard import ContractResolver, contracts_from_provider_schema

# `aws = { source = "hashicorp/aws", version = "~> 5.0" }` entries inside a
# `required_providers { }` block. version is optional; spec carries no nested
# braces so a non-brace match is sufficient once the block body is isolated.
_REQUIRED_PROVIDERS_RE = re.compile(r"required_providers\s*{")
_PROVIDER_ENTRY_RE = re.compile(
    r"(?P<local>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*{(?P<spec>[^}]*)}", re.DOTALL
)
_SOURCE_RE = re.compile(r'source\s*=\s*"(?P<source>[^"]+)"')
_VERSION_RE = re.compile(r'version\s*=\s*"(?P<version>[^"]+)"')

_DISABLE_ENV = "IAC_SMITH_SCHEMA_HARVEST"
_CACHE_DIR_ENV = "IAC_SMITH_SCHEMA_CACHE_DIR"


def _required_providers_bodies(content: str) -> list[str]:
    """Return the brace-balanced body of each ``required_providers { }`` block.

    A regex can't isolate the block because it nests one ``{ }`` per provider, so
    the closing brace is found by counting depth from the opening one.
    """
    bodies: list[str] = []
    for match in _REQUIRED_PROVIDERS_RE.finditer(content):
        depth = 1
        i = match.end()
        while i < len(content) and depth > 0:
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            bodies.append(content[match.end() : i - 1])
    return bodies


def extract_required_providers(contents: Iterable[str]) -> dict[str, str]:
    """Return ``{source: version_constraint}`` declared across the given files.

    Deduplicated by provider source (``hashicorp/aws``); when the same source is
    declared with and without a version constraint, a concrete constraint wins.
    A source with no version constraint maps to an empty string. Generic: only the
    ``required_providers`` blocks the generated files actually contain are read.
    """
    providers: dict[str, str] = {}
    for content in contents:
        for body in _required_providers_bodies(content):
            for entry in _PROVIDER_ENTRY_RE.finditer(body):
                spec = entry.group("spec")
                source_match = _SOURCE_RE.search(spec)
                if not source_match:
                    continue
                source = source_match.group("source")
                version_match = _VERSION_RE.search(spec)
                version = version_match.group("version") if version_match else ""
                if version or source not in providers:
                    providers[source] = version
    return providers


def _local_name(source: str) -> str:
    """Local provider name for a source address (``hashicorp/aws`` -> ``aws``)."""
    return source.rsplit("/", 1)[-1]


def _versions_tf(requirements: dict[str, str]) -> str:
    lines = ["terraform {", "  required_providers {"]
    for source, version in sorted(requirements.items()):
        local = _local_name(source)
        if version:
            lines.append(f'    {local} = {{ source = "{source}", version = "{version}" }}')
        else:
            lines.append(f'    {local} = {{ source = "{source}" }}')
    lines.extend(["  }", "}", ""])
    return "\n".join(lines)


def _cache_dir() -> Path:
    override = os.environ.get(_CACHE_DIR_ENV)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "iac-smith-provider-schema"


def _cache_key(requirements: dict[str, str]) -> str:
    payload = ";".join(f"{src}@{ver}" for src, ver in sorted(requirements.items()))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def harvest_provider_schema(
    requirements: dict[str, str],
    *,
    cache_dir: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> dict:
    """Return ``terraform providers schema -json`` output for the given providers.

    The providers are installed in a throwaway directory (so the result is
    independent of any module under generation) and the parsed schema is cached on
    disk keyed by provider source+version, so the ``terraform init`` cost is paid
    at most once per provider set across runs. The Terraform plugin cache is shared
    via ``TF_PLUGIN_CACHE_DIR`` so provider downloads are not duplicated.

    Best-effort: returns ``{}`` if harvesting is disabled, there are no providers,
    or any step fails.
    """
    if os.environ.get(_DISABLE_ENV) == "0" or not requirements:
        return {}

    base = cache_dir or _cache_dir()
    cache_file = base / f"schema-{_cache_key(requirements)}.json"
    try:
        if cache_file.is_file():
            return json.loads(cache_file.read_text())
    except (OSError, json.JSONDecodeError):
        pass  # Corrupt/unreadable cache — fall through and re-harvest.

    run_env = dict(env or os.environ)
    plugin_cache = base / "plugin-cache"
    try:
        plugin_cache.mkdir(parents=True, exist_ok=True)
        run_env.setdefault("TF_PLUGIN_CACHE_DIR", str(plugin_cache))
    except OSError:
        return {}

    try:
        with tempfile.TemporaryDirectory(prefix="iac-smith-schema-") as work:
            work_dir = Path(work)
            (work_dir / "versions.tf").write_text(_versions_tf(requirements))
            init = subprocess.run(
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                cwd=work_dir,
                env=run_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
            if init.returncode != 0:
                return {}
            completed = subprocess.run(
                ["terraform", "providers", "schema", "-json"],
                cwd=work_dir,
                env=run_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return {}
            schema = json.loads(completed.stdout)
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        return {}

    try:
        base.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(schema))
    except OSError:
        pass  # Caching is an optimization; a write failure must not fail the harvest.
    return schema


def build_schema_resolver(
    generated_files: dict[str, str],
    *,
    cache_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> ContractResolver:
    """Build a ``ContractResolver`` backed by the real provider schema.

    The resolver carries a contract for *every* resource type the declared
    providers expose — the full set is needed so the contract gate can flag a
    hallucinated resource type (a type whose provider is known but which the
    provider does not define), not only unsupported arguments. Returns an empty
    resolver (a no-op gate) when no providers are declared or harvesting fails.
    """
    requirements = extract_required_providers(generated_files.values())
    schema = harvest_provider_schema(requirements, cache_dir=cache_dir, env=env)
    if not schema:
        return ContractResolver()
    provider_contracts = contracts_from_provider_schema(schema)
    return ContractResolver(provider_contracts=provider_contracts)
