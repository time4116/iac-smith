# IaC Smith

IaC Smith is an AWS-focused agentic IaC workflow that turns freeform GitHub issues into validated Terraform/Terragrunt pull requests.

## Current status

IaC Smith is currently a hardened controller scaffold. Full issue-to-PR execution is not implemented yet.

Implemented pieces include the Python project structure, LangGraph routing scaffold, ruleset loading, intent parsing for narrow MVP request families, change planning, static review guardrails, PR summary generation, hardened GitHub Actions, setup documentation, and regression tests.

The remaining implementation work is the real execution path: issue fetch, target repo clone/scan, Bedrock integration, Terraform/Terragrunt code generation, validation/repair loops, branch creation, commit creation, and PR creation.

It uses AWS Bedrock, Claude Sonnet, and LangGraph to infer infrastructure intent, apply an opinionated ruleset, generate Terraform/Terragrunt projects, validate the output, run plan checks where possible, and open a reviewable PR against a target infrastructure repository.

The goal is not to blindly apply infrastructure. The goal is to turn natural-language infrastructure requests into clear, supportable, validated IaC changes that can be reviewed, merged, and applied through normal GitOps-style workflows.

## MVP boundary

IaC Smith v1 is intentionally narrow. It supports AWS infrastructure generation for a small set of safe request families and refuses unsupported or risky requests rather than hallucinating Terraform.

Supported MVP request families:

1. Baseline Terraform/Terragrunt repo with backend bootstrap
2. VPC foundation
3. EKS Fargate foundation
4. ECS Fargate foundation if enabled later

IaC Smith never applies infrastructure from the controller repo.
