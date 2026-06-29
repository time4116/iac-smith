# IaC Smith — Agent Reference

## What This System Is

IaC Smith is an agentic workflow that converts a GitHub Issue into a validated Terraform/Terragrunt pull request on a target infrastructure repository. It does not apply infrastructure; it only generates, validates, and opens a PR.

It generates **infrastructure, not application code** — no `Program.cs`, Dockerfile, or build pipeline. This is not a refusal rule but a structural property: the change planner only plans IaC/workflow/doc files, and the generator is constrained to that planned set (`dynamic_terraform.py`: "Do not generate files outside files_to_generate"). A request like "deploy my .NET app" yields the hosting infrastructure; the deployable artifact and its build/deploy belong to a separate application pipeline.

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
| `IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS` | `3` | Max Bedrock repair attempts after runtime failures |
| `BEDROCK_MODEL_ID` | (required) | Primary Bedrock model/inference-profile for generation and repair |
| `BEDROCK_ESCALATION_MODEL_ID` | unset | Stronger model used for one penultimate repair attempt (failing files only) when the primary is stuck; unset or equal to `BEDROCK_MODEL_ID` disables escalation |
| `IAC_SMITH_BEDROCK_CONCURRENCY` | `4` | Parallel file generation threads |
| `IAC_SMITH_BEDROCK_MAX_TOKENS` | `16384` | Max output tokens for one file. Generation streams, so this can be generous enough to fit even a big module's `main.tf` in a single response without truncation; temperature 0 means the model stops at `end_turn`, so the cap bounds worst case, not typical cost |
| `IAC_SMITH_BEDROCK_READ_TIMEOUT` | `180` | Bedrock read timeout (seconds). Generation streams, so this applies *between* events (a stall), not to total generation time — a long file no longer races it |
| `IAC_SMITH_BEDROCK_MAX_ATTEMPTS` | `2` | Bedrock invoke attempts per call (single retry authority; botocore's own retries are disabled so they can't nest and multiply the wall time) |
| `IAC_SMITH_CHECK_TIMEOUT` | `300` | Per-command timeout (seconds) for `terraform`/`terragrunt` runtime-validation subprocesses, so a stalled plan/init can't hang the run |
| `IAC_SMITH_SCHEMA_CACHE_DIR` | System temp (`iac-smith-provider-schema/`) | Where the generation-time provider-schema harvest caches the per-provider-version schema JSON (and shares a Terraform plugin cache). Point this at a persisted/`actions/cache` path in CI so the `terraform init` cost is paid once across runs |
| `IAC_SMITH_SCHEMA_HARVEST` | unset | Set to `0` to disable the generation-time provider-schema harvest (the contract gate then degrades to runtime-only schema, as before) |

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
  → blackboard_planner    starts the run blackboard (shared contract/negative-pattern state)
  → code_generator        Bedrock: generates files in parallel; static review; per-file repair
  → validation_runner     static review + contract validation; routes back to code_generator if needed
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
    "blackboard": RunBlackboard,          # run-scoped contract/negative-pattern state
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
| `blackboard_ready` | blackboard_planner | Run blackboard initialized |
| `needs_repair` | validation_runner | Static review (or contract validation) failed; retry code_generator |
| `validated` | validation_runner | Static review and contract validation passed |
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
```

**Per environment:**
```
bootstrap/backend/{env}/main.tf
bootstrap/backend/{env}/variables.tf
bootstrap/backend/{env}/outputs.tf
bootstrap/backend/{env}/README.md
environments/{env}/root.hcl                       # environment root config (NOT terragrunt.hcl)
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

**Foundation auto-scaffold (feedback-driven):** `requires_new_vpc` is the *up-front*
signal, but it is often false for a workload that still turns out to need shared
networking. So if the generated output itself declares a `dependency "foundation"`
(or `baseline`/`vpc`/`vpc-foundation` — `FOUNDATION_STACK_NAMES`) pointing at a stack
that is neither created by this change nor present in the target repo,
`validation_runner` calls `add_foundation_stack(change_plan)` to add the foundation
module + per-environment stack above and regenerates against the expanded plan. This
is the model's own output proving the foundation is *truly needed*, rather than
looping repair on an unfixable dangling-dependency finding (which would otherwise
only surface at `terragrunt plan`). Scoped to foundation-style targets (an arbitrary
missing stack is left to the dangling-dependency finding), guarded to run **at most
once per run** (`state["foundation_added"]`), and detected by
`static_review.missing_foundation_dependency_targets`.

