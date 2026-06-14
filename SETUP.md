# Setup

This is the short setup index for IaC Smith. The detailed setup guide is in [docs/SETUP.md](docs/SETUP.md).

## Current project status

IaC Smith is currently a hardened controller scaffold. Full issue-to-PR execution is not implemented yet.

## Required GitHub configuration

1. `IAC_SMITH_TARGET_REPO_PAT`

   Fine-grained PAT scoped to the fixed target infrastructure repo, currently `time4116/iac-smith-demo-infra`.

2. `AWS_BEDROCK_ROLE_ARN`

   GitHub Actions variable containing the controller AWS OIDC role ARN used for Bedrock access.

3. `AWS_REGION`

   GitHub Actions variable for the Bedrock/controller region. The demo default is `us-west-2`.

4. `IAC_SMITH_ALLOWED_TARGET_REPO`

   Controller allowlist value. The workflow sets this to `time4116/iac-smith-demo-infra`; the CLI fails closed unless `IAC_SMITH_TARGET_REPO` matches it exactly.

## Safety boundary

The controller repo may generate and open Terraform/Terragrunt pull requests. It must not apply infrastructure. Post-merge apply belongs to the target infrastructure repo workflow after human PR review.
