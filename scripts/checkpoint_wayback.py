from __future__ import annotations

import os
import subprocess


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True)


def has_staged_data_changes() -> bool:
    result = run(
        ["git", "diff", "--cached", "--quiet", "--", "data"],
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError("git diff failed while checking staged data")
    return result.returncode == 1


def dispatch_pages() -> None:
    if os.environ.get("WAYBACK_CHECKPOINT_PAGES") != "1":
        return
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    command = ["gh", "workflow", "run", "pages.yml", "--ref", "main"]
    if repository:
        command.extend(["--repo", repository])
    run(command)


def main() -> None:
    processed = os.environ.get("WAYBACK_CHECKPOINT_PROCESSED", "0")
    changed = os.environ.get("WAYBACK_CHECKPOINT_CHANGED", "0")
    final = os.environ.get("WAYBACK_CHECKPOINT_FINAL") == "1"

    run(["git", "add", "--", "data"])
    if not has_staged_data_changes():
        print("Wayback checkpoint: no data changes to commit.", flush=True)
        return

    suffix = "final" if final else f"after {changed} changes"
    message = f"archive: Wayback checkpoint {suffix} ({processed} processed)"
    run(["git", "commit", "-m", message])

    if os.environ.get("WAYBACK_CHECKPOINT_PUSH") == "1":
        first_push = run(["git", "push", "origin", "HEAD:main"], check=False)
        if first_push.returncode != 0:
            run(["git", "pull", "--rebase", "origin", "main"])
            run(["git", "push", "origin", "HEAD:main"])
        if not final:
            dispatch_pages()


if __name__ == "__main__":
    main()
