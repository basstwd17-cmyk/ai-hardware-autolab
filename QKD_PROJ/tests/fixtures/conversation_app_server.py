"""Offline deterministic peer; actual tiny Python commands, NOT a real model."""
import json
import subprocess
import sys
from pathlib import Path


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    method, rid, params = req.get("method"), req.get("id"), req.get("params", {})
    if method == "initialize": send({"id": rid, "result": {"userAgent": "test-peer"}})
    elif method == "initialized": pass
    elif method == "thread/start": send({"id": rid, "result": {"thread": {"id": "thread-test"}}})
    elif method == "thread/unsubscribe": send({"id": rid, "result": {}})
    elif method == "turn/start":
        task = json.loads(params["input"][0]["text"].split("\n", 1)[1])
        role = task["role"]
        send({"id": rid, "result": {"turn": {"id": "turn-test"}}})
        artifacts = []
        if role == "builder":
            root = Path(params["cwd"])
            # Exercise an actual command failure and correction in ONE work turn.
            for code in ("assert 2-3 == 5", "print(2+3)"):
                (root / "addition.py").write_text(code, encoding="utf-8")
                cmd = [sys.executable, "addition.py"]
                result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
                send({"method": "item/completed", "params": {"threadId": "thread-test", "turnId": "turn-test",
                    "item": {"id": "cmd", "type": "commandExecution", "command": "python addition.py",
                             "exitCode": result.returncode, "aggregatedOutput": result.stdout + result.stderr}}})
            (root / "result.txt").write_text(result.stdout, encoding="utf-8")
            artifacts = ["addition.py", "result.txt"]
        if role == "verifier":
            assert Path(task["workspace"], "result.txt").read_text().strip() == "5"
            artifacts = ["result.txt"]
        reply = {"status": "satisfied" if role == "verifier" else "ready",
                 "summary": "Offline addition demonstration", "artifacts": artifacts, "target": None}
        item = {"type": "agentMessage", "phase": "final_answer", "text": json.dumps(reply)}
        send({"method": "turn/completed", "params": {"threadId": "thread-test",
            "turn": {"id": "turn-test", "status": "completed", "items": [item]}}})
