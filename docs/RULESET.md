# Ruleset

Rules live in YAML under `rules/` and use three severities:

* `error`: blocks PR creation or triggers repair before PR.
* `warning`: allows PR creation but must be disclosed.
* `preference`: guides generation without blocking.

The first implementation enforces deterministic loading and severity validation. Static Terraform enforcement will be added behind the existing model and tests.
