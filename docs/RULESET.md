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
* generated Terraform must not include clearly dangerous public ingress for administrative or database ports;
* generated module READMEs should include terraform-docs markers;
* generated module files must not reference `module.x` unless that module is declared in the same generated module.

Ruleset text remains prompt guidance for broader design choices, but prompt-only enforcement is not enough for safety-critical rules. Add a deterministic static review or validation test whenever a generation failure can be recognized mechanically.
