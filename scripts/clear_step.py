import sqlite3
import sys

job_id = int(sys.argv[1])
step_name = sys.argv[2]

c = sqlite3.connect("harness_data.db")
c.execute("DELETE FROM steps WHERE job_id = ? AND step_name = ?", (job_id, step_name))
c.commit()
print(f"cleared job_id={job_id} step_name={step_name}")