import subprocess
from pathlib import Path


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def _safe_path(repo_path: Path, relative_path: str) -> Path:
    destination = (repo_path / relative_path).resolve()
    root = repo_path.resolve()
    if destination != root and root not in destination.parents:
        raise ValueError(f"Generated file path `{relative_path}` resolves outside repository")
    return destination


def apply_generated_files(repo_path: str | Path, generated_files: dict[str, str]) -> list[Path]:
    root = Path(repo_path)
    written: list[Path] = []
    for relative_path, content in generated_files.items():
        destination = _safe_path(root, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append(destination)
    return written


def commit_generated_files(repo_path: str | Path, message: str) -> bool:
    root = Path(repo_path)
    status = _run_git(root, ["status", "--porcelain"]).stdout.strip()
    if not status:
        return False
    _run_git(root, ["config", "--local", "user.email", "iac-smith@time4116.ai"])
    _run_git(root, ["config", "--local", "user.name", "IaC Smith"])
    _run_git(root, ["add", "-A"])
    _run_git(root, ["commit", "-m", message])
    return True


def create_branch(repo_path: str | Path, branch_name: str) -> None:
    _run_git(Path(repo_path), ["switch", "-C", branch_name])


def current_head(repo_path: str | Path) -> str:
    return _run_git(Path(repo_path), ["rev-parse", "HEAD"]).stdout.strip()
