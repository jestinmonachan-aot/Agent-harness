import json, glob, os, pathlib
src = os.path.expanduser("~/claude_transcripts_backup/*.jsonl")
out = pathlib.Path(os.path.expanduser("~/recovered")); out.mkdir(exist_ok=True)
n = 0
for f in sorted(glob.glob(src), key=os.path.getmtime):
    for line in open(f, encoding="utf-8"):
        try: d = json.loads(line)
        except Exception: continue
        msg = d.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list): continue
        for c in msg["content"]:
            if c.get("type") != "tool_use" or c.get("name") not in ("Write", "Edit", "MultiEdit"): continue
            inp = c["input"]; path = inp.get("file_path", "")
            if "agent-harness" not in path and "harness" not in path: continue
            ts = d.get("timestamp", "")
            print(ts, c["name"], path)
            if c["name"] == "Write":
                n += 1
                name = os.path.basename(path)
                (out / f"{ts[:19].replace(':','-')}_{n}_{name}").write_text(inp.get("content", ""), encoding="utf-8")