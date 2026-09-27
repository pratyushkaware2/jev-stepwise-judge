"""Hook entry point shared by Claude Code, Codex, Grok CLI and OpenCode (via its plugin).

  UserPromptSubmit          -> task recorded (and a todo-list reminder when there is none)
  PreToolUse                -> step recorded, judged against the state; may advise / ask / deny
  PostToolUse(+Failure)     -> effects applied to knowledge / workspace / goal; after key
                               events (test/build result, goal change, failure) the next
                               direction is pushed to the agent
  Stop                      -> stop gate: open goals, unverified edits, failed checks

Output is Claude-style hookSpecificOutput JSON, which Codex and Grok also accept.
Silence means "no opinion": this never emits `allow`, because in Claude Code an
allow would skip the user's own permission rules. Any failure fails open.
"""
import contextlib
import fcntl
import json
import os
import sys
import time

from . import config, jev, judge, state as S

EVENTS = {"pre_tool_use": "PreToolUse", "post_tool_use": "PostToolUse", "post_tool_use_failure": "PostToolUseFailure",
          "user_prompt_submit": "UserPromptSubmit", "stop": "Stop", "subagent_stop": "SubagentStop"}
PUSH_KINDS = {"test", "build", "goal_update", "run_process"}


def normalize(p, hint):
    grok = "toolName" in p or "sessionId" in p or str(p.get("hookEventName", "")).islower()
    agent = "grok" if grok else ("codex" if "turn_id" in p else hint)
    event = p.get("hook_event_name") or p.get("hookEventName") or ""
    return {
        "agent": agent,
        "event": EVENTS.get(event, event),
        "session": str(p.get("session_id") or p.get("sessionId") or "default"),
        "cwd": p.get("cwd") or p.get("workspaceRoot") or os.getcwd(),
        "tool": p.get("tool_name") or p.get("toolName") or "",
        "input": p.get("tool_input") if "tool_input" in p else p.get("toolInput"),
        "response": p.get("tool_response") if "tool_response" in p else p.get("toolResult"),
        "tool_use_id": p.get("tool_use_id") or p.get("toolUseId"),
        "prompt": p.get("prompt") or p.get("userPrompt") or "",
        "transcript": p.get("transcript_path"),
        "stop_active": bool(p.get("stop_hook_active") or p.get("stopHookActive")),
    }