**If stack module does not exist yet:**
```
modules/{stack_name}/main.tf
modules/{stack_name}/variables.tf
modules/{stack_name}/outputs.tf
modules/{stack_name}/versions.tf
modules/{stack_name}/README.md
```

---

## Node: blackboard_planner

**Module:** `src/iac_smith/blackboard.py`

Starts a run-scoped `RunBlackboard` — typed shared state that coordinates
generation and repair without becoming long-term memory. Deliberately makes **no
service- or language-specific assumptions**: it carries no curated keyword lists
or golden-path file sets. Contracts and constraints are filled in later,
dynamically:

- **`resolve_contracts_for_files(files, resolver)`** derives candidate contracts
  from the resource types that actually appear in the generated Terraform — not
  from keywords. `ContractResolver` is the generic injection point: tests inject
  fixture contracts, and in production it is populated by
  `provider_schema.build_schema_resolver` (see below).
- **`provider_schema.build_schema_resolver(files)`** (`src/iac_smith/provider_schema.py`)
  is what makes the generation-time gate real. It reads the providers the
  generated `versions.tf` files declare, then harvests the **full** provider
  schema from a *clean throwaway config* — declaring only those providers,
  `terraform init`, `terraform providers schema -json` — rather than from the
  module under generation. This matters: the runtime harvest (below) runs the
  schema command inside the generated module, which fails to load exactly when the
  module is most broken (an invalid resource type), returning `{}` precisely when
  the backstop is needed. The clean-config harvest is independent of however
  broken the module is. It is disk-cached by provider source+version
  (`IAC_SMITH_SCHEMA_CACHE_DIR`) so the `init` cost is paid at most once per
  provider set, shares a `TF_PLUGIN_CACHE_DIR`, and is best-effort: no terraform,
  no network, or any failure degrades to an empty resolver (the gate becomes a
  no-op pass, the prior behaviour). Disable with `IAC_SMITH_SCHEMA_HARVEST=0`.
- **`contracts_from_provider_schema(schema, resource_types=...)`** parses
  `terraform providers schema -json` into `TerraformContract`s (allowed arguments
  = top-level attributes + nested block names; required = schema-required
  attributes). Fully generic across providers — no per-resource knowledge.
  Runtime validation (`runtime_validation.py`) also harvests this after each
  module's `terraform init` succeeds, scoped to the resource types that module
  declares, and returns it on `RuntimeValidationResult.contract_docs`.
- **`validate_generated_contracts(files, contract_docs, known_resource_types=...)`**
  checks generated resources against resolved docs, tracking brace depth so only
  top-level (depth-0) arguments are validated (nested `setting {}` / `tag {}` keys
  are not mistaken for unsupported arguments). When `known_resource_types` (the
  full set of types the declared providers expose) is supplied, it also rejects a
  **hallucinated resource type** — a type whose provider is known (shares a name
  prefix with a real type) but which the provider does not define, e.g.
  `aws_db_proxy_target_group` — deterministically, the equivalent of Terraform's
  "does not support resource type" but caught before Terraform runs. Resources
  from providers that weren't harvested are skipped, so no false positives. Runs
  in `validation_runner` after static review: the blackboard gets the scoped docs
  (so prompt injection stays small), and the gate gets the full schema.
