"""Helpers for pulling down the target repo before analysis."""

from __future__ import annotations

import subprocess
import tempfile
import shutil
from pathlib import Path


def split_repo_source(source: str) -> tuple[str, str | None]:
    """Split 'https://github.com/org/app#9.5.5' into (url, '9.5.5').

    The UI stores the branch/tag after a '#' so that each version of the
    same repo gets its own job. Without a '#', the branch is None.
    """
    url, _, ref = source.partition("#")
    return url.strip(), (ref.strip() or None)


def clone_repo(repo_url: str, dest_dir: str | None = None, branch: str | None = None) -> Path:
    """
    Shallow-clone `repo_url` into `dest_dir` (or a fresh temp dir if
    not given). Returns the path to the cloned repo.

    `branch` may be a branch name or a tag (e.g. "9.5.5"). It can also be
    given inline as 'url#branch'.
    """
    repo_url, inline_ref = split_repo_source(repo_url)
    branch = branch or inline_ref

    if dest_dir is None:
        dest_dir = tempfile.mkdtemp(prefix="agent_harness_")

    dest_path = Path(dest_dir)
    if dest_path.exists() and any(dest_path.iterdir()):
        shutil.rmtree(dest_path)
    dest_path.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone", "--depth", "1", "--config", "core.autocrlf=false"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(dest_path)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        ref = f" (branch/tag '{branch}')" if branch else ""
        raise RuntimeError(f"git clone failed for {repo_url}{ref}:\n{e.stderr or e.stdout}") from e
    return dest_path