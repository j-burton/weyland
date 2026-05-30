"""Git verbs scoped to the per-Pi repo."""
from __future__ import annotations

import subprocess

from fastmcp import FastMCP


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def register(app: FastMCP, repo_path: str) -> None:
    @app.tool()
    def git_status() -> dict:
        """git status of the per-Pi repo."""
        proc = _git(repo_path, "status", "--porcelain=v1", "--branch")
        return {
            "ok": proc.returncode == 0,
            "repo": repo_path,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.tool()
    def git_log(n: int = 10) -> dict:
        """Recent commits in the per-Pi repo."""
        proc = _git(repo_path, "log", f"-n{n}", "--oneline", "--decorate")
        return {
            "ok": proc.returncode == 0,
            "repo": repo_path,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.tool()
    def git_pull() -> dict:
        """Fast-forward pull the per-Pi repo."""
        proc = _git(repo_path, "pull", "--ff-only")
        return {
            "ok": proc.returncode == 0,
            "repo": repo_path,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @app.tool()
    def git_commit_push(message: str, add_all: bool = True) -> dict:
        """Stage (-A), commit, push the per-Pi repo."""
        if add_all:
            _git(repo_path, "add", "-A")
        commit = _git(repo_path, "commit", "-m", message)
        if commit.returncode != 0:
            return {
                "ok": False,
                "stage": "commit",
                "stdout": commit.stdout,
                "stderr": commit.stderr,
            }
        push = _git(repo_path, "push")
        return {
            "ok": push.returncode == 0,
            "commit_stdout": commit.stdout,
            "push_stdout": push.stdout,
            "push_stderr": push.stderr,
        }
