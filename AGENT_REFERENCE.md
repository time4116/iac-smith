# IaC Smith — Agent Reference

## What This System Is

IaC Smith is an agentic workflow that converts a GitHub Issue into a validated Terraform/Terragrunt pull request on a target infrastructure repository. It does not apply infrastructure; it only generates, validates, and opens a PR.

**Input:** GitHub Issue with the `iac-smith` label  
**Output:** PR on the target repo containing Terraform/Terragrunt files, or a blocked/ignored status

---

## Invocation

```bash
uv run python -m iac_smith.cli
```

All configuration is via environment variables. There are no CLI flags.

---

## Required Environment Variables

| Variable | Description |
|---|---|
| `IAC_SMITH_SOURCE_REPO` | `owner/repo` of the controller (this repo) |
| `IAC_SMITH_ISSUE_NUMBER` | Integer GitHub issue ID |
| `IAC_SMITH_TARGET_REPO` | `owner/repo` of the infrastructure repo to write to |
| `IAC_SMITH_ALLOWED_TARGET_REPO` | Must exactly match `IAC_SMITH_TARGET_REPO`; allowlist guard |
| `IAC_SMITH_GITHUB_TOKEN` or `GITHUB_TOKEN` | Token for reading issues from source repo |
| `IAC_SMITH_TARGET_REPO_TOKEN` or `GITHUB_TOKEN` | Token for creating PRs on target repo |
| `BEDROCK_MODEL_ID` | Bedrock model ID or inference profile ARN |

## Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-west-2` | AWS region for Bedrock |
| `IAC_SMITH_TARGET_REPO_PATH` | (clones to temp dir) | Explicit local path to target repo |
| `IAC_SMITH_WORKDIR` | System temp | Dir to clone target repo into |
| `IAC_SMITH_SKIP_RUNTIME_VALIDATION` | unset | Set to `1` to skip terraform/terragrunt validation |
| `IAC_SMITH_SKIP_PUSH` | unset | Set to `1` to skip git push and PR creation |
| `IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS` | `2` | Max Bedrock repair attempts after runtime failures |
| `IAC_SMITH_BEDROCK_CONCURRENCY` | `4` | Parallel file generation threads |

---

## Exit Behavior

`cli.py:main()` calls `sys.exit(1)` if `run_iac_smith()` returns any of: `ignored`, `blocked`, `no_changes`.  
Returns exit 0 on `pr_created`.

`run_iac_smith()` returns `IaCSmithRunResult`:
```python
@dataclass
class IaCSmithRunResult:
    status: str           # "ignored" | "blocked" | "no_changes" | "pr_created"
    branch: str | None
    pr_url: str | None
    pr_number: int | None
    block_reason: str | None
```

---

## End-to-End Flow

```
GitHub Issue (with "iac-smith" label)
  → issue_intake          checks label; routes to END if missing
  → intent_parser         Bedrock: issue text → InfrastructureIntent JSON
  → ruleset_loader        reads rules/*.yaml from target repo or bundled default
  → repo_pattern_scanner  scans target repo for existing patterns
  → change_planner        plans which files to generate
  → code_generator        Bedrock: generates files in parallel; static review; per-file repair
  → validation_runner     static review on full set; routes back to code_generator if needed
  → pr_writer             builds PR body
  → cli.py post-graph     writes files, runtime validation, runtime repair loop, commit, push, PR
```

---

## LangGraph State Schema

`IaCSmithState` (TypedDict, all keys optional):

```python
{
    "issue_number": int,
    "issue_title": str,
    "issue_body": str,
    "issue_url": str,
    "labels": list[str],
    "target_repo": str,
    "target_repo_path": str,
    "intent": InfrastructureIntent,
    "ruleset": Ruleset,
    "repo_patterns": RepoPatterns,
    "change_plan": ChangePlan,
    "generated_files": dict[str, str],   # file path → file content
    "validation": ValidationResult,
    "pr_body": str | None,
    "status": str,                        # routing key (see below)
    "block_reason": str | None,
    "repair_attempts": int,
}
```

**Routing status values:**

| Value | Set by | Meaning |
|---|---|---|
| `ignored` | issue_intake | Issue lacks `iac-smith` label |
| `blocked` | intent_parser or validation_runner | Cannot safely generate |
| `accepted` | intent_parser | Intent parsed OK |
| `ruleset_loaded` | ruleset_loader | Rules ready |
| `patterns_scanned` | repo_pattern_scanner | Repo patterns ready |
| `plan_ready` | change_planner | Files to generate planned |
| `needs_repair` | validation_runner | Static review failed; retry code_generator |
| `validated` | validation_runner | Static review passed |
| `pr_ready` | pr_writer | PR body built |

---

## Node: intent_parser

