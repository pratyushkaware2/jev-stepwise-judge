"""Offline tests: Jev is replaced by a fake that returns scripted answers.

    python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jev_stepwise_judge import config, hooks, jev, judge, mcp_server, state as S, steps  # noqa: E402


def noul(p):
    return {"type": "noul", "noul": p}


def choice(c, conf=0.9):
    return {"type": "choice", "choice": c, "confidence": conf, "probabilities": {c: conf}}


class FakeJev:
    """Answers every requested question id from a script, with safe defaults."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def __call__(self, state, questions, model, timeout):
        self.calls.append((state, questions))
        out = {}
        for qid, q in questions.items():
            if qid in self.answers:
                out[qid] = self.answers[qid]
            elif q["type"] == "choice":
                out[qid] = choice(next(iter(q["criteria"])), 0.3)
            else:
                out[qid] = noul(0.5)
        return {"model": "fake", "answers": out}


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"JEV_STEPWISE_STATE": self.tmp.name,
                                                "JEV_STEPWISE_CONFIG": os.path.join(self.tmp.name, "none.json"),
                                                "JEV_STEPWISE_MODE": "enforce"})
        self.env.start()
        self.cfg = config.load()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class TestClassify(unittest.TestCase):
    def kind(self, tool, inp):
        return steps.classify(tool, inp)["kind"]

    def test_shell_kinds(self):
        cases = {
            "python3 -m pytest tests -q": "test", "npm test": "test", "go test ./...": "test",
            "cargo build --release": "build", "npx tsc --noEmit": "build", "ruff check .": "build",
            "git push origin main": "vcs_push", "git add -A && git commit -m x": "vcs_commit",
            "git status && git diff": "vcs_read", "cat src/a.py": "read", "rg foo src": "search",
            "ps aux | grep node": "check_process", "kill 123": "stop_process",
            "npm run dev": "run_process", "python3 server.py &": "run_process",
            "sed -i '' 's/a/b/' x.py": "edit", "echo hi > out.txt": "edit",
        }
        for cmd, want in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(self.kind("Bash", {"command": cmd}), want)

    def test_agent_tool_names(self):
        self.assertEqual(self.kind("run_terminal_command", {"command": "pytest"}), "test")
        self.assertEqual(self.kind("shell", {"command": ["bash", "-lc", "make test"]}), "test")
        self.assertEqual(self.kind("search_replace", {"file_path": "a.py"}), "edit")
        self.assertEqual(self.kind("todowrite", {"todos": []}), "goal_update")
        self.assertEqual(self.kind("update_plan", {"plan": []}), "goal_update")
        self.assertEqual(self.kind("Read", {"file_path": "a.py"}), "read")
        self.assertEqual(self.kind("mcp__github__create_issue", {}), "external")
        self.assertEqual(self.kind("mcp__github__get_issue", {}), "search")
        s = steps.classify("apply_patch", {"input": "*** Begin Patch\n*** Update File: src/x.py\n@@"})
        self.assertEqual((s["kind"], s["paths"]), ("edit", ["src/x.py"]))

    def test_background_flag(self):
        s = steps.classify("Bash", {"command": "python3 -m http.server", "run_in_background": True})
        self.assertEqual((s["kind"], s["background"]), ("run_process", True))


