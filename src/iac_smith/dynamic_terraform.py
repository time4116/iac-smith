import contextlib
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from iac_smith.blackboard import RunBlackboard, build_blackboard_prompt_section
from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.nodes.static_review import (
    _extract_hcl_block_body,
    _extract_hcl_block_keys,
    _strip_hcl_comments,
    static_review_generated_files,
)

_BEDROCK_THROTTLE_CODES = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"}
)


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if raw.lstrip("-").isdigit():
        value = int(raw)
        if value > 0:
            return value
    return default


_ADD_VARIABLE_TO_RE = re.compile(r'Add variable "[^"]+" to (\S+)\.')

_TG_LOCALS_HEADER_RE = re.compile(r"\blocals\s*\{")
_TG_INCLUDE_RE = re.compile(r'^\s*include\s*(?:"[^"]+"\s*)?\{', re.MULTILINE)
_TG_LOCAL_REF_RE = re.compile(r"\blocal\.([A-Za-z0-9_]+)")
_SIMPLE_LOCAL_ASSIGN_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$")


def _is_root_terragrunt(path: str) -> bool:
    """True for an environment root config (environments[/<env>]/terragrunt.hcl), not a stack."""
    parts = path.split("/")
    return parts[0] == "environments" and parts[-1] == "terragrunt.hcl" and len(parts) in (2, 3)


def _parse_simple_locals(content: str) -> dict[str, str]:
    """Return single-line `name = value` assignments from the first `locals {}` block.

    Nested/object values are skipped — only the flat scalars (environment,
    aws_region, account_id, ...) that child stacks redeclare are needed.
    """
    m = _TG_LOCALS_HEADER_RE.search(content)
    if not m:
        return {}
    body = _extract_hcl_block_body(content, m.start())
    if body is None:
        return {}
    result: dict[str, str] = {}
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            am = _SIMPLE_LOCAL_ASSIGN_RE.match(line.strip())
            if am and "{" not in am.group(2):
                result[am.group(1)] = am.group(2)
        depth += line.count("{") - line.count("}")
        depth = max(depth, 0)
    return result


def _inject_missing_child_locals(generated_files: dict[str, str]) -> None:
    """Declare root-derived locals that child Terragrunt stacks reference but drop.

    Terragrunt does not expose a parent config's locals as `local.*` in a child,
    so a stack that references `local.environment`/`local.aws_region` without its
    own `locals {}` declaration fails at init with "Unsupported attribute". The
    model drops this block unreliably and the repair loop oscillates on it, so fix
    it deterministically: copy each referenced-but-undeclared local from the
    environment root config into the child's `locals {}` block.
    """
    root_locals: dict[str, str] = {}
    for path in sorted(generated_files, key=lambda p: p.count("/")):
        if _is_root_terragrunt(path):
            root_locals.update(_parse_simple_locals(generated_files[path]))
    if not root_locals:
        return

    for path, content in list(generated_files.items()):
        if not path.endswith("terragrunt.hcl") or _is_root_terragrunt(path):
            continue
        if not _TG_INCLUDE_RE.search(content):
            continue
        declared = _extract_hcl_block_keys(content, _TG_LOCALS_HEADER_RE)
        referenced = set(_TG_LOCAL_REF_RE.findall(_strip_hcl_comments(content)))
        missing = [n for n in sorted(referenced - declared) if n in root_locals]
        if not missing:
            continue
        inject = "".join(f"  {n} = {root_locals[n]}\n" for n in missing)
        header = _TG_LOCALS_HEADER_RE.search(content)
        if header:
            brace = content.index("{", header.start())
            generated_files[path] = (
                content[: brace + 1] + "\n" + inject.rstrip("\n") + content[brace + 1 :]
            )
        else:
            generated_files[path] = f"locals {{\n{inject}}}\n\n" + content


_SOURCE_PINPOINT_RE = re.compile(r"\bon\s+(?P<file>[^\s,]+)\s+line\s+\d+")


def _error_pinpointed_basenames(error: str) -> set[str]:
    """Basenames Terraform/Terragrunt explicitly blames via ``on <file> line N``.

    Returns an empty set for directory-level or pathless errors (terragrunt
    include cycles, missing-provider init failures), which then fall back to
    whole-unit repair. The basename is taken so a pinpoint given as a relative
    path (``on modules/x/main.tf line 5``) still matches a generated file path.
    """
    return {match.group("file").rpartition("/")[2] for match in _SOURCE_PINPOINT_RE.finditer(error)}


_ENV_STACK_PATH_RE = re.compile(r"environments/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def _error_stack_names(errors: list[str]) -> set[str]:
    """Stack names blamed by an error via an ``environments/<env>/<stack>`` path.

    The basename of each environments path token is the stack name, which by
    convention matches the ``modules/<stack>`` directory that the stack sources.
    """
    names: set[str] = set()
    for error in errors:
        for token in _ENV_STACK_PATH_RE.findall(error):
            parts = token.rstrip("/").split("/")
            if len(parts) >= 3:
                names.add(parts[-1])
    return names


def _path_needs_repair(path: str, errors: list[str]) -> bool:
    """Return True if `path` appears in any error as a file that needs to be changed.

    For duplicate-declaration errors the hint reads "Remove from X, keep in Y."
    The file at Y is the canonical one — it does not need repair.  Only X does.
    This function returns False when the path appears exclusively as a "keep in"
    target so that variables.tf / outputs.tf / versions.tf are not unnecessarily
    regenerated (which can drop declarations that main.tf still references).

    For undeclared-variable errors ("var.x is referenced in main.tf but ... not
    declared in variables.tf. Add variable "x" to variables.tf.") the fix is to
    add the declaration to variables.tf, never to rewrite the main.tf that merely
    references it.  Regenerating main.tf here both drops its var references and
    confuses the model into returning variables.tf for a main.tf request — only
    the named variables.tf is repaired.

    Also matches via the parent directory of `path`: runtime validation errors
    label failures with the module or stack directory (e.g. "terraform validate
    modules/ecs-fargate failed"), not with individual file paths, so file-level
    matching alone would miss them and trigger the expensive all-files fallback.
    """
    explicitly_excluded = False
    for error in errors:
        if path not in error:
            continue
        add_target = _ADD_VARIABLE_TO_RE.search(error)
        if add_target is not None:
            if path == add_target.group(1):
                return True
            # path is the referencing file (main.tf) — leave it untouched.
            explicitly_excluded = True
            continue
        if f"keep in {path}." in error and f"Remove from {path}," not in error:
            explicitly_excluded = True
            continue
        return True

    # Honour the "keep in" hint: don't repair the canonical file.
    if explicitly_excluded:
        return False

    # Directory-based fallback: match when the error names the parent directory.
    # Use a negative lookahead for "/" to avoid matching a shorter directory
    # name that is a prefix of a longer path (e.g. `environments` must not
    # match an error about `environments/non-prod/foundation`).
    #
    # Terraform/Terragrunt errors pinpoint the exact file at fault ("on main.tf
    # line 5"). When an error carries such pinpoints, only those files are
    # repaired — regenerating the rest of the directory just hands a weak model
    # files that already validated and lets it regress them (e.g. rewriting a
    # valid `variables.tf` with `var "x" {` instead of `variable "x" {`). Only a
    # directory-level error with no file pinpoint repairs the whole unit.
    path_dir = path.rpartition("/")[0]
    basename = path.rpartition("/")[2]
    if path_dir:
        dir_pattern = re.escape(path_dir) + r"(?!/)"
        for error in errors:
            if not re.search(dir_pattern, error):
                continue
            pinpointed = _error_pinpointed_basenames(error)
            if not pinpointed or basename in pinpointed:
                return True

    # Stack-to-module bridge: a `terragrunt plan`/`validate` failure names the
    # stack dir (`environments/<env>/<stack>`), but the offending value (an image,
    # a variable default) usually lives in `modules/<stack>`. They share the stack
    # name by convention (see `_repair_unit_key`), so a stack-level failure must
    # also reach the module's `.tf` files — otherwise the repair only touches the
    # stack's terragrunt.hcl and can never fix a module-resident value.
    if path.startswith("modules/") and path.endswith(".tf"):
        module_stack = path.split("/")[1]
        if module_stack in _error_stack_names(errors):
            return True

    return False


_GEN_ORDER = {"main.tf": 0, "variables.tf": 1, "outputs.tf": 2, "versions.tf": 3}


def _repair_unit_key(path: str) -> str:
    """Group a Terraform module and its Terragrunt stack into one repair unit.

    A module's ``variables.tf`` and its stack's ``terragrunt.hcl`` must agree on
    the variable contract, but they live in different directories
    (``modules/<stack>/`` vs ``environments/<env>/<stack>/``).  Repairing them in
    separate parallel groups means each regenerates from a stale snapshot of the
    other, so the input/variable sets oscillate instead of converging.  Mapping
    both to the same unit key — keyed on the shared stack name, which
    ``modules/<stack>/`` and ``environments/<env>/<stack>/`` share by convention
    (see docs/LAYOUT.md) — lets them be repaired sequentially with shared context.
    """
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "modules":
        return f"stack:{parts[1]}"
    if len(parts) >= 4 and parts[0] == "environments" and parts[-1] == "terragrunt.hcl":
        return f"stack:{parts[-2]}"
    return f"dir:{path.rpartition('/')[0]}"


def _unit_sibling_content(path: str, all_files: dict[str, str]) -> dict[str, str]:
    """Return current content of other non-Markdown files in the same repair unit.

    Unlike :func:`_sibling_content` (same directory only), this surfaces files
    across the module/stack boundary so a stack ``terragrunt.hcl`` repair can see
    the module's freshly repaired ``variables.tf`` and vice versa.
    """
    unit = _repair_unit_key(path)
    return {
        p: c
        for p, c in all_files.items()
        if p != path and not p.endswith(".md") and _repair_unit_key(p) == unit
    }


class BedrockRuntime(Protocol):
    def invoke_model(self, **kwargs: Any) -> dict[str, Any]: ...


class GeneratedTerraform(BaseModel):
    files: dict[str, str]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GeneratedTerraformFile(BaseModel):
    path: str
    content: str
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _extract_text_from_bedrock_payload(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("content"), list):
        parts = []
        for block in payload["content"]:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        if parts:
            return "\n".join(parts)
    if isinstance(payload.get("outputText"), str):
        return payload["outputText"]
    if isinstance(payload.get("completion"), str):
        return payload["completion"]
    return json.dumps(payload)


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                "Terraform generation response must contain a valid JSON object."
            ) from None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Terraform generation response must contain a valid JSON object."
            ) from exc
    if not isinstance(value, dict):
        raise ValueError("Terraform generation response must be a JSON object.")
    return value