@contextlib.contextmanager
def locked(agent, session):
    os.makedirs(S.sessions_dir(), mode=0o700, exist_ok=True)
    lock_path = S._path(agent, session) + ".lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def log(entry):
    try:
        os.makedirs(config.state_dir(), mode=0o700, exist_ok=True)
        with open(os.path.join(config.state_dir(), "judgments.jsonl"), "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}, default=str) + "\n")
    except OSError:
        pass


def transcript_task(path):
    """Claude Code fallback when the hook was installed mid-session."""
    task = ""
    try:
        with open(path) as f:
            lines = f.readlines()[-400:]
    except (OSError, TypeError):
        return task
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "user" or ev.get("isMeta"):
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
        if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
            task = content.strip()
    return task


def _out(obj):
    print(json.dumps(obj))


def _context(event, text):
    _out({"hookSpecificOutput": {"hookEventName": event, "additionalContext": "[jev-stepwise-judge] " + text}})


def _effective(severity, mode):
    if mode == "shadow":
        return "none"
    if mode == "advise" and severity in ("deny", "ask"):
        return "advice"
    return severity


def handle(h, cfg):
    agent, sid = h["agent"], h["session"]
    ev = h["event"]

    if ev == "UserPromptSubmit":
        with locked(agent, sid):
            st = S.load(agent, sid, h["cwd"])
            S.on_prompt(st, h["prompt"])
            st["pids"] = S.ancestor_pids()
            remind = not st["goal"]["todos"] and not st.get("reminded")
            st["reminded"] = st.get("reminded") or remind
            S.save(st)
        if remind and cfg["mode"] != "shadow":
            _context(ev, "keep a todo list for this task (one item per goal, one in_progress, mark items completed "
                         "only when done and verified). Your state is being recorded; at the joints you choose "
                         "(goal done, a failure, before finishing, when unsure) call the jev-stepwise-judge MCP tool "
                         "set_state with your own view and intended next step to get a direction.")
        return

    if ev == "PreToolUse":
        with locked(agent, sid):
            st = S.load(agent, sid, h["cwd"])
            if not st["pids"]:
                st["pids"] = S.ancestor_pids()
            if not st["task"] and h["transcript"]:
                st["task"] = transcript_task(h["transcript"])[:4000]
            rec = S.on_pre(st, h["tool"], h["input"], h["tool_use_id"])
            need = _report_required(st, rec, cfg)
            if need and cfg["mode"] != "shadow":
                rec["status"] = "blocked"
            elif rec["kind"] in S.ACTING_KINDS:
                st["last_acting_seq"] = rec["seq"]
            S.save(st)
        if need:
            log({"agent": agent, "event": ev, "kind": rec["kind"], "required": "set_state",
                 "blocked": cfg["mode"] != "shadow"})
            if cfg["mode"] != "shadow":
                _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "[jev-stepwise-judge] " + need}})
                return
        auto = cfg["auto_judge"]
        if auto == "off" or rec["kind"] in cfg["skip_kinds"]:
            return
        if auto == "gates" and not _is_gate(st, rec):
            return
        if S.is_sensitive(rec["summary"] + " " + " ".join(rec["paths"]), cfg["sensitive"]):
            log({"agent": agent, "event": ev, "kind": rec["kind"], "skipped": "sensitive"})
            return
        try:
            res = judge.judge_step(st, cfg, rec)
        except jev.JevUnavailable as e:
            log({"agent": agent, "event": ev, "kind": rec["kind"], "error": str(e)})
            return
        eff = _effective(res["severity"], cfg["mode"])
        log({"agent": agent, "event": ev, "kind": rec["kind"], "tool": h["tool"], "mode": cfg["mode"],
             "severity": res["severity"], "effective": eff, "message": res["message"],
             "direction": res["direction"]["direction"], "ms": res["ms"], "answers": res["answers"]})
        with locked(agent, sid):
            st = S.load(agent, sid, h["cwd"])
            st["last_direction"] = res["direction"]["direction"]
            if eff == "advice" and agent in ("codex", "opencode"):
                st["outbox"].append(res["message"])  # delivered with the tool result
            S.save(st)
        if eff in ("deny", "ask"):
            msg = res["message"]
            if eff == "ask" and agent in ("codex", "opencode"):
                eff, msg = "deny", msg + " Confirm with the user before retrying."
            _out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": eff,
                                         "permissionDecisionReason": "[jev-stepwise-judge] " + msg}})
        elif eff == "advice" and agent not in ("codex", "opencode"):
            _context("PreToolUse", res["message"])
        return

    if ev in ("PostToolUse", "PostToolUseFailure"):
        with locked(agent, sid):
            st = S.load(agent, sid, h["cwd"])
            rec = S.on_post(st, h["tool"], h["tool_use_id"], ev, h["response"])
            outbox, st["outbox"] = st["outbox"], []
            unknown = rec["kind"] in ("test", "build") and rec.get("verification") == "unknown"
            S.save(st)
        notes = list(outbox)
        if (unknown and cfg["auto_judge"] != "off" and cfg["classify_unknown_results"]
                and not S.is_sensitive(rec["summary"], cfg["sensitive"])):
            _classify_result(h, rec, cfg)
        push = ((rec["kind"] in PUSH_KINDS or rec["status"] == "error") and cfg["mode"] != "shadow"
                and cfg["auto_judge"] == "every_step")
        if push and not S.is_sensitive(rec["summary"] + " " + " ".join(rec["paths"]), cfg["sensitive"]):
            try:
                with locked(agent, sid):
                    st = S.load(agent, sid, h["cwd"])
                d = judge.get_direction(st, cfg)
                log({"agent": agent, "event": ev, "kind": rec["kind"], "pushed": d["direction"],
                     "confidence": d["confidence"], "ms": d["ms"]})
                confident = d["confidence"] >= cfg["thresholds"]["push_conf"] or d.get("rule")
                if confident and (d["direction"] != st.get("last_direction")
                                  or rec["kind"] in ("test", "build", "goal_update")):
                    notes.append(judge.direction_text(d))
                with locked(agent, sid):
                    st = S.load(agent, sid, h["cwd"])
                    st["last_direction"] = d["direction"]
                    S.save(st)
            except jev.JevUnavailable as e:
                log({"agent": agent, "event": ev, "error": str(e)})
        if notes:
            _context("PostToolUse", " | ".join(notes))
        return

    if ev == "Stop":
        with locked(agent, sid):
            st = S.load(agent, sid, h["cwd"])
        needs_report = (cfg["require_set_state"] in ("joints", "every_step") and not S.report_is_fresh(st)
                        and st.get("last_acting_seq", 0) > 0)
        if needs_report and not h["stop_active"] and cfg["mode"] != "shadow":
            log({"agent": agent, "event": ev, "required": "set_state", "blocked": True})
            _out({"decision": "block", "reason": "[jev-stepwise-judge] " + REPORT_ASK % cfg["require_set_state"]
                  + " Before stopping, set believes_goal_done / believes_verified honestly and use next_step "
                    "to say what you will report to the user."})
            return
        if not (cfg["stop_gate"] and cfg["auto_judge"] in ("gates", "every_step")):
            return
        if h["stop_active"] or st.get("stop_blocks", 0) >= 1:
            return
        try:
            block, msg, p = judge.judge_stop(st, cfg)
        except jev.JevUnavailable as e:
            log({"agent": agent, "event": ev, "error": str(e)})
            return
        log({"agent": agent, "event": ev, "block": block, "finished": p, "mode": cfg["mode"]})
        if not block or cfg["mode"] == "shadow":
            return
        if cfg["mode"] == "enforce":
            with locked(agent, sid):
                st = S.load(agent, sid, h["cwd"])
                st["stop_blocks"] = st.get("stop_blocks", 0) + 1
                S.save(st)
            _out({"decision": "block", "reason": "[jev-stepwise-judge] " + msg})
        else:
            _out({"systemMessage": "[jev-stepwise-judge] " + msg})


