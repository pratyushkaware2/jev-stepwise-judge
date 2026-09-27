#!/usr/bin/env python3
"""Replay a realistic Claude Code session through the real hook and real Jev.

    TYPESAFE_API_KEY=... python3 examples/simulated_session.py

Uses a throwaway project and state directory and runs in enforce mode, so you
can see every verdict: todo-list mandate, goal gate, failure -> fix direction,
push gate, advance-goal direction, stop gate.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "jev-stepwise-judge")
P = tempfile.mkdtemp(prefix="jsj-proj-")
os.makedirs(os.path.join(P, "src"))
with open(os.path.join(P, "src", "parse.py"), "w") as f:
    f.write("def parse(s):\n    return s.split()\n")
ENV = dict(os.environ, JEV_STEPWISE_STATE=tempfile.mkdtemp(prefix="jsj-state-"), JEV_STEPWISE_MODE="enforce")
n = [0]


def hook(ev, **kw):
    n[0] += 1
    p = {"hook_event_name": ev, "session_id": "demo", "cwd": P, **kw}
    if "tool_name" in kw:
        p.setdefault("tool_use_id", "u%d" % n[0])
    r = subprocess.run([BIN, "hook"], input=json.dumps(p), capture_output=True, text=True, env=ENV)
    if r.stdout.strip():
        o = json.loads(r.stdout)
        h = o.get("hookSpecificOutput", {})
        msg = h.get("permissionDecisionReason") or h.get("additionalContext") or o.get("reason") or o.get("systemMessage")
        tag = h.get("permissionDecision") or o.get("decision") or "note"
        print("    -> %s: %s" % (tag.upper(), msg.replace("[jev-stepwise-judge] ", "")[:400]))
    return p.get("tool_use_id")


def step(label, tool, inp, resp, fail=False):
    print("  *", label)
    uid = hook("PreToolUse", tool_name=tool, tool_input=inp)
    hook("PostToolUseFailure" if fail else "PostToolUse", tool_name=tool, tool_input=inp, tool_use_id=uid,
         tool_response=resp)


def todo(a, b):
    return {"todos": [{"content": "Fix parse() dropping trailing tokens", "status": a},
                      {"content": "Add a regression test", "status": b}]}


src = os.path.join(P, "src", "parse.py")
print("  * user prompt")
hook("UserPromptSubmit", prompt="parse() in src/parse.py drops trailing tokens when the input ends with a "
                                "comma. Fix it and add a regression test.")
print("  * edit before writing a todo list")
hook("PreToolUse", tool_name="Edit", tool_input={"file_path": src, "old_string": "s.split()", "new_string": "s.split(',')"})
step("write the todo list", "TodoWrite", todo("in_progress", "pending"), {})
step("read parse.py", "Read", {"file_path": src}, {"content": "def parse(s): return s.split()"})
step("edit parse.py", "Edit", {"file_path": src, "old_string": "s.split()", "new_string": "s.split(',')"},
     {"success": True})
print("  * mark goal 1 completed without testing")
hook("PreToolUse", tool_name="TodoWrite", tool_input=todo("completed", "in_progress"))
step("run tests (they fail)", "Bash", {"command": "python3 -m pytest tests -q"},
     "FAILED tests/test_parse.py::test_trailing - AssertionError: ['a', 'b', ''] != ['a', 'b']\n1 failed, 2 passed",
     fail=True)
print("  * commit and push while tests fail")
hook("PreToolUse", tool_name="Bash", tool_input={"command": "git commit -am fix && git push"})
step("fix the edit", "Edit", {"file_path": src, "old_string": "s.split(',')",
                              "new_string": "[t for t in s.split(',') if t]"}, {"success": True})
step("run tests (they pass)", "Bash", {"command": "python3 -m pytest tests -q"},
     {"stdout": "3 passed in 0.02s", "exit_code": 0})
step("advance to goal 2", "TodoWrite", todo("completed", "in_progress"), {})
print("  * try to stop with goal 2 open")
hook("Stop", stop_hook_active=False)
sys.exit(0)
