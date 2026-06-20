# Setup

IaC Smith requires two repositories:

1. Controller repo: `time4116/iac-smith`
2. Target demo infra repo: `time4116/iac-smith-demo-infra`

IaC Smith never applies infrastructure from the controller repo.

## GitHub Actions prerequisites

Configure these in the controller repo:

* Secret `IAC_SMITH_TARGET_REPO_PAT`: fine-grained PAT scoped only to `time4116/iac-smith-demo-infra` with contents and pull request write permissions.
* Secret `BEDROCK_MODEL_ID`: Bedrock model ID or inference profile ARN. Do not hardcode this in source.
* Secret `AWS_ROLE_ARN_NON_PROD`: non-production IAM role ARN used by the controller workflow to call Bedrock and by generated non-prod validation workflows.
* Secret `AWS_ROLE_ARN_PROD`: production IAM role ARN used by generated production validation workflows.
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

Create IAM roles trusted by GitHub Actions OIDC and store their ARNs as `AWS_ROLE_ARN_NON_PROD` and `AWS_ROLE_ARN_PROD`. Restrict the controller trust policy to this controller repo and the `main` branch.

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

## Target repo apply workflow

The controller only generates and opens PRs; the generated `.github/workflows/terraform-apply.yml` runs in the **target** repo after a PR is merged to `main`. For it to work — and to be safe to make public — configure the target repo (`time4116/iac-smith-demo-infra`):

1. **Approval gate (GitHub Environment).** The apply workflow gates every AWS-mutating job behind `environment: <env>` (e.g. `non-prod`). The workflow only *references* the environment by name; the protection is a repo setting. In the target repo, go to **Settings → Environments**, create an environment matching the generated name (e.g. `non-prod`), and add **Required reviewers**. Until you do this, GitHub auto-creates the environment with no protection on first run, so a merge would apply without sign-off. The gate becomes enforceable once the repo can use environment protection rules (public repos, or private repos on a plan that includes them).

2. **OIDC role + secret for applying.** The apply workflow assumes `${{ secrets.AWS_ROLE_ARN_NON_PROD }}` via GitHub Actions OIDC. Add that secret to the target repo and create/extend an IAM role whose trust policy allows the target repo's OIDC subject (`repo:time4116/iac-smith-demo-infra:ref:refs/heads/main`), with the permissions needed to apply the generated infrastructure. This is separate from the controller's Bedrock role.

Making the target repo public exposes the generated Terraform/Terragrunt and the backend resource names it hardcodes (state bucket and lock-table names); confirm those contain nothing sensitive before flipping visibility. Secrets are never in the repo — they are referenced as `${{ secrets.* }}` and stored in repo settings.

## Local development

Run checks locally with locked dependencies:

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
