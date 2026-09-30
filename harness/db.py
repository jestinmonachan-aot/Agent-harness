"""SQLite persistence for the agent harness. Three tables:

  jobs         — one row per repo analysis session (repo_url, findings,
                 migration/deployment results)
  steps        — one row per (job_id, step_name) execution, updated live by
                 worker.py so a crash mid-run still leaves an accurate status
                 to resume from.
  deployments  — one row per (job_id, kind) live deployment, kind being
                 'legacy' (the original app) or 'migrated'. Feeds the deploy
                 pages' "deployed apps" lists.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("data/harness_data.db")

# Which deployment kind each deploy step produces, and which result key
# holds its URL.
_STEP_DEPLOYMENT_KIND = {"legacy_deploy": "legacy", "deploy": "migrated"}


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reader (Streamlit) + writer (worker)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_url        TEXT NOT NULL,
                repo_path       TEXT,
                findings        TEXT,
                migration_url   TEXT,
                deployment_url  TEXT,
                created_at      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER NOT NULL REFERENCES jobs(id),
                step_name   TEXT NOT NULL,       -- 'analyze' | 'migrate' | 'deploy' | 'legacy_deploy'
                status      TEXT NOT NULL,       -- 'running' | 'done' | 'error'
                result      TEXT,                -- JSON-ish string payload, step-specific
                error       TEXT,
                started_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                UNIQUE(job_id, step_name)
            );

            CREATE TABLE IF NOT EXISTS deployments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      INTEGER NOT NULL REFERENCES jobs(id),
                kind        TEXT NOT NULL,       -- 'legacy' | 'migrated'
                app_name    TEXT NOT NULL,
                url         TEXT NOT NULL,
                created_at  REAL NOT NULL,
                UNIQUE(job_id, kind)
            );
            """
        )


def app_name_from_url(repo_url: str) -> str:
    return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")


# ---------------------------------------------------------------- jobs ----

def create_job(repo_url: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (repo_url, created_at) VALUES (?, ?)",
            (repo_url, time.time()),
        )
        return cur.lastrowid


def get_job(job_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def save_repo_path(job_id: int, repo_path: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET repo_path = ? WHERE id = ?", (repo_path, job_id))


def save_findings(job_id: int, findings: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET findings = ? WHERE id = ?", (findings, job_id))


def save_migration(job_id: int, migration_url: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET migration_url = ? WHERE id = ?", (migration_url, job_id))


def save_deployment(job_id: int, deployment_url: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE jobs SET deployment_url = ? WHERE id = ?", (deployment_url, job_id))


def find_latest_job_for_repo(repo_url: str) -> sqlite3.Row | None:
    """Used on Streamlit restart to resume a job instead of starting fresh."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE repo_url = ? ORDER BY created_at DESC LIMIT 1",
            (repo_url,),
        ).fetchone()


# --------------------------------------------------------------- steps ----

def start_step(job_id: int, step_name: str) -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO steps (job_id, step_name, status, started_at, updated_at)
            VALUES (?, ?, 'running', ?, ?)
            ON CONFLICT(job_id, step_name) DO UPDATE SET
                status = 'running', result = NULL, error = NULL,
                started_at = excluded.started_at, updated_at = excluded.updated_at
            """,
            (job_id, step_name, now, now),
        )


def finish_step(job_id: int, step_name: str, status: str, result: str = "", error: str = "") -> None:
    assert status in ("done", "error")
    with _conn() as conn:
        conn.execute(
            """
            UPDATE steps SET status = ?, result = ?, error = ?, updated_at = ?
            WHERE job_id = ? AND step_name = ?
            """,
            (status, result, error, time.time(), job_id, step_name),
        )


def get_step(job_id: int, step_name: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM steps WHERE job_id = ? AND step_name = ?",
            (job_id, step_name),
        ).fetchone()


def get_all_steps(job_id: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM steps WHERE job_id = ? ORDER BY started_at", (job_id,)
        ).fetchall()


def clear_step(job_id: int, step_name: str) -> None:
    """Removes a step's row entirely so it can be re-launched fresh
    (e.g. switching migration scope from 'tickets' to 'auth'). Clearing a
    deploy step also removes its entry from the deployed-apps list."""
    kind = _STEP_DEPLOYMENT_KIND.get(step_name)
    with _conn() as conn:
        conn.execute(
            "DELETE FROM steps WHERE job_id = ? AND step_name = ?",
            (job_id, step_name),
        )
        if kind:
            conn.execute(
                "DELETE FROM deployments WHERE job_id = ? AND kind = ?",
                (job_id, kind),
            )


def list_jobs_with_done_step(step_name: str) -> list[dict]:
    """Jobs whose given step finished successfully, newest first. Each item:
    {"job_id", "repo_url", "app_name", "result": <parsed JSON dict>}.
    Used to fill the app dropdowns ('analyze' -> legacy apps, 'migrate' ->
    migrated apps)."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT j.id AS job_id, j.repo_url AS repo_url, s.result AS result
            FROM jobs j JOIN steps s ON s.job_id = j.id
            WHERE s.step_name = ? AND s.status = 'done'
            ORDER BY s.updated_at DESC
            """,
            (step_name,),
        ).fetchall()
    items = []
    for r in rows:
        try:
            result = json.loads(r["result"]) if r["result"] else {}
        except json.JSONDecodeError:
            result = {}
        items.append({
            "job_id": r["job_id"],
            "repo_url": r["repo_url"],
            "app_name": app_name_from_url(r["repo_url"]),
            "result": result,
        })
    return items


# --------------------------------------------------------- deployments ----

def record_deployment(job_id: int, kind: str, url: str) -> None:
    assert kind in ("legacy", "migrated")
    job = get_job(job_id)
    name = app_name_from_url(job["repo_url"]) if job else f"job-{job_id}"
    with _conn() as conn:
        conn.execute(
            "DELETE FROM deployments WHERE kind = ? AND url = ? AND job_id != ?",
            (kind, url, job_id),
        )
        conn.execute(
            """
            INSERT INTO deployments (job_id, kind, app_name, url, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id, kind) DO UPDATE SET
                app_name = excluded.app_name, url = excluded.url,
                created_at = excluded.created_at
            """,
            (job_id, kind, name, url, time.time()),
        )


def record_step_deployment(job_id: int, step_name: str, result: str) -> None:
    """Called by worker.py after a deploy step finishes successfully.
    Pulls the URL out of the step's JSON result and records it. Ignores
    non-deploy steps and unparseable results."""
    kind = _STEP_DEPLOYMENT_KIND.get(step_name)
    if kind is None:
        return
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return
    url = data.get("url") or data.get("app_url") if isinstance(data, dict) else None
    if url:
        record_deployment(job_id, kind, url)


def list_deployments(kind: str | None = None) -> list[sqlite3.Row]:
    with _conn() as conn:
        if kind:
            return conn.execute(
                "SELECT * FROM deployments WHERE kind = ? ORDER BY created_at DESC",
                (kind,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM deployments ORDER BY created_at DESC"
        ).fetchall()


def delete_deployment(job_id: int, kind: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM deployments WHERE job_id = ? AND kind = ?",
            (job_id, kind),
        )