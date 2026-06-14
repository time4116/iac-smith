# IaC Smith Project Brief

## Purpose

**IaC Smith** is an AWS-focused agentic infrastructure workflow that turns freeform GitHub issues into validated, reviewable Terraform/Terragrunt pull requests.

The core value is not "push-button app hosting." The core value is:

> Can I loosely describe the infrastructure I want in natural language and receive a clear, concise, validated Terraform/Terragrunt PR that looks like a senior platform engineer wrote it?

IaC Smith should generate infrastructure-as-code changes that are understandable, supportable, reviewable, and ready to apply after merge.

## High-Level Concept

A user opens a GitHub issue in the `iac-smith` controller repo with a freeform infrastructure request. When the issue is labeled `iac-smith`, a GitHub Actions workflow runs the LangGraph-based agent.

The agent reads the issue, infers the AWS infrastructure intent, scans the fixed target infrastructure repo, generates a complete Terraform/Terragrunt change, validates it, runs plan checks where possible, and opens a pull request against the fixed demo infrastructure repo.

## Project Name

- Repo name: `iac-smith`
- Display name: **IaC Smith**
- Target demo repo: `time4116/iac-smith-demo-infra`

## Primary Workflow

1. User creates a GitHub issue in `time4116/iac-smith`.
2. User writes the infrastructure request in freeform natural language.
3. User applies the label `iac-smith`.
4. GitHub Actions workflow runs.
5. IaC Smith reads the issue body and metadata.
6. IaC Smith scans the target repo `time4116/iac-smith-demo-infra`.
7. IaC Smith infers the requested AWS infrastructure, environment scope, constraints, and assumptions.
8. IaC Smith loads the Terraform/Terragrunt/AWS ruleset.
9. IaC Smith generates a complete infrastructure PR.
10. IaC Smith validates and repairs generated code when needed.
11. IaC Smith runs plan checks where credentials and backend state allow.
12. IaC Smith opens a PR against `time4116/iac-smith-demo-infra`.
13. The PR description explains what was generated, assumptions, validations, plan results, warnings, and next steps.
14. After review and merge, the target repo workflow applies the infrastructure.

## MVP Scope

### In Scope

- AWS only.
- Freeform GitHub issue input.
- GitHub Actions controller workflow.
- AWS Bedrock Claude Sonnet as the default model.
- LangGraph `StateGraph` orchestration.
- Terraform/Terragrunt project generation.
- OpenTofu-compatible implementation where practical.
- Terragrunt as the primary interface for validation, plan, and apply.
- Fixed target demo infra repo: `time4116/iac-smith-demo-infra`.
- Fine-grained GitHub PAT for creating branches and PRs in the target repo.
- AWS GitHub Actions OIDC role as a manual prerequisite.
- Generated target repo workflows for PR checks and post-merge apply.
- Backend/bootstrap generation for remote state.
- S3 state bucket and DynamoDB lock table.
- `non-prod` and `prod` environment model.
- Standardized environment structure with minimal deviations.
- `terraform-aws-modules` preferred where appropriate.
- `tflint` included.
- `terraform-docs` included.
- High-value tests for the controller repo.
- `uv`, `ruff`, and `pytest` for Python project quality.

### Out of Scope for MVP

- Azure or GCP.
- Multi-cloud support.
- Kubernetes workload manifests.
- Full application deployment workflow.
- Dockerfile generation.
- App build standards.
- App framework detection.
- Database migrations.
- DNS/TLS automation.
- Cost estimation.
- Checkov/tfsec/deeper security scanning.
- Local CLI.
- GitHub App auth.
- Pre-commit hooks.
- Manual issue comments after PR creation.
- Auto-apply from the IaC Smith controller repo.

## Important Product Boundary

IaC Smith should not be tied to a single proof-of-concept such as "deploy my app to AWS." The project should focus on infrastructure generation from natural-language requirements.

The MVP may use ECS Fargate or EKS Fargate as a demonstration scenario if useful, but the agent should not be hardcoded to one blueprint. It should infer what to generate from the issue text.

The goal is:

> One natural-language infrastructure request = one complete, reviewable Terraform/Terragrunt PR.

That PR may include one module, multiple modules, live configuration, backend/bootstrap, workflows, documentation, and summaries depending on what the request needs.

