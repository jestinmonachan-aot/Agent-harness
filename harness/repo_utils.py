"""Helpers for pulling down the target repo before analysis."""

from __future__ import annotations

import subprocess
import tempfile
import shutil
from pathlib import Path


def clone_repo(repo_url: str, dest_dir: str | None = None) -> Path:
    """
    Shallow-clone `repo_url` into `dest_dir` (or a fresh temp dir if
    not given). Returns the path to the cloned repo.
    """
    if dest_dir is None:
        dest_dir = tempfile.mkdtemp(prefix="agent_harness_")

    dest_path = Path(dest_dir)
    if dest_path.exists() and any(dest_path.iterdir()):
        shutil.rmtree(dest_path)
    dest_path.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "clone", "--depth", "1", "--config", "core.autocrlf=false", repo_url, str(dest_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest_path
