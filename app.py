"""
UI for the legacy app modernization workflow:
  1. Select analysis focus areas, paste a repo link
  2. Analyze the codebase for the selected focus areas only
  3. Review findings (plain-language summary first, technical detail on click)
  4. Migrate to a modern stack
  5. Deploy and get a live URL
"""

import time
from pathlib import Path

import streamlit as st

from harness import db, job_runner
from harness.claude_cli import check_claude_available
from harness.skills import SKILLS
from harness.md_utils import generate_pdf_report

USAGE_LIMIT_MARKER = "RESUMABLE_USAGE_LIMIT::"

st.set_page_config(
    page_title="Legacy App Modernization",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; max-width: 1100px; }
    h1 { font-weight: 800 !important; letter-spacing: -0.5px; }
    h2, h3, h4 { font-weight: 700 !important; }
    div[data-testid="stCheckbox"] label p { font-size: 1.02rem !important; }

    .stButton>button {
        border-radius: 8px;
        padding: 0.55rem 1.4rem;
        font-weight: 600;
        transition: all 0.15s ease;
    }
    .stButton>button[kind="primary"] {
        background-color: #E02424 !important;
        border: 1px solid #E02424 !important;
        color: #ffffff !important;
    }
    .stButton>button[kind="primary"]:hover {
        background-color: #C81E1E !important;
        border-color: #C81E1E !important;
    }
    .stButton>button[kind="primary"]:disabled {
        background-color: #7a2323 !important;
        border-color: #7a2323 !important;
        color: #d9a3a3 !important;
        opacity: 1 !important;
    }
    .stButton>button:hover { transform: translateY(-1px); }

    /* Start-over button: neutral grey, distinct from primary red */
    .st-key-start_over_btn button {
        background-color: #3a4048 !important;
        border: 1px solid #58606a !important;
        color: #e6edf3 !important;
    }
    .st-key-start_over_btn button:hover {
        background-color: #474e58 !important;
        border-color: #6a7280 !important;
    }

    /* Skills container: subtle grey tint so it reads as a grouped panel */
    .st-key-skills_container {
        background-color: #1c2128 !important;
        border-color: #30363d !important;
    }

    /* Step badges */
    .step-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 3px 10px;
        border-radius: 20px;
        font-size: 0.78rem;
        font-weight: 600;
        margin-left: 8px;
    }
    .step-done { background: #1a3a2a; color: #4ade80; }
    .step-running { background: #2a2a1a; color: #facc15; }
    .step-error { background: #3a1a1a; color: #f87171; }
    .step-pending { background: #24292f; color: #8b949e; }

    /* Section cards */
    .section-header {
        margin-bottom: 0.4rem;
    }
    .section-header h3 { margin: 0 !important; }

    hr { margin: 2rem 0 !important; opacity: 0.15; }
    </style>
    """,
    unsafe_allow_html=True,
)


def step_badge(status: str | None) -> str:
    """Return an HTML badge for a step's status (running/error only)."""
    labels = {
        "running": ("● Running", "step-running"),
        "error": ("✕ Error", "step-error"),
    }
    if status not in labels:
        return ""
    text, cls = labels[status]
    return f'<span class="step-badge {cls}">{text}</span>'


def section_header(title: str, status: str | None):
    badge = step_badge(status)
    st.markdown(
        f'<div class="section-header"><h3>{title}{" " + badge if badge else ""}</h3></div>',
        unsafe_allow_html=True,
    )


db.init_db()

st.title("Legacy App Modernization")

for key in ["job_id", "repo_url"]:
    if key not in st.session_state:
        st.session_state[key] = None

with st.sidebar:
    st.subheader("Status")
    check = check_claude_available()
    if check.success:
        st.success("Ready")
    else:
        st.error("Not available — check setup.")
    try:
        st.page_link("pages/1_Legacy_Deploy.py", label="Deploy the legacy app as-is")
    except Exception:
        pass   

st.subheader("Select analysis focus areas")
st.caption("Select the skills that you want in this application.")

skill_ids = list(SKILLS.keys())
selected_skills = []

with st.container(border=True, key="skills_container"):
    n_cols = min(3, len(skill_ids))
    skill_rows = [skill_ids[i:i + n_cols] for i in range(0, len(skill_ids), n_cols)]
    for row in skill_rows:
        cols = st.columns(n_cols)
        for col, skill_id in zip(cols, row):
            with col:
                with st.container(border=True):
                    checked = st.checkbox(
                        f"**{SKILLS[skill_id]['label']}**",
                        value=True,
                        key=f"skill_{skill_id}",
                    )
                    if checked:
                        selected_skills.append(skill_id)

st.subheader("Repository")
repo_col, btn_col = st.columns([4, 1])
with repo_col:
    repo_url = st.text_input(
        "Repository URL",
        placeholder="https://github.com/org/app",
        label_visibility="collapsed",
    )
with btn_col:
    run_clicked = st.button("Run analysis", type="primary", disabled=not repo_url, use_container_width=True)

st.write("")

if run_clicked:
    job_id = job_runner.resume_or_new_job(repo_url)
    existing_status = job_runner.get_step_status(job_id, "analyze")
    if not existing_status or existing_status["status"] != "done":
        job_runner.launch_step(job_id, "analyze", {"repo_url": repo_url, "skills": selected_skills})
    st.session_state.job_id = job_id
    st.session_state.repo_url = repo_url
    st.rerun()

job_id = st.session_state.job_id

if job_id is not None:
    job = db.get_job(job_id)
    analyze_status = job_runner.get_step_status(job_id, "analyze")

    if analyze_status and analyze_status["status"] == "running":
        st.info("Analyzing... this page refreshes automatically.")
        with st.expander("Progress log"):
            st.code(job_runner.get_worker_log(job_id, "analyze") or "(no output yet)")
        time.sleep(2)
        st.rerun()

    elif analyze_status and analyze_status["status"] == "error":
        first_line = analyze_status["error"].strip().splitlines()[-1]
        st.error(f"Analysis failed: {first_line}")
        with st.expander("Full details"):
            st.code(analyze_status["error"])

    elif analyze_status and analyze_status["status"] == "done":
        result_data = analyze_status["result"]
        findings = result_data["findings"]
        repo_path = result_data["repo_path"]
        skills_used = result_data.get("skills_used", [])
        codebase_map = result_data.get("codebase_map", "")

        st.divider()
        section_header("Findings", "done")

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        severity_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "info": "⚪"}

        if findings:
            counts = {}
            for f in findings:
                sev = f.get("severity", "info")
                counts[sev] = counts.get(sev, 0) + 1
            metric_cols = st.columns(5)
            for col, sev in zip(metric_cols, ["critical", "high", "medium", "low", "info"]):
                with col:
                    st.metric(f"{severity_icon[sev]} {sev.title()}", counts.get(sev, 0))
            st.write("")

        if not findings:
            st.info("No issues found.")
        else:
            grouped = {}
            for f in findings:
                cat = f.get("category", "general")
                grouped.setdefault(cat, []).append(f)

            ordered_categories = [s for s in skills_used if s in grouped] + \
                                  [c for c in grouped if c not in skills_used]

            for cat in ordered_categories:
                cat_label = SKILLS.get(cat, {}).get("label", cat.replace("_", " ").title())
                cat_findings = sorted(grouped[cat], key=lambda x: severity_order.get(x.get("severity", "info"), 4))
                st.markdown(f"#### {cat_label}  `{len(cat_findings)}`")
                for f in cat_findings:
                    icon = severity_icon.get(f.get("severity", "info"), "⚪")
                    title = f.get("title", "Untitled finding")
                    summary = f.get("summary") or f.get("description", "")
                    with st.expander(f"{icon}  **{title}**  —  {summary}"):
                        if f.get("location") or f.get("description"):
                            st.caption("Technical details")
                            if f.get("location"):
                                st.code(f.get("location"), language=None)
                            if f.get("description"):
                                st.write(f.get("description", ""))
                        if f.get("recommendation"):
                            st.info(f"**Recommendation:** {f.get('recommendation')}")
                st.write("")

        pdf_path = Path("data/reports") / f"analysis_report_{job_id}.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        findings_key = f"pdf_findings_hash_{job_id}"
        current_hash = hash(str(findings))
        if st.session_state.get(findings_key) != current_hash:
            generate_pdf_report(pdf_path, st.session_state.repo_url, findings, skills_used)
            st.session_state[findings_key] = current_hash

        st.download_button(
            "Download report (PDF)",
            data=pdf_path.read_bytes(),
            file_name="analysis_report.pdf",
            mime="application/pdf",
            key="download_pdf_btn",
        )

        st.divider()
        migrate_status_check = job_runner.get_step_status(job_id, "migrate")
        deploy_status_check = job_runner.get_step_status(job_id, "deploy")

        section_header("Migrate to a modern stack", migrate_status_check["status"] if migrate_status_check else None)

        from harness.md_utils import extract_module_names
        suggested_modules = extract_module_names(codebase_map) if codebase_map else []
        scope_options = ["Full app"] + suggested_modules + ["Other (type below)"]

        scope_choice = st.selectbox(
            "What to migrate",
            scope_options,
            key="scope_choice",
        )
        if scope_choice == "Other (type below)":
            scope_input = st.text_input(
                "Module/feature name",
                key="scope_input_custom",
            )
        elif scope_choice == "Full app":
            scope_input = "full app"
        else:
            scope_input = scope_choice

        migrate_status = job_runner.get_step_status(job_id, "migrate")
        migrate_running = bool(migrate_status and migrate_status["status"] == "running")
        migrate_done = bool(migrate_status and migrate_status["status"] == "done")

        resume_output_dir = None
        usage_limit_no_resume_msg = None
        if migrate_status and migrate_status["status"] == "error" and migrate_status["error"]:
            err_text = migrate_status["error"]
            if err_text.startswith(USAGE_LIMIT_MARKER):
                rest = err_text[len(USAGE_LIMIT_MARKER):]
                output_dir_part, _, human_msg = rest.partition("::")
                if output_dir_part:
                    resume_output_dir = output_dir_part
                else:
                    usage_limit_no_resume_msg = human_msg

        if resume_output_dir:
            st.warning(
                "Migration paused — a usage limit was hit partway. "
                "Progress so far is saved; you can pick up where it left off."
            )
            if st.button("Resume migration", key="resume_migration_btn"):
                job_runner.launch_step(
                    job_id, "migrate",
                    {
                        "repo_path": repo_path, "findings": findings,
                        "scope": scope_input, "resume_output_dir": resume_output_dir,
                        "codebase_map": codebase_map,
                    },
                )
                st.rerun()
        else:
            if st.button("Run migration", type="primary", key="run_migration_btn", disabled=migrate_running or migrate_done):
                job_runner.launch_step(
                    job_id, "migrate",
                    {"repo_path": repo_path, "findings": findings, "scope": scope_input, "codebase_map": codebase_map},
                )
                st.rerun()

        if migrate_status:
            if migrate_status["status"] == "running":
                st.info("Migrating... this page refreshes automatically.")
                with st.expander("Progress log", expanded=True):
                    st.code(job_runner.get_worker_log(job_id, "migrate") or "(no output yet)")
                time.sleep(2)
                st.rerun()
            elif migrate_status["status"] == "error" and not resume_output_dir:
                if usage_limit_no_resume_msg:
                    st.warning(usage_limit_no_resume_msg)
                else:
                    first_line = migrate_status["error"].strip().splitlines()[-1]
                    st.error(f"Migration failed: {first_line}")
                    with st.expander("Full details"):
                        st.code(migrate_status["error"])
            elif migrate_status["status"] == "done":
                m_result = migrate_status["result"]
                out_url = m_result["repo_url"]
                stack_chosen = m_result.get("stack_chosen", "unknown")
                stack_reasoning = m_result.get("stack_reasoning", "")
                st.success(f"Pushed to {out_url}")
                st.write(f"**Stack chosen:** {stack_chosen}")
                if stack_reasoning:
                    with st.expander("Why this stack was chosen"):
                        st.markdown(stack_reasoning)

        if migrate_status_check or deploy_status_check:
            _spacer, _btn_col = st.columns([4, 1.3])
            with _btn_col:
                if st.button("Start over (change scope)", key="start_over_btn", use_container_width=True):
                    from harness import db
                    from harness.deploy import stop_deployment
                    if deploy_status_check and deploy_status_check["status"] == "done":
                        stop_deployment("migrated_app")
                    db.clear_step(job_id, "deploy")
                    db.clear_step(job_id, "migrate")
                    st.session_state.pop("resume_migration_btn", None)
                    st.rerun()

        
        st.divider()

        deploy_target = repo_path
        if migrate_status and migrate_status["status"] == "done":
            deploy_target = migrate_status["result"]["repo_path"]

        deploy_status = job_runner.get_step_status(job_id, "deploy")
        deploy_running = bool(deploy_status and deploy_status["status"] == "running")
        deploy_done = bool(deploy_status and deploy_status["status"] == "done")

        section_header("Deploy", deploy_status["status"] if deploy_status else None)

        if st.button("Run deployment", type="primary", key="run_deployment_btn", disabled=deploy_running or deploy_done):
            job_runner.launch_step(
                job_id, "deploy",
                {"target_path": deploy_target, "container_name": "migrated_app"},
            )
            st.rerun()

        if deploy_status:
            if deploy_status["status"] == "running":
                st.info("Deploying... this page refreshes automatically.")
                with st.expander("Progress log"):
                    st.code(job_runner.get_worker_log(job_id, "deploy") or "(no output yet)")
                time.sleep(2)
                st.rerun()
            elif deploy_status["status"] == "error":
                first_line = deploy_status["error"].strip().splitlines()[-1]
                st.error(f"Deployment failed: {first_line}")
                with st.expander("Full details"):
                    st.code(deploy_status["error"])
            elif deploy_status["status"] == "done":
                app_url = deploy_status["result"]["app_url"]
                st.success(f"App live at: {app_url}")