def _rules_payload(ruleset: Ruleset | None) -> list[dict[str, str]]:
    if not ruleset:
        return []
    return [
        {
            "id": rule.id,
            "category": rule.category,
            "severity": rule.severity.value,
            "description": rule.description,
        }
        for rule in ruleset.rules
    ]


_CANONICAL_FILE_SHAPES = r"""
Canonical file shapes — treat these as structural templates:

--- versions.tf (SOLE location for required_providers; this block must NEVER appear in main.tf) ---
```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

--- main.tf (resources and data sources ONLY — NO terraform{} block, NO variable declarations, NO output declarations) ---
```hcl
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr   # var.xxx comes from variables.tf, never declared here
  enable_dns_hostnames = true
  tags = { Name = var.environment }
}

data "aws_availability_zones" "available" {}
```

--- variables.tf (ALL variable declarations — every var.xxx used in main.tf AND every key from the Terragrunt stack's inputs = {}) ---
```hcl
variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}
```

--- outputs.tf (ALL output declarations — no resources, no variable blocks here) ---
```hcl
output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "IDs of private subnets"
  value       = aws_subnet.private[*].id
}
```

--- environments/non-prod/root.hcl (environment root config — remote_state, shared locals) ---
```hcl
locals {
  environment = "non-prod"
  aws_region  = "us-east-1"
}

remote_state {
  backend = "s3"
  config = {
    bucket         = "my-terraform-state"
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = local.aws_region
    encrypt        = true
    dynamodb_table = "terraform-locks"
  }
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
}

# Generate the AWS provider config so every stack inherits the region.
# CRITICAL: this block sets ONLY the provider. It must NEVER contain a
# `terraform { required_providers { } }` block — required_providers lives solely
# in each module's versions.tf. Declaring it here too makes `terraform init`
# fail with "Duplicate required providers configuration".
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "${local.aws_region}"
}
EOF
}
```

--- environments/non-prod/<stack>/terragrunt.hcl (stack config — source, dependency blocks, inputs) ---
```hcl
# The environment root config is named root.hcl (Terragrunt deprecated using
# terragrunt.hcl as an include root), so name it explicitly here.
include "root" {
  path = find_in_parent_folders("root.hcl")
}

# locals from the included parent config are NOT available as local.xxx here.
# Redeclare any values you need from the parent in this locals {} block.
locals {
  environment = "non-prod"
  aws_region  = "us-east-1"
}

terraform {
  source = "../../../modules/ecs-fargate"
}

# ALWAYS use dependency blocks to consume outputs from another stack.
# NEVER write module.<name>.output_name — that syntax only works inside a Terraform module, not in terragrunt.
# ALWAYS include mock_outputs so that `terragrunt plan` works in CI before the dependency is deployed.
dependency "foundation" {
  config_path = "../foundation"

  mock_outputs = {
    vpc_id             = "vpc-00000000000000000"
    private_subnet_ids = ["subnet-00000000000000000"]
    public_subnet_ids  = ["subnet-11111111111111111"]
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  environment        = local.environment
  aws_region         = local.aws_region   # pass EVERY required (no-default) module variable
  vpc_id             = dependency.foundation.outputs.vpc_id
  private_subnet_ids = dependency.foundation.outputs.private_subnet_ids
}
```

--- modules/<stack>/README.md (every module README MUST include the terraform-docs marker pair) ---
```markdown
# <stack>

One- or two-sentence description of what this module provisions.

## Usage

This module is consumed through its Terragrunt stack under `environments/<env>/<stack>/`.

<!-- BEGIN_TF_DOCS -->
<!-- terraform-docs fills in inputs/outputs/providers here; leave this block empty -->
<!-- END_TF_DOCS -->
```

--- .github/workflows/terraform-pr-check.yml (trigger paths and working-dirs must match files_to_generate exactly) ---
```yaml
on:
  pull_request:
    paths:
      - "environments/**"
      - "modules/**"

# No id-token permission needed — init uses -backend=false, validate is schema-only.
permissions:
  contents: read
  pull-requests: write

jobs:
  validate:
    name: Validate Terraform modules and HCL
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - name: Install terragrunt
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          TG_VERSION=$(curl -sL -H "Authorization: Bearer ${GH_TOKEN}" "https://api.github.com/repos/gruntwork-io/terragrunt/releases/latest" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
          curl -sL "https://github.com/gruntwork-io/terragrunt/releases/download/${TG_VERSION}/terragrunt_linux_amd64" -o /usr/local/bin/terragrunt
          chmod +x /usr/local/bin/terragrunt
      - name: HCL format check
        working-directory: environments
        run: terragrunt hcl format --check
      - name: Terraform format check
        run: terraform fmt -check -recursive -diff modules
      - name: Terraform init and validate — foundation
        working-directory: modules/foundation
        run: |
          terraform init -backend=false -input=false
          terraform validate
      - name: Terraform init and validate — <stack-name>
        working-directory: modules/<stack-name>
        run: |
          terraform init -backend=false -input=false
          terraform validate
```

IMPORTANT:
- The `<stack-name>` placeholder above is a template — replace it with the actual stack module
  name(s) from files_to_generate. Add one step per module under `modules/`. Never leave
  `<stack-name>` literally in the output.
- This MUST be a single job. Do NOT split into multiple jobs per stack — that installs
  terragrunt multiple times and only uses it once.
- Do NOT add `Configure AWS credentials` — `terraform init -backend=false` and
  `terraform validate` are schema-only and do not contact AWS. No OIDC or AWS secrets needed.
- Do NOT add `terragrunt validate` or `terragrunt plan` steps — those require a deployed
  S3 backend which does not exist for brand-new infrastructure. `terraform validate` is
  the correct check: it validates provider schema and attribute types without a backend.

--- .github/workflows/terraform-apply.yml ---
```yaml
on:
  push:
    branches:
      - main
    paths:
      - "environments/**"
      - "modules/**"
      - "bootstrap/**"

permissions:
  contents: read
  id-token: write

jobs:
  bootstrap:
    name: Bootstrap state backend
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_wrapper: false
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}
          aws-region: us-west-2
          mask-aws-account-id: true
      - name: Bootstrap state backend (idempotent)
        working-directory: bootstrap/backend/non-prod
        run: |
          terraform init
          BUCKET=$(python3 -c "import re; txt=open('variables.tf').read(); m=re.search(r'variable\s+\"state_bucket_name\".*?default\s*=\s*\"([^\"]+)\"', txt, re.DOTALL); print(m.group(1) if m else '')")
          TABLE=$(python3 -c "import re; txt=open('variables.tf').read(); m=re.search(r'variable\s+\"state_lock_table_name\".*?default\s*=\s*\"([^\"]+)\"', txt, re.DOTALL); print(m.group(1) if m else '')")
          terraform import aws_s3_bucket.terraform_state "$BUCKET" 2>/dev/null || true
          terraform import aws_dynamodb_table.terraform_locks "$TABLE" 2>/dev/null || true
          terraform apply -auto-approve

  apply-foundation:
    name: Apply — non-prod/foundation
    needs: bootstrap
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - name: Install terragrunt
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          TG_VERSION=$(curl -sL -H "Authorization: Bearer ${GH_TOKEN}" "https://api.github.com/repos/gruntwork-io/terragrunt/releases/latest" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
          curl -sL "https://github.com/gruntwork-io/terragrunt/releases/download/${TG_VERSION}/terragrunt_linux_amd64" -o /usr/local/bin/terragrunt
          chmod +x /usr/local/bin/terragrunt
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}
          aws-region: us-west-2
          mask-aws-account-id: true
      - name: Apply foundation
        working-directory: environments/non-prod/foundation
        run: terragrunt apply --non-interactive --auto-approve

  apply-<stack-name>:
    name: Apply — non-prod/<stack-name>
    needs: apply-foundation
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
      - name: Install terragrunt
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          TG_VERSION=$(curl -sL -H "Authorization: Bearer ${GH_TOKEN}" "https://api.github.com/repos/gruntwork-io/terragrunt/releases/latest" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
          curl -sL "https://github.com/gruntwork-io/terragrunt/releases/download/${TG_VERSION}/terragrunt_linux_amd64" -o /usr/local/bin/terragrunt
          chmod +x /usr/local/bin/terragrunt
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}
          aws-region: us-west-2
          mask-aws-account-id: true
      - name: Apply <stack-name>
        working-directory: environments/non-prod/<stack-name>
        run: terragrunt apply --non-interactive --auto-approve
```

IMPORTANT for terraform-apply.yml:
- Replace `<stack-name>` with actual stack names from files_to_generate. Use a matrix job
  for workload stacks instead of copy/pasting one nearly identical job per stack.
  Each non-foundation workload apply must declare `needs: apply-foundation` when
  foundation exists, otherwise `needs: bootstrap`.
- Every apply path must run an explicit saved plan first (`terraform plan -out=tfplan`
  or `terragrunt plan --non-interactive -lock-timeout=20m -out=tfplan`) and apply
  that exact plan file. Do not run blind `terragrunt apply --auto-approve` without
  a preceding plan step.
- The `bootstrap` job is idempotent — it imports existing S3/DynamoDB resources
  before planning/applying so the workflow is safe to run on ephemeral CI runners without persisted state.
- A `detect` job runs first and diffs the push range to scope the run: `bootstrap`,
  `apply-foundation`, and the workload matrix each run only when their files changed
  (greenfield pushes apply everything). Downstream applies guard with
  `if: always() && ...` so a skipped upstream job does not cancel them.
- A single `gate` job (`environment:`) sits between `detect` and the apply jobs to
  require manual approval before any AWS mutation. Do not add `environment:` to the
  individual apply jobs — one gate covers the run.
- Always use `secrets.AWS_ROLE_ARN_NON_PROD` — never `AWS_ROLE_TO_ASSUME` or `AWS_ROLE_ARN`.
- Only trigger on branch `main`, not `master`.

This workflow file is normalized deterministically after generation, so match this
structure but small deviations are corrected automatically.
"""


