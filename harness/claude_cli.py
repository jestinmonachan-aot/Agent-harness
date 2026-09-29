"""
Thin wrapper around the Claude Code CLI so the rest of the harness
can call it programmatically: pass a prompt, get text back.

Assumes the `claude` CLI is installed and authenticated
(claude login) on this machine.
"""

from __future__ import annotations

import subprocess
import shutil
import sys
from dataclasses import dataclass


@dataclass
class ClaudeCLIResult:
    success: bool
    stdout: str
    stderr: str
    returncode: int


def _resolve_claude_path() -> str | None:
    """
    Find the actual claude executable. On Windows, npm installs a
    .cmd/.ps1 wrapper, and subprocess.run(["claude", ...]) alone often
    can't locate it without shell=True. shutil.which checks PATHEXT
    (.cmd, .bat, .exe) automatically on Windows, so use that first.
    """
    return shutil.which("claude")


def run_claude_prompt(
    prompt: str,
    cwd: str | None = None,
    timeout: int = 300,
    extra_args: list[str] | None = None,
    permission_mode: str = "bypassPermissions",
) -> ClaudeCLIResult:
    """
    Run a single non-interactive prompt through the Claude Code CLI.

    Pipes the prompt via stdin (using `-p` with no trailing arg, reading
    from stdin) instead of passing it as a command-line argument, since
    long prompts can exceed Windows' command-line length limit (~8191
    chars) and get silently truncated or fail.

    permission_mode: defaults to "bypassPermissions" (full read/write,
    used by every step that needs to actually create/edit files -
    migration, assembly, direct-scope). Pass "plan" for read-only
    planning calls (see migrate.py's _plan_modules) where Claude should
    explore and reason but never write anything.
    """
    claude_path = _resolve_claude_path()
    if claude_path is None:
        return ClaudeCLIResult(
            success=False,
            stdout="",
            stderr=(
                "`claude` CLI not found on PATH. Install it first: "
                "npm install -g @anthropic-ai/claude-code (or see "
                "official Claude Code install docs) and run `claude login`."
            ),
            returncode=127,
        )

    args = [claude_path, "-p", "--permission-mode", permission_mode]
    if extra_args:
        args.extend(extra_args)

    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=(sys.platform == "win32"),
        )
        return ClaudeCLIResult(
            success=proc.returncode == 0,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            returncode=proc.returncode,
        )
    except FileNotFoundError:
        return ClaudeCLIResult(
            success=False,
            stdout="",
            stderr=(
                "`claude` CLI not found on PATH. Install it first: "
                "npm install -g @anthropic-ai/claude-code (or see "
                "official Claude Code install docs) and run `claude login`."
            ),
            returncode=127,
        )
    except subprocess.TimeoutExpired:
        return ClaudeCLIResult(
            success=False,
            stdout="",
            stderr=f"Claude CLI call timed out after {timeout}s.",
            returncode=-1,
        )


def check_claude_available() -> ClaudeCLIResult:
    """Quick sanity check: is the CLI installed and authenticated?"""
    claude_path = _resolve_claude_path()
    if claude_path is None:
        return ClaudeCLIResult(
            success=False,
            stdout="",
            stderr="`claude` CLI not found on PATH.",
            returncode=127,
        )
    try:
        proc = subprocess.run(
            [claude_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=(sys.platform == "win32"),
        )
        return ClaudeCLIResult(
            success=proc.returncode == 0,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
            returncode=proc.returncode,
        )
    except FileNotFoundError:
        return ClaudeCLIResult(
            success=False,
            stdout="",
            stderr="`claude` CLI not found on PATH.",
            returncode=127,
        )
    except subprocess.TimeoutExpired:
        return ClaudeCLIResult(
            success=False, stdout="", stderr="Version check timed out.", returncode=-1
        )