## Input Model

The issue format should be freeform. Do not require a rigid issue template.

Example issue:

```md
Create AWS infrastructure for a non-prod EKS Fargate setup in us-west-2. Use a new VPC, private subnets, standard tags, remote state, and basic logging. Generate the Terraform/Terragrunt structure and open a PR.
```

IaC Smith should:
- infer intent from natural language,
- make reasonable defaults where safe,
- document assumptions in the PR,
- avoid blocking unless the request is too risky or impossible to interpret.

README guidance should say:

> The more detail you provide in the issue, the better the generated Terraform PR will be.

## Defaults and Inference Rules

IaC Smith should make reasonable defaults when safe.

Possible defaults:
- AWS-only.
- Default region can be configured, with `us-west-2` as a reasonable default for the demo.
- If environment is unspecified, generate both `non-prod` and `prod` structure.
- If the issue explicitly says `non-prod only`, generate only `non-prod`.
- If the issue explicitly says `prod only`, generate only `prod`.
- Prefer private subnets where appropriate.
- Avoid public access unless explicitly requested or clearly implied.
- Prefer `terraform-aws-modules` where appropriate.
- Use standard tags.
- Use S3 + DynamoDB remote state.
- Use Terragrunt path-based dynamic state keys.
- Use repo-derived names for backend resources.
- Never hardcode one-off backend state keys.
- Never run apply from the IaC Smith controller repo.

## Environment Model

Supported environments for MVP:

- `non-prod`
- `prod`

No `dev`, `stage`, or `qa` sub-environments in MVP.

Infrastructure should be standardized across `non-prod` and `prod`. Avoid environment-specific deviations unless explicitly requested.

Environment differences should normally be limited to:
- environment name/tag,
- backend resource names,
- Terragrunt path/state key,
- values explicitly requested by the issue.

## Backend and State

Use a shared backend per target repo/environment.

Example backend resources for `time4116/iac-smith-demo-infra`:

```text
iac-smith-demo-infra-non-prod-tfstate
iac-smith-demo-infra-non-prod-tflock
iac-smith-demo-infra-prod-tfstate
iac-smith-demo-infra-prod-tflock
```

Backend rules:
- S3 backend for state.
- DynamoDB table for locking.
- S3 bucket versioning enabled.
- S3 encryption enabled.
- S3 public access blocked.
- DynamoDB billing mode should be on-demand.
- Backend resource names should be derived from target repo name and environment.
- Names should be sanitized for AWS limits.
- Backend state keys should be derived from Terragrunt path, not hardcoded.

Terragrunt key pattern:

```hcl
key = "${path_relative_to_include()}/terraform.tfstate"
```

## Bootstrap Behavior

IaC Smith should generate everything the target repo needs.

Because remote state cannot be used until the S3 bucket and DynamoDB table exist, generated output should include backend bootstrap code.

Expected bootstrap pattern:
1. Use local state temporarily for backend bootstrap.
2. Create S3 state bucket and DynamoDB lock table.
3. Configure Terragrunt remote state for generated infrastructure.
4. Use remote state for all main infrastructure stacks.

## Target Repo Generated Structure

The target repo starts completely empty for the MVP.

IaC Smith should generate a complete structure based on the request. It should not be restricted to a single module.

Possible generated structure:

```text
bootstrap/
  backend/
    non-prod/
      main.tf
      variables.tf
      outputs.tf
      README.md
    prod/
      main.tf
      variables.tf
      outputs.tf
      README.md

modules/
  <generated-module-or-stack>/
    main.tf
    variables.tf
    outputs.tf
    versions.tf
    README.md

live/
  terragrunt.hcl
  non-prod/
    terragrunt.hcl
    <generated-stack>/
      terragrunt.hcl
      README.md
  prod/
    terragrunt.hcl
    <generated-stack>/
      terragrunt.hcl
      README.md

.github/
  workflows/
    terraform-pr-check.yml
    terraform-apply.yml
```

The actual generated structure should be determined by the request and ruleset.

## Existing Repo Support

MVP target repo starts empty, but IaC Smith should be designed for future existing-repo support.