def _sibling_content(path: str, all_files: dict[str, str]) -> dict[str, str]:
    """Return current content of other non-Markdown files in the same directory as path."""
    path_dir = path.rpartition("/")[0]
    if not path_dir:
        return {}
    return {
        p: c
        for p, c in all_files.items()
        if p != path and p.rpartition("/")[0] == path_dir and not p.endswith(".md")
    }


def build_generation_prompt(
    *,
    intent: InfrastructureIntent,
    change_plan: ChangePlan,
    repo_patterns: RepoPatterns,
    ruleset: Ruleset | None,
    target_repo: str,
    blackboard: RunBlackboard | None = None,
    repair_errors: list[str] | None = None,
    previous_content: str | None = None,
    sibling_content: dict[str, str] | None = None,
    existing_content: str | None = None,
) -> str:
    context = {
        "target_repo": target_repo,
        "intent": intent.model_dump(mode="json"),
        "change_plan": change_plan.model_dump(mode="json"),
        "repo_patterns": repo_patterns.model_dump(mode="json"),
        "rules": _rules_payload(ruleset),
        "files_to_generate": change_plan.files_to_generate,
    }
    shape = '{"path": "path/to/file.tf", "content": "file body", "assumptions": [], "warnings": []}'
    sibling_section = ""
    if sibling_content:
        parts = [f"--- {p} ---\n```\n{c}\n```" for p, c in sorted(sibling_content.items())]
        sibling_section = (
            "\n\nCurrent content of sibling files in the same module"
            " (READ-ONLY — do not regenerate these; use them to ensure resource"
            " names, variable names, and output references are consistent with"
            " what is actually declared in the module):\n\n" + "\n\n".join(parts)
        )

    existing_section = ""
    if existing_content:
        existing_section = (
            "\n\nThis file already exists in the target repository. Current content:\n"
            f"```\n{existing_content}\n```\n"
            "Update this file to incorporate the new infrastructure. "
            "Preserve all existing content that remains valid. "
            "For README.md, add new sections rather than replacing existing ones. "
            "For workflow YAML, add or update relevant jobs while keeping the canonical structure. "
            "Do not start from scratch when the existing content is substantially correct."
        )

    repair_section = ""
    if repair_errors:
        repair_section = f"""

Static review failures:
{json.dumps(repair_errors, indent=2)}

Runtime validation failures use this same repair path when errors came from
terraform fmt/init/validate, terragrunt hclfmt/init/validate, or terragrunt plan.

Previous generated content that failed review:
```text
{previous_content or ""}
```
Regenerate the same file path only. Fix every validation failure. Do not
repeat the failing pattern. Validation failures may come from static review,
terraform fmt/init/validate, terragrunt hclfmt/init/validate, or terragrunt plan.
"""
    return f"""You are IaC Smith's Terraform/Terragrunt generator.

Generate reviewable Terraform and Terragrunt file contents from structured issue
intent, repository patterns, and the active ruleset.

Non-negotiable rules:
* Return only JSON. Do not include markdown.
* Use this exact top-level shape: {shape}.
* Do not generate files outside files_to_generate.
* Existing repository conventions win over IaC Smith defaults unless the issue
  explicitly says not to follow them.
* Follow every active rule. Error-severity rules are hard requirements. Warning
  and preference rules must be followed unless they conflict with the explicit
  issue request or existing repo convention; explain conflicts in warnings.
* Never apply infrastructure, destroy resources, or include plaintext credentials.
* **Never hardcode a secret value — not even a placeholder like "change-me".**
  For any application secret (signing keys such as `WEBUI_SECRET_KEY`, passwords,
  API tokens, DB credentials), do NOT assign a literal string. Instead generate it
  with `random_password`/`random_id`, source it from AWS Secrets Manager or SSM
  Parameter Store (via a data source), or declare a `sensitive` required variable
  with no default so the operator must supply it. A predictable hardcoded secret
  is a real vulnerability even when the value looks like a placeholder.
* **When a customer-managed KMS key encrypts an AWS service resource, the key's
  policy MUST grant that service's principal access — or apply fails.** This is an
  apply-time failure that `terraform validate` and `plan` do NOT catch. The most
  common case: a `aws_cloudwatch_log_group` with `kms_key_id` pointing at your own
  `aws_kms_key` needs a key policy statement allowing `logs.<region>.amazonaws.com`
  to `kms:Encrypt*`/`Decrypt*`/`ReEncrypt*`/`GenerateDataKey*`/`Describe*` (scoped
  with `kms:EncryptionContext:aws:logs:arn`). A KMS key with no `policy` uses the
  default policy, which grants no service — so encrypting a log group with it fails
  `CreateLogGroup` with AccessDenied. The same principle applies to SNS, SQS, S3,
  Firehose, etc. encrypted with a CMK: grant the using service in the key policy.
* Terraform apply workflows must never run on pull_request events or feature
  branches. `.github/workflows/terraform-apply.yml` must trigger only on push
  to `main` — never `master`, never both.
* `terraform-apply.yml` must have a `bootstrap` job that runs first (before any
  stack apply job). The bootstrap job runs `terraform apply -auto-approve` in
  `bootstrap/backend/non-prod` and imports existing resources first to be
  idempotent on ephemeral CI runners. All stack apply jobs must declare
  `needs: bootstrap` (or `needs: apply-foundation` for dependent stacks).
* `terraform-apply.yml` must use `secrets.AWS_ROLE_ARN_NON_PROD` for the
  `role-to-assume` input — never `AWS_ROLE_TO_ASSUME`, `AWS_ROLE_ARN`, or any
  other name.
* The bootstrap module's S3 bucket resource MUST be named
  `aws_s3_bucket.terraform_state` and the DynamoDB table MUST be named
  `aws_dynamodb_table.terraform_locks`. The variables for their names MUST be
  `state_bucket_name` and `state_lock_table_name`. These names are hardcoded in
  the apply workflow's idempotent import step and must match exactly.
* Prefer secure AWS defaults: private networking, encryption, least privilege,
  no dangerous public ingress.
* If a request needs both networking/foundation and a workload, split ownership
  cleanly. `modules/foundation` owns ONLY shared network primitives: VPC,
  subnets, route tables, NAT/IGW. Every security group is a WORKLOAD resource —
  ALB security groups, ECS task/service security groups, and database security
  groups all belong in the workload module, declared exactly ONCE there,
  referencing `dependency.foundation.outputs.vpc_id` for their `vpc_id`. So an
  `aws_security_group` named `${{var.environment}}-alb-sg` or
  `${{var.environment}}-ecs-tasks-sg` MUST appear in `modules/ecs-fargate` only,
  never also in `modules/foundation`. Foundation must likewise NOT create ALBs,
  target groups/listeners, CloudWatch log groups, ECS clusters, task
  definitions, ECS services, or workload IAM. Never declare the same AWS
  provider-level resource `name` for the same resource type in two modules; that
  is an apply-time name collision Terraform's per-module validation cannot catch.
  If foundation genuinely must own a security group that several workloads
  share, declare it once in foundation, expose its id via an `output`, and have
  each workload consume that id — do not re-declare the group in any workload.
* If a workload stack depends on foundation outputs for VPC/subnets/security
  groups, do not reference module.vpc unless that same module declares
  module "vpc". In Terragrunt, every `dependency.foundation.outputs.<name>`
  reference must exactly match an `output "<name>"` in
  `modules/foundation/outputs.tf`; do not invent aliases such as `alb_sg_id`
  unless that exact output is declared.
* **Never depend on a stack that is not part of this change.** Only declare a
  Terragrunt `dependency` on a stack whose `terragrunt.hcl` is in the
  files_to_generate list (or that already exists in the target repo). Every
  `dependency.<name>.outputs.*` you reference MUST have a matching
  `dependency "<name>"` block, and that block's `config_path` MUST resolve to a
  stack that exists. If the issue needs shared infrastructure (a VPC, subnets,
  etc.) that is NOT among the planned files, do not invent a `dependency` on a
  foundation that isn't there — instead provision those resources inside this
  module, or read existing ones with Terraform data sources (e.g.
  `data "aws_vpc"`, `data "aws_subnets"`, or the account's default VPC). A
  `dependency` pointing at a directory that does not exist fails `terragrunt
  plan` with "There is no variable named dependency".
* Generate complete, syntactically valid file bodies for each requested path.
  Do not use placeholder comments instead of Terraform resources when the issue
  asks for concrete infrastructure.
* **Terraform module file organization — CRITICAL: Do not duplicate
  declarations across files.** Each type of declaration belongs in exactly
  one file and must NOT be repeated in another file of the same module:
  - `variables.tf` — ONLY variable declarations using the exact Terraform block
    keyword `variable "name"` (never `var "name"`)
  - `outputs.tf` — ONLY output declarations (e.g. `output "name"` defined here)
  - `versions.tf` — ONLY terraform settings and required_providers
  - `main.tf` — resource and data source definitions (NOT variables,
    outputs, or required_providers — those go in their dedicated files)
* When files_to_generate includes both a `main.tf` and a `variables.tf`
  for the same module, put variables in `variables.tf` only, not in
  `main.tf`. Same rule applies to outputs.tf and versions.tf.
* **required_providers placement — CRITICAL:** `required_providers` belongs in
  exactly ONE place: each module's `versions.tf`. NEVER put a
  `terraform {{ required_providers {{}} }}` block inside a Terragrunt `generate`
  block (e.g. a generated `provider.tf`) or in `main.tf`. A Terragrunt provider
  `generate` block must contain ONLY `provider "aws" {{ region = ... }}` — set
  the region there, nothing else. Declaring `required_providers` in both a
  module's `versions.tf` and a generated `provider.tf` makes `terraform init`
  fail with "Duplicate required providers configuration". Use the AWS provider
  `generate "provider"` block shown in the canonical root config below.
* Every generated `modules/<stack>/README.md` MUST contain the terraform-docs
  marker pair `<!-- BEGIN_TF_DOCS -->` and `<!-- END_TF_DOCS -->` (on their own
  lines, in that order) so terraform-docs can populate the inputs/outputs table.
  A module README without both markers is incomplete — follow the canonical
  README shape below.
* When files_to_generate contains one path, return exactly that one file path
  in files. Use the full change_plan and repo_patterns as context, but do not
  include sibling planned files in the response.
* Generated GitHub Action workflows (e.g. `.github/workflows/terraform-pr-check.yml`
  and `.github/workflows/terraform-apply.yml`) must strictly align with the
  concrete directory structure in files_to_generate. For example, if the planned
  directories are under `environments/`, your workflows must trigger on `environments/**`
  and use `environments/` subdirectories as their job working-directories. Do not
  hallucinate independent folder structures such as `envs/`, `live/`, or `environments/non-prod`
  (without the trailing `environments/` prefix) that are not present in the files_to_generate list.
* Every Terragrunt `dependency` block MUST include `mock_outputs` and
  `mock_outputs_allowed_terraform_commands = ["validate", "plan"]`. This
  allows `terragrunt plan` to run locally before the dependency stack has been
  deployed. The mock values must match the output types declared in the
  dependency module (strings for IDs, lists for subnet ID lists, etc.).
* Generated CI workflows MUST use OIDC for AWS credentials — never static keys.
  Use `aws-actions/configure-aws-credentials` with `role-to-assume` and set
  `permissions: id-token: write` on the job or workflow. Never emit
  `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` in any workflow step.
* Every `aws-actions/configure-aws-credentials` step MUST set
  `mask-aws-account-id: true`. The action does not mask the account ID by
  default, so terraform's ARN output would otherwise print it in plaintext in
  the run logs — undesirable if the repo is ever public.
* Install terragrunt in CI via the authenticated `curl` pattern shown in the
  canonical workflow template. Never use `autero1/action-terragrunt` or any
  other third-party terragrunt installer action — the curl approach avoids
  pinning an old version and avoids incorrect action input names.
* `terraform-pr-check.yml` MUST NOT run `terragrunt validate` or `terragrunt plan`
  on any environment stack. Those commands require a deployed S3 backend which does
  not exist for brand-new infrastructure and will always fail on first PR. Instead,
  validate HCL syntax with `terragrunt hcl format --check` on `environments/` and
  validate each module with `terraform init -backend=false -input=false &&
  terraform validate` in the corresponding `modules/<name>` directory.
* In `terraform-pr-check.yml`, always use `terragrunt hcl format --check` for HCL
  format checking — NEVER `terragrunt hclfmt --check` (that flag does not exist on
  the `hclfmt` subcommand and will always fail). Since CI installs the latest
  terragrunt, `hcl format` is always available.
* `terraform-pr-check.yml` does NOT need `Configure AWS credentials` or
  `id-token: write` — `terraform init -backend=false` and `terraform validate` are
  schema-only operations that never contact AWS. Do not add OIDC steps.
* `terraform-pr-check.yml` MUST use a single job named `validate`. Do NOT split
  into multiple jobs per stack — that wastes runners, installs tools multiple times,
  and makes some tool installs appear unused.
* **Terragrunt includes — CRITICAL:** the environment root config is named
  `environments/<env>/root.hcl` and holds `remote_state` + the provider `generate`
  block DIRECTLY — it has no `include` block of its own. Leaf stack configs
  (`environments/<env>/<stack>/terragrunt.hcl`) include that root explicitly with
  `include "root" {{ path = find_in_parent_folders("root.hcl") }}`. Never put an
  `include`/`find_in_parent_folders` in the `root.hcl` itself (there is no parent to
  find — Terragrunt will report it includes itself / only one level of includes is
  allowed). Do not name the root config `terragrunt.hcl`; that is a deprecated
  Terragrunt anti-pattern.
* **Terragrunt locals scoping — CRITICAL:** In any terragrunt.hcl that has an
  `include` block, locals from the included parent config are NOT accessible as
  `local.xxx` in the child file. You MUST redeclare any needed values in a
  `locals {{}}` block in the child file itself, exactly as shown in the canonical
  stack config template below. Using `local.environment` without a local
  declaration in the same file will cause `Error: Unsupported attribute` at init.
* **variables.tf completeness — CRITICAL:** variables.tf MUST declare every
  variable referenced as var.xxx anywhere in the module (main.tf, outputs.tf,
  etc.) AND every top-level key listed in the corresponding Terragrunt stack's
  `inputs = {{}}` block. Nested object keys are not inputs: in
  `tags = {{ Environment = local.environment }}`, only `tags` is a module input,
  not `Environment`. Before writing variables.tf, enumerate all var.xxx
  references in main.tf and all top-level keys in the stack's inputs = {{}} to
  build the complete variable set. Every required variable in variables.tf
  (any variable without a `default =`) must be passed by the corresponding
  Terragrunt stack's `inputs = {{}}` block; otherwise non-interactive plan/apply
  will fail. If a variable should not be passed by Terragrunt, give it an
  explicit safe default. Commonly-forgotten required inputs are `aws_region` and
  other root values: if the module declares `variable "aws_region"` without a
  default, the stack MUST pass `aws_region = local.aws_region` (redeclared in the
  stack's own `locals {{}}` since parent locals are not accessible), exactly as
  the canonical stack template shows. When repairing variables.tf,
  preserve existing valid variable declarations and append missing ones; do not
  replace the file with only the newly mentioned variables. Missing any one
  variable will fail `terraform validate` or live Terragrunt plan/apply.
* **Provider schema errors are authoritative — CRITICAL:** when repair errors
  include Terraform/Terragrunt validation or plan output, treat quoted provider
  schema constraints as the source of truth for any resource type. Satisfy the
  exact regex, enum, type, range, and unsupported-argument errors reported by the
  provider rather than preserving the previous value. If a requested third-party
  artifact, endpoint, image, ARN, region, or identifier does not match a provider
  constraint, dynamically choose the smallest valid Terraform design that bridges
  the mismatch using generated resources, inputs, or workflows inside the planned
  file boundary. Do not special-case one AWS service; infer the fix from the
  issue text, repo patterns, active rules, and the exact provider error.
{_CANONICAL_FILE_SHAPES}{build_blackboard_prompt_section(blackboard)}{sibling_section}{existing_section}{repair_section}
Generation context JSON:
{json.dumps(context, indent=2)}
"""


