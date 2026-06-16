"""Detect and install required Terraform/Terragrunt versions for a target repo.

Greenfield repos (no version files, no existing infra) get the latest stable
release. Existing repos get the version pinned in their `.terraform-version`
or `.terragrunt-version` file. Downloaded binaries go to a workspace temp
dir and are prepended to PATH so downstream validation picks them up.
"""

import io
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def _arch_suffix() -> str:
    arch = platform.machine()
    if arch == "x86_64":
        return "amd64"
    if arch in ("aarch64", "arm64"):
        return "arm64"
    return arch


def _os_name() -> str:
    return platform.system().lower()


def _github_api(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "iac-smith/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5**attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def _latest_release_tag(owner: str, repo: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    data = _github_api(url)
    return data["tag_name"]


def _version_from_tag(tag: str) -> str:
    return tag.lstrip("v")


def _read_version_file(repo_path: Path, filename: str) -> str | None:
    path = repo_path / filename
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return content
    return None


def _installed_version(command: str) -> str | None:
    """Return the semver version string of an installed command, or None."""
    exe = shutil.which(command)
    if not exe:
        return None
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        # terraform --version: "Terraform v1.10.0"
        # terragrunt --version: "terragrunt version v0.71.1"
        parts = result.stdout.strip().split()
        for part in parts:
            v = part.strip("v,")
            if v[0].isdigit() and "." in v:
                return v
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _download_to(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "iac-smith/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _install_terraform(version: str, bin_dir: Path) -> Path:
    """Download terraform zip, extract binary to bin_dir."""
    dest = bin_dir / "terraform"
    if dest.exists():
        return dest
    os_name = _os_name()
    arch = _arch_suffix()
    url = (
        f"https://releases.hashicorp.com/terraform/{version}/"
        f"terraform_{version}_{os_name}_{arch}.zip"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "iac-smith/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        zip_data = resp.read()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        zf.extract("terraform", path=bin_dir)
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _install_terragrunt(version: str, bin_dir: Path) -> Path:
    """Download terragrunt binary to bin_dir."""
    dest = bin_dir / "terragrunt"
    if dest.exists():
        return dest
    os_name = _os_name()
    arch = _arch_suffix()
    # terragrunt releases use format: terragrunt_linux_amd64
    url = (
        f"https://github.com/gruntwork-io/terragrunt/releases/download/v{version}/"
        f"terragrunt_{os_name}_{arch}"
    )
    _download_to(url, dest)
    return dest


def _resolve_tf_version(repo_path: Path, bin_dir: Path) -> bool:
    """Ensure the correct terraform version is available. Returns True if installed to bin_dir."""
    version_file = _read_version_file(repo_path, ".terraform-version")
    if version_file:
        target = _version_from_tag(version_file)
    else:
        # Greenfield: check if already installed
        installed = _installed_version("terraform")
        if installed:
            return False  # already available
        latest_tag = _latest_release_tag("hashicorp", "terraform")
        target = _version_from_tag(latest_tag)

    installed = _installed_version("terraform")
    if installed and installed == target:
        return False  # correct version already on PATH

    _install_terraform(target, bin_dir)
    return True


def _resolve_tg_version(repo_path: Path, bin_dir: Path) -> bool:
    """Ensure the correct terragrunt version is available. Returns True if installed to bin_dir."""
    version_file = _read_version_file(repo_path, ".terragrunt-version")
    if version_file:
        target = _version_from_tag(version_file)
    else:
        installed = _installed_version("terragrunt")
        if installed:
            return False
        latest_tag = _latest_release_tag("gruntwork-io", "terragrunt")
        target = _version_from_tag(latest_tag)

    installed = _installed_version("terragrunt")
    if installed and installed == target:
        return False

    _install_terragrunt(target, bin_dir)
    return True


def ensure_terraform_terragrunt(repo_path: str | Path) -> dict[str, str]:
    """Ensure terraform and terragrunt are on PATH at correct versions.

    Checks the target repo for ``.terraform-version`` / ``.terragrunt-version``
    files. If found, ensures matching binaries are available. If not found
    (greenfield) and nothing is on PATH yet, installs the latest stable release.

    Returns an env dict with an updated ``PATH`` to pass to subprocess calls.
    Callers should merge this into their subprocess environment before running
    any terraform/terragrunt commands.
    """
    repo_root = Path(repo_path)
    bin_dir = Path(tempfile.mkdtemp(prefix="iac-smith-bins-"))
    installed_any = False

    installed_any |= _resolve_tf_version(repo_root, bin_dir)
    installed_any |= _resolve_tg_version(repo_root, bin_dir)

    env = dict(os.environ)
    if installed_any:
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    else:
        shutil.rmtree(bin_dir, ignore_errors=True)

    return env