- **`normalize_validation_findings(errors)`** turns `terraform`/`terragrunt
  validate`/`plan` failures into negative patterns; `RunBlackboard.with_findings`
  merges them. Recognized error classes: unsupported argument, unsupported block
  type, unsupported resource type, and the plan-time provider **value
  constraints** that `validate` cannot catch — `expected … to match regular
  expression` (e.g. an App Runner image that isn't ECR/`public.ecr.aws`),
  `expected … to be in the range` (e.g. a health-check interval outside 1–20), and
  `No value for required variable`. The runtime-repair loop (`cli.py`) feeds these
  back into the blackboard — together with the harvested provider contracts — and
  into `repair_files`, so each repair prompt is told both the real allowed/required
  arguments and what not to repeat.

The blackboard is injected into generation/repair prompts via
`build_blackboard_prompt_section`, which emits nothing until something has
actually been resolved or learned (no boilerplate on the first pass).

---

## Node: code_generator

**Module:** `src/iac_smith/dynamic_terraform.py` (class `BedrockTerraformGenerator`)

**Bedrock call per file:**
- Max tokens: `IAC_SMITH_BEDROCK_MAX_TOKENS` (default 4096), temperature 0. `invoke_model` is non-streaming, so a runaway generation that exceeds the read timeout looks like a dead connection; the tight cap keeps each call well under `IAC_SMITH_BEDROCK_READ_TIMEOUT`
- Output constrained to JSON schema: `{"path": str, "content": str, "assumptions": [], "warnings": []}`
- JSON format enforced via `output_config.format.type = "json_schema"` on the first call
- **Streaming generation:** file generation uses `invoke_model_with_response_stream` (`_invoke_file_generation` → `_read_stream_document`), accumulating `content_block_delta` text and tracking the final `stop_reason`. Streaming keeps the connection alive between events, so the read timeout applies per-event rather than to total generation time. That decouples a large file's generation length from the timeout and lets `IAC_SMITH_BEDROCK_MAX_TOKENS` be generous enough to fit a big `main.tf` in one response — no mid-document truncation, no continuation/prefill stitching (which models can't always do). If the model still reports `stop_reason == "max_tokens"`, the document may be clipped: the caller's parse-retry catches the malformed JSON and the log says to raise `IAC_SMITH_BEDROCK_MAX_TOKENS`

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
- Apply workflows must never trigger on `pull_request`; `terraform-apply.yml` must trigger only on push to `main` — never `master`, never both
- `terraform-apply.yml` must have a `bootstrap` job that runs first (before any stack apply job); the bootstrap job imports `aws_s3_bucket.terraform_state` and `aws_dynamodb_table.terraform_locks` before applying, making it idempotent on ephemeral CI runners
- `terraform-apply.yml` must use `secrets.AWS_ROLE_ARN_NON_PROD` for `role-to-assume` — never `AWS_ROLE_TO_ASSUME`, `AWS_ROLE_ARN`, or any other name
- Bootstrap module S3 resource must be named `aws_s3_bucket.terraform_state`; DynamoDB must be `aws_dynamodb_table.terraform_locks`; variables must be `state_bucket_name` and `state_lock_table_name` — these names are hardcoded in the apply workflow's import step
- Prefer private networking, encryption, least privilege
- Follow all `error`-severity rules; follow `warning`/`preference` rules unless conflict
- Generated workflows that require AWS access must use OIDC (`aws-actions/configure-aws-credentials` with `role-to-assume`); never emit `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`
- Every `aws-actions/configure-aws-credentials` step must set `mask-aws-account-id: true` — the action does not mask it by default, so terraform ARN output would otherwise leak the account ID into run logs
- Install terragrunt via the authenticated `curl` pattern; never use `autero1/action-terragrunt`
- `terraform-pr-check.yml` must use a single job named `validate`; never split into multiple jobs
- `terraform-pr-check.yml` must not include `terragrunt validate` or `terragrunt plan` steps — those require a deployed S3 backend that does not exist for brand-new infrastructure
- `terraform-pr-check.yml` must use `terragrunt hcl format --check` for HCL format checking; never `terragrunt hclfmt --check` (the `--check` flag does not exist on the `hclfmt` subcommand)
- `terraform-pr-check.yml` does not need AWS credentials or `id-token: write` — `terraform init -backend=false` and `terraform validate` are schema-only operations

**Canonical file shape examples injected (`_CANONICAL_FILE_SHAPES`):**

Eight annotated structural templates are appended to every generation prompt immediately after the non-negotiable rules. They cover:
- `versions.tf` — sole owner of `required_providers`; explicitly labelled "must NEVER appear in main.tf"
- `main.tf` — resources and data sources only; no `terraform{}` block, no variable/output declarations
- `variables.tf` — all `variable` declarations; shows `var.xxx` cross-file reference pattern
- `outputs.tf` — all `output` declarations only
- `environments/non-prod/root.hcl` (environment root, NOT terragrunt.hcl) — `remote_state`, `locals`, provider `generate` block, held directly with no `include`
- `environments/non-prod/<stack>/terragrunt.hcl` (stack) — `include "root" { path = find_in_parent_folders("root.hcl") }`, `dependency` blocks, `inputs`; explicitly warns "NEVER write `module.<name>.output_name`"
- `.github/workflows/terraform-pr-check.yml` — trigger path and working-directory alignment example
- `.github/workflows/terraform-apply.yml` — bootstrap job with idempotent imports, apply-foundation and stack apply jobs with `needs:` dependencies, `secrets.AWS_ROLE_ARN_NON_PROD` usage

---

## Static Review Checks