def parse_generation_payload(raw_payload: str, allowed_paths: list[str]) -> GeneratedTerraform:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    generated = GeneratedTerraform.model_validate(_extract_json_object(text))
    allowed = set(allowed_paths)
    for path in generated.files:
        if path not in allowed:
            raise ValueError(f"Terraform generation returned unplanned file path `{path}`.")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"Terraform generation returned unsafe file path `{path}`.")
    missing = sorted(allowed - set(generated.files))
    if missing:
        raise ValueError(f"Terraform generation is missing planned file `{missing[0]}`.")
    return generated


def validate_single_file_document(
    document_text: str, *, expected_path: str
) -> GeneratedTerraformFile:
    """Validate the inner JSON document (the model's `{path, content, ...}`) for one file.

    Separate from the Bedrock-envelope extraction so callers that stitch a
    continued/truncated response can validate the reassembled document directly.
    """
    generated = GeneratedTerraformFile.model_validate(_extract_json_object(document_text))
    if generated.path != expected_path:
        raise ValueError(f"Terraform generation returned unplanned file path `{generated.path}`.")
    if generated.path.startswith("/") or ".." in generated.path.split("/"):
        raise ValueError(f"Terraform generation returned unsafe file path `{generated.path}`.")
    return generated


def parse_single_file_generation_payload(
    raw_payload: str, *, expected_path: str
) -> GeneratedTerraformFile:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    return validate_single_file_document(text, expected_path=expected_path)


