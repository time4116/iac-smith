import shutil
import subprocess
from pathlib import Path

import pytest

from iac_smith.dynamic_terraform import _FOUNDATION_MODULE_FILES


@pytest.mark.need_binaries
def test_deterministic_foundation_passes_terraform_validate(tmp_path: Path) -> None:
    """The deterministic foundation must validate against the *real*
    `terraform-aws-modules/vpc` module.

    `terraform init` downloads the module and the AWS provider; `terraform
    validate` then checks the `module "vpc"` call against the module's actual
    input/output schema — so a renamed input or a wrong `module.vpc.<output>`
    reference fails here, in our CI, instead of only at the user's live run.
    Needs terraform + network but no AWS credentials (validate does not call AWS).
    """
    if shutil.which("terraform") is None:
        pytest.skip("terraform not on PATH")

    for rel_path, content in _FOUNDATION_MODULE_FILES.items():
        dest = tmp_path / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    mod_dir = tmp_path / "modules" / "foundation"

    init = subprocess.run(
        ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=mod_dir,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert init.returncode == 0, init.stdout + init.stderr

    validate = subprocess.run(
        ["terraform", "validate", "-no-color"],
        cwd=mod_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert validate.returncode == 0, validate.stdout + validate.stderr
