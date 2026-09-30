import sqlite3
from pathlib import Path

# 1. db.py: replace any older record on the same URL
p = Path("harness/db.py")
s = p.read_text(encoding="utf-8")
old = '''    name = app_name_from_url(job["repo_url"]) if job else f"job-{job_id}"
    with _conn() as conn:
'''
new = old + '''        conn.execute(
            "DELETE FROM deployments WHERE kind = ? AND url = ? AND job_id != ?",
            (kind, url, job_id),
        )
'''
assert s.count(old) == 1, "db.py anchor not found"
p.write_text(s.replace(old, new), encoding="utf-8", newline="\n")

# 2. legacy page: split App / Version
p = Path("pages/1_Legacy_Deploy.py")
s = p.read_text(encoding="utf-8")
old = '"App": d["app_name"],'
new = ('"App": d["app_name"].partition("#")[0],\n'
       '                "Version": d["app_name"].partition("#")[2] or "-",')
assert s.count(old) == 1, "page anchor not found"
p.write_text(s.replace(old, new), encoding="utf-8", newline="\n")

# 3. one-time cleanup of the stale GLPI 11 record
conn = sqlite3.connect("data/harness_data.db")
n = conn.execute(
    "DELETE FROM deployments WHERE kind='legacy' AND app_name='glpi'"
).rowcount
conn.commit()
conn.close()
print("done, stale rows removed:", n)