"""Tests for version detection logic."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from iac_smith.version_detection import (
    _installed_version,
    _latest_release_tag,
    _read_version_file,
    _version_from_tag,
    ensure_terraform_terragrunt,
)


class TestVersionFromTag:
    def test_strips_v_prefix(self) -> None:
        assert _version_from_tag("v1.10.0") == "1.10.0"

    def test_no_prefix(self) -> None:
        assert _version_from_tag("1.10.0") == "1.10.0"


class TestReadVersionFile:
    def test_file_exists(self, tmp_path: Path) -> None:
        f = tmp_path / ".terraform-version"
        f.write_text("1.10.0\n")
        assert _read_version_file(tmp_path, ".terraform-version") == "1.10.0"

    def test_no_file(self, tmp_path: Path) -> None:
        assert _read_version_file(tmp_path, ".terragrunt-version") is None

    def test_blank_file(self, tmp_path: Path) -> None:
        f = tmp_path / ".terraform-version"
        f.write_text("  \n")
        assert _read_version_file(tmp_path, ".terraform-version") is None


class TestInstalledVersion:
    def test_python_is_found(self) -> None:
        v = _installed_version("python3")
        assert v is not None
        parts = v.split(".")
        assert len(parts) >= 2
        assert all(p.isdigit() for p in parts[:2])

    def test_nonexistent_returns_none(self) -> None:
        assert _installed_version("this-command-does-not-exist-99999") is None


class TestLatestReleaseTag:
    def test_terraform_latest(self) -> None:
        tag = _latest_release_tag("hashicorp", "terraform")
        assert tag.startswith("v")
        v = _version_from_tag(tag)
        parts = v.split(".")
        assert len(parts) >= 2
        assert all(p.isdigit() for p in parts[:2])

    def test_terragrunt_latest(self) -> None:
        tag = _latest_release_tag("gruntwork-io", "terragrunt")
        assert tag.startswith("v")
        v = _version_from_tag(tag)
        parts = v.split(".")
        assert len(parts) >= 2
        assert all(p.isdigit() for p in parts[:2])


class TestEnsureTerraformTerragrunt:
    def test_greenfield_no_existing_bins_stubs_bin_dir(self, tmp_path: Path) -> None:
        """Greenfield repo with no binaries on PATH — bins go to temp dir."""
        repo = tmp_path / "target"
        repo.mkdir()
        env = ensure_terraform_terragrunt(repo)
        assert "PATH" in env
        bin_dir = env["PATH"].split(":")[0]
        terraform = Path(bin_dir) / "terraform"
        terragrunt = Path(bin_dir) / "terragrunt"
        assert terraform.exists()
        assert terragrunt.exists()
        # Verify they actually run
        import subprocess

        r = subprocess.run([str(terraform), "--version"], capture_output=True, text=True, env=env)
        assert r.returncode == 0
        r2 = subprocess.run([str(terragrunt), "--version"], capture_output=True, text=True, env=env)
        assert r2.returncode == 0

    @pytest.mark.need_binaries
    def test_greenfield_existing_bins_uses_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If terraform/terragrunt are already on PATH, no downloads needed."""
        # Save original PATH and restore after test to avoid cross-test pollution
        import shutil

        original_path = os.environ.get("PATH", "")

        # Remove any iac-smith temp dirs that prior tests may have injected
        clean_path = ":".join(p for p in original_path.split(":") if "iac-smith-bins-" not in p)
        monkeypatch.setenv("PATH", clean_path)

        if not shutil.which("terraform") or not shutil.which("terragrunt"):
            pytest.skip("terraform or terragrunt not on PATH")

        repo = tmp_path / "target"
        repo.mkdir()
        env = ensure_terraform_terragrunt(repo)
        tf_path = shutil.which("terraform", path=env.get("PATH", clean_path))
        assert tf_path is not None
        assert "iac-smith-bins-" not in tf_path

    def test_version_file_respected(self, tmp_path: Path) -> None:
        """When .terraform-version exists, that version is installed."""
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / ".terraform-version").write_text("1.9.0\n")
        env = ensure_terraform_terragrunt(repo)
        bin_dir = env["PATH"].split(":")[0]
        tf = Path(bin_dir) / "terraform"
        import subprocess

        r = subprocess.run([str(tf), "--version"], capture_output=True, text=True, env=env)
        assert r.returncode == 0
        assert "v1.9" in r.stdout or "1.9" in r.stdout

    def test_terragrunt_version_file_respected(self, tmp_path: Path) -> None:
        """When .terragrunt-version exists, that version is installed."""
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / ".terragrunt-version").write_text("0.68.0\n")
        env = ensure_terraform_terragrunt(repo)
        bin_dir = env["PATH"].split(":")[0]
        tg = Path(bin_dir) / "terragrunt"
        import subprocess

        r = subprocess.run([str(tg), "--version"], capture_output=True, text=True, env=env)
        assert r.returncode == 0
        assert "v0.68" in r.stdout or "0.68" in r.stdout
