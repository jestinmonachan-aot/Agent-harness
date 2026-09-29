"""Detached step executor. Invoked as:

    python -m harness.worker <job_id> <step_name> <params_json_path>

Runs entirely outside Streamlit's process tree (see job_runner.launch_step,
which starts this with CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS on
Windows). Writes status to SQLite via db.start_step/finish_step as it goes,
so Streamlit never blocks on this and a Streamlit crash can't kill it.

Params are passed via a JSON file (not argv) because findings/prompts can be
long and argv has length limits on some platforms.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from harness import db
from harness.repo_utils import clone_repo
from harness.claude_cli import run_claude_prompt
from harness.md_utils import generate_pdf_report, extract_json_findings
from harness.skills import build_analysis_prompt
from harness.migrate import migrate_and_push, UsageLimitError
from harness.deploy import dockerize_and_run

# Marker prefix used to encode a resumable usage-limit failure into the
# plain-text `error` column app.py already reads. Kept as a simple string
# marker (rather than a DB schema change) so this works with the existing
# db.finish_step(..., error=<str>) signature. Format:
#   RESUMABLE_USAGE_LIMIT::<output_dir>::<human message>
USAGE_LIMIT_MARKER = "RESUMABLE_USAGE_LIMIT::"


def run_analyze(job_id: str, params: dict) -> str:
    repo_url = params["repo_url"]
    selected_skills = params.get("skills") or ["security"]

    repo_path = clone_repo(repo_url)
    db.save_repo_path(job_id, str(repo_path))

    prompt = build_analysis_prompt(str(repo_path), selected_skills)
    result = run_claude_prompt(prompt, cwd=str(repo_path), timeout=1200)
    if not result.success:
        raise RuntimeError(result.stderr or "Claude CLI analysis failed")

    from harness.md_utils import extract_codebase_map  # add to existing import line instead if you prefer

    findings_list = extract_json_findings(result.stdout)
    codebase_map = extract_codebase_map(result.stdout)
    db.save_findings(job_id, json.dumps(findings_list))

    pdf_path = Path("reports") / f"analysis_report_{job_id}.pdf"
    generate_pdf_report(pdf_path, repo_url, findings_list, selected_skills)

    return json.dumps({
        "repo_path": str(repo_path),
        "findings": findings_list,
        "skills_used": selected_skills,
        "pdf_path": str(pdf_path),
        "codebase_map": codebase_map,
    })


def run_legacy_deploy(job_id: str, params: dict) -> str:
    from harness.legacy_deploy import deploy_legacy_app

    repo_path = params["repo_path"]
    result = deploy_legacy_app(repo_path)
    return json.dumps(result)


def run_migrate(job_id: str, params: dict) -> str:
    repo_path = params["repo_path"]
    findings = params["findings"]
    scope = params.get("scope", "full app")
    resume_output_dir = params.get("resume_output_dir")
    codebase_map = params.get("codebase_map", "")

    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except (json.JSONDecodeError, TypeError):
            pass

    if isinstance(findings, list):
        findings_md = "\n".join(
            f"- [{f.get('severity', 'info').upper()}] {f.get('title', '')}: "
            f"{f.get('description', '')}"
            for f in findings
        )
    else:
        findings_md = str(findings)

    if resume_output_dir:
        print(f"[migrate] Resuming migration from {resume_output_dir}", flush=True)

    out_url, out_path, stack_chosen, stack_reasoning = migrate_and_push(
        repo_path, findings_md, scope,
        resume_output_dir=resume_output_dir,
        codebase_map=codebase_map,
    )
    db.save_migration(job_id, out_url)

    return json.dumps({
        "repo_url": out_url,
        "repo_path": out_path,
        "stack_chosen": stack_chosen,
        "stack_reasoning": stack_reasoning,
    })


def run_deploy(job_id: str, params: dict) -> str:
    target_path = params["target_path"]
    container_name = params["container_name"]

    app_url = dockerize_and_run(target_path, container_name=container_name)
    db.save_deployment(job_id, app_url)

    return json.dumps({"app_url": app_url})


STEP_FUNCS = {
    "analyze": run_analyze,
    "migrate": run_migrate,
    "deploy": run_deploy,
    "legacy_deploy": run_legacy_deploy,
}


def main() -> None:
    if len(sys.argv) != 4:
        print("usage: python -m harness.worker <job_id> <step_name> <params_json_path>", file=sys.stderr)
        sys.exit(2)

    job_id = sys.argv[1]
    step_name = sys.argv[2]
    params_path = Path(sys.argv[3])

    db.init_db()  # idempotent; safe even though Streamlit already called it
    params = json.loads(params_path.read_text(encoding="utf-8"))

    func = STEP_FUNCS.get(step_name)
    if func is None:
        db.finish_step(job_id, step_name, "error", error=f"Unknown step: {step_name}")
        sys.exit(1)

    db.start_step(job_id, step_name)
    try:
        result = func(job_id, params)
        db.finish_step(job_id, step_name, "done", result=result)
        from harness.tracker import log_finished_step
        log_finished_step(job_id, step_name)
        db.record_step_deployment(job_id, step_name, result)
    except UsageLimitError as e:
        # Encode as resumable so app.py can offer "Resume migration"
        # instead of just showing a dead-end failure.
        db.finish_step(
            job_id, step_name, "error",
            error=f"{USAGE_LIMIT_MARKER}{e.output_dir}::{e}",
        )
        sys.exit(1)
    except RuntimeError as e:
        msg = str(e)
        if "usage/rate limit" in msg.lower() or "session limit" in msg.lower():
            # Same family of failure as UsageLimitError, but raised as a plain
            # RuntimeError by call sites with nothing to resume (e.g. planning,
            # which fails before any output exists). Mark it the same way so
            # app.py shows a clean one-liner instead of a traceback, just
            # without an output_dir (no "Resume migration" button).
            db.finish_step(
                job_id, step_name, "error",
                error=f"{USAGE_LIMIT_MARKER}::{msg}",
            )
        else:
            db.finish_step(job_id, step_name, "error", error=traceback.format_exc())
        sys.exit(1)
    except Exception:
        db.finish_step(job_id, step_name, "error", error=traceback.format_exc())
        sys.exit(1)
    finally:
        try:
            params_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()