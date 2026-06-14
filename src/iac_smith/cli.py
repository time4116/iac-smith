import os
from collections.abc import Mapping


def validate_allowed_target_repo(env: Mapping[str, str]) -> str:
    target_repo = env.get("IAC_SMITH_TARGET_REPO")
    allowed_target_repo = env.get("IAC_SMITH_ALLOWED_TARGET_REPO")

    if not allowed_target_repo:
        raise SystemExit("IAC_SMITH_ALLOWED_TARGET_REPO must be set; failing closed.")
    if not target_repo:
        raise SystemExit("IAC_SMITH_TARGET_REPO must be set.")
    if target_repo != allowed_target_repo:
        raise SystemExit(f"Target repo `{target_repo}` is not allowed.")
    return target_repo


def main() -> None:
    validate_allowed_target_repo(os.environ)
    message = (
        "CLI execution is not implemented yet. Current scaffold covers graph, intent, "
        "rules, planning, and PR summary tests."
    )
    raise SystemExit(message)


if __name__ == "__main__":
    main()
