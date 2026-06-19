import contextlib
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from iac_smith.models.change_plan import ChangePlan
from iac_smith.models.intent import InfrastructureIntent
from iac_smith.models.repo_patterns import RepoPatterns
from iac_smith.models.rules import Ruleset
from iac_smith.nodes.static_review import static_review_generated_files


def _path_needs_repair(path: str, errors: list[str]) -> bool:
    """Return True if `path` appears in any error as a file that needs to be changed.

    For duplicate-declaration errors the hint reads "Remove from X, keep in Y."
    The file at Y is the canonical one — it does not need repair.  Only X does.
    This function returns False when the path appears exclusively as a "keep in"
    target so that variables.tf / outputs.tf / versions.tf are not unnecessarily
    regenerated (which can drop declarations that main.tf still references).

    Also matches via the parent directory of `path`: runtime validation errors
    label failures with the module or stack directory (e.g. "terraform validate
    modules/ecs-fargate failed"), not with individual file paths, so file-level
    matching alone would miss them and trigger the expensive all-files fallback.
    """
    explicitly_excluded = False
    for error in errors:
        if path not in error:
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
    path_dir = path.rpartition("/")[0]
    if path_dir:
        dir_pattern = re.escape(path_dir) + r"(?!/)"
        if any(re.search(dir_pattern, error) for error in errors):
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

--- environments/non-prod/terragrunt.hcl (root config — remote_state, shared locals) ---
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
```

--- environments/non-prod/<stack>/terragrunt.hcl (stack config — source, dependency blocks, inputs) ---
```hcl
include "root" {
  path = find_in_parent_folders()
}

# locals from the included parent config are NOT available as local.xxx here.
# Redeclare any values you need from the parent in this locals {} block.
locals {
  environment = "non-prod"
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
  vpc_id             = dependency.foundation.outputs.vpc_id
  private_subnet_ids = dependency.foundation.outputs.private_subnet_ids
}
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
- The `bootstrap` job runs first and is idempotent — it imports existing S3/DynamoDB resources
  before planning/applying so the workflow is safe to run on ephemeral CI runners without persisted state.
- Always use `secrets.AWS_ROLE_ARN_NON_PROD` — never `AWS_ROLE_TO_ASSUME` or `AWS_ROLE_ARN`.
- Only trigger on branch `main`, not `master`.
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
  cleanly. `modules/foundation` may create only shared network primitives such
  as VPC, subnets, route tables, NAT/IGW, and explicitly shared network-boundary
  security groups that downstream stacks consume. It must NOT create workload
  security groups, ALBs, target groups/listeners, CloudWatch log groups, ECS
  clusters, task definitions, ECS services, or workload IAM. Those belong in the
  workload module, which consumes `modules/foundation` outputs through
  Terragrunt dependency inputs. Never generate the same AWS provider-level
  resource `name` for the same resource type in both foundation and workload
  modules; that creates apply-time name collisions Terraform module validation
  cannot catch.
* If a workload stack depends on foundation outputs for VPC/subnets/security
  groups, do not reference module.vpc unless that same module declares
  module "vpc". In Terragrunt, every `dependency.foundation.outputs.<name>`
  reference must exactly match an `output "<name>"` in
  `modules/foundation/outputs.tf`; do not invent aliases such as `alb_sg_id`
  unless that exact output is declared.
* Generate complete, syntactically valid file bodies for each requested path.
  Do not use placeholder comments instead of Terraform resources when the issue
  asks for concrete infrastructure.
* **Terraform module file organization — CRITICAL: Do not duplicate
  declarations across files.** Each type of declaration belongs in exactly
  one file and must NOT be repeated in another file of the same module:
  - `variables.tf` — ONLY variable declarations (e.g. `variable "name"` defined here)
  - `outputs.tf` — ONLY output declarations (e.g. `output "name"` defined here)
  - `versions.tf` — ONLY terraform settings and required_providers
  - `main.tf` — resource and data source definitions (NOT variables,
    outputs, or required_providers — those go in their dedicated files)
* When files_to_generate includes both a `main.tf` and a `variables.tf`
  for the same module, put variables in `variables.tf` only, not in
  `main.tf`. Same rule applies to outputs.tf and versions.tf.
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
  explicit safe default. When repairing variables.tf, preserve existing valid
  variable declarations and append missing ones; do not replace the file with
  only the newly mentioned variables. Missing any one variable will fail
  `terraform validate` or live Terragrunt plan/apply.
{_CANONICAL_FILE_SHAPES}{sibling_section}{existing_section}{repair_section}
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


