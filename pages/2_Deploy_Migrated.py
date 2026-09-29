"""Deployment page for migrated apps.

Pick a migrated app from the dropdown, deploy it, and get its URL. All
deployed migrated apps are listed at the bottom with clickable links.
"""

import time
from datetime import datetime

import streamlit as st

from harness import db, job_runner
from harness.deploy import stop_deployment

CONTAINER_NAME = "migrated_app"  # same name app.py uses, so "Start over" there still stops it

st.set_page_config(page_title="Deploy Migrated App", layout="wide")
db.init_db()

st.title("Deploy migrated app")
st.caption("Deploys a migrated application and gives you its URL.")

apps = db.list_jobs_with_done_step("migrate")
should_poll = False

if not apps:
    st.info("No migrated apps yet. Run a migration on the main page first.")
else:
    labels = [f"{a['app_name']} (job {a['job_id']})" for a in apps]
    default_index = 0
    current_job = st.session_state.get("job_id")
    for i, a in enumerate(apps):
        if a["job_id"] == current_job:
            default_index = i
            break

    choice = st.selectbox("Migrated app", labels, index=default_index)
    app = apps[labels.index(choice)]
    job_id = app["job_id"]
    target_path = app["result"].get("repo_path")
    stack = app["result"].get("stack_chosen")
    if stack:
        st.caption(f"Stack: {stack}")

    deploy_status = job_runner.get_step_status(job_id, "deploy")
    deploy_running = bool(deploy_status and deploy_status["status"] == "running")
    deploy_done = bool(deploy_status and deploy_status["status"] == "done")

    if not target_path:
        st.error("This job's migration result has no output path, so it can't be deployed.")
    elif st.button("Deploy app", type="primary", key="run_deploy_btn",
                   disabled=deploy_running or deploy_done):
        job_runner.launch_step(
            job_id, "deploy",
            {"target_path": target_path, "container_name": CONTAINER_NAME},
        )
        st.rerun()

    if deploy_status:
        if deploy_status["status"] == "running":
            st.info("Deploying... this page refreshes automatically.")
            with st.expander("Progress log", expanded=True):
                st.code(job_runner.get_worker_log(job_id, "deploy") or "(no output yet)")
            should_poll = True
        elif deploy_status["status"] == "error":
            first_line = deploy_status["error"].strip().splitlines()[-1]
            st.error(f"Deployment failed: {first_line}")
            with st.expander("Full details"):
                st.code(deploy_status["error"])
        elif deploy_status["status"] == "done":
            url = deploy_status["result"]["app_url"]
            st.success(f"App live at: {url}")
            st.link_button("Open app", url)

    if deploy_status and not deploy_running:
        if st.button("Stop and reset", key="reset_deploy_btn"):
            stop_deployment(CONTAINER_NAME)
            db.clear_step(job_id, "deploy")
            st.rerun()

st.divider()
st.subheader("Deployed apps")
st.caption("Only one migrated app can run at a time (they share one container name), "
           "so older entries may no longer be reachable.")
deployed = db.list_deployments("migrated")
if deployed:
    st.dataframe(
        [
            {
                "App": d["app_name"],
                "URL": d["url"],
                "Deployed": datetime.fromtimestamp(d["created_at"]).strftime("%d/%m/%Y %H:%M"),
            }
            for d in deployed
        ],
        column_config={"URL": st.column_config.LinkColumn("URL")},
        hide_index=True,
        use_container_width=True,
    )
else:
    st.caption("No migrated apps deployed yet.")

if should_poll:
    time.sleep(2)
    st.rerun()