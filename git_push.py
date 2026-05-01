"""
git_push.py

Automates pushing updates to the GitHub repository.
Stages all tracked and modified files, commits with a timestamped
message, and pushes to the main branch.

Usage
-----
python git_push.py                        # auto commit message with timestamp
python git_push.py --message "your message"  # custom commit message
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent


def run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"\n  [ERROR] Command failed: {' '.join(cmd)}", file=sys.stderr)
        print(f"  {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push updates to GitHub repository.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--message", "-m",
        default=None,
        help="Commit message. Defaults to timestamped auto message.",
    )
    args = parser.parse_args()

    sep = "=" * 50
    print(f"\n  {sep}")
    print(f"  GitHub Auto-Push")
    print(f"  {sep}")
    print(f"  Repo: {REPO_ROOT}")

    # Check git is available
    result = run(["git", "--version"], check=False)
    if result.returncode != 0:
        print("  [ERROR] git is not installed or not in PATH.", file=sys.stderr)
        sys.exit(1)

    # Check we're inside a git repo
    result = run(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode != 0:
        print("  [ERROR] Not inside a git repository.", file=sys.stderr)
        print(f"  Run: git init && git remote add origin <url>", file=sys.stderr)
        sys.exit(1)

    # Show current status
    status = run(["git", "status", "--short"])
    if not status.stdout.strip():
        print("\n  Nothing to commit — repo is up to date.")
        return

    print(f"\n  Changes detected:")
    for line in status.stdout.strip().splitlines():
        print(f"    {line}")

    # Stage all changes
    print(f"\n  Staging all changes...")
    run(["git", "add", "-A"])

    # Build commit message
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    message   = args.message or f"Update {timestamp}"
    print(f"  Committing: '{message}'")
    run(["git", "commit", "-m", message])

    # Push
    print(f"  Pushing to origin/main...")
    result = run(["git", "push", "origin", "main"], check=False)

    if result.returncode != 0:
        # Try setting upstream if first push
        if "no upstream" in result.stderr.lower() or "has no upstream" in result.stderr.lower():
            print("  Setting upstream and retrying...")
            run(["git", "push", "--set-upstream", "origin", "main"])
        else:
            print(f"\n  [ERROR] Push failed:", file=sys.stderr)
            print(f"  {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)

    print(f"\n  Done. Repository updated successfully.")
    print(f"  {sep}\n")


if __name__ == "__main__":
    main()
