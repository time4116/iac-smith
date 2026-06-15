from typing import Any, dict, list

from iac_smith.models.intent import IntentExtraction


class IaCGenerator:
    def __init__(self, target_repo_path: str):
        self.target_repo_path = target_repo_path

    def generate(self, extraction: IntentExtraction, plan: dict[str, Any]) -> list[dict[str, str]]:
        files = []

        # 1. Generate core IaC files from plan
        for file_path, content in plan.get("files_to_create", {}).items():
            files.append(
                {"path": file_path, "content": self._apply_secure_defaults(file_path, content)}
            )

        # 2. Add Local State Fallback helper (Lesson from PR 4)
        files.append(
            {
                "path": "local_state.hcl",
                "content": (
                    'remote_state { backend = "local" config = { path = "terraform.tfstate" } }'
                ),
            }
        )

        # 3. Add Matrix-based non-stomping workflows
        files.append(
            {
                "path": ".github/workflows/terraform-pr-check.yml",
                "content": self._get_pr_check_workflow(),
            }
        )

        return files

    def _apply_secure_defaults(self, path: str, content: str) -> str:
        if (path.endswith(".tf") or path.endswith(".hcl")) and "public_access" in content:
            content = content.replace("public_access = true", "public_access = false")
        return content

    def _get_pr_check_workflow(self) -> str:
        return """name: Terraform PR Check
on:
  pull_request:
    paths:
      - 'live/**'
      - 'modules/**'

permissions:
  id-token: write
  contents: read
  pull-requests: read

jobs:
  plan:
    name: Plan (non-prod)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Check for changes
        id: filter
        uses: dorny/paths-filter@v3
        with:
          filters: |
            changed:
              - 'live/non-prod/**'
              - 'modules/**'
      
      - name: Setup Terragrunt
        if: steps.filter.outputs.changed == 'true'
        uses: autero1/action-terragrunt@v3
        with:
          terragrunt-version: 0.58.0
      
      - name: Setup Terragrunt
        if: steps.filter.outputs.changed == 'true'
        uses: autero1/action-terragrunt@v3
        with:
          terragrunt-version: 0.58.0

      - name: Configure AWS Credentials
        if: steps.filter.outputs.changed == 'true'
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN_NON_PROD }}
          aws-region: us-west-2
          audience: sts.amazonaws.com

      - name: Terragrunt Plan
        if: steps.filter.outputs.changed == 'true'
        working-directory: live/non-prod
        run: terragrunt run-all plan --terragrunt-non-interactive -lock-timeout=20m
"""
