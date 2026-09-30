from pathlib import Path

p = Path("harness/migrate.py")
s = p.read_text(encoding="utf-8")

def sub(old, new):
    global s
    assert s.count(old) == 1, f"anchor not found: {old[:50]}"
    s = s.replace(old, new)

# 1. resumable verification failure
sub('''def create_github_repo(''',
'''class PhaseVerificationError(UsageLimitError):
    """A phase failed verification twice. Carries output_dir so the UI can
    offer Resume (completed phases are saved in MIGRATION_STATE.json)."""


def create_github_repo(''')

sub('''        raise RuntimeError(
            f"Phase '{phase_id}' is still incomplete after a fix-up pass, so "
            f"the migration stopped here instead of advancing. Output so far "
            f"is preserved in {output_dir}.\\nRemaining problems:\\n{details}"
        )''',
'''        raise PhaseVerificationError(
            f"Phase '{phase_id}' is still incomplete after a fix-up pass, so "
            f"the migration stopped here instead of advancing. Completed "
            f"phases are saved; resume to retry this phase.\\n"
            f"Remaining problems:\\n{details}",
            output_dir,
        )''')

# 2. warn when the plan falls back to a single phase
sub('''    return [{"id": "full_app", "description": "Entire application", "done_criteria": []}]''',
'''    print("[migrate] WARNING: could not parse a phase plan from Claude's "
          "output; falling back to ONE 'full_app' phase. Raw output "
          f"(first 500 chars): {result.stdout[:500]!r}", flush=True)
    return [{"id": "full_app", "description": "Entire application", "done_criteria": []}]''')

p.write_text(s, encoding="utf-8", newline="\n")
print("done")