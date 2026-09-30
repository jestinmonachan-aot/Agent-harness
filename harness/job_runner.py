"""Launches harness.worker as a detached OS process (Windows-safe) and
gives Streamlit a tiny polling API. Nothing here blocks — app.py should
call launch_step() on a button click, then rely on st.rerun() + get_step()
to reflect progress on each poll.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from harness import db

# On Windows this fully detaches the child from Streamlit's process group —
# a Streamlit crash/restart cannot take the worker down with it.
if sys.platform == "win32":
    _CREATIONFLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
else:
    _CREATIONFLAGS = 0

_PARAMS_DIR = Path(tempfile.gettempdir()) / "harness_step_params"
_PARAMS_DIR.mkdir(exist_ok=True)

_LOG_DIR = Path("data/worker_logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# The project root — the parent of this harness/ package directory.
# Passed as cwd so `python -m harness.worker` resolves regardless of
# what directory `streamlit run` happened to be launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def launch_step(job_id: str, step_name: str, params: dict) -> None:
    """Fire-and-forget: starts the worker for this step and returns
    immediately. Marks the step 'running' before the subprocess even
    starts, so a poll right after this call already shows progress.

    If the subprocess itself fails to launch (bad interpreter path, etc.)
    that's caught synchronously below and recorded as an 'error' step
    immediately — it never gets stuck showing 'running' forever."""
    params_path = _PARAMS_DIR / f"{job_id}_{step_name}.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    db.start_step(job_id, step_name)

    log_path = _LOG_DIR / f"{job_id}_{step_name}.log"
    log_file = open(log_path, "wb")  # left open deliberately; child inherits the fd

    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        cwd=str(_PROJECT_ROOT),
        close_fds=True,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATIONFLAGS
    else:
        kwargs["start_new_session"] = True  # POSIX equivalent of detaching

    try:
        subprocess.Popen(
            [sys.executable, "-m", "harness.worker", str(job_id), step_name, str(params_path)],
            **kwargs,
        )
    except OSError as e:
        # Interpreter not found, permissions issue, etc. — the subprocess
        # never started, so worker.py never got a chance to record its
        # own error. Record it here instead of leaving 'running' forever.
        db.finish_step(job_id, step_name, "error", error=f"Failed to launch worker process: {e}")
    finally:
        log_file.close()


def get_worker_log(job_id: str, step_name: str) -> str:
    """Returns the worker subprocess's captured stdout+stderr for this
    step, or '' if no log exists yet. Useful when a step is stuck on
    'running' with no DB error — the log shows what's actually happening
    (e.g. a stack trace from before the try/except in worker.main even
    engaged, or git/docker output)."""
    log_path = _LOG_DIR / f"{job_id}_{step_name}.log"
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8", errors="replace")


def get_step_status(job_id: str, step_name: str) -> dict | None:
    """Returns {'status': 'running'|'done'|'error', 'result': dict|None, 'error': str}
    or None if the step hasn't been launched yet."""
    row = db.get_step(job_id, step_name)
    if row is None:
        return None
    result = json.loads(row["result"]) if row["result"] else None
    return {"status": row["status"], "result": result, "error": row["error"]}


def resume_or_new_job(repo_url: str) -> int:
    """On app restart, reuse the most recent job for this repo_url if one
    exists, so a completed 'analyze' step isn't re-run for nothing."""
    existing = db.find_latest_job_for_repo(repo_url)
    if existing is not None:
        return existing["id"]
    return db.create_job(repo_url)