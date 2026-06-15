# Setup

This is the short setup index for IaC Smith. The detailed setup guide is in [docs/SETUP.md](docs/SETUP.md).

## Current project status

IaC Smith's Bedrock-backed issue-to-PR MVP path is implemented. `BEDROCK_MODEL_ID` must be supplied by GitHub Actions secret or local environment configuration.

## Required GitHub configuration

1. `IAC_SMITH_TARGET_REPO_PAT`

   Fine-grained PAT scoped to the fixed target infrastructure repo, currently `time4116/iac-smith-demo-infra`.

2. `BEDROCK_MODEL_ID`

   Bedrock model ID or inference profile ARN. Store this as a repository secret or environment secret. Do not commit model IDs, account IDs, or account-specific Bedrock ARNs.

3. `AWS_BEDROCK_ROLE_ARN`

   GitHub Actions variable containing the controller AWS OIDC role ARN used for Bedrock access.

4. `AWS_REGION`

   GitHub Actions variable for the Bedrock/controller region. The demo default is `us-west-2`.

5. `IAC_SMITH_ALLOWED_TARGET_REPO`

   Controller allowlist value. The workflow sets this to `time4116/iac-smith-demo-infra`; the CLI fails closed unless `IAC_SMITH_TARGET_REPO` matches it exactly.

## Safety boundary

The controller repo may generate and open Terraform/Terragrunt pull requests. It must not apply infrastructure. Post-merge apply belongs to the target infrastructure repo workflow after human PR review.