def parse_single_file_generation_payload(
    raw_payload: str, *, expected_path: str
) -> GeneratedTerraformFile:
    payload = _extract_json_object(raw_payload)
    text = _extract_text_from_bedrock_payload(payload)
    generated = GeneratedTerraformFile.model_validate(_extract_json_object(text))
    if generated.path != expected_path:
        raise ValueError(f"Terraform generation returned unplanned file path `{generated.path}`.")
    if generated.path.startswith("/") or ".." in generated.path.split("/"):
        raise ValueError(f"Terraform generation returned unsafe file path `{generated.path}`.")
    return generated


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
    "          aws-region: us-west-2"
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


def _build_apply_workflow(change_plan: ChangePlan) -> str:
    """Build terraform-apply.yml deterministically from the actual module paths."""
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
        "  bootstrap:",
        "    name: Bootstrap state backend",
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
            "    needs: bootstrap",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: hashicorp/setup-terraform@v3",
            _TG_INSTALL_STEP,
            _AWS_CREDS_STEP,
            *_terragrunt_plan_apply_steps("foundation", f"environments/{env}/foundation"),
        ]

    if workload_modules:
        needs = "apply-foundation" if has_foundation else "bootstrap"
        matrix_stacks = ", ".join(workload_modules)
        lines += [
            "",
            "  apply-workloads:",
            f"    name: Apply — {env}/${{{{ matrix.stack }}}}",
            f"    needs: {needs}",
            "    runs-on: ubuntu-latest",
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
            f"        stack: [{matrix_stacks}]",
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
        read_timeout_seconds: int = 240,
        max_attempts: int = 3,
        max_repair_attempts: int = 2,
        concurrency: int | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv("BEDROCK_MODEL_ID", "")
        if not self.model_id:
            raise ValueError("BEDROCK_MODEL_ID must be set to generate Terraform with Bedrock.")
        self._bedrock_runtime = bedrock_runtime
        self.read_timeout_seconds = read_timeout_seconds
        self.max_attempts = max_attempts
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
                    retries={"max_attempts": self.max_attempts, "mode": "standard"},
                ),
            )
        return self._bedrock_runtime

    def _invoke_model_with_retries(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )

        retryable_errors = (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
        )
        last_error: Exception | None = None
        for _attempt in range(1, self.max_attempts + 1):
            try:
                return self.bedrock_runtime.invoke_model(**kwargs)
            except retryable_errors as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

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
    ) -> str:
        single_file_plan = change_plan.model_copy(update={"files_to_generate": [path]})
        prompt = build_generation_prompt(
            intent=intent,
            change_plan=single_file_plan,
            repo_patterns=repo_patterns,
            ruleset=ruleset,
            target_repo=target_repo,
            repair_errors=repair_errors,
            previous_content=previous_content,
            sibling_content=sibling_content,
            existing_content=existing_content,
        )
        last_error: Exception | None = None
        for attempt in range(3):
            response = self._invoke_model_with_retries(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 16384,
                        "temperature": 0,
                        "messages": [{"role": "user", "content": prompt}],
                        "output_config": {
                            "format": {
                                "type": "json_schema",
                                "schema": TERRAFORM_FILE_SCHEMA,
                            }
                        },
                    }
                ),
            )
            raw_body = response["body"].read().decode("utf-8")
            try:
                generated = parse_single_file_generation_payload(raw_body, expected_path=path)
                return generated.content
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                self._log(
                    f"IaC Smith: JSON parse failed for {path} (attempt {attempt + 1}/3): "
                    f"{len(raw_body)} chars received, error: {exc}"
                )
                # Truncation often means the content is too complex — ask the model
                # to be more concise on retry by appending a hint to the prompt
                if attempt == 0:
                    prompt += (
                        "\n\nYour previous response was truncated or contained invalid JSON. "
                        "Be more concise. Focus on essential resources only."
                    )
        raise ValueError(
            f"Failed to generate valid JSON for `{path}` after 3 attempts: {last_error}"
        )

    def generate_files(
        self,
        *,
        intent: InfrastructureIntent,
        change_plan: ChangePlan,
        repo_patterns: RepoPatterns,
        ruleset: Ruleset | None,
        target_repo: str,
        repo_path: Path | None = None,
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

        return {path: repaired_files[path] for path in change_plan.files_to_generate}