_WORKFLOW_PATHS = frozenset(
    {
        ".github/workflows/terraform-pr-check.yml",
        ".github/workflows/terraform-apply.yml",
    }
)


def _extract_module_names(files_to_generate: list[str]) -> list[str]:
    """Return unique module directory names in plan order."""
    seen: set[str] = set()
    names: list[str] = []
    for path in files_to_generate:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "modules":
            name = parts[1]
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _extract_bootstrap_envs(files_to_generate: list[str]) -> list[str]:
    """Return unique environments from bootstrap/backend/<env> paths in plan order."""
    seen: set[str] = set()
    envs: list[str] = []
    for path in files_to_generate:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "bootstrap" and parts[1] == "backend":
            env = parts[2]
            if env not in seen:
                seen.add(env)
                envs.append(env)
    return envs


# Module-level constants for repeated YAML snippets so the workflow builders
# don't have to deal with nested string escaping inline.

_TG_INSTALL_STEP = (
    "      - name: Install terragrunt\n"
    "        env:\n"
    "          GH_TOKEN: ${{ github.token }}\n"
    "        run: |\n"
    '          TG_VERSION=$(curl -sL -H "Authorization: Bearer ${GH_TOKEN}" '
    '"https://api.github.com/repos/gruntwork-io/terragrunt/releases/latest" '
    "| python3 -c \"import sys,json; print(json.load(sys.stdin)['tag_name'])\")\n"
    '          curl -sL "https://github.com/gruntwork-io/terragrunt/releases/download/'
    '${TG_VERSION}/terragrunt_linux_amd64" -o /usr/local/bin/terragrunt\n'
    "          chmod +x /usr/local/bin/terragrunt"
)

_AWS_CREDS_STEP = (
    "      - name: Configure AWS credentials\n"
    "        uses: aws-actions/configure-aws-credentials@v4\n"
    "        with:\n"
    "          role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}\n"
    "          aws-region: us-west-2\n"
    "          mask-aws-account-id: true"
)

# YAML block scalar for the bootstrap backend job's run step.
# `\\s` in this Python source becomes `\s` in the output file (literal regex escape).
# `\\"` in this Python source becomes `\"` in the output file (escaped quote inside
# the shell's `"..."` argument to python3 -c).
_BOOTSTRAP_BACKEND_RUN = (
    "          terraform init\n"
    "          BUCKET=$(python3 -c \"import re; txt=open('variables.tf').read();"
    ' m=re.search(r\'variable\\s+\\"state_bucket_name\\".*?default\\s*=\\s*\\"([^\\"]+)\\"\','
    " txt, re.DOTALL); print(m.group(1) if m else '')\")\n"
    "          TABLE=$(python3 -c \"import re; txt=open('variables.tf').read();"
    ' m=re.search(r\'variable\\s+\\"state_lock_table_name\\".*?default\\s*=\\s*\\"([^\\"]+)\\"\','
    " txt, re.DOTALL); print(m.group(1) if m else '')\")\n"
    '          terraform import aws_s3_bucket.terraform_state "$BUCKET" 2>/dev/null || true\n'
    '          terraform import aws_dynamodb_table.terraform_locks "$TABLE" 2>/dev/null || true\n'
    "          terraform plan -out=tfplan\n"
    "          terraform apply -auto-approve tfplan"
)


def _terragrunt_plan_apply_steps(stack_label: str, working_directory: str) -> list[str]:
    return [
        f"      - name: Plan {stack_label}",
        f"        working-directory: {working_directory}",
        "        run: terragrunt plan --non-interactive -lock-timeout=20m -out=tfplan",
        f"      - name: Apply {stack_label}",
        f"        working-directory: {working_directory}",
        "        run: terragrunt apply --non-interactive tfplan",
    ]


def _build_pr_check_workflow(change_plan: ChangePlan) -> str:
    """Build terraform-pr-check.yml deterministically from the actual module paths."""
    module_names = _extract_module_names(change_plan.files_to_generate)
    bootstrap_envs = (
        _extract_bootstrap_envs(change_plan.files_to_generate)
        or change_plan.environments
        or ["non-prod"]
    )

    lines: list[str] = [
        "on:",
        "  pull_request:",
        "    paths:",
        '      - "environments/**"',
        '      - "modules/**"',
        '      - "bootstrap/**"',
        "",
        "permissions:",
        "  contents: read",
        "  pull-requests: write",
        "",
        "jobs:",
        "  validate:",
        "    name: Validate Terraform modules and HCL",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - uses: hashicorp/setup-terraform@v3",
        _TG_INSTALL_STEP,
        "      - name: HCL format check",
        "        working-directory: environments",
        "        run: terragrunt hcl format --check",
        "      - name: Terraform format check",
        "        run: terraform fmt -check -recursive -diff modules bootstrap",
    ]

    for name in module_names:
        lines += [
            f"      - name: Terraform init and validate — {name}",
            f"        working-directory: modules/{name}",
            "        run: |",
            "          terraform init -backend=false -input=false",
            "          terraform validate",
        ]

    for env in bootstrap_envs:
        label = f" ({env})" if len(bootstrap_envs) > 1 else ""
        lines += [
            f"      - name: Terraform init and validate — bootstrap backend{label}",
            f"        working-directory: bootstrap/backend/{env}",
            "        run: |",
            "          terraform init -backend=false -input=false",
            "          terraform validate",
        ]

    return "\n".join(lines) + "\n"


