import csv
import json
import tempfile
from datetime import datetime
from pathlib import Path

TRACKER_FILE = Path("run_tracker.csv")
HEADERS = ["Legacy App Name", "Module", "Action", "Time taken",
           "Limit usage %", "Claude code Account Type", "Date and Time"]

STEP_TO_ACTION = {
    "analyze": "Analysis",
    "plan": "Migration planning",
    "migrate": "Migration",
    "deploy": "Deployment",
}

def _to_dt(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v)
    return datetime.fromisoformat(v)

def log_step(app_name, module, action, started_at, ended_at, account_type="Claude Pro"):
    try:
        s = _to_dt(started_at)
        e = _to_dt(ended_at)
        mins = round((e - s).total_seconds() / 60, 1)
        new_file = not TRACKER_FILE.exists()
        with open(TRACKER_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(HEADERS)
            w.writerow([app_name, module, action, f"{mins} min", "",
                        account_type, s.strftime("%d/%m/%Y %H:%M")])
    except Exception as ex:
        print(f"tracker: could not log step: {ex}")


def log_finished_step(job_id, step_name, account_type="Claude Pro"):
    if step_name not in STEP_TO_ACTION:
        return
    try:
        from harness import db
        row = db.get_step(job_id, step_name)
        job = db.get_job(job_id)
        if not row or not job:
            return
        app_name = job["repo_url"].rstrip("/").split("/")[-1].removesuffix(".git")

        params_path = Path(tempfile.gettempdir()) / "harness_step_params" / f"{job_id}_{step_name}.json"
        module = "All"
        if params_path.exists():
            module = json.loads(params_path.read_text(encoding="utf-8")).get("scope") or "All"

        log_step(app_name, module, STEP_TO_ACTION[step_name],
                 row["started_at"], row["updated_at"], account_type)
    except Exception as ex:
        print(f"tracker: {ex}")