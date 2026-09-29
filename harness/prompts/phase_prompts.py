"""Prompts for the phased migration flow.

Call 1 (plan mode, read-only): build_phase_planning_prompt asks for an
ordered list of phases, each with concrete, checkable done_criteria.

Call 2..N (one per phase): build_phase_prompt reuses the existing
per-module prompt from migration_prompts.py and appends the phase's
done criteria plus a machine-readable PHASE_RESULT line, which
harness/migrate.py parses to confirm the phase really completed before
moving to the next one.

If verification fails, build_phase_fix_prompt is used for one targeted
fix-up call that addresses only the listed problems.
"""

from __future__ import annotations

from pathlib import Path

from harness.prompts.migration_prompts import _codebase_map_block, build_module_prompt


def build_phase_planning_prompt(input_repo_path: str, codebase_map: str = "") -> str:
    codebase_map_block = _codebase_map_block(
        codebase_map, "identifying the app's migration phases"
    )
    return (
        "You are planning a PHASED migration of the ENTIRE application to a "
        f"modern stack. Legacy source (read-only): {input_repo_path}\n\n"
        f"{codebase_map_block}"
        "Do not write or modify any files. Only read and plan.\n\n"
        "Break the migration into ordered phases, one per distinct functional "
        "module (e.g. 'auth', 'tickets', 'assets', 'reporting'). If the "
        "codebase map above is provided, use it instead of re-exploring the "
        "directory structure. Typically 3-8 phases. Keep each phase small "
        "enough to be read and reimplemented in roughly 30-45 minutes of "
        "focused work; split a large area into two phases rather than "
        "proposing one oversized phase.\n\n"
        "Order phases so foundational/shared ones (auth, core data model, "
        "users) come BEFORE phases that depend on them.\n\n"
        "For each phase, give 2-5 done_criteria: concrete, checkable "
        "statements a reviewer could verify by running the app (for example "
        "'ticket list shows the same columns as the legacy list view' or "
        "'creating a ticket through the form persists it'). Avoid vague "
        "criteria like 'works well'.\n\n"
        "Respond with ONLY a JSON array, nothing else, no markdown fences. "
        "Use simple ids (letters, digits, underscores):\n"
        '[{"id": "auth", "description": "Login, sessions, permissions", '
        '"done_criteria": ["Users can log in and out", '
        '"Protected pages redirect to login"]}, ...]\n'
    )


def _phase_result_instructions(phase_id: str) -> str:
    return (
        "\n\nPHASE RESULT (required):\n"
        "Immediately BEFORE the final STACK_CHOSEN line (STACK_CHOSEN must "
        "still be the very last line), output exactly one line of JSON in "
        "this form:\n"
        f'PHASE_RESULT: {{"phase": "{phase_id}", "status": "COMPLETE", '
        '"missing": []}\n'
        'Use "COMPLETE" only if every done criterion above is met, the '
        f"PARITY_CHECK_{phase_id}.md file has no MISSING items, and you "
        'actually ran the smoke tests. Otherwise use "INCOMPLETE" and list '
        'what is unfinished in "missing". Do not claim COMPLETE to be safe; '
        "an honest INCOMPLETE triggers a fix-up pass.\n"
    )


def build_phase_prompt(
    input_repo_path: str,
    output_dir: Path,
    phase: dict,
    findings_md: str,
    is_first_phase: bool,
    codebase_map: str = "",
) -> str:
    base = build_module_prompt(
        input_repo_path, output_dir, phase, findings_md, is_first_phase,
        codebase_map=codebase_map,
    )
    criteria = phase.get("done_criteria") or []
    criteria_block = ""
    if criteria:
        criteria_block = (
            "\n\nThis phase is only COMPLETE when ALL of these are true:\n"
            + "\n".join(f"- {c}" for c in criteria)
        )
    return base + criteria_block + _phase_result_instructions(phase["id"])


def build_phase_fix_prompt(output_dir: Path, phase: dict, problems: list[str]) -> str:
    phase_id = phase["id"]
    problem_list = "\n".join(f"- {p}" for p in problems)
    return (
        f"The '{phase_id}' phase ({phase.get('description', phase_id)}) of a "
        f"migration in {output_dir} was checked and is NOT complete yet. "
        "Problems found:\n"
        f"{problem_list}\n\n"
        "Fix ONLY these problems. Do not redo work that is already done and "
        "do not touch other phases. Read "
        f"EXISTING_UX_INVENTORY_{phase_id}.md and "
        f"PARITY_CHECK_{phase_id}.md first to see what is missing. Do NOT "
        "modify the legacy source.\n\n"
        "When finished:\n"
        f"1. Rewrite PARITY_CHECK_{phase_id}.md so every item has an "
        "honest DONE/MISSING status.\n"
        "2. Re-run the phase's smoke tests for real.\n"
        "3. Output one line of JSON: "
        f'PHASE_RESULT: {{"phase": "{phase_id}", "status": "COMPLETE", '
        '"missing": []} (use "INCOMPLETE" and list what remains in '
        '"missing" if something is still unfinished).\n'
        "4. As the LAST LINE, output exactly: STACK_CHOSEN: <short stack name>\n"
    )