Future behavior:
- scan existing structure,
- detect Terraform/Terragrunt conventions,
- follow existing naming/module/environment patterns,
- minimally modify existing code,
- avoid stamping a new structure over an established repo,
- disclose conflicts or assumptions in the PR.

## Generated Workflows in Target Repo

IaC Smith should create target repo workflows if they do not already exist.

If workflows already exist, IaC Smith should inspect them and avoid overwriting blindly. It may:
- leave them unchanged,
- update them minimally if needed,
- explain workflow assumptions in the PR description.

### PR Check Workflow

File:

```text
.github/workflows/terraform-pr-check.yml
```

Runs on pull requests.

Responsibilities:
- checkout,
- configure AWS credentials using GitHub Actions OIDC,
- install Terraform/OpenTofu-compatible tooling as needed,
- install Terragrunt,
- install tflint,
- install terraform-docs,
- run Terragrunt formatting checks,
- run validation,
- run tflint,
- run terraform-docs validation,
- run plan where possible,
- surface validation/plan results.

### Apply Workflow

File:

```text
.github/workflows/terraform-apply.yml
```

Runs after merge to `main`.

Responsibilities:
- apply only the generated/changed live path, not the entire repo,
- use GitHub Actions OIDC,
- run formatting/validation checks,
- run plan,
- run `terragrunt apply -auto-approve`.

The human approval step is PR review and merge. Once approved and merged, the target repo workflow should apply the infrastructure.

IaC Smith itself must never apply infrastructure.

## PR Behavior

IaC Smith opens a branch in the target repo using:

```text
iac-smith/issue-<issue-number>-<short-slug>
```

Example:

```text
iac-smith/issue-12-create-eks-fargate-infra
```

The PR description should include:
- source issue link,
- generated infrastructure summary,
- assumptions/defaults used,
- files created/changed,
- validation results,
- plan status/results,
- warnings or risks,
- target environment(s),
- expected post-merge apply behavior,
- confirmation that IaC Smith did not apply infrastructure.

No issue comment is required for MVP. Opening the PR with a clear description is enough.

## Documentation

IaC Smith should generate README files where they make the Terraform/Terragrunt project supportable.

Good README locations:
- root `README.md` if useful,
- `bootstrap/backend/README.md`,
- `modules/<module>/README.md`,
- `live/non-prod/<stack>/README.md`,
- `live/prod/<stack>/README.md`.

README files should explain:
- what the stack/module creates,
- how Terragrunt wires it together,
- required inputs,
- outputs,
- remote state behavior,
- validation/apply workflow,
- assumptions made from the issue,
- how to safely modify the stack later.

The PR description is for reviewing the generated change. README files are for long-term support.

## terraform-docs

Use `terraform-docs` for generated module reference documentation.

IaC Smith should:
- generate useful README context,
- add terraform-docs markers to module READMEs,
- run terraform-docs in the generated target repo workflows,
- fail PR checks if module docs are missing or outdated.

Marker style:

```md
<!-- BEGIN_TF_DOCS -->
<!-- END_TF_DOCS -->
```

## Ruleset

Rules should live as YAML files in the controller repo, not as `SKILL.md`.

Recommended structure:

```text
rules/
  terraform.yaml
  terragrunt.yaml
  aws.yaml
  security.yaml
  tagging.yaml
  pr_review.yaml
```

### Rule Categories

`rules/terraform.yaml`
- file structure,
- formatting,
- provider pinning,
- module version pinning,
- variables,
- outputs,
- use of community modules,
- OpenTofu compatibility where practical.

`rules/terragrunt.yaml`
- live folder layout,
- remote state pattern,
- dynamic state keys,
- environment config,
- Terragrunt include patterns.

`rules/aws.yaml`
- AWS defaults,
- preferred AWS modules,
- VPC/networking patterns,
- logging,
- IAM,
- CloudWatch,
- S3/DynamoDB backend standards.

`rules/security.yaml`
- no hardcoded secrets,
- least privilege IAM,
- no public access unless requested,
- narrow security group rules,
- avoid exposing sensitive ports.

`rules/tagging.yaml`
- standard tags such as:
  - `Project`,
  - `Environment`,
  - `ManagedBy`,
  - `Owner`,
  - `Repository`.

