"""The agent state, kept per session by the hooks and read by the MCP server.

  knowledge  what the agent has observed: files read, the latest test and build
             results, background processes and what is known about them, errors
  workspace  what has changed and what still has to change: edits made, edits
             not yet verified, and the required edits / runs the agent declared
  goal       the todo list (mandatory under the skill): current goal, progress

Facts that code can compute exactly (which edits are unverified, which declared
runs are outstanding, which processes were never checked) are computed here and
handed to Jev as facts, so Jev only makes the semantic calls.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import time

from . import config, steps

MAX_STEPS = 24
MAX_FILES = 60


def _now():
    return int(time.time())


def ancestor_pids(depth=6):
    """This process's ancestors, nearest first. Links hooks and the MCP server
    that were both spawned (directly or via a shell) by the same agent process."""
    chain, pid = [], os.getppid()
    for _ in range(depth):
        if pid <= 1:
            break
        chain.append(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=1)
            pid = int(out.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return chain


def new_state(agent, session, cwd):
    return {
        "version": 1, "agent": agent, "session": session, "cwd": cwd,
        "created": _now(), "updated": _now(), "pids": [],
        "task": "", "earlier_prompts": [], "seq": 0,
        "knowledge": {"files": {}, "tests": None, "build": None, "processes": [], "errors": []},
        "workspace": {"edits": {}, "last_edit_seq": 0, "required_edits": [], "required_runs": []},
        "goal": {"todos": [], "updated_seq": 0, "source": None},
        "steps": [], "pending": {}, "outbox": [], "stop_blocks": 0, "last_direction": None,
        "report": None,  # the agent's own view of its state (MCP set_state)
    }


# --------------------------------------------------------------- persistence

def sessions_dir():
    return os.path.join(config.state_dir(), "sessions")


def _path(agent, session):
    digest = hashlib.sha1(("%s:%s" % (agent, session)).encode()).hexdigest()[:20]
    return os.path.join(sessions_dir(), "%s-%s.json" % (agent, digest))


def load(agent, session, cwd=""):
    try:
        with open(_path(agent, session)) as f:
            st = json.load(f)
        if st.get("version") == 1:
            return st
    except (OSError, ValueError):
        pass
    return new_state(agent, session, cwd)


def save(st):
    st["updated"] = _now()
    st["steps"] = st["steps"][-MAX_STEPS:]
    files = st["knowledge"]["files"]
    if len(files) > MAX_FILES:
        for p, _ in sorted(files.items(), key=lambda kv: kv[1].get("seq", 0))[:len(files) - MAX_FILES]:
            files.pop(p, None)
    os.makedirs(sessions_dir(), mode=0o700, exist_ok=True)
    path = _path(st["agent"], st["session"])
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if _now() % 40 == 0:
        prune()


def prune(max_age_days=7):
    for p in glob.glob(os.path.join(sessions_dir(), "*.json")):
        try:
            if time.time() - os.path.getmtime(p) > max_age_days * 86400:
                os.remove(p)
        except OSError:
            pass


def all_sessions():
    out = []
    for p in glob.glob(os.path.join(sessions_dir(), "*.json")):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def find_session(my_pids=None, cwd=None, session=None, max_age_s=6 * 3600):
    """Pick the session an MCP server (or CLI) belongs to: an explicit id wins,
    then the nearest shared ancestor process, then the newest session in cwd."""
    cands = [s for s in all_sessions() if _now() - s.get("updated", 0) < max_age_s]
    if session:
        for s in cands:
            if s.get("session") == session:
                return s
    if my_pids:
        best, best_rank = None, None
        for s in cands:
            shared = [i for i, pid in enumerate(my_pids) if pid in (s.get("pids") or [])]
            if shared:
                rank = (shared[0], -s.get("updated", 0))
                if best_rank is None or rank < best_rank:
                    best, best_rank = s, rank
        if best is not None:
            return best
    if cwd:
        in_cwd = [s for s in cands if s.get("cwd") and (cwd == s["cwd"] or cwd.startswith(s["cwd"] + "/"))]
        if in_cwd:
            return max(in_cwd, key=lambda s: s.get("updated", 0))
    return None


# ------------------------------------------------------------------ updates

def _norm(path, cwd):
    if not path:
        return path
    path = os.path.expanduser(path)
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _same_goal(a, b):
    wa, wb = set(re.findall(r"\w{3,}", a.lower())), set(re.findall(r"\w{3,}", b.lower()))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def _rel(path, cwd):
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return os.path.relpath(path, cwd)
    return path.replace(os.path.expanduser("~"), "~")


def on_prompt(st, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    if st["task"]:
        st["earlier_prompts"] = (st["earlier_prompts"] + [st["task"][:500]])[-2:]
    st["task"] = prompt[:4000]
    st["stop_blocks"] = 0
    st["seq"] += 1
    st["steps"].append({"seq": st["seq"], "kind": "user_prompt", "summary": prompt[:160], "status": "ok"})


def on_pre(st, tool, tool_input, tool_use_id=None):
    """Record a proposed step; returns the classified step (not yet executed)."""
    step = steps.classify(tool, tool_input)
    st["seq"] += 1
    rec = {"seq": st["seq"], "tool": tool, "kind": step["kind"], "status": "pending",
           "paths": [_norm(p, st["cwd"]) for p in step["paths"]][:8],
           "summary": _summary(tool, tool_input, step), "background": step["background"]}
    if step["kind"] == "goal_update":
        rec["todos_after"] = steps.apply_goal_update(tool, tool_input, st["goal"]["todos"])
    st["steps"].append(rec)
    st["pending"][tool_use_id or "tool:" + (tool or "")] = st["seq"]
    return rec


def _summary(tool, tool_input, step):
    if step["command"]:
        return step["command"][:300]
    if step["kind"] == "goal_update":
        return "update todo list"
    if step["paths"]:
        return "%s %s" % (tool, ", ".join(step["paths"][:3]))
    return "%s %s" % (tool, json.dumps(tool_input, default=str)[:200])


def find_pending(st, tool, tool_use_id):
    seq = st["pending"].pop(tool_use_id, None) if tool_use_id else None
    if seq is None:
        seq = st["pending"].pop("tool:" + (tool or ""), None)
    if seq is None:  # fall back to the newest pending step for this tool
        for rec in reversed(st["steps"]):
            if rec.get("status") == "pending" and rec.get("tool") == tool:
                return rec
        return None
    for rec in reversed(st["steps"]):
        if rec["seq"] == seq:
            return rec
    return None


def on_post(st, tool, tool_use_id, event, response):
    """Apply a finished step's effects to knowledge / workspace / goal."""
    rec = find_pending(st, tool, tool_use_id)
    if rec is None:
        rec = on_pre(st, tool, {}, tool_use_id)
        st["pending"].pop(tool_use_id or "tool:" + (tool or ""), None)
    failed = steps.looks_failed(event, response)
    text = steps.result_text(response)
    rec["status"] = "error" if failed else "ok"
    rec["result"] = text[-300:]
    if rec["kind"] in EVIDENCE_KINDS:
        st["seq"] += 1
        st["last_evidence_seq"] = st["seq"]
    k, w, seq = st["knowledge"], st["workspace"], rec["seq"]
    kind = rec["kind"]

    if kind in ("read", "search", "vcs_read") and not failed:
        for p in rec["paths"]:
            k["files"][p] = {"seq": seq, "how": kind}
    elif kind == "edit" and not failed:
        for p in rec["paths"]:
            e = w["edits"].setdefault(p, {"count": 0})
            e["seq"], e["count"] = seq, e["count"] + 1
            k["files"][p] = {"seq": seq, "how": "edited"}
            for r in w["required_edits"]:
                if not r.get("done") and r.get("path") and _norm(r["path"], st["cwd"]) == p:
                    r["done"], r["done_seq"] = True, seq
        w["last_edit_seq"] = seq
    elif kind in ("test", "build"):
        status = "fail" if failed else steps.verification_status(event, response)
        k["tests" if kind == "test" else "build"] = {
            "command": rec["summary"], "seq": seq, "status": status, "tail": text[-600:]}
        rec["verification"] = status
    elif kind == "run_process":
        k["processes"].append({"command": rec["summary"], "seq": seq, "background": rec["background"],
                               "status": "failed" if failed else ("started" if rec["background"] else "ran"),
                               "checked_seq": None if rec["background"] else seq, "tail": text[-300:]})
        k["processes"] = k["processes"][-8:]
    elif kind == "check_process":
        for p in k["processes"]:
            if p["status"] == "started":
                p["checked_seq"], p["status"] = seq, "failed" if failed else "running"
                p["tail"] = text[-300:]
    elif kind == "stop_process":
        for p in k["processes"]:
            if p["status"] in ("started", "running"):
                p["status"], p["checked_seq"] = "stopped", seq
    elif kind == "goal_update" and not failed:
        st["goal"]["todos"] = rec.get("todos_after") or st["goal"]["todos"]
        st["goal"]["updated_seq"], st["goal"]["source"] = seq, tool

    if rec.get("summary") and kind in ("shell", "test", "build", "run_process") and not failed:
        for r in w["required_runs"]:
            if not r.get("done") and r.get("command") and r["command"].strip() in rec["summary"]:
                r["done"], r["done_seq"] = True, seq
    if failed:
        k["errors"] = (k["errors"] + [{"seq": seq, "step": rec["summary"][:160], "error": text[-300:]}])[-4:]
    return rec


