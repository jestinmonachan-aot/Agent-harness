"""Migration step: copy the input repo, ask Claude Code to apply
targeted fixes to the app (one scoped module, or the entire
application), then push to a NEW output repo (never touches the input
repo).

Two paths:

- NARROW scope (e.g. "tickets"): a single direct Claude Code call
  (_migrate_direct), audit/smoke-test steps included, bounded by
  DEFAULT_NARROW_SCOPE_TIMEOUT. Fast, deployable on its own.

- FULL APP: routes through the plan -> build-per-module -> assemble
  pipeline (_run_full_app_module_pipeline) for real breadth across the
  app's functional modules, rather than the single-call "pick one
  central workflow" shortcut this file used previously. This trades a
  strict 1-hour ceiling for a longer but much more complete run -
  expect this to take considerably longer for a large app. Progress is
  persisted to MIGRATION_STATE.json after every completed module, and
  a usage/rate-limit failure raises UsageLimitError carrying the
  output directory so a failed run can be resumed (pass the same
  directory back in as resume_output_dir) rather than restarted.

  Planning (_plan_modules) runs under --permission-mode plan: it should
  only read/explore and return a JSON module list, never write files.
  Every other call (module build, assembly, direct scope) stays on the
  default bypassPermissions mode since those steps must write output.

CODEBASE MAP: when analyze has already produced a codebase_map, it is
passed through to EVERY Claude call in both paths - the direct call,
planning, and every individual module - so none of them re-explore the
repo's directory structure from scratch. This was previously only
wired into the direct path; per-module calls were re-deriving the same
structural context analyze had already produced, which is exactly the
duplicated work this was meant to avoid.
"""

from __future__ import annotations
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from harness.claude_cli import run_claude_prompt
from harness.prompts.migration_prompts import (
    build_direct_prompt,
    build_planning_prompt,
    build_module_prompt,
    build_assembly_prompt,
)

DEFAULT_PLANNING_TIMEOUT = 600
DEFAULT_PER_MODULE_TIMEOUT = 2700
DEFAULT_ASSEMBLY_TIMEOUT = 1800
DEFAULT_NARROW_SCOPE_TIMEOUT = 3000  # 50 min

STATE_FILENAME = "MIGRATION_STATE.json"
KNOWLEDGE_BASE_FILENAME = "KNOWLEDGE_BASE.md"

USAGE_LIMIT_SIGNATURES = [
    "usage limit", "session limit", "rate limit", "rate_limit", "quota",
    "too many requests", "429", "resets at", "resets ", "try again later",
    "overloaded",
]


class UsageLimitError(RuntimeError):
    def __init__(self, message: str, output_dir: Path):
        super().__init__(message)
        self.output_dir = output_dir