`rules/pr_review.yaml`
- required PR sections,
- validation result formatting,
- plan result formatting,
- assumption/warning disclosure,
- no-apply confirmation.

## Rule Severity Model

Use a tiered ruleset.

Each rule should have a severity:

```text
error | warning | preference
```

### Error Rules

Error rules block PR creation or trigger repair before PR.

Examples:
- hardcoded secrets or credentials,
- public S3 buckets unless explicitly requested,
- unrestricted `0.0.0.0/0` on SSH/RDP/database ports,
- missing provider/module version constraints,
- invalid formatting,
- failed validation,
- broken plan due to generated code errors,
- missing required variable descriptions,
- hardcoded one-off backend state keys.

### Warning Rules

Warning rules do not block PR creation but must be disclosed in the PR.

Examples:
- region defaulted because none was specified,
- public ALB created because request implied public service,
- new VPC created because no existing network was specified,
- default CIDR ranges used,
- plan skipped or partially run because credentials/backend were unavailable.

### Preference Rules

Preference rules guide generation but do not block.

Examples:
- prefer maintained `terraform-aws-modules`,
- prefer reusable modules plus Terragrunt live config,
- standard tags,
- clear variable names,
- concise outputs,
- minimal generated complexity,
- include README where useful.

If hard-rule violations cannot be repaired after a retry limit, IaC Smith should fail the run and report the reason in the GitHub Action logs.

## Preferred Terraform Modules

Prefer established Terraform AWS community modules when appropriate, especially from `terraform-aws-modules`.

Examples:
- VPC: `terraform-aws-modules/vpc/aws`
- EKS: `terraform-aws-modules/eks/aws`
- ECS: `terraform-aws-modules/ecs/aws`
- ALB: `terraform-aws-modules/alb/aws`
- Security groups: `terraform-aws-modules/security-group/aws`
- RDS: `terraform-aws-modules/rds/aws`

Rules:
- use explicit versions,
- avoid floating latest,
- wrap modules cleanly,
- expose useful variables and outputs,
- avoid hardcoded account-specific values,
- document assumptions.

## IaC Engine and Tooling

Use Terraform/Terragrunt language in README and positioning for familiarity.

Implementation should standardize on OpenTofu-compatible workflows where practical, with Terragrunt as the primary interface.

Primary commands should be Terragrunt-based:
- `terragrunt hclfmt --check`,
- `terragrunt validate`,
- `terragrunt plan`,
- `terragrunt apply`.

Include:
- `tflint`,
- `terraform-docs`.

Exclude from MVP:
- `checkov`,
- `tfsec`,
- pre-commit hooks.

## Controller Repo Tech Stack

- Python
- `uv`
- LangGraph `StateGraph`
- AWS Bedrock Claude Sonnet
- GitHub Actions
- GitHub API
- `ruff`
- `pytest`

## Controller Repo Structure

```text
iac-smith/
  .github/
    workflows/
      ci.yml
      issue-to-pr.yml

  rules/
    terraform.yaml
    terragrunt.yaml
    aws.yaml
    security.yaml
    tagging.yaml
    pr_review.yaml

  src/
    iac_smith/
      __init__.py
      graph.py
      state.py
      nodes/
        issue_intake.py
        repo_scan.py
        intent_parser.py
        ruleset_loader.py
        change_planner.py
        code_generator.py
        static_review.py
        validation_runner.py
        repair_loop.py
        plan_runner.py
        pr_writer.py
      services/
        bedrock.py
        github.py
        shell.py
        git.py
      models/
        intent.py
        rules.py
        validation.py

  tests/
    fixtures/
      issues/
      repos/
      rules/
    test_ruleset.py
    test_intent_parser.py
    test_graph_routing.py
    test_pr_summary.py

  docs/
    PROJECT_BRIEF.md
    RULESET.md
    SETUP.md

  pyproject.toml
  uv.lock
  README.md
```

## LangGraph StateGraph

Use LangGraph `StateGraph` from day one.

Recommended nodes:

1. **Issue Intake**
   - Read GitHub issue text and metadata.
   - Extract issue number, title, body, labels, repository info.

2. **Repo Scan**
   - Clone/inspect target repo.
   - MVP target repo is empty, but keep this node for future existing-repo support.