def _change_detect_job(workload_modules: list[str]) -> list[str]:
    """Build the `detect` job that scopes apply to the components that changed.

    It diffs the push range (`github.event.before`..`github.sha`) and emits, per
    component, whether it changed. A change to a shared root config
    (`environments/**/terragrunt.hcl`) fans out to foundation and every workload
    stack. When the before-SHA is missing/zero (first push) it treats every
    tracked infra file as changed so greenfield applies everything.
    """
    stack_list = " ".join(workload_modules)
    return [
        "  detect:",
        "    name: Detect changed components",
        "    runs-on: ubuntu-latest",
        "    outputs:",
        "      any: ${{ steps.scan.outputs.any }}",
        "      bootstrap: ${{ steps.scan.outputs.bootstrap }}",
        "      foundation: ${{ steps.scan.outputs.foundation }}",
        "      stacks: ${{ steps.scan.outputs.stacks }}",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "        with:",
        "          fetch-depth: 0",
        "      - name: Scan changed paths",
        "        id: scan",
        "        env:",
        "          BEFORE: ${{ github.event.before }}",
        "          SHA: ${{ github.sha }}",
        f'          WORKLOAD_STACKS: "{stack_list}"',
        "        run: |",
        "          set -euo pipefail",
        '          if [ -z "${BEFORE:-}" ] || [ "$BEFORE" = '
        '"0000000000000000000000000000000000000000" ]'
        ' || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then',
        "            CHANGED=$(git ls-files environments modules bootstrap)",
        "          else",
        '            CHANGED=$(git diff --name-only "$BEFORE" "$SHA")',
        "          fi",
        "          printf 'Changed files:\\n%s\\n' \"$CHANGED\"",
        '          changed() { grep -qE "$1" <<< "$CHANGED"; }',
        "          bootstrap=false",
        "          foundation=false",
        "          root=false",
        "          if changed '^bootstrap/'; then bootstrap=true; fi",
        "          if changed '^environments/terragrunt\\.hcl$'"
        " || changed '^environments/[^/]+/terragrunt\\.hcl$'; then root=true; fi",
        "          if changed '^modules/foundation/'"
        " || changed '^environments/[^/]+/foundation/'; then foundation=true; fi",
        '          if [ "$root" = true ]; then foundation=true; fi',
        "          stack_args=()",
        "          for s in ${WORKLOAD_STACKS}; do",
        '            if [ "$root" = true ] || changed "^modules/${s}/"'
        ' || changed "^environments/[^/]+/${s}/"; then',
        '              stack_args+=("$s")',
        "            fi",
        "          done",
        "          if [ ${#stack_args[@]} -eq 0 ]; then",
        '            stacks="[]"',
        "          else",
        "            stacks=$(printf '%s\\n' \"${stack_args[@]}\" | python3 -c"
        ' "import sys,json;'
        ' print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")',
        "          fi",
        "          any=false",
        '          if [ "$bootstrap" = true ] || [ "$foundation" = true ]'
        ' || [ "$stacks" != "[]" ]; then any=true; fi',
        "          {",
        '            echo "bootstrap=$bootstrap"',
        '            echo "foundation=$foundation"',
        '            echo "stacks=$stacks"',
        '            echo "any=$any"',
        '          } >> "$GITHUB_OUTPUT"',
    ]


def _gate_environment_job(env: str) -> list[str]:
    """Single manual-approval checkpoint for the whole apply run.

    `environment:` references a GitHub Environment; the apply only proceeds once a
    required reviewer approves it in the target repo. It runs only when `detect`
    found something to apply, so unrelated pushes never prompt for approval.
    """
    return [
        "  gate:",
        "    name: Approve apply",
        "    needs: detect",
        "    if: ${{ needs.detect.outputs.any == 'true' }}",
        "    runs-on: ubuntu-latest",
        f"    environment: {env}",
        "    steps:",
        '      - run: echo "Approved — applying only the changed components."',
    ]


def _build_apply_workflow(change_plan: ChangePlan) -> str:
    """Build terraform-apply.yml deterministically from the actual module paths.

    The run is scoped to the components whose files changed (`detect` job) and
    routed through one manual approval (`gate` job backed by a GitHub Environment)
    before any AWS mutation. Greenfield pushes apply every component in dependency
    order. Skipped upstream jobs do not cancel independent downstream applies.
    """
    module_names = _extract_module_names(change_plan.files_to_generate)
    bootstrap_envs = (
        _extract_bootstrap_envs(change_plan.files_to_generate)
        or change_plan.environments
        or ["non-prod"]
    )
    env = bootstrap_envs[0]
    has_foundation = "foundation" in module_names
    workload_modules = [n for n in module_names if n != "foundation"]

    lines: list[str] = [
        "on:",
        "  push:",
        "    branches:",
        "      - main",
        "    paths:",
        '      - "environments/**"',
        '      - "modules/**"',
        '      - "bootstrap/**"',
        "",
        "permissions:",
        "  contents: read",
        "  id-token: write",
        "",
        "jobs:",
        *_change_detect_job(workload_modules),
        "",
        *_gate_environment_job(env),
        "",
        "  bootstrap:",
        "    name: Bootstrap state backend",
        "    needs: [detect, gate]",
        "    if: ${{ always() && needs.gate.result == 'success'"
        " && needs.detect.outputs.bootstrap == 'true' }}",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - uses: hashicorp/setup-terraform@v3",
        "        with:",
        "          terraform_wrapper: false",
        _AWS_CREDS_STEP,
        "      - name: Bootstrap state backend (idempotent)",
        f"        working-directory: bootstrap/backend/{env}",
        "        run: |",
        _BOOTSTRAP_BACKEND_RUN,
    ]

    if has_foundation:
        lines += [
            "",
            "  apply-foundation:",
            f"    name: Apply — {env}/foundation",
            "    needs: [detect, gate, bootstrap]",
            "    if: ${{ always() && needs.gate.result == 'success'"
            " && needs.detect.outputs.foundation == 'true'"
            " && needs.bootstrap.result != 'failure'"
            " && needs.bootstrap.result != 'cancelled' }}",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: hashicorp/setup-terraform@v3",
            _TG_INSTALL_STEP,
            _AWS_CREDS_STEP,
            *_terragrunt_plan_apply_steps("foundation", f"environments/{env}/foundation"),
        ]

    if workload_modules:
        if has_foundation:
            pred_job = "apply-foundation"
            pred_ref = "needs['apply-foundation']"
        else:
            pred_job = "bootstrap"
            pred_ref = "needs.bootstrap"
        lines += [
            "",
            "  apply-workloads:",
            f"    name: Apply — {env}/${{{{ matrix.stack }}}}",
            f"    needs: [detect, gate, {pred_job}]",
            "    if: ${{ always() && needs.gate.result == 'success'"
            " && needs.detect.outputs.stacks != '[]'"
            f" && {pred_ref}.result != 'failure'"
            f" && {pred_ref}.result != 'cancelled' }}}}",
            "    runs-on: ubuntu-latest",
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
            "        stack: ${{ fromJson(needs.detect.outputs.stacks) }}",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: hashicorp/setup-terraform@v3",
            _TG_INSTALL_STEP,
            _AWS_CREDS_STEP,
            *_terragrunt_plan_apply_steps(
                "${{ matrix.stack }}", f"environments/{env}/${{{{ matrix.stack }}}}"
            ),
        ]

    return "\n".join(lines) + "\n"


def _apply_workflow_overrides(generated_files: dict[str, str], change_plan: ChangePlan) -> None:
    """Replace model-generated workflow files with deterministically correct content."""
    if ".github/workflows/terraform-pr-check.yml" in generated_files:
        generated_files[".github/workflows/terraform-pr-check.yml"] = _build_pr_check_workflow(
            change_plan
        )
    if ".github/workflows/terraform-apply.yml" in generated_files:
        generated_files[".github/workflows/terraform-apply.yml"] = _build_apply_workflow(
            change_plan
        )