def create_github_repo(repo_name: str, private: bool = True) -> str:
    visibility = "--private" if private else "--public"
    proc = subprocess.run(
        ["gh", "repo", "create", repo_name, visibility],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh repo create failed: {proc.stderr}")
    url_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return url_line or f"https://github.com/<your-user>/{repo_name}"


def _is_full_app(scope: str | None) -> bool:
    return not scope or scope.strip().lower() in ("full app", "entire application", "all", "*")


def _run_git(cwd: Path, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")


def _looks_like_usage_limit(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return any(sig in combined for sig in USAGE_LIMIT_SIGNATURES)


def _load_state(output_dir: Path) -> dict | None:
    state_path = output_dir / STATE_FILENAME
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_state(output_dir: Path, state: dict) -> None:
    state_path = output_dir / STATE_FILENAME
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Narrow-scope path: single direct call
# ---------------------------------------------------------------------------

def _migrate_direct(
    input_repo_path: str, output_dir: Path, findings_md: str, scope: str,
    timeout: int, fast_mode: bool = False, codebase_map: str = "",
) -> str:
    prompt = build_direct_prompt(input_repo_path, findings_md, scope, fast_mode=fast_mode, codebase_map=codebase_map)
    result = run_claude_prompt(
        prompt, cwd=str(output_dir), timeout=timeout,
        extra_args=["--add-dir", str(input_repo_path)],
    )
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise UsageLimitError(
                f"Hit what looks like a usage/rate limit during migration: "
                f"{result.stderr[:300] or result.stdout[:300]}",
                output_dir,
            )
        raise RuntimeError(f"Migration prompt failed. returncode={result.returncode}, stdout={result.stdout[:500]!r}")
    stack_match = re.search(r"STACK_CHOSEN:\s*(.+)", result.stdout)
    return stack_match.group(1).strip() if stack_match else "unknown"


# ---------------------------------------------------------------------------
# Full-app path: plan -> build-per-module -> assemble
# ---------------------------------------------------------------------------

def _plan_modules(input_repo_path: str, timeout: int, codebase_map: str = "") -> list[dict]:
    print("[migrate] Planning module breakdown...", flush=True)
    prompt = build_planning_prompt(input_repo_path, codebase_map=codebase_map)
    result = run_claude_prompt(
        prompt, cwd=str(input_repo_path), timeout=timeout,
        extra_args=["--add-dir", str(input_repo_path)],
        permission_mode="plan",
    )
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise RuntimeError(
                f"Hit what looks like a usage/rate limit during module "
                f"planning (nothing written yet, nothing to resume - just retry "
                f"once the limit resets): {result.stderr[:300] or result.stdout[:300]}"
            )
        raise RuntimeError(f"Module planning failed. returncode={result.returncode}, stdout={result.stdout[:500]!r}")

    match = re.search(r"\[.*\]", result.stdout, re.DOTALL)
    if match:
        try:
            modules = json.loads(match.group(0))
            if isinstance(modules, list) and modules:
                return modules
        except json.JSONDecodeError:
            pass
    return [{"id": "full_app", "description": "Entire application"}]


def _migrate_module(
    input_repo_path: str,
    output_dir: Path,
    module: dict,
    findings_md: str,
    is_first_module: bool,
    timeout: int,
    codebase_map: str = "",
) -> str:
    module_id = module["id"]
    print(f"[migrate] Building module: {module_id} ({module.get('description', module_id)})", flush=True)

    prompt = build_module_prompt(
        input_repo_path, output_dir, module, findings_md, is_first_module,
        codebase_map=codebase_map,
    )
    result = run_claude_prompt(
        prompt, cwd=str(output_dir), timeout=timeout,
        extra_args=["--add-dir", str(input_repo_path)],
    )
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise UsageLimitError(
                f"Hit what looks like a usage/rate limit while migrating "
                f"module '{module_id}': {result.stderr[:300] or result.stdout[:300]}. "
                f"Modules completed before this one are safely saved in "
                f"{output_dir} - resume to continue from here.",
                output_dir,
            )
        raise RuntimeError(
            f"Migration of module '{module_id}' failed. "
            f"returncode={result.returncode}, stdout={result.stdout[:500]!r}"
        )
    stack_match = re.search(r"STACK_CHOSEN:\s*(.+)", result.stdout)
    stack_result = stack_match.group(1).strip() if stack_match else "unknown"
    print(f"[migrate] Module '{module_id}' done.", flush=True)
    return stack_result


def _assemble_modules(output_dir: Path, modules: list[dict], chosen_stack: str, timeout: int) -> None:
    if len(modules) <= 1:
        return
    print(f"[migrate] Assembling {len(modules)} modules into final app...", flush=True)
    prompt = build_assembly_prompt(output_dir, modules, chosen_stack)
    result = run_claude_prompt(prompt, cwd=str(output_dir), timeout=timeout)
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise UsageLimitError(
                f"Hit what looks like a usage/rate limit during the final "
                f"assembly pass. All modules are already migrated and saved in "
                f"{output_dir} - resume to retry just the assembly step.",
                output_dir,
            )
        raise RuntimeError(
            f"Assembly pass failed (per-module output is still on disk at "
            f"{output_dir}, nothing was lost). returncode={result.returncode}, "
            f"stdout={result.stdout[:500]!r}"
        )


def _run_full_app_module_pipeline(
    input_repo_path: str, findings_md: str, scope: str | None,
    planning_timeout: int, per_module_timeout: int, assembly_timeout: int,
    resume_output_dir: str | None, codebase_map: str = "",
) -> tuple[Path, str, list[dict]]:
    """The full-breadth full-app path: plan -> build each module -> assemble.
    Now the active path for full-app scope (see migrate_and_push) rather
    than a kept-but-unused alternative."""
    if resume_output_dir:
        output_dir = Path(resume_output_dir)
        state = _load_state(output_dir)
        if state is None:
            raise RuntimeError(
                f"Asked to resume from {output_dir} but no {STATE_FILENAME} "
                "was found there - can't tell what's already done. Start a "
                "fresh migration instead."
            )
        modules = state["modules"]
        completed_ids = set(state.get("completed_module_ids", []))
        chosen_stack = state.get("chosen_stack", "unknown")
        assembly_done = state.get("assembly_done", False)
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="migration_output_"))
        modules = _plan_modules(input_repo_path, planning_timeout, codebase_map=codebase_map)
        completed_ids = set()
        chosen_stack = "unknown"
        assembly_done = False
        _save_state(output_dir, {
            "scope": scope, "modules": modules,
            "completed_module_ids": [], "chosen_stack": "unknown",
            "assembly_done": False,
        })

    print(f"[migrate] Plan: {len(modules)} module(s) - {[m['id'] for m in modules]}", flush=True)
    if completed_ids:
        print(f"[migrate] Already completed (will skip): {sorted(completed_ids)}", flush=True)

    for i, module in enumerate(modules):
        if module["id"] in completed_ids:
            continue
        chosen_stack = _migrate_module(
            input_repo_path, output_dir, module, findings_md,
            is_first_module=(i == 0), timeout=per_module_timeout,
            codebase_map=codebase_map,
        )

        # Require the module's own parity-check file as proof it actually
        # finished its self-audit, not just that the CLI call returned
        # successfully with STACK_CHOSEN text. Every module prompt (see
        # migration_prompts.build_module_prompt, step 7) mandates writing
        # this file, so its absence means the run didn't really complete.
        parity_file = output_dir / f"PARITY_CHECK_{module['id']}.md"
        if not parity_file.exists():
            raise RuntimeError(
                f"Module '{module['id']}' finished without writing "
                f"{parity_file.name} - treating it as incomplete rather than "
                f"advancing. Output so far is preserved in {output_dir}."
            )

        completed_ids.add(module["id"])
        _save_state(output_dir, {
            "scope": scope, "modules": modules,
            "completed_module_ids": sorted(completed_ids),
            "chosen_stack": chosen_stack, "assembly_done": False,
        })

    if not assembly_done:
        _assemble_modules(output_dir, modules, chosen_stack, assembly_timeout)
        _save_state(output_dir, {
            "scope": scope, "modules": modules,
            "completed_module_ids": sorted(completed_ids),
            "chosen_stack": chosen_stack, "assembly_done": True,
        })

    return output_dir, chosen_stack, modules


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def migrate_and_push(
    input_repo_path: str,
    findings_md: str,
    scope: str | None,
    timeout: int | None = None,
    planning_timeout: int = DEFAULT_PLANNING_TIMEOUT,
    per_module_timeout: int = DEFAULT_PER_MODULE_TIMEOUT,
    assembly_timeout: int = DEFAULT_ASSEMBLY_TIMEOUT,
    resume_output_dir: str | None = None,
    codebase_map: str = "",
) -> tuple[str, str, str, str]:
    """Returns (repo_url, local_output_path, stack_chosen, stack_reasoning).

    Narrow scope -> single direct call (_migrate_direct), fast, not
    resumable (nothing partial to resume from one call).

    Full app -> the module pipeline (_run_full_app_module_pipeline): plan,
    build each module, assemble. RESUMABLE - if a module or assembly hits
    a usage/rate limit, UsageLimitError carries output_dir; pass it back
    as resume_output_dir to continue from the next incomplete step
    without redoing finished modules or replanning.

    `timeout`, if explicitly passed, overrides per_module_timeout, for
    backward compatibility with older callers.
    """
    if timeout is not None:
        per_module_timeout = timeout

    full_app = _is_full_app(scope)

    if full_app:
        output_dir, chosen_stack, modules = _run_full_app_module_pipeline(
            input_repo_path, findings_md, scope,
            planning_timeout, per_module_timeout, assembly_timeout,
            resume_output_dir, codebase_map=codebase_map,
        )
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="migration_output_"))
        chosen_stack = _migrate_direct(
            input_repo_path, output_dir, findings_md, scope,
            DEFAULT_NARROW_SCOPE_TIMEOUT, fast_mode=False,
            codebase_map=codebase_map,
        )
        modules = [{"id": scope.strip().lower().replace(" ", "_"), "description": scope}]

    decision_file = output_dir / "STACK_DECISION.md"
    stack_reasoning = decision_file.read_text(encoding="utf-8") if decision_file.exists() else ""

    written_files = [
        p for p in output_dir.rglob("*")
        if p.is_file() and ".git" not in p.parts and p.name != STATE_FILENAME
    ]
    if not written_files:
        raise RuntimeError("Migration produced no output files.")

    branch_name = f"migration-{int(time.time())}"
    target_repo = "https://github.com/jestinmonachan-aot/harness-new-test.git"

    _run_git(output_dir, "init")
    _run_git(output_dir, "add", ".")
    module_summary = ", ".join(m["id"] for m in modules)
    _run_git(output_dir, "commit", "-m", f"Modernized migration ({chosen_stack}): {scope or 'full app'} [{module_summary}]")
    _run_git(output_dir, "branch", "-M", branch_name)
    _run_git(output_dir, "remote", "add", "origin", target_repo)
    _run_git(output_dir, "push", "-u", "origin", branch_name)

    repo_url = f"https://github.com/jestinmonachan-aot/harness-new-test/tree/{branch_name}"
    return repo_url, str(output_dir), chosen_stack, stack_reasoning