REPORT_ASK = ("Call the jev-stepwise-judge MCP tool set_state first (require_set_state=%s): report current_goal, "
              "what you learned and still don't know, what you changed / still must change / must run, whether you "
              "believe the goal is done and verified, and this step as next_step. It returns the direction and a "
              "verdict on this step; then retry.")


def _report_required(st, rec, cfg):
    """Message if this step needs a fresh set_state report first, else None. Code only."""
    policy = cfg.get("require_set_state", "agent")
    if policy == "agent" or S.report_is_fresh(st):
        return None
    if policy == "every_step":
        planning = rec["kind"] == "goal_update" and not S.facts(st, rec).get("marks_current_goal_completed")
        if rec["kind"] in S.ACTING_KINDS and not planning:
            return REPORT_ASK % policy
    elif policy == "joints" and _is_gate(st, rec):
        return REPORT_ASK % policy
    return None


def _is_gate(st, rec):
    """High-stakes moments judged under auto_judge=gates."""
    if rec["kind"] in ("vcs_commit", "vcs_push"):
        return True
    return rec["kind"] == "goal_update" and bool(S.facts(st, rec).get("marks_current_goal_completed"))


def _classify_result(h, rec, cfg):
    """Exit status unknown: ask Jev whether the test/build output shows success."""
    q = {"passed": {"type": "noul",
                    "instructions": "Does `output` show that `command` succeeded (tests passed / build clean)?"}}
    try:
        resp = jev.ask({"command": S.scrub(rec["summary"]), "output": S.scrub(rec.get("result") or "")},
                       q, cfg["model"], cfg["timeout_s"])
    except jev.JevUnavailable:
        return
    p = (resp.get("answers", {}).get("passed") or {}).get("noul")
    if p is None:
        return
    status = "pass" if p >= 0.7 else ("fail" if p <= 0.3 else "unknown")
    with locked(h["agent"], h["session"]):
        st = S.load(h["agent"], h["session"], h["cwd"])
        key = "tests" if rec["kind"] == "test" else "build"
        if st["knowledge"][key] and st["knowledge"][key]["seq"] == rec["seq"]:
            st["knowledge"][key]["status"] = status
            st["knowledge"][key]["classified_by"] = "jev"
        S.save(st)


def main(argv):
    cfg = config.load()
    if cfg["mode"] == "off":
        return 0
    hint = argv[argv.index("--agent") + 1] if "--agent" in argv else "claude"
    try:
        payload = json.load(sys.stdin)
        h = normalize(payload, hint)
    except (ValueError, AttributeError):
        return 0
    try:
        handle(h, cfg)
    except Exception as e:  # a judge bug must never break the agent
        log({"agent": h.get("agent"), "event": h.get("event"), "error": "internal %s: %s" % (type(e).__name__, e)})
    return 0
