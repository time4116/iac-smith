# Setup

IaC Smith requires two repositories:

1. Controller repo: `time4116/iac-smith`
2. Target demo infra repo: `time4116/iac-smith-demo-infra`

IaC Smith never applies infrastructure from the controller repo.

## GitHub Actions prerequisites

Configure these in the controller repo:

* Secret `IAC_SMITH_TARGET_REPO_PAT`: fine-grained PAT scoped only to `time4116/iac-smith-demo-infra` with contents and pull request write permissions.
* Secret `BEDROCK_MODEL_ID`: Bedrock model ID or inference profile ARN. Do not hardcode this in source.
* Variable `AWS_BEDROCK_ROLE_ARN`: IAM role ARN used by the controller workflow to call Bedrock.
* Variable `AWS_REGION`: optional, defaults to `us-west-2`.

The workflow only runs when the `iac-smith` label is applied by `time4116`. If ownership changes, update `.github/workflows/issue-to-pr.yml` deliberately instead of broadening this check to all users.

Use a project-specific runtime environment variable for the target repo PAT:

```text
IAC_SMITH_TARGET_REPO_TOKEN
```

Do not expose the target repo PAT as a generic `GITHUB_TOKEN` unless a specific library requires that name.

## Fine-grained PAT scope

The target repo PAT should be restricted to this repository only:

```text
time4116/iac-smith-demo-infra
```

Required permissions:

* Contents: read and write
* Pull requests: read and write
* Metadata: read

Do not grant organization-wide access. Do not reuse a personal all-repos token.

## Bedrock setup

Bedrock is required for the MVP intent parser and the default dynamic Terraform/Terragrunt generator.

The controller sends Bedrock structured issue intent, the planned file set, loaded rules, repo-scanned conventions, and bounded representative Terraform/Terragrunt snippets from the target repo. Generation must follow existing repo patterns unless the issue explicitly asks not to, and each generated file is rejected if it returns a path outside the plan. Generated files are statically reviewed immediately; on hard failures, IaC Smith sends the specific review errors back to Bedrock for one bounded repair attempt before moving to sibling files.

Create an IAM role trusted by GitHub Actions OIDC. Restrict the trust policy to this controller repo and the `main` branch.

Example trust policy shape:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:time4116/iac-smith:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

If you later allow pull request workflows or other branches, add explicit entries. Do not use `repo:time4116/iac-smith:*` unless you intentionally accept broader access.

## Minimum Bedrock policy

Scope Bedrock permissions to the configured model ARN when possible. Avoid `Resource: "*"` for production use.

Example policy shape:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": "arn:aws:bedrock:us-west-2:<account-id>:inference-profile/<model-or-profile-id>"
    }
  ]
}
```

For early local testing, a broader Bedrock resource may be temporarily easier, but tighten it before treating the repo as production-like.

## Target repo safety boundary

The controller workflow sets:

```text
IAC_SMITH_ALLOWED_TARGET_REPO=time4116/iac-smith-demo-infra
```

The CLI should fail closed if `IAC_SMITH_TARGET_REPO` does not exactly match that allowlist value.

## Local development

Run checks locally with locked dependencies:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
