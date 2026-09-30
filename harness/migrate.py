"""Migration step: copy the input repo, ask Claude Code to apply
targeted fixes to the app (one scoped module, or the entire
application), then push to a NEW output repo (never touches the input
repo).

Two paths:

- NARROW scope (e.g. "tickets"): a single direct Claude Code call
  (_migrate_direct), audit/smoke-test steps included, bounded by
  DEFAULT_NARROW_SCOPE_TIMEOUT. Fast, deployable on its own.

- FULL APP: a PHASED migration (_run_full_app_module_pipeline):

    1. PLAN (one call, --permission-mode plan, read-only): returns an
       ordered list of phases, each with an id, a description and
       done_criteria. Saved to MIGRATION_PLAN.json in the output dir.
    2. EXECUTE (one call per phase, default bypassPermissions): each
       phase's result is then VERIFIED before the next phase starts:
         - the phase printed a PHASE_RESULT line with status COMPLETE
         - EXISTING_UX_INVENTORY_<id>.md and PARITY_CHECK_<id>.md exist
         - PARITY_CHECK_<id>.md has no MISSING items
       If verification fails, ONE targeted fix-up call is made, then the
       phase is verified again. If it still fails, the run stops with a
       clear error instead of advancing on an incomplete phase.
    3. ASSEMBLE: wire the phases into one app.

  Progress is persisted to MIGRATION_STATE.json after every completed
  phase, and a usage/rate-limit failure raises UsageLimitError carrying
  the output directory so a failed run can be resumed (pass the same
  directory back in as resume_output_dir) rather than restarted.

CODEBASE MAP: when analyze has already produced a codebase_map, it is
passed through to EVERY Claude call in both paths, so none of them
re-explore the repo's directory structure from scratch.
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
    build_assembly_prompt,
)
from harness.prompts.phase_prompts import (
    build_phase_planning_prompt,
    build_phase_prompt,
    build_phase_fix_prompt,
)

DEFAULT_PLANNING_TIMEOUT = 600
DEFAULT_PER_MODULE_TIMEOUT = 2700
DEFAULT_ASSEMBLY_TIMEOUT = 1800
DEFAULT_NARROW_SCOPE_TIMEOUT = 3000  # 50 min
DEFAULT_FIX_TIMEOUT = 1500  # 25 min for the targeted fix-up call

STATE_FILENAME = "MIGRATION_STATE.json"
PLAN_FILENAME = "MIGRATION_PLAN.json"
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


class PhaseVerificationError(UsageLimitError):
    """A phase failed verification twice. Carries output_dir so the UI can
    offer Resume (completed phases are saved in MIGRATION_STATE.json)."""


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


def _snapshot(
    scope, modules, completed_ids, chosen_stack, assembly_done, phases_state,
) -> dict:
    return {
        "scope": scope,
        "modules": modules,
        "completed_module_ids": sorted(completed_ids),
        "chosen_stack": chosen_stack,
        "assembly_done": assembly_done,
        "phases": phases_state,
    }


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
# Full-app path: plan -> execute + verify each phase -> assemble
# ---------------------------------------------------------------------------

def _clean_phase_id(raw_id, index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw_id)).strip("_")
    return cleaned or f"phase_{index + 1}"


def _plan_modules(input_repo_path: str, timeout: int, codebase_map: str = "") -> list[dict]:
    """Call 1: read-only planning. Returns ordered phases, each
    {"id", "description", "done_criteria"}."""
    print("[migrate] Planning phases...", flush=True)
    prompt = build_phase_planning_prompt(input_repo_path, codebase_map=codebase_map)
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

    match = re.search(r"\[\s*\{.*\}\s*\]", result.stdout, re.DOTALL)
    if match:
        try:
            raw = json.loads(match.group(0))
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, list):
            phases: list[dict] = []
            seen: set[str] = set()
            for i, item in enumerate(raw):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                phase_id = _clean_phase_id(item["id"], i)
                if phase_id in seen:
                    phase_id = f"{phase_id}_{i + 1}"
                seen.add(phase_id)
                criteria = item.get("done_criteria")
                phases.append({
                    "id": phase_id,
                    "description": str(item.get("description") or phase_id),
                    "done_criteria": [str(c) for c in criteria] if isinstance(criteria, list) else [],
                })
            if phases:
                return phases
    print("[migrate] WARNING: could not parse a phase plan from Claude's "
          "output; falling back to ONE 'full_app' phase. Raw output "
          f"(first 500 chars): {result.stdout[:500]!r}", flush=True)
    return [{"id": "full_app", "description": "Entire application", "done_criteria": []}]


def _call_build(
    prompt: str, output_dir: Path, input_repo_path: str, timeout: int, what: str,
):
    """One writing Claude call with uniform usage-limit / failure handling."""
    result = run_claude_prompt(
        prompt, cwd=str(output_dir), timeout=timeout,
        extra_args=["--add-dir", str(input_repo_path)],
    )
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise UsageLimitError(
                f"Hit what looks like a usage/rate limit while {what}: "
                f"{result.stderr[:300] or result.stdout[:300]}. "
                f"Phases completed before this one are safely saved in "
                f"{output_dir} - resume to continue from here.",
                output_dir,
            )
        raise RuntimeError(
            f"{what} failed. returncode={result.returncode}, "
            f"stdout={result.stdout[:500]!r}"
        )
    return result


def _parse_phase_result(stdout: str) -> dict | None:
    """Returns the last PHASE_RESULT JSON object printed, or None."""
    found = None
    for match in re.finditer(r"PHASE_RESULT:\s*(\{.*\})", stdout):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            found = candidate
    return found


def _parity_missing_items(parity_file: Path) -> list[str]:
    """Lines in a PARITY_CHECK file that still carry a MISSING status."""
    missing = []
    for line in parity_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "DONE/MISSING" in line:  # legend/instruction line, not a status
            continue
        if re.search(r"\bMISSING\b", line):
            missing.append(line.strip()[:200])
    return missing


def _verify_phase(output_dir: Path, phase: dict, stdout: str) -> list[str]:
    """Returns a list of problems; an empty list means the phase is verified."""
    phase_id = phase["id"]
    problems: list[str] = []

    inventory = output_dir / f"EXISTING_UX_INVENTORY_{phase_id}.md"
    if not inventory.exists():
        problems.append(f"{inventory.name} was not written.")

    parity = output_dir / f"PARITY_CHECK_{phase_id}.md"
    if not parity.exists():
        problems.append(f"{parity.name} was not written.")
    else:
        for item in _parity_missing_items(parity):
            problems.append(f"Parity check still lists a MISSING item: {item}")

    result = _parse_phase_result(stdout)
    if result is None:
        problems.append("No PHASE_RESULT line was printed.")
    elif str(result.get("status", "")).upper() != "COMPLETE":
        missing = result.get("missing") or []
        detail = "; ".join(str(m) for m in missing) if missing else "no details given"
        problems.append(f"Phase reported INCOMPLETE: {detail}")

    return problems


def _run_phase(
    input_repo_path: str, output_dir: Path, phase: dict, findings_md: str,
    is_first_phase: bool, timeout: int, codebase_map: str,
) -> tuple[str, int]:
    """Call 2..N: execute one phase, verify its result, and make one
    targeted fix-up attempt if verification fails. Returns
    (stack_name, attempts)."""
    phase_id = phase["id"]
    print(f"[migrate] Building phase: {phase_id} ({phase.get('description', phase_id)})", flush=True)

    prompt = build_phase_prompt(
        input_repo_path, output_dir, phase, findings_md, is_first_phase,
        codebase_map=codebase_map,
    )
    result = _call_build(
        prompt, output_dir, input_repo_path, timeout,
        f"migrating phase '{phase_id}'",
    )
    stack_match = re.search(r"STACK_CHOSEN:\s*(.+)", result.stdout)
    stack = stack_match.group(1).strip() if stack_match else "unknown"

    problems = _verify_phase(output_dir, phase, result.stdout)
    attempts = 1
    if problems:
        print(f"[migrate] Phase '{phase_id}' failed verification ({len(problems)} problem(s)); running one fix-up pass:", flush=True)
        for p in problems:
            print(f"[migrate]   - {p}", flush=True)
        fix_prompt = build_phase_fix_prompt(output_dir, phase, problems)
        fix_result = _call_build(
            fix_prompt, output_dir, input_repo_path, DEFAULT_FIX_TIMEOUT,
            f"fixing phase '{phase_id}'",
        )
        attempts = 2
        fix_stack = re.search(r"STACK_CHOSEN:\s*(.+)", fix_result.stdout)
        if fix_stack:
            stack = fix_stack.group(1).strip()
        problems = _verify_phase(output_dir, phase, fix_result.stdout)

    if problems:
        details = "\n".join(f"  - {p}" for p in problems)
        raise PhaseVerificationError(
            f"Phase '{phase_id}' is still incomplete after a fix-up pass, so "
            f"the migration stopped here instead of advancing. Completed "
            f"phases are saved; resume to retry this phase.\n"
            f"Remaining problems:\n{details}",
            output_dir,
        )

    print(f"[migrate] Phase '{phase_id}' verified complete.", flush=True)
    return stack, attempts


def _assemble_modules(output_dir: Path, modules: list[dict], chosen_stack: str, timeout: int) -> None:
    if len(modules) <= 1:
        return
    print(f"[migrate] Assembling {len(modules)} phases into final app...", flush=True)
    prompt = build_assembly_prompt(output_dir, modules, chosen_stack)
    result = run_claude_prompt(prompt, cwd=str(output_dir), timeout=timeout)
    if not result.success:
        if _looks_like_usage_limit(result.stdout, result.stderr):
            raise UsageLimitError(
                f"Hit what looks like a usage/rate limit during the final "
                f"assembly pass. All phases are already migrated and saved in "
                f"{output_dir} - resume to retry just the assembly step.",
                output_dir,
            )
        raise RuntimeError(
            f"Assembly pass failed (per-phase output is still on disk at "
            f"{output_dir}, nothing was lost). returncode={result.returncode}, "
            f"stdout={result.stdout[:500]!r}"
        )


def _run_full_app_module_pipeline(
    input_repo_path: str, findings_md: str, scope: str | None,
    planning_timeout: int, per_module_timeout: int, assembly_timeout: int,
    resume_output_dir: str | None, codebase_map: str = "",
) -> tuple[Path, str, list[dict]]:
    """Phased full-app migration: plan, then execute and verify each
    phase in order, then assemble."""
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
        phases_state = state.get("phases", {})
    else:
        output_dir = Path(tempfile.mkdtemp(prefix="migration_output_"))
        modules = _plan_modules(input_repo_path, planning_timeout, codebase_map=codebase_map)
        completed_ids = set()
        chosen_stack = "unknown"
        assembly_done = False
        phases_state = {}
        (output_dir / PLAN_FILENAME).write_text(json.dumps(modules, indent=2), encoding="utf-8")
        _save_state(output_dir, _snapshot(scope, modules, completed_ids, chosen_stack, assembly_done, phases_state))

    print(f"[migrate] Plan: {len(modules)} phase(s) - {[m['id'] for m in modules]}", flush=True)
    if completed_ids:
        print(f"[migrate] Already completed (will skip): {sorted(completed_ids)}", flush=True)

    for i, phase in enumerate(modules):
        if phase["id"] in completed_ids:
            continue
        print(f"[migrate] === Phase {i + 1}/{len(modules)} ===", flush=True)
        stack, attempts = _run_phase(
            input_repo_path, output_dir, phase, findings_md,
            is_first_phase=(i == 0), timeout=per_module_timeout,
            codebase_map=codebase_map,
        )
        if stack != "unknown" or chosen_stack == "unknown":
            chosen_stack = stack

        completed_ids.add(phase["id"])
        phases_state[phase["id"]] = {"status": "complete", "attempts": attempts}
        _save_state(output_dir, _snapshot(scope, modules, completed_ids, chosen_stack, False, phases_state))

    if not assembly_done:
        _assemble_modules(output_dir, modules, chosen_stack, assembly_timeout)
        _save_state(output_dir, _snapshot(scope, modules, completed_ids, chosen_stack, True, phases_state))

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

    Full app -> the phased pipeline (_run_full_app_module_pipeline): plan,
    execute and verify each phase, assemble. RESUMABLE - if a phase or
    assembly hits a usage/rate limit, UsageLimitError carries output_dir;
    pass it back as resume_output_dir to continue from the next incomplete
    step without redoing finished phases or replanning.

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