TERRAFORM_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The single planned file path."},
        "content": {
            "type": "string",
            "description": "Complete Terraform or Terragrunt file content.",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short factual assumptions used while generating this file.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short risks, conflicts, or ambiguities for review.",
        },
    },
    "required": ["path", "content", "assumptions", "warnings"],
    "additionalProperties": False,
}


class BedrockTerraformGenerator:
    def __init__(
        self,
        model_id: str | None = None,
        bedrock_runtime: BedrockRuntime | None = None,
        *,
        read_timeout_seconds: int = 180,
        max_attempts: int = 2,
        max_tokens: int = 4096,
        max_continuations: int = 3,
        max_repair_attempts: int = 2,
        concurrency: int | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to generate Terraform with Bedrock.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = _int_env("IAC_SMITH_BEDROCK_READ_TIMEOUT", read_timeout_seconds)
        self.max_attempts = _int_env("IAC_SMITH_BEDROCK_MAX_ATTEMPTS", max_attempts)
        # Per-call output cap. The old 16384 cap let the model run away on one
        # file; at observed Bedrock throughput (~40 tok/s) that exceeded the read
        # timeout and read-timed out on every retry, stalling the whole run. A
        # tight cap keeps each call comfortably under read_timeout. Env-tunable.
        self.max_tokens = _int_env("IAC_SMITH_BEDROCK_MAX_TOKENS", max_tokens)
        # A genuinely large file (e.g. a data-platform module's main.tf) can need
        # more than one max_tokens budget. When a response comes back truncated
        # (stop_reason == "max_tokens") the JSON document is cut mid-object and
        # cannot parse; re-asking from scratch just truncates at the same wall.
        # Instead we continue the assistant's own turn and stitch the chunks, so
        # each call still stays under the per-call cap (and the read timeout) while
        # the file as a whole can exceed it. Env-tunable.
        self.max_continuations = max(
            0, _int_env("IAC_SMITH_BEDROCK_MAX_CONTINUATIONS", max_continuations)
        )
        self.max_repair_attempts = max_repair_attempts
        configured_concurrency = concurrency or int(os.getenv("IAC_SMITH_BEDROCK_CONCURRENCY", "4"))
        self.concurrency = max(1, configured_concurrency)
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    @property
    def bedrock_runtime(self) -> BedrockRuntime:
        if self._bedrock_runtime is None:
            import boto3
            from botocore.config import Config

            region = os.getenv("AWS_REGION", "us-west-2")
            self._bedrock_runtime = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(
                    connect_timeout=10,
                    read_timeout=self.read_timeout_seconds,
                    # Disable botocore's own retries: _invoke_model_with_retries is
                    # the single retry authority. Nesting an app-level loop inside
                    # botocore's retries multiplied the worst-case wall time
                    # (app_attempts * botocore_attempts * read_timeout) into tens of
                    # minutes, so one stalled Bedrock call hung the whole run.
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._bedrock_runtime

    def _invoke_model_with_retries(self, context: str = "", **kwargs: Any) -> dict[str, Any]:
        # Single retry authority (botocore's own retries are disabled in the
        # client Config). Worst-case wall time is bounded to
        # max_attempts * read_timeout, and every retry is logged (with the file it
        # was generating) so a slow/stalled Bedrock call shows up in the run output
        # — and names the culprit file — instead of looking like a hang.
        from botocore.exceptions import (
            ClientError,
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        transient = (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.bedrock_runtime.invoke_model(**kwargs)
            except transient as exc:
                last_error = exc
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _BEDROCK_THROTTLE_CODES:
                    raise
                last_error = exc
            if attempt < self.max_attempts:
                where = f" for {context}" if context else ""
                self._log(
                    f"IaC Smith: Bedrock call failed ({type(last_error).__name__})"
                    f"{where}; retrying (attempt {attempt + 1}/{self.max_attempts})."
                )
        assert last_error is not None
        raise last_error

    def summarize_failure(self, block_reason: str) -> str:
        """Summarize a run's block reason in one short paragraph for the issue author.

        Grounded strictly on the real terraform/terragrunt error text — the model
        compresses a concrete failure into plain language, it does not speculate.
        Used to comment back on the source issue when no PR could be opened.
        """
        prompt = (
            "You are IaC Smith. A request to generate Terraform/Terragrunt could not be "
            "fulfilled and no pull request was opened. In ONE short paragraph (at most 80 "
            "words), in plain language for the person who filed the issue, explain why, "
            "based ONLY on the errors below. If a concrete next step is obvious from the "
            "errors, add one sentence suggesting it. Do not invent any detail that is not "
            "in the errors, and do not include code blocks.\n\nErrors:\n" + block_reason
        )
        response = self._invoke_model_with_retries(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 400,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                }
            ),
        )
        raw_body = response["body"].read().decode("utf-8")
        payload = json.loads(raw_body)
        parts = payload.get("content") or []
        text = "".join(block.get("text", "") for block in parts if isinstance(block, dict))
        return text.strip()

    def _generate_planned_file(
        self,
        *,
        path: str,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        repair_errors: list[str] | None = None,
        previous_content: str | None = None,
        sibling_content: dict[str, str] | None = None,
        existing_content: str | None = None,
        blackboard: RunBlackboard | None = None,
    ) -> str:
        single_file_plan = change_plan.model_copy(update={"files_to_generate": [path]})
        prompt = build_generation_prompt(
            intent=intent,
            change_plan=single_file_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
            blackboard=blackboard,
            repair_errors=repair_errors,
            previous_content=previous_content,
            sibling_content=sibling_content,
            existing_content=existing_content,
        )
        last_error: Exception | None = None
        for attempt in range(3):
            document = self._invoke_file_generation(prompt=prompt, path=path)
            try:
                generated = validate_single_file_document(document, expected_path=path)
                return generated.content
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                self._log(
                    f"IaC Smith: JSON parse failed for {path} (attempt {attempt + 1}/3): "
                    f"{len(document)} chars received, error: {exc}"
                )
                # A stitched document that still won't parse is genuinely malformed
                # (bad escaping), not mere truncation — nudge the model on retry.
                if attempt == 0:
                    prompt += (
                        "\n\nYour previous response contained invalid JSON. Return only the "
                        "JSON object for this one file, with valid string escaping."
                    )
        raise ValueError(
            f"Failed to generate valid JSON for `{path}` after 3 attempts: {last_error}"
        )

    def _invoke_file_generation(self, *, prompt: str, path: str) -> str:
        """Generate one file's JSON document, continuing the turn if it truncates.

        The first call uses the json_schema output format. When Bedrock stops with
        `stop_reason == "max_tokens"` the document is cut mid-object, so we continue
        the assistant's own truncated turn and concatenate the chunks. Structured
        output is incompatible with an assistant prefill, so continuation calls omit
        it and simply finish the JSON the model began. Each call stays capped at
        max_tokens, keeping every request under the read timeout.
        """
        user_message = {"role": "user", "content": prompt}
        chunks: list[str] = []
        for continuation in range(self.max_continuations + 1):
            body: dict[str, Any] = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self.max_tokens,
                "temperature": 0,
            }
            if not chunks:
                body["messages"] = [user_message]
                body["output_config"] = {
                    "format": {"type": "json_schema", "schema": TERRAFORM_FILE_SCHEMA}
                }
            else:
                body["messages"] = [
                    user_message,
                    {"role": "assistant", "content": "".join(chunks).rstrip()},
                ]
            response = self._invoke_model_with_retries(
                context=path,
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            payload = json.loads(response["body"].read().decode("utf-8"))
            chunks.append(_extract_text_from_bedrock_payload(payload))
            if payload.get("stop_reason") != "max_tokens":
                break
            self._log(
                f"IaC Smith: {path} truncated at max_tokens; continuing the document "
                f"(continuation {continuation + 1}/{self.max_continuations})."
            )
        return "".join(chunks)

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        repo_path: Path | None = None,
        blackboard: RunBlackboard | None = None,
    ) -> dict[str, str]:
        generated_files: dict[str, str] = {}
        total_files = len(change_plan.files_to_generate)
        path_positions = {
            path: index for index, path in enumerate(change_plan.files_to_generate, start=1)
        }
        self._log(
            f"IaC Smith: generating {total_files} planned file(s) with Bedrock "
            f"(model: {self.model_id}, concurrency: {self.concurrency})."
        )

        existing_contents: dict[str, str] = {}
        if repo_path is not None:
            for file_path in change_plan.files_to_generate:
                candidate = repo_path / file_path
                if candidate.is_file():
                    with contextlib.suppress(OSError, UnicodeDecodeError):
                        existing_contents[file_path] = candidate.read_text(encoding="utf-8")

        # Group files by directory and sort within each group so main.tf is
        # generated before outputs.tf and variables.tf, giving those files
        # concrete sibling context for consistent resource names.
        groups: dict[str, list[str]] = {}
        for path in change_plan.files_to_generate:
            groups.setdefault(path.rpartition("/")[0], []).append(path)
        for paths in groups.values():
            paths.sort(key=lambda p: _GEN_ORDER.get(p.rpartition("/")[2], 4))

        def generate_group(paths: list[str]) -> list[tuple[str, str]]:
            results: list[tuple[str, str]] = []
            accumulated: dict[str, str] = {}
            for path in paths:
                file_index = path_positions[path]
                self._log(f"IaC Smith: generating file {file_index}/{total_files}: {path}")
                content = self._generate_planned_file(
                    path=path,
                    intent=intent,
                    change_plan=change_plan,
                    repo_patterns=repo_patterns,
                    ruleset=ruleset,
                    target_repo=target_repo,
                    sibling_content=_sibling_content(path, accumulated) or None,
                    existing_content=existing_contents.get(path),
                    blackboard=blackboard,
                )
                accumulated[path] = content
                results.append((path, content))
            return results

        group_workers = min(self.concurrency, max(1, len(groups)))
        with ThreadPoolExecutor(max_workers=group_workers) as executor:
            futures = {executor.submit(generate_group, paths): paths for paths in groups.values()}
            for future in as_completed(futures):
                for path, content in future.result():
                    generated_files[path] = content

        generated_files = {path: generated_files[path] for path in change_plan.files_to_generate}

        # Overwrite model-generated workflow files with deterministically correct content.
        # The model occasionally halluccinates the stack name in working-directory references;
        # generating from files_to_generate directly is always correct.
        _apply_workflow_overrides(generated_files, change_plan)

        seen_issue_sets: list[frozenset[str]] = []
        for repair_attempt in range(self.max_repair_attempts + 1):
            # Deterministically declare root-derived locals the model dropped, so
            # the orphaned-locals check stops the repair loop oscillating on them.
            _inject_missing_child_locals(generated_files)
            validation = static_review_generated_files(generated_files)
            # Autofix both security errors and structural issues; advisory
            # warnings (public ingress, docs markers) are surfaced for review,
            # not repaired.
            issues = [*validation.errors, *validation.structural]
            if not issues:
                for path in change_plan.files_to_generate:
                    suffix = " after repair" if repair_attempt else ""
                    self._log(f"IaC Smith: static review passed for {path}{suffix}.")
                self._log(f"IaC Smith: generated {len(generated_files)} file(s).")
                return generated_files

            self._log("IaC Smith: static review found issues: " + "; ".join(issues))

            # Oscillation guard: if this exact issue set already recurred from an
            # earlier round, repairs are cycling rather than converging.  Stop and
            # return the best-effort files — the graph's validation_runner gates on
            # security errors and the real terraform/terragrunt validation in
            # cli.py is the authoritative correctness gate.  A structural check
            # that real Terraform would accept must never crash the run.
            issue_set = frozenset(issues)
            if issue_set in seen_issue_sets:
                self._log(
                    "IaC Smith: static review issues are oscillating; returning "
                    "best-effort files for downstream validation to gate."
                )
                return generated_files
            seen_issue_sets.append(issue_set)

            if repair_attempt >= self.max_repair_attempts:
                self._log(
                    "IaC Smith: static review did not converge within the repair budget; "
                    "returning best-effort files for downstream validation to gate."
                )
                return generated_files

            # Workflow files are generated deterministically — exclude them from repair
            # so the model cannot overwrite the correct working-directory references.
            repairable = [p for p in change_plan.files_to_generate if p not in _WORKFLOW_PATHS]
            paths_to_repair = [
                path for path in repairable if _path_needs_repair(path, issues)
            ] or repairable
            self._log(
                f"IaC Smith: repairing {len(paths_to_repair)} file(s) after static review issues."
            )
            repair_errors = list(issues)
            previous_files = dict(generated_files)

            # Group by repair unit (module + its Terragrunt stack) so co-dependent
            # files are repaired sequentially with shared context instead of in
            # parallel from stale snapshots.
            repair_groups: dict[str, list[str]] = {}
            for path in paths_to_repair:
                repair_groups.setdefault(_repair_unit_key(path), []).append(path)
            for paths in repair_groups.values():
                paths.sort(key=lambda p: _GEN_ORDER.get(p.rpartition("/")[2], 4))

            def repair_group(
                paths: list[str],
                repair_errors: list[str] = repair_errors,
                previous_files: dict[str, str] = previous_files,
            ) -> list[tuple[str, str]]:
                accumulated = dict(previous_files)
                results: list[tuple[str, str]] = []
                for path in paths:
                    self._log(
                        f"IaC Smith: repairing file {path_positions[path]}/{total_files}: {path}"
                    )
                    try:
                        content = self._generate_planned_file(
                            path=path,
                            intent=intent,
                            change_plan=change_plan,
                            repo_patterns=repo_patterns,
                            ruleset=ruleset,
                            target_repo=target_repo,
                            repair_errors=repair_errors,
                            previous_content=previous_files[path],
                            sibling_content=_unit_sibling_content(path, accumulated) or None,
                            blackboard=blackboard,
                        )
                    except (ValueError, json.JSONDecodeError) as exc:
                        # A single file failing to repair must not crash the run.
                        # Keep the previous content and let the oscillation guard
                        # and the real terraform/terragrunt validation gate.
                        self._log(
                            f"IaC Smith: could not repair {path} ({exc}); keeping previous "
                            "content for downstream validation to gate."
                        )
                        content = previous_files[path]
                    accumulated[path] = content
                    results.append((path, content))
                return results

            repair_group_workers = min(self.concurrency, max(1, len(repair_groups)))
            with ThreadPoolExecutor(max_workers=repair_group_workers) as executor:
                futures = {
                    executor.submit(repair_group, paths): paths for paths in repair_groups.values()
                }
                for future in as_completed(futures):
                    for path, content in future.result():
                        generated_files[path] = content

            generated_files = {
                path: generated_files[path] for path in change_plan.files_to_generate
            }

        return generated_files

    def repair_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        generated_files: dict[str, str],
        repair_errors: list[str],
        blackboard: RunBlackboard | None = None,
    ) -> dict[str, str]:
        """Repair generated files using runtime validation/plan failures.

        Runtime validation happens after files are written into the target repo,
        so failures can include provider/schema/Terragrunt errors that static
        review cannot catch. Feed those exact errors back to Bedrock and repair
        only the implicated files when possible, falling back to the full planned
        file set when the error is cross-file or pathless.
        """

        paths_to_repair = [
            path
            for path in change_plan.files_to_generate
            if _path_needs_repair(path, repair_errors)
        ] or list(change_plan.files_to_generate)
        total_files = len(change_plan.files_to_generate)
        path_positions = {
            path: index for index, path in enumerate(change_plan.files_to_generate, start=1)
        }
        repaired_files = dict(generated_files)
        self._log(
            f"IaC Smith: repairing {len(paths_to_repair)} file(s) after runtime validation failure."
        )

        repair_groups: dict[str, list[str]] = {}
        for path in paths_to_repair:
            repair_groups.setdefault(_repair_unit_key(path), []).append(path)
        for paths in repair_groups.values():
            paths.sort(key=lambda p: _GEN_ORDER.get(p.rpartition("/")[2], 4))

        def repair_group(paths: list[str]) -> list[tuple[str, str]]:
            accumulated = dict(generated_files)
            results: list[tuple[str, str]] = []
            for path in paths:
                self._log(f"IaC Smith: repairing file {path_positions[path]}/{total_files}: {path}")
                content = self._generate_planned_file(
                    path=path,
                    intent=intent,
                    change_plan=change_plan,
                    repo_patterns=repo_patterns,
                    ruleset=ruleset,
                    target_repo=target_repo,
                    repair_errors=repair_errors,
                    previous_content=generated_files[path],
                    sibling_content=_unit_sibling_content(path, accumulated) or None,
                    blackboard=blackboard,
                )
                accumulated[path] = content
                results.append((path, content))
            return results

        repair_group_workers = min(self.concurrency, max(1, len(repair_groups)))
        with ThreadPoolExecutor(max_workers=repair_group_workers) as executor:
            futures = {
                executor.submit(repair_group, paths): paths for paths in repair_groups.values()
            }
            for future in as_completed(futures):
                for path, content in future.result():
                    repaired_files[path] = content

        _inject_missing_child_locals(repaired_files)
        return {path: repaired_files[path] for path in change_plan.files_to_generate}
