from pathlib import Path
p = Path("harness/job_runner.py")
s = p.read_text(encoding="utf-8")
old = "subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS"
assert s.count(old) == 1
p.write_text(s.replace(old, "subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW"),
             encoding="utf-8", newline="\n")
print("done")