**Module:** `src/iac_smith/nodes/intent_parser.py`  
**Bedrock call:** `bedrock-runtime.invoke_model`  
**Max tokens:** 1200, temperature 0

**Blocks on:**
- Request to apply/destroy infrastructure directly
- Request to store plaintext credentials

**Does not block on:**
- Ambiguous resource names
- Missing region (defaults to `us-west-2`, adds warning)
- Missing environment scope (defaults to `both` with `["non-prod", "prod"]`)

**Output model:** `InfrastructureIntent`

```python
class InfrastructureIntent(BaseModel):
    raw_request: str
    resource_type: str           # e.g., "vpc_foundation", "eks_fargate", "rds_postgres"
    environment_scope: str       # "non_prod_only" | "prod_only" | "both"
    environments: list[str]      # ["non-prod"] | ["prod"] | ["non-prod", "prod"]
    region: str                  # default "us-west-2"
    requires_new_vpc: bool       # default False
    features: list[str]          # e.g., ["encryption", "private_subnets"]
    assumptions: list[str]
    warnings: list[str]
    blocked: bool
    block_reason: str | None
```

---

## Node: change_planner

**Module:** `src/iac_smith/nodes/change_planner.py`

**Output model:** `ChangePlan`

```python
class ChangePlan(BaseModel):
    stack_name: str                          # e.g., "vpc-foundation", "eks-fargate"
    environments: list[str]
    files_to_generate: list[str]             # all paths relative to target repo root
    backend_resources: dict[str, BackendResource]  # env → {bucket, lock_table}
    summary: list[str]                       # human-readable list of what's being generated

class BackendResource(BaseModel):
    bucket: str      # pattern: "iac-smith-state-{env}-{account_id}"
    lock_table: str  # pattern: "iac-smith-lock-{env}"
```

**File set always included:**
```
README.md
.github/workflows/terraform-pr-check.yml
.github/workflows/terraform-apply.yml
environments/terragrunt.hcl
```

**Per environment:**
```
bootstrap/backend/{env}/main.tf
bootstrap/backend/{env}/variables.tf
bootstrap/backend/{env}/outputs.tf
bootstrap/backend/{env}/README.md
environments/{env}/terragrunt.hcl
environments/{env}/{stack_name}/terragrunt.hcl
environments/{env}/{stack_name}/README.md
```

**If `requires_new_vpc: true` (foundation module):**
```
modules/foundation/main.tf
modules/foundation/variables.tf
modules/foundation/outputs.tf
modules/foundation/versions.tf
modules/foundation/README.md
environments/{env}/foundation/terragrunt.hcl
environments/{env}/foundation/README.md
```

**If stack module does not exist yet:**
```
modules/{stack_name}/main.tf
modules/{stack_name}/variables.tf
modules/{stack_name}/outputs.tf
modules/{stack_name}/versions.tf
modules/{stack_name}/README.md
```

---

## Node: code_generator

**Module:** `src/iac_smith/dynamic_terraform.py` (class `BedrockTerraformGenerator`)

**Bedrock call per file:**
- Max tokens: 16384, temperature 0
- Output constrained to JSON schema: `{"path": str, "content": str, "assumptions": [], "warnings": []}`
- JSON format enforced via `output_config.format.type = "json_schema"`

**Concurrency:** `IAC_SMITH_BEDROCK_CONCURRENCY` threads (default 4), one file per thread

**Static repair within code_generator (max 1 attempt, full set):**
After all files are generated in parallel:
1. Run static review on complete `generated_files` dict
2. If FAILED: identify which files are implicated in errors; re-generate only those
3. Accept result

**Prompt non-negotiables injected:**
- Return only JSON
- File organization rules: `variables.tf` = variables only, `outputs.tf` = outputs only, `versions.tf` = terraform block + required_providers only, `main.tf` = resources + data sources only
- No duplicate declarations across files in a module
- No hardcoded credentials
- Apply workflows must never trigger on `pull_request`
- Prefer private networking, encryption, least privilege
- Follow all `error`-severity rules; follow `warning`/`preference` rules unless conflict

**Canonical file shape examples injected (`_CANONICAL_FILE_SHAPES`):**

Seven annotated structural templates are appended to every generation prompt immediately after the non-negotiable rules. They cover:
- `versions.tf` — sole owner of `required_providers`; explicitly labelled "must NEVER appear in main.tf"
- `main.tf` — resources and data sources only; no `terraform{}` block, no variable/output declarations
- `variables.tf` — all `variable` declarations; shows `var.xxx` cross-file reference pattern
- `outputs.tf` — all `output` declarations only
- `environments/non-prod/terragrunt.hcl` (root) — `remote_state`, `locals`, `generate` block
- `environments/non-prod/<stack>/terragrunt.hcl` (stack) — `include`, `dependency` blocks, `inputs`; explicitly warns "NEVER write `module.<name>.output_name`"
- `.github/workflows/terraform-pr-check.yml` — trigger path and working-directory alignment example