ACTING_KINDS = {"edit", "test", "build", "run_process", "stop_process", "vcs_commit", "vcs_push", "goal_update",
                "shell", "external", "delegate"}


# steps whose results are new evidence: once one completes, an earlier report is stale.
# Todo updates are bookkeeping and reads/searches only add knowledge, so neither
# invalidates a report; steps sent together in one batch share a report.
EVIDENCE_KINDS = ACTING_KINDS - {"goal_update"}


def report_is_fresh(st):
    """Has the agent reported (set_state) since the last new evidence arrived?"""
    rep = st.get("report")
    return bool(rep) and rep["seq"] > st.get("last_evidence_seq", 0)


def set_plan(st, todos=None, required_edits=None, required_runs=None):
    """Explicit plan from the agent (MCP set_plan): goals and required workspace changes."""
    if todos is not None:
        st["goal"]["todos"] = steps.apply_goal_update("set_plan", {"todos": todos}, [])
        st["goal"]["updated_seq"], st["goal"]["source"] = st["seq"], "set_plan"
    if required_edits is not None:
        st["workspace"]["required_edits"] = [
            {"path": e.get("path") if isinstance(e, dict) else str(e),
             "why": (e.get("why") or "")[:200] if isinstance(e, dict) else "", "done": False}
            for e in required_edits][:30]
        for r in st["workspace"]["required_edits"]:
            p = _norm(r["path"], st["cwd"])
            if p in st["workspace"]["edits"] and st["workspace"]["edits"][p]["seq"] > st["goal"]["updated_seq"]:
                r["done"] = True
    if required_runs is not None:
        st["workspace"]["required_runs"] = [
            {"command": r.get("command") if isinstance(r, dict) else str(r),
             "why": (r.get("why") or "")[:200] if isinstance(r, dict) else "", "done": False}
            for r in required_runs][:20]