class TestGoals(unittest.TestCase):
    def test_todowrite_and_transitions(self):
        before = steps.apply_goal_update("TodoWrite", {"todos": [
            {"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]}, [])
        after = steps.apply_goal_update("TodoWrite", {"todos": [
            {"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]}, before)
        self.assertEqual(steps.current_goal(before)["content"], "a")
        self.assertEqual([t["content"] for t in steps.newly_completed(before, after)], ["a"])

    def test_update_plan_and_tasks(self):
        plan = steps.apply_goal_update("update_plan", {"plan": [{"step": "x", "status": "in-progress"}]}, [])
        self.assertEqual(plan[0]["status"], "in_progress")
        t = steps.apply_goal_update("TaskCreate", {"subject": "one"}, [])
        t = steps.apply_goal_update("TaskUpdate", {"taskId": "1", "status": "completed"}, t)
        self.assertEqual(t[0]["status"], "completed")

    def test_merge(self):
        t = steps.apply_goal_update("todo_write", {"todos": [{"id": "1", "content": "a", "status": "pending"}]}, [])
        t = steps.apply_goal_update("todo_write", {"merge": True, "todos": [{"id": "1", "status": "completed"}]}, t)
        self.assertEqual((t[0]["content"], t[0]["status"]), ("a", "completed"))


class TestResults(unittest.TestCase):
    def test_status(self):
        self.assertEqual(steps.verification_status("PostToolUse", {"stdout": "5 passed in 0.1s"}), "pass")
        self.assertEqual(steps.verification_status("PostToolUse", {"stdout": "1 failed, 4 passed"}), "fail")
        self.assertEqual(steps.verification_status("PostToolUseFailure", "boom"), "fail")
        self.assertEqual(steps.verification_status("PostToolUse", {"exit_code": 0}), "pass")
        self.assertEqual(steps.verification_status("PostToolUse", "compiling..."), "unknown")


class TestState(TempState):
    def run_step(self, st, tool, inp, resp, event="PostToolUse", uid=None):
        rec = S.on_pre(st, tool, inp, uid)
        S.on_post(st, tool, uid, event, resp)
        return rec

    def test_knowledge_workspace_goal(self):
        with tempfile.TemporaryDirectory() as proj:
            open(os.path.join(proj, "a.py"), "w").close()
            st = S.new_state("claude", "s1", proj)
            S.on_prompt(st, "fix it")
            self.run_step(st, "TodoWrite", {"todos": [{"content": "fix a", "status": "in_progress"}]}, {})
            self.run_step(st, "Read", {"file_path": "a.py"}, {"content": "x"})
            self.run_step(st, "Edit", {"file_path": "a.py"}, {"success": True})
            f = S.facts(st)
            self.assertEqual(f["unverified_edits"], ["a.py"])
            self.assertEqual(f["current_goal"], "fix a")
            self.run_step(st, "Bash", {"command": "pytest -q"}, {"stdout": "3 passed"})
            f = S.facts(st)
            self.assertEqual(f["unverified_edits"], [])
            self.assertTrue(f["last_verification"]["after_last_edit"])
            proposed = S.on_pre(st, "Edit", {"file_path": os.path.join(proj, "a.py")})
            self.assertEqual(S.facts(st, proposed)["edit_targets_never_read"], [])

    def test_required_changes_ticked(self):
        st = S.new_state("claude", "s2", "/p")
        S.set_plan(st, required_edits=[{"path": "src/x.py"}], required_runs=[{"command": "make test"}])
        self.run_step(st, "Edit", {"file_path": "/p/src/x.py"}, {"success": True})
        self.run_step(st, "Bash", {"command": "make test"}, {"stdout": "ok", "exit_code": 0})
        f = S.facts(st)
        self.assertEqual((f["outstanding_required_edits"], f["outstanding_required_runs"]), ([], []))

    def test_processes(self):
        st = S.new_state("claude", "s3", "/p")
        self.run_step(st, "Bash", {"command": "npm run dev", "run_in_background": True}, {"stdout": ""})
        self.assertEqual(S.facts(st)["unchecked_processes"], ["npm run dev"])
        self.run_step(st, "BashOutput", {"bash_id": "1"}, {"stdout": "ready on :3000"})
        self.assertEqual(S.facts(st)["unchecked_processes"], [])

    def test_sensitive_masking(self):
        st = S.new_state("claude", "s4", "/p")
        cfg = dict(self.cfg, sensitive=self.cfg["sensitive"] + [r"private/"])
        self.run_step(st, "Read", {"file_path": "/p/private/notes.md"}, {"content": "secret"})
        blob = json.dumps(S.to_jev_state(st, cfg))
        self.assertNotIn("private/notes.md", blob)
        self.assertIn("[sensitive: not shared]", blob)

    def test_scrub(self):
        self.assertNotIn("abcdef123456", S.scrub("export API_KEY=abcdef123456"))


class TestPolicy(TempState):
    def state_with_todo(self, edits=True, failed=False):
        st = S.new_state("claude", "p", "/p")
        S.on_prompt(st, "fix the parser")
        rec = S.on_pre(st, "TodoWrite", {"todos": [{"content": "fix parser", "status": "in_progress"},
                                                   {"content": "docs", "status": "pending"}]}, "t")
        S.on_post(st, "TodoWrite", "t", "PostToolUse", {})
        if edits:
            S.on_pre(st, "Edit", {"file_path": "/p/parse.py"}, "e")
            S.on_post(st, "Edit", "e", "PostToolUse", {"success": True})
        if failed:
            S.on_pre(st, "Bash", {"command": "pytest"}, "b")
            S.on_post(st, "Bash", "b", "PostToolUse", {"stdout": "1 failed", "exit_code": 1})
        return st

    def decide(self, st, tool, inp, **answers):
        rec = S.on_pre(st, tool, inp)
        return judge.decide_step(FakeJev(**answers)(None, judge.step_questions(S.facts(st, rec)), "", 0)["answers"],
                                 S.facts(st, rec), st, self.cfg)

    def test_goal_gate_blocks_premature_completion(self):
        st = self.state_with_todo()
        sev, msg, _ = self.decide(st, "TodoWrite", {"todos": [{"content": "fix parser", "status": "completed"},
                                                             {"content": "docs", "status": "in_progress"}]},
                                  current_goal_done=noul(0.2), verification_due=noul(0.9))
        self.assertEqual(sev, "deny")
        self.assertIn("don't mark 'fix parser' completed", msg)

    def test_goal_gate_allows_verified_completion(self):
        st = self.state_with_todo(edits=False)
        sev, _, _ = self.decide(st, "TodoWrite", {"todos": [{"content": "fix parser", "status": "completed"},
                                                           {"content": "docs", "status": "in_progress"}]},
                                current_goal_done=noul(0.95), direction=choice("advance_goal"), step_sound=noul(0.9))
        self.assertEqual(sev, "none")

    def test_ship_gate(self):
        st = self.state_with_todo(failed=True)
        sev, msg, d = self.decide(st, "Bash", {"command": "git commit -am x"})
        self.assertEqual(sev, "deny")
        self.assertIn("last test/build failed", msg)

    def test_todo_list_mandatory(self):
        st = S.new_state("claude", "n", "/p")
        S.on_prompt(st, "add a feature")
        sev, msg, d = self.decide(st, "Edit", {"file_path": "/p/x.py"})
        self.assertEqual((sev, d["direction"]), ("deny", "plan_goals"))

    def test_off_direction_advice(self):
        st = self.state_with_todo()
        sev, msg, d = self.decide(st, "Bash", {"command": "git status"},
                                  direction=choice("run_verification", 0.9), step_sound=noul(0.3))
        self.assertEqual((sev, d["direction"]), ("advice", "run_verification"))
        self.assertIn("off-direction", msg)

    def test_repeat_failure(self):
        st = self.state_with_todo(failed=True)
        sev, msg, _ = self.decide(st, "Bash", {"command": "pytest"}, repeats_failure=noul(0.95))
        self.assertEqual(sev, "deny")

    def test_first_todo_list_is_never_flagged(self):
        st = S.new_state("claude", "f", "/p")
        S.on_prompt(st, "fix the parser")
        sev, _, d = self.decide(st, "TodoWrite", {"todos": [{"content": "a", "status": "in_progress"}]},
                                direction=choice("gather_knowledge", 0.9), step_sound=noul(0.2),
                                knowledge_sufficient=noul(0.1))
        self.assertEqual((sev, d["direction"]), ("none", "plan_goals"))

    def test_advance_ruled_out_when_goal_not_done(self):
        st = self.state_with_todo(edits=False)
        ans = {"direction": {"type": "choice", "choice": "advance_goal", "confidence": 0.56,
                             "probabilities": {"advance_goal": 0.56, "edit_workspace": 0.3, "finish": 0.1}},
               "current_goal_done": noul(0.1)}
        d = judge.direction_from(ans, S.facts(st), st)
        self.assertEqual(d["direction"], "edit_workspace")
        self.assertIn("ruled out", d["rule"])

    def test_failed_verification_overrides_advance(self):
        st = self.state_with_todo(failed=True)
        d = judge.direction_from({"direction": choice("advance_goal")}, S.facts(st), st)
        self.assertEqual(d["direction"], "fix_failure")


class TestHooks(TempState):
    def hook(self, payload, fake):
        out = io.StringIO()
        with mock.patch.object(jev, "ask", fake), redirect_stdout(out):
            with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                hooks.main([])
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_claude_flow(self):
        fake = FakeJev(direction=choice("run_verification"), step_sound=noul(0.9))
        base = {"session_id": "h1", "cwd": "/p"}
        r = self.hook(dict(base, hook_event_name="UserPromptSubmit", prompt="fix parser"), fake)
        self.assertIn("todo list", r["hookSpecificOutput"]["additionalContext"])
        r = self.hook(dict(base, hook_event_name="PreToolUse", tool_name="Edit", tool_use_id="1",
                           tool_input={"file_path": "/p/a.py"}), fake)
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")  # no todo list yet
        self.hook(dict(base, hook_event_name="PreToolUse", tool_name="TodoWrite", tool_use_id="2",
                       tool_input={"todos": [{"content": "fix", "status": "in_progress"}]}), fake)
        r = self.hook(dict(base, hook_event_name="PostToolUse", tool_name="TodoWrite", tool_use_id="2",
                           tool_response={}), fake)
        self.assertIn("next:", r["hookSpecificOutput"]["additionalContext"])  # direction pushed
        st = S.load("claude", "h1")
        self.assertEqual(S.facts(st)["current_goal"], "fix")

    def test_grok_payload_detected(self):
        fake = FakeJev()
        self.hook({"hookEventName": "user_prompt_submit", "hook_event_name": "UserPromptSubmit",
                   "sessionId": "g1", "cwd": "/p", "prompt": "hello"}, fake)
        self.assertEqual(S.load("grok", "g1")["task"], "hello")

    def test_codex_advice_goes_to_outbox(self):
        fake = FakeJev(direction=choice("gather_knowledge", 0.95), step_sound=noul(0.2), knowledge_sufficient=noul(0.1))
        with mock.patch.dict(os.environ, {"JEV_STEPWISE_MODE": "advise"}):
            base = {"session_id": "c1", "turn_id": "t", "cwd": "/p"}
            self.hook(dict(base, hook_event_name="PreToolUse", tool_name="update_plan", tool_use_id="0",
                           tool_input={"plan": [{"step": "x", "status": "in_progress"}]}), fake)
            self.hook(dict(base, hook_event_name="PostToolUse", tool_name="update_plan", tool_use_id="0",
                           tool_response={}), fake)
            r = self.hook(dict(base, hook_event_name="PreToolUse", tool_name="shell", tool_use_id="1",
                               tool_input={"command": ["bash", "-lc", "python3 build.py"]}), fake)
            self.assertIsNone(r)  # codex: advice is not sent on PreToolUse
            r = self.hook(dict(base, hook_event_name="PostToolUse", tool_name="shell", tool_use_id="1",
                               tool_response={"output": "ok", "exit_code": 0}), fake)
            self.assertIn("jev-stepwise-judge", r["hookSpecificOutput"]["additionalContext"])

    def test_stop_gate(self):
        fake = FakeJev(finished=noul(0.1))
        base = {"session_id": "s", "cwd": "/p"}
        self.hook(dict(base, hook_event_name="PreToolUse", tool_name="TodoWrite", tool_use_id="1",
                       tool_input={"todos": [{"content": "a", "status": "in_progress"}]}), FakeJev())
        self.hook(dict(base, hook_event_name="PostToolUse", tool_name="TodoWrite", tool_use_id="1",
                       tool_response={}), FakeJev())
        r = self.hook(dict(base, hook_event_name="Stop", stop_hook_active=False), fake)
        self.assertEqual(r["decision"], "block")
        r = self.hook(dict(base, hook_event_name="Stop", stop_hook_active=False), fake)
        self.assertIsNone(r)  # blocks at most once

    def test_jev_down_fails_open(self):
        def down(*a, **k):
            raise jev.JevUnavailable("HTTP 529")
        base = {"session_id": "d", "cwd": "/p"}
        r = self.hook(dict(base, hook_event_name="PreToolUse", tool_name="Bash", tool_use_id="1",
                           tool_input={"command": "rm -rf build"}), down)
        self.assertIsNone(r)


class TestMCP(TempState):
    def test_protocol_and_tools(self):
        srv = mcp_server.Server()
        srv.pids, srv.cwd = [], "/p"
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "jev-stepwise-judge")
        names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(names, {"get_direction", "get_state", "judge_step", "choose_next", "set_plan"})
        r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "get_state", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])  # no session yet
        st = S.new_state("claude", "m1", "/p")
        S.save(st)
        r = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "set_plan", "arguments": {"todos": [{"content": "a", "status": "in_progress"}],
                                              "required_runs": [{"command": "make test"}]}}})
        body = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual((body["current_goal"], body["outstanding_required_runs"]), ("a", ["make test"]))
        with mock.patch.object(jev, "ask", FakeJev(direction=choice("edit_workspace"))):
            r = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "get_direction", "arguments": {}}})
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["direction"], "edit_workspace")
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
