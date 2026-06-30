import subprocess
from pathlib import Path

from iac_smith import provider_lock
from iac_smith.provider_lock import (
    _module_dirs,
    ensure_terraform_gitignore,
    generate_provider_locks,
)


def _module(repo: Path, name: str, *, versions: bool = True) -> Path:
    d = repo / "modules" / name
    d.mkdir(parents=True)
    (d / "main.tf").write_text('resource "null_resource" "x" {}\n')
    if versions:
        (d / "versions.tf").write_text(
            'terraform { required_providers { aws = { source = "hashicorp/aws" } } }\n'
        )
    return d


def test_ensure_terraform_gitignore_writes_when_absent(tmp_path: Path) -> None:
    assert ensure_terraform_gitignore(tmp_path) is True
    body = (tmp_path / ".gitignore").read_text()
    assert ".terraform/" in body
    assert ".terragrunt-cache/" in body
    # The lockfile must NOT be ignored.
    assert "\n.terraform.lock.hcl\n" not in body


def test_ensure_terraform_gitignore_does_not_clobber_existing(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("custom\n")
    assert ensure_terraform_gitignore(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text() == "custom\n"


def test_module_dirs_only_includes_modules_with_versions_tf(tmp_path: Path) -> None:
    _module(tmp_path, "vpc")
    _module(tmp_path, "no_versions", versions=False)
    dirs = _module_dirs(tmp_path)
    assert [d.name for d in dirs] == ["vpc"]


def test_generate_provider_locks_skips_without_terraform(tmp_path: Path, monkeypatch) -> None:
    _module(tmp_path, "vpc")
    monkeypatch.setattr(provider_lock, "_terraform_path", lambda _env: None)
    assert generate_provider_locks(tmp_path, env={}) == []


def test_generate_provider_locks_inits_then_locks_and_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    _module(tmp_path, "vpc")
    _module(tmp_path, "rds")
    monkeypatch.setattr(provider_lock, "_terraform_path", lambda _env: "/usr/bin/terraform")

    calls: list[tuple[str, Path]] = []

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        verb = cmd[1] if cmd[1] != "providers" else "lock"
        calls.append((verb, cwd))
        if verb == "init":
            (cwd / ".terraform").mkdir(exist_ok=True)  # plugin dir that must be cleaned
        if verb == "lock":
            (cwd / ".terraform.lock.hcl").write_text("# locked\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provider_lock.subprocess, "run", fake_run)

    written = generate_provider_locks(tmp_path, env={"PATH": "/usr/bin"})

    assert sorted(p.parent.name for p in written) == ["rds", "vpc"]
    # Every module was init'd then locked.
    verbs_by_module: dict[str, list[str]] = {}
    for verb, cwd in calls:
        verbs_by_module.setdefault(cwd.name, []).append(verb)
    assert verbs_by_module["vpc"] == ["init", "lock"]
    assert verbs_by_module["rds"] == ["init", "lock"]
    # The .terraform plugin dir is removed; only the lockfile remains.
    for name in ("vpc", "rds"):
        mod = tmp_path / "modules" / name
        assert not (mod / ".terraform").exists()
        assert (mod / ".terraform.lock.hcl").is_file()


def test_generate_provider_locks_is_best_effort_on_failure(tmp_path: Path, monkeypatch) -> None:
    _module(tmp_path, "good")
    _module(tmp_path, "bad")
    monkeypatch.setattr(provider_lock, "_terraform_path", lambda _env: "/usr/bin/terraform")
    logs: list[str] = []

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        if cwd.name == "bad":
            raise subprocess.CalledProcessError(1, cmd, "", "boom")
        if cmd[1] == "providers":
            (cwd / ".terraform.lock.hcl").write_text("# locked\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(provider_lock.subprocess, "run", fake_run)

    written = generate_provider_locks(tmp_path, env={}, log=logs.append)

    assert [p.parent.name for p in written] == ["good"]
    assert any("could not lock providers for bad" in m for m in logs)