def _decode(v):
    """Models sometimes send nested objects as JSON text."""
    if isinstance(v, str) and v.strip()[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y")
    return bool(v)


def set_report(st, report):
    """The agent's own account of its state (MCP set_state). Kept apart from the
    observed state: it is a claim to check against the evidence, not a fact.
    Commands it says still have to run become tracked required runs."""
    def _list(v, n=12):
        v = _decode(v)
        if isinstance(v, list):
            return [str(x)[:300] for x in v if str(x).strip()][:n]
        return [str(v)[:300]] if v else []

    def _section(v, default_key):
        v = _decode(v)
        if isinstance(v, dict):
            return v
        return {default_key: v} if v else {}   # a bare string or list: treat as the main field
    report = _decode(report) if not isinstance(report, dict) else report
    k = _section(report.get("knowledge"), "learned")
    w = _section(report.get("workspace"), "changed")
    st["seq"] += 1  # a report is newer than every step before it
    st["report"] = {
        "seq": st["seq"],
        "current_goal": str(report.get("current_goal") or "")[:300],
        "knowledge": {"learned": _list(k.get("learned")), "open_questions": _list(k.get("open_questions"), 8)},
        "workspace": {"changed": _list(w.get("changed")), "still_required": _list(w.get("still_required")),
                      "to_run": _list(w.get("to_run"), 8)},
        "believes_goal_done": _truthy(report.get("believes_goal_done")),
        "believes_verified": _truthy(report.get("believes_verified")),
        "next_step": str(report.get("next_step") or "")[:400],
    }
    known = {r["command"].strip() for r in st["workspace"]["required_runs"]}
    for cmd in st["report"]["workspace"]["to_run"]:
        if cmd.strip() and cmd.strip() not in known:
            st["workspace"]["required_runs"].append({"command": cmd.strip(), "why": "declared in set_state",
                                                     "done": False})
    st["workspace"]["required_runs"] = st["workspace"]["required_runs"][-20:]


# -------------------------------------------------------------------- facts

def facts(st, proposed=None):
    """Exact, code-computed facts about the state (and the proposed step)."""
    k, w, g = st["knowledge"], st["workspace"], st["goal"]
    verified_seq = max([v["seq"] for v in (k["tests"], k["build"]) if v and v["status"] == "pass"] or [0])
    unverified = sorted(p for p, e in w["edits"].items() if e["seq"] > verified_seq)
    last_ver = max([v for v in (k["tests"], k["build"]) if v], key=lambda v: v["seq"], default=None)
    todos = g["todos"]
    cur = steps.current_goal(todos)
    counts = {s: sum(1 for t in todos if t.get("status") == s) for s in ("pending", "in_progress", "completed")}
    f = {
        "has_todo_list": bool(todos),
        "current_goal": cur["content"] if cur else None,
        "current_goal_status": cur["status"] if cur else None,
        "next_goal": next((t["content"] for t in todos if t.get("status") == "pending" and t is not cur), None),
        "goals_done": "%d/%d" % (counts["completed"], len(todos)) if todos else "0/0",
        "all_goals_done": bool(todos) and all(t.get("status") in ("completed", "cancelled") for t in todos),
        "edited_files": len(w["edits"]),
        "unverified_edits": [_rel(p, st["cwd"]) for p in unverified][:12],
        "last_verification": ({"kind": "test" if last_ver is k["tests"] else "build", "status": last_ver["status"],
                               "after_last_edit": last_ver["seq"] > w["last_edit_seq"]} if last_ver else None),
        "last_verification_failed": bool(last_ver and last_ver["status"] == "fail"),
        "unchecked_processes": [p["command"][:120] for p in k["processes"] if p["status"] == "started"],
        "failed_processes": [p["command"][:120] for p in k["processes"] if p["status"] == "failed"],
        "outstanding_required_edits": [r["path"] for r in w["required_edits"] if not r.get("done")][:12],
        "outstanding_required_runs": [r["command"] for r in w["required_runs"] if not r.get("done")][:8],
        "recent_errors": len([s for s in st["steps"][-6:] if s.get("status") == "error"]),
    }
    rep = st.get("report")
    f["has_agent_report"] = bool(rep)
    f["agent_report_age_steps"] = (st["seq"] - rep["seq"]) if rep else None
    if rep:
        mism = []
        if rep["believes_verified"] and unverified:
            mism.append("claims its work is verified, but %d edited file(s) have no passing test/build since "
                        "their last edit" % len(unverified))
        if rep["believes_verified"] and f["last_verification_failed"]:
            mism.append("claims its work is verified, but the last test/build failed")
        if rep["believes_verified"] and not last_ver and w["edits"]:
            mism.append("claims its work is verified, but no test or build has run")
        if rep["believes_goal_done"] and cur and cur["status"] == "in_progress" and f["unverified_edits"]:
            mism.append("believes '%s' is done while its edits are unverified" % cur["content"][:60])
        # announcing the next goal just before updating the list is normal; only work that
        # matches no todo item at all is off-list
        if rep["current_goal"] and g["todos"] and not any(_same_goal(rep["current_goal"], t["content"])
                                                          for t in g["todos"]):
            mism.append("reports working on '%s', which is not on the todo list; add it or update the list"
                        % rep["current_goal"][:60])
        f["report_mismatches"] = mism
    if proposed:
        f["proposed_kind"] = proposed["kind"]
        if proposed["kind"] == "edit":
            f["edits_tests_only"] = bool(proposed["paths"]) and all(steps.is_test_path(p) for p in proposed["paths"])
            f["edit_targets_never_read"] = [_rel(p, st["cwd"]) for p in proposed["paths"]
                                            if p not in k["files"] and os.path.exists(p)][:5]
        if proposed["kind"] == "goal_update":
            done = steps.newly_completed(todos, proposed.get("todos_after"))
            f["marks_completed"] = [t["content"] for t in done]
            f["marks_current_goal_completed"] = bool(cur and any(t["id"] == cur["id"] for t in done))
    return f


# ------------------------------------------------------------- jev payload

SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|apikey|token|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"),
     r"\1[REDACTED]"),
    (re.compile(r"\b(?:sk-|xai-|fc-|gh[pousr]_|AKIA|glpat-|xox[abp]-)[A-Za-z0-9_\-]{10,}"), "[REDACTED]"),
    (re.compile(r"[A-Za-z0-9+/_\-]{40,}={0,2}"), "[REDACTED]"),
]


