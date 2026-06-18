# Terraform / Terragrunt layout (greenfield)

This document describes the canonical layout IaC Smith generates when starting from an empty or near-empty target repository. It covers the directory structure, the rules that govern it, and how to extend the repo with additional stacks over time.

IaC Smith is designed to scan and adapt to existing Terraform/Terragrunt conventions in the target repository before generating anything — but that path has not been tested against real-world existing infrastructure. Treat this layout as the guaranteed baseline for greenfield projects.

## Stacks

A **stack** is one independently deployable unit of infrastructure — a named group of AWS resources that is planned and applied together and owns its own Terraform state. IaC Smith derives the stack name from the issue as a short `snake_case` or `kebab-case` label.

Examples:

| Issue request | Stack name |
|---|---|
| ECS Fargate cluster with ALB | `ecs-fargate-stack` |
| RDS PostgreSQL database | `rds-postgres` |
| S3 bucket with CloudFront | `s3-cloudfront` |
| EKS cluster | `eks-fargate` |
| Baseline account guardrails | `baseline` |

Each stack lives at `environments/<env>/<stack-name>/` and maps to a reusable Terraform module at `modules/<stack-name>/`. A repository can contain multiple stacks — each gets its own live path and its own isolated Terraform state file in S3.

## Directory structure

```
<repo-root>/
├── bootstrap/
│   └── backend/
│       └── <env>/               # one directory per environment
│           ├── main.tf          # S3 bucket + DynamoDB table (idempotent)
│           ├── variables.tf
│           ├── outputs.tf
│           └── README.md
├── environments/
│   ├── terragrunt.hcl           # root config: remote_state, shared locals
│   └── <env>/                   # e.g. non-prod, prod
│       ├── terragrunt.hcl       # environment config: includes root, sets env locals
│       ├── foundation/          # present when a VPC/networking layer is needed
│       │   ├── terragrunt.hcl
│       │   └── README.md
│       └── <stack-name>/        # the requested infrastructure stack
│           ├── terragrunt.hcl
│           └── README.md
└── modules/
    ├── foundation/              # shared VPC/networking module (reused across stacks)
    │   ├── main.tf
    │   ├── variables.tf
    │   ├── outputs.tf
    │   ├── versions.tf
    │   └── README.md
    └── <stack-name>/            # reusable Terraform module for this stack
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        ├── versions.tf
        └── README.md
```

Generated CI workflows are placed at `.github/workflows/terraform-pr-check.yml` and `.github/workflows/terraform-apply.yml`.

## File responsibilities

Each file in a module has a fixed responsibility. IaC Smith enforces these with static review checks.

| File | Contains |
|---|---|
| `main.tf` | Resources and data sources only — no `terraform {}` block, no `variable`, no `output` |
| `variables.tf` | All `variable` declarations |
| `outputs.tf` | All `output` declarations |
| `versions.tf` | The sole `terraform { required_providers {} }` block — never in `main.tf` |

## Terragrunt hierarchy

The three-level hierarchy keeps remote state config DRY while giving each stack isolated state:

1. **`environments/terragrunt.hcl`** — defines `remote_state` once for all environments. The state key uses `path_relative_to_include()`, which resolves to the directory path of the calling stack relative to this file (e.g. `non-prod/ecs-fargate-stack/terraform.tfstate`). Every stack therefore gets its own isolated state file in S3 automatically — no manual key management and no risk of one stack's plan touching another's state.
2. **`environments/<env>/terragrunt.hcl`** — includes the root config and sets environment locals (`env`, `region`, `account_id`).
3. **`environments/<env>/<stack>/terragrunt.hcl`** — declares the module source (relative path into `modules/`), dependency blocks, and input variable bindings.

Because state is isolated per stack, a `terragrunt plan` or `apply` in `environments/non-prod/ecs-fargate-stack/` only reads and writes state for that stack. The foundation stack and any future stacks each have their own state files and can be planned or applied independently.

## Backend resource naming

IaC Smith derives backend resource names from the environment and target repository slug:

| Resource | Name pattern |
|---|---|
| S3 state bucket | `iac-smith-state-<env>-<repo-slug>` |
| DynamoDB lock table | `iac-smith-lock-<env>` |

The bootstrap Terraform under `bootstrap/backend/<env>/` creates these resources. It is designed to be applied once and is idempotent.

## Inter-stack dependencies

When one stack consumes outputs from another (e.g. `ecs-fargate-stack` reading VPC IDs from `foundation`), the consuming `terragrunt.hcl` uses a `dependency` block:

```hcl
dependency "foundation" {
  config_path = "../foundation"
  mock_outputs = {
    vpc_id             = "vpc-00000000"
    private_subnet_ids = ["subnet-00000000", "subnet-11111111"]
  }
}

inputs = {
  vpc_id             = dependency.foundation.outputs.vpc_id
  private_subnet_ids = dependency.foundation.outputs.private_subnet_ids
}
```

`mock_outputs` are required so that `terragrunt plan` works in CI before the dependency stack has been applied. Never use `module.<name>.output` syntax in Terragrunt configs — that syntax is only valid inside a Terraform module.

## Foundation module

The `foundation` module is generated whenever a stack requires a VPC and the repository does not already contain one. It provides VPC, public/private subnets, NAT gateway, route tables, and internet gateway — everything a compute or data stack needs as inputs.

If a `modules/foundation` directory already exists in the target repository, IaC Smith wires the new stack to the existing foundation rather than generating a second one.

## Adding a new stack to an existing repo

Create a new GitHub issue in the controller repository labeled `iac-smith`. IaC Smith scans the existing repository before generating anything, so it will:

- Reuse `modules/foundation` if it exists rather than regenerating it
- Add a new `environments/<env>/<new-stack>/` live path wired to the existing environment `terragrunt.hcl`
- Generate `modules/<new-stack>/` only if the module does not already exist

Follow-on PRs are fully additive — they do not modify existing module code unless the issue explicitly requests a change to an existing resource.
