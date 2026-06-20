# Ruleset

Rules live in YAML under `rules/` and use three severities:

* `error`: blocks PR creation or triggers repair before PR.
* `warning`: allows PR creation but must be disclosed.
* `preference`: guides generation without blocking.

Rules are loaded before Terraform generation and are included in the Bedrock generation prompt. They are also backed by deterministic post-generation checks where the rule can be evaluated safely without cloud access.

Current hard checks include:

* generated paths must stay inside the planned change set;
* generated paths must be relative and safe;
* every planned file must be present in the model response;
* generated Terragrunt remote state keys must not be fixed shared values;
* generated Terraform should flag clearly dangerous public ingress for administrative or database ports in PR review warnings;
* a literal value assigned to a secret-named field (e.g. `WEBUI_SECRET_KEY = "change-me"`) is flagged as a warning — application secrets should come from `random_password`, Secrets Manager/SSM, or a `sensitive` required variable, never a hardcoded literal;
* generated module READMEs should include terraform-docs markers;
* generated module files must not reference `module.x` unless that module is declared in the same generated module;
* the same AWS provider-level resource `name` must not be declared for the same resource type in two modules — this is an apply-time collision (e.g. an ALB or ECS-tasks security group duplicated across `foundation` and a workload module) that Terraform's per-module validation cannot catch, so it blocks PR creation;
* the generated apply workflow must trigger only on push to `main`, gate apply behind a manual-approval `environment:`, and scope the run to the components that changed;
* structural checks — duplicate declarations across a module's files, undeclared `var.`/`module.` references, missing required Terragrunt inputs (a no-default module variable the stack never passes), dependency-output mismatches, and dangling cross-stack dependencies (a `dependency` block or `dependency.<name>.outputs.*` reference whose target stack is neither created by this change nor already present in the repo) — are surfaced for review and fed to the bounded repair loop rather than blocking; the real `terraform`/`terragrunt` validation is the correctness gate.

Ruleset text remains prompt guidance for broader design choices, but prompt-only enforcement is not enough for safety-critical rules. Add a deterministic static review or validation test whenever a generation failure can be recognized mechanically.
