"""Terraform Registry community-module discovery and input/output contracts.

The composer may implement a component by calling a published community module
(e.g. the `terraform-aws-modules` namespace) instead of composing raw provider
resources. Like provider-schema composition, the model only ever *selects* from
harvested contracts: candidates come from the public registry search API and
their input/output contracts from the registry module details API, so a
hallucinated module or input is rejected before anything is rendered.

Everything here is fail-soft: no network, a non-200, or an unparseable payload
yields no candidates, and composition proceeds with raw provider resources.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

from pydantic import BaseModel, Field

from iac_smith.models.intent import InfrastructureIntent

REGISTRY_BASE_URL = "https://registry.terraform.io/v1/modules"

_DEFAULT_NAMESPACES = "terraform-aws-modules"


def registry_modules_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("IAC_SMITH_REGISTRY_MODULES", "1") != "0"


def registry_namespaces(env: Mapping[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    raw = source.get("IAC_SMITH_REGISTRY_NAMESPACES", _DEFAULT_NAMESPACES)
    return [item.strip() for item in raw.split(",") if item.strip()]


class RegistryModuleInput(BaseModel):
    name: str
    type: str = ""
    description: str = ""
    required: bool = False


class RegistryModuleContract(BaseModel):
    source: str
    version: str
    description: str = ""
    inputs: dict[str, RegistryModuleInput] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)

    @property
    def required_inputs(self) -> list[str]:
        return sorted(name for name, spec in self.inputs.items() if spec.required)


def _get_json(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": "iac-smith"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def search_modules(
    query: str,
    *,
    namespace: str,
    provider: str = "aws",
    limit: int = 3,
    timeout: float = 10.0,
) -> list[str]:
    """Module sources (``namespace/name/provider``) matching the query."""
    params = urllib.parse.urlencode(
        {"q": query, "namespace": namespace, "provider": provider, "limit": limit}
    )
    payload = _get_json(f"{REGISTRY_BASE_URL}/search?{params}", timeout)
    if not isinstance(payload, dict):
        return []
    sources: list[str] = []
    for module in payload.get("modules", []):
        if not isinstance(module, dict):
            continue
        # The namespace filter is enforced client-side too: the model must only
        # ever see modules from the configured namespaces.
        if module.get("namespace") != namespace or module.get("provider") != provider:
            continue
        name = module.get("name")
        if isinstance(name, str) and name:
            sources.append(f"{namespace}/{name}/{provider}")
    return sources[:limit]


def fetch_module_contract(source: str, *, timeout: float = 10.0) -> RegistryModuleContract | None:
    """Latest-version input/output contract for one registry module."""
    payload = _get_json(f"{REGISTRY_BASE_URL}/{source}", timeout)
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    root = payload.get("root")
    if not isinstance(version, str) or not version or not isinstance(root, dict):
        return None
    inputs: dict[str, RegistryModuleInput] = {}
    for entry in root.get("inputs", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        inputs[name] = RegistryModuleInput(
            name=name,
            type=str(entry.get("type") or ""),
            description=str(entry.get("description") or ""),
            required=bool(entry.get("required", not entry.get("default"))),
        )
    outputs = [
        entry["name"]
        for entry in root.get("outputs", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    return RegistryModuleContract(
        source=source,
        version=version,
        description=str(payload.get("description") or ""),
        inputs=inputs,
        outputs=outputs,
    )


def discover_registry_candidates(
    intent: InfrastructureIntent,
    *,
    logger: Callable[[str], None] | None = None,
    limit: int = 3,
    timeout: float = 10.0,
) -> list[RegistryModuleContract]:
    """Community-module candidates for the parsed intent, with harvested contracts.

    The search query is derived from the parsed request (never a hardcoded
    service list), so discovery stays generic across whatever the registry
    exposes for the configured namespaces.
    """
    query = " ".join(
        part for part in (intent.resource_type or "").replace("_", " ").split() if part
    )
    if not query:
        return []
    candidates: list[RegistryModuleContract] = []
    seen: set[str] = set()
    for namespace in registry_namespaces():
        for source in search_modules(query, namespace=namespace, limit=limit, timeout=timeout):
            if source in seen or len(candidates) >= limit:
                continue
            seen.add(source)
            contract = fetch_module_contract(source, timeout=timeout)
            if contract is not None:
                candidates.append(contract)
    if logger:
        if candidates:
            logger(
                "IaC Smith: registry module candidates for "
                f"`{query}`: {', '.join(c.source for c in candidates)}"
            )
        else:
            logger(f"IaC Smith: no registry module candidates for `{query}`.")
    return candidates