3. **Intent Parser**
   - Infer AWS infrastructure intent, environment scope, region, resources, constraints, and defaults.

4. **Ruleset Loader**
   - Load YAML rules.
   - Normalize error/warning/preference rules.

5. **Change Planner**
   - Decide project structure, generated files, modules, live paths, backend/bootstrap needs, workflows, and docs.

6. **Code Generator**
   - Generate Terraform/Terragrunt files, workflows, READMEs, terraform-docs markers, and supporting docs.

7. **Static Review**
   - Check generated output against hard rules and preference rules before running shell validation.

8. **Validation Runner**
   - Run formatting, validation, tflint, terraform-docs checks, and plan where possible.

9. **Repair Loop**
   - Feed errors back into the generation/revision step.
   - Retry within a fixed limit.
   - Fail if unrecoverable.

10. **Plan Runner**
   - Run Terragrunt plan where credentials/backend allow.
   - Capture result for PR description.

11. **PR Writer**
   - Create branch in target repo.
   - Commit generated files.
   - Open PR.
   - Write concise PR body with results and assumptions.

## Authentication and Secrets

### GitHub

MVP uses a fine-grained GitHub PAT stored in GitHub Actions secrets.

The PAT should be scoped only to:

```text
time4116/iac-smith-demo-infra
```

Needed permissions:
- read repo,
- create branch,
- commit files,
- open PR.

Future version can move to GitHub App auth.

### AWS

AWS GitHub Actions OIDC/IAM role setup is a manual prerequisite for MVP.

Docs should include:
- AWS IAM role for GitHub Actions OIDC,
- allowed repo/branch conditions,
- permissions needed for plan/apply,
- GitHub variable for role ARN,
- GitHub variable for AWS region.

IaC Smith does not generate/manage the AWS OIDC setup in v1.

## GitHub Actions

### Controller Repo CI

File:

```text
.github/workflows/ci.yml
```

Runs on PRs to the controller repo.

Commands:
```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

### Controller Issue-to-PR Workflow

File:

```text
.github/workflows/issue-to-pr.yml
```

Triggered when issue label `iac-smith` is applied.

Responsibilities:
- checkout controller repo,
- install `uv`,
- install dependencies,
- configure AWS credentials for Bedrock access,
- read issue payload,
- run IaC Smith workflow,
- open PR in target demo repo.

## Testing Strategy

Include high-quality tests where they add value.

Test:
- ruleset loading and validation,
- severity handling,
- freeform issue parsing into structured intent,
- StateGraph routing decisions,
- generated file plan creation,
- PR summary generation,
- validation result parsing,
- repair-loop retry limits,
- branch/PR naming logic.

Avoid low-value tests like asserting exact LLM-generated Terraform output.

Use:
- `pytest`,
- mocked Bedrock responses,
- fixture issue files,
- fixture target repos,
- fixture rules,
- golden snapshots only for stable outputs like PR summaries or parsed intent.

## Suggested README Positioning

```md
# IaC Smith

IaC Smith is an AWS-focused agentic IaC workflow that turns freeform GitHub issues into validated Terraform/Terragrunt pull requests.

It uses AWS Bedrock, Claude Sonnet, and LangGraph to infer infrastructure intent, apply an opinionated ruleset, generate Terraform/Terragrunt projects, validate the output, run plan checks, and open a reviewable PR against a target infrastructure repository.

The goal is not to blindly apply infrastructure. The goal is to turn natural-language infrastructure requests into clear, supportable, validated IaC changes that can be reviewed, merged, and applied through normal GitOps-style workflows.
```

## Implementation Notes for Next Agent

Ask clarifying questions one by one if additional naming, structure, workflow, permission, or scope decisions are needed.

Do not ask multiple unrelated questions at once.

Use the decisions in this brief as the source of truth unless the user changes them.

Prioritize a clean MVP that demonstrates:
- freeform issue input,
- LangGraph StateGraph orchestration,
- AWS Bedrock Claude Sonnet,
- ruleset-driven Terraform/Terragrunt generation,
- backend/bootstrap generation,
- generated workflows,
- validation/plan,
- target repo PR creation,
- post-merge apply workflow.

Avoid expanding scope into app deployment, multi-cloud, local CLI, or advanced security scanning unless explicitly requested.
