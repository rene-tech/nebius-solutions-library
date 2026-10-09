#!/usr/bin/env python3
"""Short-lived tasks, serialized main integration and completed-branch cleanup."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=check)


def value(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def root() -> Path:
    return Path(value(Path.cwd(), "rev-parse", "--show-toplevel"))


def common(repo: Path) -> Path:
    return Path(value(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def policy(repo: Path) -> dict:
    result = json.loads((repo / ".scientific-ai-workflow.json").read_text())
    owner = result["personal_repository"]
    if not owner.startswith("rene-tech/") or len(owner.split("/")) != 2:
        raise ValueError("Expected the explicitly authorized personal GitHub repository")
    return result


def enabled(repo: Path) -> bool:
    return git(repo, "config", "--bool", "scientificai.singlemain", check=False).stdout.strip() == "true"


def pre_push(repo: Path, url: str, updates: list[str]) -> None:
    if not enabled(repo):
        return
    owner = policy(repo)["personal_repository"]
    allowed = {f"https://github.com/{owner}", f"https://github.com/{owner}.git",
               f"git@github.com:{owner}", f"git@github.com:{owner}.git",
               f"ssh://git@github.com/{owner}", f"ssh://git@github.com/{owner}.git"}
    if url not in allowed:
        raise ValueError("Pushes are restricted to the authorized personal fork; upstream is read-only")
    main = value(repo, "rev-parse", "refs/heads/main")
    for line in updates:
        local_ref, local_sha, remote_ref, remote_sha = line.split()
        if remote_ref == "refs/heads/main":
            if local_sha != main:
                raise ValueError("Only the integrated local main may update remote main; main deletion is forbidden")
            if set(remote_sha) != {"0"} and git(repo, "merge-base", "--is-ancestor", remote_sha, local_sha, check=False).returncode:
                raise ValueError("Main must be a fast-forward of the remote; fetch and integrate current main first")


def reference_transaction(repo: Path, state: str, updates: list[str]) -> None:
    if state != "prepared" or not enabled(repo):
        return
    for line in updates:
        old, new, ref = line.split()
        if ref != "refs/heads/main":
            continue  # Temporary branches are explicitly permitted.
        if set(new) == {"0"}:
            raise ValueError("Main deletion is forbidden; retire only completed task branches")
        if set(old) != {"0"} and git(repo, "merge-base", "--is-ancestor", old, new, check=False).returncode:
            raise ValueError("Main history must be fast-forward; incorporate current main before integration")


def require_clean(repo: Path) -> None:
    if value(repo, "status", "--porcelain=v1"):
        raise ValueError(f"Preserve and resolve dirty files before integration: {repo}")


def main_checkout(repo: Path) -> Path:
    for block in value(repo, "worktree", "list", "--porcelain").split("\n\n"):
        if "branch refs/heads/main" in block.splitlines():
            return Path(next(line[9:] for line in block.splitlines() if line.startswith("worktree ")))
    raise ValueError("No checkout owns main; restore a main checkout before integration")


def validated_candidate(task: Path, evidence: Path) -> str:
    require_clean(task)
    sha = value(task, "rev-parse", "HEAD")
    receipt = json.loads(evidence.read_text())
    checks = receipt.get("checks", [])
    if receipt.get("commit") != sha or not checks or any(c.get("exit_code") != 0 or not c.get("command") for c in checks):
        raise ValueError("Evidence must identify this exact commit and at least one successful relevant check")
    return sha


def retire_task_branch(repo: Path, task: Path, branch: str, sha: str, push: bool) -> None:
    """Retire only this exact clean, integrated task; never remove its directory."""
    if not branch:
        return
    if branch == "refs/heads/main" or value(repo, "rev-parse", branch) != sha:
        raise ValueError("Task branch changed; retain it for explicit closeout")
    if git(repo, "merge-base", "--is-ancestor", sha, "main", check=False).returncode:
        raise ValueError("Unmerged task branches must be preserved, not deleted")
    require_clean(task)
    if push:
        url = f"https://github.com/{policy(main_checkout(repo))['personal_repository']}.git"
        rows = value(repo, "ls-remote", "--heads", url, branch).splitlines()
        if rows:
            if len(rows) != 1 or rows[0].split()[0] != sha:
                raise ValueError("Remote task branch moved; preserve it and review before closeout")
            git(repo, "push", f"--force-with-lease={branch}:{sha}", url, f":{branch}")
    # Preserve every file, staged state and active process; detach only this task.
    git(task, "update-ref", "--no-deref", "HEAD", sha, sha)
    git(repo, "update-ref", "-d", branch, sha)
    receipts = common(repo) / "scientificai-closeout"
    receipts.mkdir(mode=0o700, exist_ok=True)
    (receipts / f"{sha}.json").write_text(json.dumps({
        "branch": branch, "commit": sha, "main": value(repo, "rev-parse", "main"),
        "personal_fork_cleanup_requested": push, "worktree_preserved": str(task),
    }, indent=2) + "\n")


def integrate(repo: Path, task: Path, evidence: Path, push: bool) -> str:
    if common(repo) != common(task):
        raise ValueError("The task worktree belongs to another repository")
    task = task.resolve()
    checkout = main_checkout(repo).resolve()
    if task == checkout:
        raise ValueError("Integration expects a separate detached task worktree")
    branch = git(task, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if branch == "refs/heads/main":
        raise ValueError("Task work must be separate from maintained main")
    with (common(repo) / "scientificai-main.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_clean(checkout)
        sha = validated_candidate(task, evidence)
        if push:
            url = f"https://github.com/{policy(checkout)['personal_repository']}.git"
            git(checkout, "fetch", "--no-tags", url, "refs/heads/main")
            git(checkout, "merge", "--ff-only", "FETCH_HEAD")
        current = value(checkout, "rev-parse", "main")
        if git(repo, "merge-base", "--is-ancestor", current, sha, check=False).returncode:
            raise ValueError("Main moved: rebase the detached task on current main, resolve conflicts and rerun checks")
        git(checkout, "merge", "--ff-only", sha)
        if push:
            git(checkout, "push", url, "refs/heads/main:refs/heads/main")
        retire_task_branch(repo, task, branch, sha, push)
        return sha


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install", help="Protect main while allowing temporary branches")
    start = sub.add_parser("start", help="Create a detached task worktree from main")
    start.add_argument("path", type=Path)
    merge = sub.add_parser("integrate", help="Serialize a validated fast-forward into main")
    merge.add_argument("task", type=Path)
    merge.add_argument("--evidence", required=True, type=Path)
    merge.add_argument("--push", action="store_true", help="Refresh/publish main in the personal fork")
    check = sub.add_parser("check", help="Audit branch counts and completed branch leftovers")
    check.add_argument("--remote", action="store_true", help="Also inspect the personal fork")
    args = parser.parse_args()
    repo = root()
    if args.action == "install":
        policy(repo)
        old = git(repo, "config", "--get", "core.hooksPath", check=False).stdout.strip()
        hooks = str(repo / ".githooks")
        if old and Path(old).resolve() != Path(hooks).resolve():
            raise ValueError("An existing hooksPath must be reviewed and preserved before installing these hooks")
        for name in ["pre-push", "reference-transaction"]:
            (repo / ".githooks" / name).chmod(0o755)
        git(repo, "config", "scientificai.singlemain", "true")
        git(repo, "config", "core.hooksPath", hooks)
        print("Protected main; temporary branches allowed; detached Task Deck launches enabled")
    elif args.action == "start":
        git(repo, "worktree", "add", "--detach", str(args.path.resolve()), "refs/heads/main")
        print(args.path.resolve())
    elif args.action == "integrate":
        sha = integrate(repo, args.task, args.evidence, args.push)
        print(f"Integrated {sha}" + (" and pushed main" if args.push else "; remote publication is still pending"))
        print("After the task session exits, remove only its clean finished worktree with git worktree remove")
    elif args.action == "check":
        heads = value(repo, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
        if "refs/heads/main" not in heads:
            raise ValueError("A maintained main branch is required")
        print(f"Local branches: {len(heads)}; main is the maintained trunk")
        leftovers = value(repo, "for-each-ref", "--merged=main", "--format=%(refname)", "refs/heads").splitlines()
        for branch in leftovers:
            if branch != "refs/heads/main":
                print(f"Closeout review: already merged {branch}")
        if len(heads) > 20:
            print("Branch count exceeds 20: review active ownership and retire finished work", file=sys.stderr)
        if args.remote:
            url = f"https://github.com/{policy(repo)['personal_repository']}.git"
            heads = value(repo, "ls-remote", "--heads", url).splitlines()
            if not any(row.split()[1] == "refs/heads/main" for row in heads):
                raise ValueError("The personal fork needs a maintained main")
            print(f"Personal-fork branches: {len(heads)}")
            if len(heads) > 20:
                print("Personal-fork count exceeds 20: review and close out completed branches", file=sys.stderr)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "hook-pre-push":
            pre_push(root(), sys.argv[3], sys.stdin.read().splitlines())
        elif len(sys.argv) > 1 and sys.argv[1] == "hook-reference-transaction":
            reference_transaction(root(), sys.argv[2], sys.stdin.read().splitlines())
        else:
            cli()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"single-main: {detail}", file=sys.stderr)
        sys.exit(1)