**Module:** `src/iac_smith/nodes/static_review.py`  
**Function:** `static_review_generated_files(generated_files, known_stack_dirs=None) → ValidationResult` — `known_stack_dirs` carries the stack directories already present in the target repo (from `existing_stack_dirs(repo_path)`) so cross-stack dependency checks don't false-positive on pre-existing infrastructure.

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
- A literal string assigned to a secret-named field (`*secret*`/`*password*`/`*token*`, e.g. `WEBUI_SECRET_KEY = "change-me"`). Reference-style identifiers (`*_arn`/`*_id`/`*_name`) and ARN/URL/path values are excluded. Advisory because the blocking credential patterns only match `secret =`/`password =` anchored directly before `=`, so a secret-*named* field with a suffix slips past them

**Terragrunt input direction is asymmetric:** Terragrunt passes inputs as `TF_VAR_*` environment variables and Terraform silently ignores undeclared ones, so a stack passing an *extra* input the module does not declare is **not** an error. Only the reverse is flagged — a *required* module variable (one declared without a `default`) that the stack fails to pass, which would fail non-interactive `terragrunt plan/apply`.

**Dangling cross-stack dependencies (structural):** `_find_terragrunt_dangling_dependencies` flags a stack that references `dependency.<name>.outputs.*` with no matching `dependency "<name>"` block, or whose `dependency` `config_path` resolves to a stack that is neither generated by this change nor in `known_stack_dirs`. This catches — at generation time, where regeneration can still fix it — the case where the model invents a dependency on a `foundation` stack that was never created (otherwise it surfaces only as a cryptic `terragrunt plan` failure: "There is no variable named dependency"). Generic: the rule is "the target stack must exist", never "it must be called foundation". The complementary prompt rule tells the model to provision missing shared infra in its own module or via data sources rather than depend on a stack that isn't there.

---

## Runtime Validation

**Module:** `src/iac_smith/runtime_validation.py`  
**Runs only if `IAC_SMITH_SKIP_RUNTIME_VALIDATION != "1"`**

**Commands run on target repo after files are written:**

| Scope | Command |
|---|---|
| `environments/` (if exists) | `terragrunt hcl format` (v0.71+) or `terragrunt hclfmt` — auto-fix, no `--check` |
| `modules/` and `bootstrap/` | `terraform fmt -recursive` — auto-fix, silently corrects formatting in place |
| Every standalone Terraform root with `*.tf` (any dir under `modules/`, `bootstrap/`, etc. — everything except `environments/` Terragrunt stacks and `.`-prefixed cache dirs) | `terraform init -backend=false -input=false` then `terraform validate` |

After each successful `terraform init`, IaC Smith also runs `terraform providers schema -json` in that module and parses the authoritative resource contracts (scoped to the resource types the module declares) onto `RuntimeValidationResult.contract_docs`. This is best-effort — any failure to read or parse the schema is swallowed and never blocks validation. The contracts feed the run blackboard so repair prompts get real allowed/required arguments (see Runtime Repair Loop).

By default, Terragrunt stacks under `environments/` are **not** plan-validated at runtime: `terragrunt validate/plan` against the real backend require all dependency stacks to be deployed, which is never true for brand-new infrastructure. HCL syntax errors are caught by the formatter; provider and schema errors are caught by `terraform validate` on the underlying modules.

**Optional runtime planning (`IAC_SMITH_RUNTIME_PLAN=1`):** when set, IaC Smith copies the tree to a throwaway scratch dir, rewrites the root `remote_state` to a local backend, and runs `terragrunt plan` per stack (every `environments/<env>/.../<stack>/terragrunt.hcl`, grouped stacks included). It is plan-only and never `apply`s; cross-stack dependencies are resolved through each stack's `mock_outputs` (allowed for `validate`/`plan`), so the foundation stack need not be applied first. This exercises the real provider/plan path before a PR is opened, and failures feed the runtime repair loop. The assumed AWS role must have read/describe permissions for the providers being planned.

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
for attempt in range(IAC_SMITH_RUNTIME_REPAIR_ATTEMPTS):   # default 3
    blackboard.contract_docs += runtime_validation.contract_docs  # authoritative provider schema
    blackboard += normalize_validation_findings(runtime_errors, contract_docs)
                  # learn negative patterns; an unsupported block/arg carries the
                  # resource's real allowed args inline so the model gets the fix
    repairer = escalation_model if penultimate attempt and BEDROCK_ESCALATION_MODEL_ID else primary
                  # the stronger model does one heavy pass when the primary is stuck;
                  # a final primary pass then cleans up cheaper follow-on errors
    call repairer.repair_files(intent, change_plan, repo_patterns, ruleset, target_repo,
                      generated_files, runtime_errors, blackboard)
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

