import os
from typing import Dict, Any, List
from iac_smith.models.intent import IntentExtraction
from iac_smith.models.rules import ValidationResult

class IaCGenerator:
    def __init__(self, target_repo_path: str):
        self.target_repo_path = target_repo_path

    def generate(self, extraction: IntentExtraction, plan: Dict[str, Any]) -> List[Dict[str, str]]:
        files = []
        
        # 1. Generate Terraform/Terragrunt files based on the plan
        for file_path, content in plan.get("files_to_create", {}).items():
            files.append({
                "path": file_path,
                "content": self._apply_secure_defaults(file_path, content)
            })

        # 2. Add/Update Workflows with non-stomping targeting logic
        files.append({
            "path": ".github/workflows/terraform-pr-check.yml",
            "content": self._get_pr_check_workflow()
        })
        files.append({
            "path": ".github/workflows/terraform-apply.yml",
            "content": self._get_apply_workflow()
        })
        
        return files

    def _apply_secure_defaults(self, path: str, content: str) -> str:
        # Avoid dangerous defaults even if Bedrock suggested them
        if path.endswith(".tf") or path.endswith(".hcl"):
            if "public_access" in content:
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

jobs:
  plan:
    name: Plan (${{ matrix.env }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        # Targeted environments. 
        # In a more advanced version, we parse the live/ directory dynamically.
        env: [dev, staging, prod]
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Check for changes in ${{ matrix.env }}
        id: filter
        uses: dorny/paths-filter@v3
        with:
          filters: |
            changed:
              - 'live/${{ matrix.env }}/**'
              - 'modules/**'

      - name: Setup Terraform
        if: steps.filter.outputs.changed == 'true'
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.9.0

      - name: Setup Terragrunt
        if: steps.filter.outputs.changed == 'true'
        uses: autero1/action-terragrunt@v3
        with:
          terragrunt-version: 0.58.0

      - name: Configure AWS Credentials
        if: steps.filter.outputs.changed == 'true'
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: us-west-2

      - name: Terragrunt Plan
        if: steps.filter.outputs.changed == 'true'
        working-directory: live/${{ matrix.env }}
        run: |
          # Use run-all within the env directory to handle local dependencies
          # -lock-timeout handles short contention between parallel jobs
          terragrunt run-all plan --terragrunt-non-interactive -lock-timeout=20m
"""

    def _get_apply_workflow(self) -> str:
        return """name: Terraform Apply
on:
  push:
    branches:
      - main
    paths:
      - 'live/**'
      - 'modules/**'

permissions:
  id-token: write
  contents: read

jobs:
  apply:
    name: Apply (${{ matrix.env }})
    runs-on: ubuntu-latest
    # Sequential apply to prevent race conditions during infrastructure creation
    # But environment-isolated so dev doesn't wait for prod approval
    strategy:
      max-parallel: 1 
      matrix:
        env: [dev, staging, prod]
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Check for changes
        id: filter
        uses: dorny/paths-filter@v3
        with:
          filters: |
            changed:
              - 'live/${{ matrix.env }}/**'
              - 'modules/**'

      - name: Setup Terraform
        if: steps.filter.outputs.changed == 'true'
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: 1.9.0

      - name: Setup Terragrunt
        if: steps.filter.outputs.changed == 'true'
        uses: autero1/action-terragrunt@v3
        with:
          terragrunt-version: 0.58.0

      - name: Configure AWS Credentials
        if: steps.filter.outputs.changed == 'true'
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: us-west-2

      - name: Terragrunt Apply
        if: steps.filter.outputs.changed == 'true'
        working-directory: live/${{ matrix.env }}
        run: |
          terragrunt run-all apply --terragrunt-non-interactive -lock-timeout=20m
"""
