"""MCP server (stdio, JSON-RPC 2.0, stdlib only) over the hook-tracked agent state.

The hooks see every prompt, tool call and result and keep the state; this server
lets the agent read it and ask Jev for a direction. It finds its own session by
the nearest ancestor process it shares with the hooks (both are spawned by the
same agent process), falling back to the newest session in the working dir.
"""
import json
import os
import sys

from . import __version__, config, hooks, jev, judge, state as S, steps

PROTOCOL = "2025-06-18"
SESSION_PROP = {"session": {"type": "string", "description": "Session id; omit to use this agent's own session."}}

TOOLS = [
    {"name": "set_state",
     "description": ("Report your own view of your state, then get judged. Call this at every joint: before "
                     "moving to the next goal, after a failure, before finishing, and whenever unsure. Give what "
                     "you know, what you changed and still must change, which goal you are on, whether you believe "
                     "it is done and verified, and the next step you intend. Jev checks your report against the "
                     "observed evidence (files read, test/build results, edits, todo list) and returns: the "
                     "direction to move in, a verdict on your intended next step, and any mismatches between your "
                     "claims and the evidence. Commands in workspace.to_run are tracked until they succeed."),
     "inputSchema": {"type": "object", "properties": {
         "current_goal": {"type": "string", "description": "The todo item you are working on."},
         "knowledge": {"type": "object", "properties": {
             "learned": {"type": "array", "items": {"type": "string"},
                         "description": "Facts you established (root cause, where code lives, what output said)."},
             "open_questions": {"type": "array", "items": {"type": "string"},
                                "description": "What you still do not know."}}},
         "workspace": {"type": "object", "properties": {
             "changed": {"type": "array", "items": {"type": "string"}, "description": "Changes made so far."},
             "still_required": {"type": "array", "items": {"type": "string"},
                                "description": "Changes the goal still needs."},
             "to_run": {"type": "array", "items": {"type": "string"},
                        "description": "Commands that still have to run (tests, builds, migrations)."}}},
         "believes_goal_done": {"type": "boolean"},
         "believes_verified": {"type": "boolean",
                               "description": "True only if tests/builds have passed after your last edit."},
         "next_step": {"type": "string", "description": "The step you intend to take next, in one line."},
         **SESSION_PROP}, "required": ["current_goal", "next_step"], "additionalProperties": False}},
    {"name": "get_direction",
     "description": ("Ask Jev which direction to move in next, judged from your tracked knowledge (files read, "
                     "test/build results, processes), workspace (edits made, unverified edits, required changes) "
                     "and goal state (todo list). Returns the direction, its confidence, concrete next actions and "
                     "alternatives. Prefer set_state, which also checks your own report; use this for a quick look."),
     "inputSchema": {"type": "object", "properties": dict(SESSION_PROP), "additionalProperties": False}},
    {"name": "get_state",
     "description": ("Your tracked agent state as the judge sees it: current goal and progress, files known, "
                     "latest test and build results, background processes, unverified edits, and outstanding "
                     "required edits / runs. Local only; nothing is sent to Jev."),
     "inputSchema": {"type": "object", "properties": dict(SESSION_PROP), "additionalProperties": False}},
    {"name": "judge_step",
     "description": ("Judge a step you are considering before taking it: is it sound, does it serve the current "
                     "goal, do you know enough to take it, and which direction Jev would pick instead."),
     "inputSchema": {"type": "object", "properties": {
         "description": {"type": "string", "description": "The step in one line, e.g. 'run pytest -q' or "
                                                          "'mark goal 2 completed'."},
         "tool": {"type": "string", "description": "Optional tool name you would call (e.g. Bash, Edit)."},
         "input": {"type": "object", "description": "Optional tool input you would pass."},
         **SESSION_PROP}, "required": ["description"], "additionalProperties": False}},
    {"name": "choose_next",
     "description": "Give 2-8 candidate next steps; Jev ranks them against your task and state.",
     "inputSchema": {"type": "object", "properties": {
         "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 8},
         "context": {"type": "string", "description": "Anything relevant the state does not show."},
         **SESSION_PROP}, "required": ["options"], "additionalProperties": False}},
    {"name": "set_plan",
     "description": ("Declare your plan so the judge can track it: the todo list (only if your agent has no "
                     "todo tool), and the workspace changes the task requires: files that must be edited and "
                     "commands that must run (tests, builds, migrations). Each is marked done automatically "
                     "when a matching edit or successful run happens."),
     "inputSchema": {"type": "object", "properties": {
         "todos": {"type": "array", "items": {"type": "object", "properties": {
             "content": {"type": "string"},
             "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}},
             "required": ["content"]}},
         "required_edits": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"}, "why": {"type": "string"}}, "required": ["path"]}},
         "required_runs": {"type": "array", "items": {"type": "object", "properties": {
             "command": {"type": "string"}, "why": {"type": "string"}}, "required": ["command"]}},
         **SESSION_PROP}, "additionalProperties": False}},
]


class NoSession(Exception):
    pass


class Server:
    def __init__(self):
        self.pids = S.ancestor_pids()
        self.cwd = os.getcwd()

    def session(self, sid=None):
        st = S.find_session(self.pids, self.cwd, sid)
        if st is None:
            raise NoSession("No tracked session found. The jev-stepwise-judge hooks record state from your tool "
                            "calls; make sure they are installed (jev-stepwise-judge install) and take a step first.")
        return st

    # -------------------------------------------------------------- tools
    def get_direction(self, args):
        cfg = config.load()
        st = self.session(args.get("session"))
        d = judge.get_direction(st, cfg)
        d.pop("answers", None)
        d["summary"] = judge.direction_text(d)
        return d

    def get_state(self, args):
        st = self.session(args.get("session"))
        k, w, g = st["knowledge"], st["workspace"], st["goal"]
        return {
            "session": st["session"], "agent": st["agent"], "cwd": st["cwd"], "task": st["task"][:500],
            "facts": S.facts(st),
            "goal": {"todos": g["todos"], "source": g["source"]},
            "knowledge": {"files_known": len(k["files"]),
                          "recent_files": [p for p, _ in sorted(k["files"].items(), key=lambda kv: -kv[1]["seq"])[:10]],
                          "tests": k["tests"] and {x: k["tests"].get(x) for x in ("command", "status")},
                          "build": k["build"] and {x: k["build"].get(x) for x in ("command", "status")},
                          "processes": [{x: p.get(x) for x in ("command", "status")} for p in k["processes"]],
                          "recent_errors": [e["step"] for e in k["errors"]]},
            "workspace": {"edited": sorted(w["edits"]), "required_edits": w["required_edits"],
                          "required_runs": w["required_runs"]},
            "last_direction": st.get("last_direction"),
        }

    def judge_step(self, args):
        cfg = config.load()
        st = self.session(args.get("session"))
        tool = args.get("tool") or "Bash"
        tinput = args.get("input") or ({"command": args["description"]} if not args.get("tool") else {})
        cls = steps.classify(tool, tinput)
        rec = {"seq": st["seq"] + 1, "tool": tool, "kind": cls["kind"], "paths": cls["paths"],
               "summary": args["description"][:300], "background": cls["background"]}
        if cls["kind"] == "goal_update":
            rec["todos_after"] = steps.apply_goal_update(tool, tinput, st["goal"]["todos"])
        if S.is_sensitive(rec["summary"] + " " + " ".join(rec["paths"]), cfg["sensitive"]):
            return {"verdict": "not judged: the step touches a sensitive path"}
        res = judge.judge_step(st, cfg, rec)
        verdict = {"none": "go ahead", "advice": "reconsider", "ask": "confirm with the user", "deny": "don't"}
        return {"verdict": verdict[res["severity"]], "why": res["message"] or "no concerns",
                "direction": res["direction"], "answers": res["answers"]}

    def choose_next(self, args):
        cfg = config.load()
        st = self.session(args.get("session"))
        ctx = json.dumps({"facts": S.facts(st), "extra": args.get("context", "")}, default=str)
        if S.is_sensitive(" ".join(args["options"]) + ctx, cfg["sensitive"]):
            return {"error": "not sent: input mentions a sensitive path"}
        return judge.choose(st["task"], ctx, args["options"], cfg)

    def set_plan(self, args):
        st = self.session(args.get("session"))
        with hooks.locked(st["agent"], st["session"]):
            st = S.load(st["agent"], st["session"])
            S.set_plan(st, args.get("todos"), args.get("required_edits"), args.get("required_runs"))
            S.save(st)
        f = S.facts(st)
        return {"ok": True, "current_goal": f["current_goal"], "goals_done": f["goals_done"],
                "outstanding_required_edits": f["outstanding_required_edits"],
                "outstanding_required_runs": f["outstanding_required_runs"]}

    def set_state(self, args):
        cfg = config.load()
        st = self.session(args.get("session"))
        with hooks.locked(st["agent"], st["session"]):
            st = S.load(st["agent"], st["session"])
            S.set_report(st, args)
            S.save(st)
        if S.is_sensitive(json.dumps(st["report"]), cfg["sensitive"]):
            return {"recorded": True, "judged": False, "why": "the report mentions a sensitive path; not sent to Jev"}
        res = judge.judge_report(st, cfg)
        with hooks.locked(st["agent"], st["session"]):
            fresh = S.load(st["agent"], st["session"])
            fresh["last_direction"] = res["direction"]["direction"]
            S.save(fresh)
        hooks.log({"agent": st["agent"], "event": "set_state", "direction": res["direction"]["direction"],
                   "verdict": res["next_step_verdict"], "mismatches": len(res["mismatches"]), "ms": res["ms"]})
        res["direction"].pop("alternatives", None)
        return res

    # ----------------------------------------------------------- protocol
    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return None  # notification
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            return self.result(mid, {"protocolVersion": requested, "capabilities": {"tools": {}},
                                     "serverInfo": {"name": "jev-stepwise-judge", "version": __version__},
                                     "instructions": ("Tracks your knowledge, workspace and goal state from hooks and "
                                                      "asks TypeSafe Jev which direction to move next. Keep a todo "
                                                      "list. At every joint (goal done, failure, before finishing) "
                                                      "call set_state with your own view and intended next step; "
                                                      "it returns the direction and a verdict.")})
        if method == "ping":
            return self.result(mid, {})
        if method == "tools/list":
            return self.result(mid, {"tools": TOOLS})
        if method == "tools/call":
            params = msg.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
            if fn is None:
                return self.error(mid, -32602, "unknown tool: %s" % name)
            try:
                out, is_err = fn(args), False
            except NoSession as e:
                out, is_err = {"error": str(e)}, True
            except jev.JevUnavailable as e:
                out, is_err = {"error": "Jev unavailable (%s); decide without it" % e}, True
            except Exception as e:
                out, is_err = {"error": "%s: %s" % (type(e).__name__, e)}, True
            return self.result(mid, {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}],
                                     "isError": is_err})
        return self.error(mid, -32601, "method not found: %s" % method)

    @staticmethod
    def result(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def error(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(argv=None):
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = Server.error(None, -32700, "parse error")
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else Server.error(None, -32600, "invalid request")
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0