---

## Static Review Checks

**Module:** `src/iac_smith/nodes/static_review.py`  
**Function:** `static_review_generated_files(generated_files: dict[str, str]) → ValidationResult`

**ValidationResult:**
```python
class ValidationResult(BaseModel):
    status: str          # "PASSED" | "PARTIAL" | "FAILED"
    errors: list[str]
    warnings: list[str]
    checks: list[str]    # passed check descriptions
```

**FAILED (blocks PR without repair) if any of:**
- AWS access key pattern matched (`AKIA...` / `ASIA...` 20-char)
- Private key header found (`BEGIN RSA/OPENSSH/EC/DSA PRIVATE KEY`)
- `aws_access_key_id=` or `aws_secret_access_key=` literal found
- Generic secret pattern: `(password|token|secret)\s*=\s*(?:"[^"]{6,}"|'[^']{6,}')` — alternation keeps each delimiter paired with its own exclusion class, so values containing the opposite quote character are still caught
- Terraform apply workflow has `pull_request` trigger without branch filter
- Terragrunt remote state key does not use `path_relative_to_include()`
- Duplicate `variable` declarations across files in same module
- Duplicate `output` declarations across files in same module
- More than one `required_providers` block in a module
- `var.xxx` referenced but not declared in module
- `module.xxx` referenced but the `module "xxx"` block not found

**PARTIAL (warnings, no block) if:**
- Public ingress (0.0.0.0/0 or ::/0) to any of: SSH (22), RDP (3389), PostgreSQL (5432), MySQL (3306), MSSQL (1433), Redis (6379), MongoDB (27017)
- Module README missing terraform-docs markers

---

## Runtime Validation

**Module:** `src/iac_smith/runtime_validation.py`  
**Runs only if `IAC_SMITH_SKIP_RUNTIME_VALIDATION != "1"`**

**Commands run on target repo after files are written:**

| Scope | Command |
|---|---|
| `environments/` (if exists) | `terragrunt hcl format` (v0.71+) or `terragrunt hclfmt` |
| `modules/` and `bootstrap/` | `terraform fmt -check -recursive -diff` |
| Each dir in `modules/` with `*.tf` | `terraform init -backend=false -input=false` then `terraform validate` |
| Each dir in `environments/` with `terragrunt.hcl` | `terragrunt init -reconfigure` then `terragrunt validate` then `terragrunt plan -lock=false -out=tfplan.binary` |

**Environment set for all commands:**
```
TF_INPUT=false
TF_IN_AUTOMATION=true
CI=true
```

**Fails fast** on first error; returns error message with command label and captured stdout/stderr.

---

## Runtime Repair Loop (in cli.py)

After runtime validation fails:

```
for attempt in range(IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS):
    call repair_files(intent, change_plan, repo_patterns, ruleset, target_repo,
                      generated_files, runtime_errors)
    static_review repaired files
    if static_review FAILED:
        append static errors to runtime errors
        call repair_files again with combined errors
    write repaired files to disk
    re-run runtime validation
    if passed: break
else:
    return status=blocked
```

**File selection for repair (`_path_needs_repair`):**

A file is selected for repair if it appears in any error string, with two refinements:
1. **"keep in" exclusion** — for duplicate-declaration errors the hint reads "Remove from X, keep in Y." The canonical file (Y) is excluded from repair so its declarations are not dropped.
2. **Directory-based fallback** — runtime validation errors name the module/stack directory (e.g. `terraform validate modules/ecs-fargate failed`), not individual file paths. Any file whose parent directory matches the error is implicated. A negative-lookahead regex (`(?!/)`) prevents a shorter directory name (e.g. `environments`) from matching errors about a deeper path (e.g. `environments/non-prod/foundation`).

If no files match either criterion, all files are repaired as a fallback.

`repair_files` sends the original Bedrock generation prompt plus a repair section containing:
- Each validation failure message
- The previously generated content that failed

---

## Repository Scanner

**Module:** `src/iac_smith/repo_scanner.py`  
**Function:** `scan_repo_patterns(root: Path) → RepoPatterns`

```python
class RepoPatterns(BaseModel):
    uses_terraform: bool = False
    uses_terragrunt: bool = False
    environments: list[str] = []
    default_environment_names: list[str] = ["non-prod", "prod"]
    module_sources: list[str] = []
    preferred_layout: str = "iac_smith_default"   # or "terragrunt_live_modules"
    remote_state_uses_path_relative_to_include: bool = False
    existing_stack_paths: list[str] = []
    representative_files: dict[str, str] = {}     # path → content (max 4000 chars each, up to 12)
    warnings: list[str] = []
```