A file is selected for repair if it appears in any error string, with these refinements:
1. **"keep in" exclusion** — for duplicate-declaration errors the hint reads "Remove from X, keep in Y." The canonical file (Y) is excluded from repair so its declarations are not dropped.
2. **Directory-based fallback** — runtime validation errors name the module/stack directory (e.g. `terraform validate modules/ecs-fargate failed`), not individual file paths. Any file whose parent directory matches the error is implicated. A negative-lookahead regex (`(?!/)`) prevents a shorter directory name (e.g. `environments`) from matching errors about a deeper path (e.g. `environments/non-prod/foundation`).
3. **Pinpoint scoping** — when an error pinpoints exact files (`on main.tf line 5`), only those files are repaired, not the whole directory. This stops a weak model from regenerating files that already validated and regressing them (e.g. rewriting a valid `variables.tf` with `var "x" {`). A directory-level error with no file pinpoint still repairs the whole unit.
4. **Stack→module bridge** — a `terragrunt plan`/`validate` failure names the stack dir (`environments/<env>/<stack>`), but the offending value (an image, a variable default) often lives in `modules/<stack>`; the module's `.tf` files are repaired too, matched by the shared stack name.

If no files match any criterion, all files are repaired as a fallback.

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

**Representative files** are sampled from `environments/**/root.hcl`, `environments/**/terragrunt.hcl`, `modules/**/*.tf`, `modules/**/README.md` and injected verbatim into the Bedrock generation prompt to teach it existing conventions.

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

Network-level errors (`ConnectionClosedError`, `ConnectTimeoutError`, `EndpointConnectionError`, `ReadTimeoutError`) and Bedrock throttling (`ThrottlingException`, `TooManyRequestsException`, `ServiceUnavailableException`) are retried up to `IAC_SMITH_BEDROCK_MAX_ATTEMPTS` times (default 2) before re-raising. This loop is the single retry authority — botocore's own `Config(retries=...)` is set to `max_attempts=1` so the two layers can't nest and multiply the worst-case wall time. Every retry is logged with the file it was generating, so a slow/stalled call is visible and pinpointed instead of looking like a hang. Non-throttle `ClientError`s (e.g. `AccessDeniedException`) are not retried.

For **streamed** file generation, the failure surface is during *consumption*, not just the initial call: a mid-stream service error arrives either as a raised `EventStreamError` or as a modeled exception-member event (`throttlingException`, `modelTimeoutException`, `internalServerException`, `modelStreamErrorException`, `serviceUnavailableException`, `validationException`). `_read_stream_document` raises `BedrockStreamError` on a member event so it is never silently dropped as a short document, and `_stream_file_with_retries` wraps the whole invoke + stream-read as one retryable unit — a fresh invoke restarts the stream (a consumed stream can't be resumed). Transient members and stream/connection errors retry up to `IAC_SMITH_BEDROCK_MAX_ATTEMPTS`; `validationException` (non-transient) propagates immediately rather than burning the parse-retry budget.

`ThrottlingException` (daily token quota exhausted) is caught in `cli.py` around both the graph invocation and the runtime repair loop, and returns a clean `IaCSmithRunResult(status="blocked", block_reason="Bedrock throttled: ...")` instead of a raw traceback.

All other Bedrock errors (auth failures, model errors) are not caught and propagate as unhandled exceptions, exiting non-zero with a Python traceback.

---

## Key Invariants

- IaC Smith **never** runs `terraform apply` or `terragrunt apply`.
- IaC Smith **never** commits to `main` on the target repo; always creates a new branch.
- Generated apply workflows must have a `push` trigger scoped to `main` only — never `master`, never `pull_request`.
- Terragrunt remote state keys must always use `path_relative_to_include()` — hardcoded keys are a FAILED static check.
- File organization within a Terraform module is strictly partitioned: declarations belong in exactly one canonical file. Cross-file duplicates are a FAILED static check.
- Path traversal in generated file paths is rejected before write (`..` and leading `/` are invalid).
- The target repo allowlist (`IAC_SMITH_ALLOWED_TARGET_REPO`) must exactly match `IAC_SMITH_TARGET_REPO` or the run aborts before any Bedrock call.