def scrub(text):
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def is_sensitive(text, patterns):
    text = (text or "").replace(os.path.expanduser("~"), "~")
    return any(re.search(p, text) for p in patterns)


def _mask(obj, patterns, limit):
    """Clip, scrub secrets, and replace anything touching a sensitive path."""
    if isinstance(obj, dict):
        return {k: _mask(v, patterns, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(v, patterns, limit) for v in obj]
    if isinstance(obj, str):
        if is_sensitive(obj, patterns):
            return "[sensitive: not shared]"
        home = os.path.expanduser("~")
        s = scrub(obj.replace(home, "~"))
        return s if len(s) <= limit else s[:limit] + "…"
    return obj


def to_jev_state(st, cfg, proposed=None):
    """Compact state for Jev: named sections referenced by backticked paths."""
    k, w, g = st["knowledge"], st["workspace"], st["goal"]
    lim = cfg["max_field_chars"]
    recent = [{"kind": s["kind"], "step": s.get("summary", ""), "status": s.get("status"),
               "result": (s.get("result") or "")[-200:]}
              for s in st["steps"][-cfg["recent_steps"]:] if s.get("status") != "pending"]
    files = sorted(k["files"].items(), key=lambda kv: -kv[1]["seq"])[:25]
    state = {
        "task": st["task"] or "(no user prompt recorded)",
        "earlier_prompts": st["earlier_prompts"],
        "goal": {"todos": [{"content": t["content"], "status": t["status"]} for t in g["todos"]][:20]},
        "knowledge": {
            "files_known": [{"path": p, "how": v["how"]} for p, v in files],
            "tests": k["tests"] and {x: k["tests"][x] for x in ("command", "status", "tail")},
            "build": k["build"] and {x: k["build"][x] for x in ("command", "status", "tail")},
            "processes": [{x: p.get(x) for x in ("command", "status", "tail")} for p in k["processes"]],
            "recent_errors": [{"step": e["step"], "error": e["error"]} for e in k["errors"]],
        },
        "workspace": {
            "edited_files": sorted(w["edits"])[:25],
            "required_edits": w["required_edits"],
            "required_runs": w["required_runs"],
        },
        "facts": facts(st, proposed),
        "recent_steps": recent,
    }
    rep = st.get("report")
    if rep:
        state["agent_report"] = {x: rep[x] for x in ("current_goal", "knowledge", "workspace", "believes_goal_done",
                                                     "believes_verified", "next_step")}
        state["agent_report"]["note"] = ("The agent's own account of its state: claims to check against `knowledge`, "
                                         "`workspace` and `recent_steps`, not evidence.")
    cur = steps.current_goal(g["todos"])
    state["goal"]["current"] = cur["content"] if cur else "(no todo list: the whole `task`)"
    if proposed:
        state["proposed_step"] = {"kind": proposed["kind"], "tool": proposed.get("tool"),
                                  "step": proposed.get("summary", ""), "paths": proposed.get("paths", [])}
    return _mask(state, cfg["sensitive"], lim)