**Layout detection:** `terragrunt_live_modules` if `environments/` directory exists, else `iac_smith_default`

**Representative files** are sampled from `environments/**/terragrunt.hcl`, `modules/**/*.tf`, `modules/**/README.md` and injected verbatim into the Bedrock generation prompt to teach it existing conventions.

---

## Rules System

**Location:** `{target_repo}/rules/*.yaml` if present; otherwise bundled `iac-smith/rules/*.yaml`

> **Per-target-repo configurability:** If the target repo ships its own `rules/` directory, those rules fully replace the bundled defaults — the controller is not modified. This means each infrastructure repo can enforce its own compliance requirements, naming conventions, or tag policies without any changes to the IaC Smith source. The bundled rules apply only when the target repo has no `rules/` directory at all.

**Rule schema:**
```yaml
rules:
  - id: string
    severity: error | warning | preference
    description: string
```

**Severity behavior:**
- `error`: Bedrock must comply; static review or runtime failure triggers repair
- `warning`: Bedrock should comply; non-compliance disclosed in PR, does not block
- `preference`: Guides generation; not enforced

**Bundled rule files:** `aws.yaml`, `terraform.yaml`, `terragrunt.yaml`, `security.yaml`, `tagging.yaml`, `pr_review.yaml`

---

## Version Detection

**Module:** `src/iac_smith/version_detection.py`  
**Function:** `ensure_terraform_terragrunt(repo_path: Path) → dict[str, str]`

Returns env dict with `PATH` prepended to downloaded binaries (in a temp dir).

**Version sources (in priority order):**
1. `.terraform-version` file in target repo root
2. `terraform` already on `PATH`
3. Latest GitHub release (fetched via API)

Same logic for terragrunt via `.terragrunt-version`.

**Minimum tested versions:** Terraform 1.0.0, Terragrunt 0.68.0

**Terragrunt v0.71.0+ behavior change:** uses `hcl format` and `--non-interactive` (older: `hclfmt` and `--terragrunt-non-interactive`)

---

## PR Branch Naming

**Pattern:** `iac-smith/issue-{issue_number}-{slug}`  
**Slug:** lowercase alphanumeric + hyphens, max 48 chars, derived from issue title

---

## PR Body Structure

Sections in order:
1. Source issue link
2. Generated infrastructure summary
3. Target environments, region, stack name
4. Assumptions and defaults
5. Files created or changed
6. Backend resources (S3 bucket + DynamoDB lock table per env)
7. Validation results (status + check list)
8. Warnings and risks
9. Expected post-merge apply behavior
10. Explicit confirmation: IaC Smith did not apply anything

---

## GitHub API Calls

**Issue fetch:** `GET /repos/{repo}/issues/{number}`  
**PR creation:** `POST /repos/{repo}/pulls` (idempotent: checks for existing open PR on same head/base first)  
**Auth:** `Authorization: Bearer {token}`, `X-GitHub-Api-Version: 2022-11-28`

---

## GitHub Actions Trigger

**Workflow:** `.github/workflows/issue-to-pr.yml`  
**Trigger:** `on: issues: types: [labeled]`  
**Guard:** `if: github.event.label.name == 'iac-smith' && github.actor == 'time4116'`

The workflow installs Python, uv, terraform, terragrunt, configures AWS OIDC credentials, sets all required env vars, then runs `uv run python -m iac_smith.cli`.

---

## Bedrock Hard-Failure Behavior

Network-level errors (`ConnectionClosedError`, `ConnectTimeoutError`, `EndpointConnectionError`, `ReadTimeoutError`) are retried up to `max_attempts` times (default 3) before re-raising.

`ThrottlingException` (daily token quota exhausted) is caught in `cli.py` around both the graph invocation and the runtime repair loop, and returns a clean `IaCSmithRunResult(status="blocked", block_reason="Bedrock throttled: ...")` instead of a raw traceback.

All other Bedrock errors (auth failures, model errors) are not caught and propagate as unhandled exceptions, exiting non-zero with a Python traceback.

---

## Key Invariants

- IaC Smith **never** runs `terraform apply` or `terragrunt apply`.
- IaC Smith **never** commits to `main` on the target repo; always creates a new branch.
- Generated apply workflows must have a `push` trigger scoped to `main`/`master` only — never `pull_request`.
- Terragrunt remote state keys must always use `path_relative_to_include()` — hardcoded keys are a FAILED static check.
- File organization within a Terraform module is strictly partitioned: declarations belong in exactly one canonical file. Cross-file duplicates are a FAILED static check.
- Path traversal in generated file paths is rejected before write (`..` and leading `/` are invalid).
- The target repo allowlist (`IAC_SMITH_ALLOWED_TARGET_REPO`) must exactly match `IAC_SMITH_TARGET_REPO` or the run aborts before any Bedrock call.
