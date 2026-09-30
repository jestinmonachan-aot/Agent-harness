"""Legacy app deployment page.

Pick an analyzed legacy app from the dropdown, deploy it as-is in its own
Docker environment, and get its URL. All deployed legacy apps are listed
at the bottom with clickable links.
"""

import time
from datetime import datetime

import streamlit as st

from harness import db, job_runner
from harness.legacy_deploy import (
    stop_legacy_app, app_status, pause_legacy_app, start_legacy_app,
)

st.set_page_config(page_title="Legacy App Deployment", layout="wide")
db.init_db()

st.title("Deploy legacy app")
st.caption("Brings up the original application in its own Docker environment, unmodified.")

apps = db.list_jobs_with_done_step("analyze")
should_poll = False

if not apps:
    st.info("No analyzed apps yet. Run analysis on the main page first.")
else:
    labels = [f"{a['app_name']} (job {a['job_id']})" for a in apps]
    default_index = 0
    current_job = st.session_state.get("job_id")
    for i, a in enumerate(apps):
        if a["job_id"] == current_job:
            default_index = i
            break

    choice = st.selectbox("Legacy app", labels, index=default_index)
    app = apps[labels.index(choice)]
    job_id = app["job_id"]
    repo_path = app["result"].get("repo_path")

    legacy_status = job_runner.get_step_status(job_id, "legacy_deploy")
    legacy_running = bool(legacy_status and legacy_status["status"] == "running")
    legacy_done = bool(legacy_status and legacy_status["status"] == "done")

    if not repo_path:
        st.error("This job's analysis result has no repository path, so it can't be deployed.")
    elif st.button("Deploy legacy app", type="primary", key="run_legacy_deploy_btn",
                   disabled=legacy_running or legacy_done):
        job_runner.launch_step(job_id, "legacy_deploy", {"repo_path": repo_path})
        st.rerun()

    if legacy_status:
        if legacy_status["status"] == "running":
            st.info("Deploying legacy app... this page refreshes automatically.")
            with st.expander("Progress log", expanded=True):
                st.code(job_runner.get_worker_log(job_id, "legacy_deploy") or "(no output yet)")
            should_poll = True
        elif legacy_status["status"] == "error":
            first_line = legacy_status["error"].strip().splitlines()[-1]
            st.error(f"Legacy deployment failed: {first_line}")
            with st.expander("Full details"):
                st.code(legacy_status["error"])
        elif legacy_status["status"] == "done":
            url = legacy_status["result"]["url"]
            st.success(f"Legacy app live at: {url}")
            st.link_button("Open app", url)

    if legacy_status and not legacy_running and repo_path:
        if st.button("Stop and reset", key="reset_legacy_btn"):
            stop_legacy_app(repo_path, job_id)
            db.clear_step(job_id, "legacy_deploy")
            st.rerun()

st.divider()
st.subheader("Deployed legacy apps")
deployed = db.list_deployments("legacy")
if not deployed:
    st.caption("No legacy apps deployed yet.")
for d in deployed:
    jid = d["job_id"]
    job = db.get_job(jid)
    rp = job["repo_path"] if job else None
    status = app_status(rp, jid) if rp else "unknown"
    name, _, ver = d["app_name"].partition("#")
    when = datetime.fromtimestamp(d["created_at"]).strftime("%d/%m/%Y %H:%M")
    cols = st.columns([2, 1, 3, 1.3, 1, 1])
    cols[0].markdown(f"**{name}**")
    cols[1].write(ver or "-")
    cols[2].markdown(f"[{d['url']}]({d['url']})<br><small>{when}</small>", unsafe_allow_html=True)
    cols[3].write({"running": "🟢 Running", "stopped": "⚪ Stopped"}.get(status, "❔ Unknown"))
    if status == "running":
        if cols[4].button("Stop", key=f"stop_{jid}"):
            pause_legacy_app(rp, jid)
            st.rerun()
    elif status == "stopped":
        if cols[4].button("Start", key=f"start_{jid}"):
            err = None
            with st.spinner("Starting..."):
                try:
                    start_legacy_app(rp, jid, d["url"])
                except RuntimeError as e:
                    err = str(e)
            if err:
                st.error(err)
            else:
                st.rerun()
    if cols[5].button("Delete", key=f"del_{jid}"):
        st.session_state[f"confirm_del_{jid}"] = True
    if st.session_state.get(f"confirm_del_{jid}"):
        st.warning(f"Delete {name} {ver}? This removes its containers and database.")
        y, n, _sp = st.columns([1, 1, 6])
        if y.button("Yes, delete", key=f"yes_{jid}"):
            if rp:
                stop_legacy_app(rp, jid)
            db.clear_step(jid, "legacy_deploy")
            st.session_state.pop(f"confirm_del_{jid}", None)
            st.rerun()
        if n.button("Cancel", key=f"no_{jid}"):
            st.session_state.pop(f"confirm_del_{jid}", None)
            st.rerun()

# Poll last, so the deployed-apps list above still renders while a run is in progress.
if should_poll:
    time.sleep(2)
    st.rerun()