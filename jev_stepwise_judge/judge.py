"""Jev questions and the policy that turns Jev's answers into a direction.

One batched request per judgment. Jev answers narrow typed questions about the
agent state; code owns the policy (what to advise, block, or let through).
"""
import os
import time

from . import jev, state as S

DIRECTIONS = {
    "plan_goals": "Write or update the todo list first: there is no todo list, or the work has outgrown it.",
    "gather_knowledge": "Read files, search, or inspect output: something the next change depends on is still "
                        "unknown or out of date.",
    "edit_workspace": "Make the file changes the current goal still requires.",
    "run_verification": "Run the tests, build, lint or type-check: edits have not been verified since they were made.",
    "fix_failure": "Diagnose and fix the most recent failure (failing test, build error, crashed process) "
                   "before anything else.",
    "run_process": "Start or restart a process the work needs (server, script, migration, generator).",
    "check_process": "Check on a background process whose state is unknown or that may have failed.",
    "advance_goal": "Mark the current goal completed in the todo list and move on to the next goal: its work is "
                    "done and verified.",
    "ship": "Commit, push or publish work that is complete and verified.",
    "ask_user": "Stop and ask the user: a decision, missing information, or consent is needed.",
    "finish": "Every goal is done and verified: report back to the user.",
}

# which step kinds are consistent with moving in a direction
COMPATIBLE = {
    "plan_goals": {"goal_update"},
    "gather_knowledge": {"read", "search", "web", "vcs_read", "check_process", "delegate"},
    "edit_workspace": {"edit", "shell", "delegate"},
    "run_verification": {"test", "build"},
    "fix_failure": {"read", "search", "vcs_read", "edit", "test", "build", "shell", "check_process", "web"},
    "run_process": {"run_process", "shell"},
    "check_process": {"check_process", "stop_process", "read", "run_process"},
    "advance_goal": {"goal_update"},
    "ship": {"vcs_commit", "vcs_push", "external", "vcs_read"},
    "ask_user": {"meta"},
    "finish": {"goal_update", "meta"},
}
TYPED_KINDS = {"read", "search", "web", "vcs_read", "edit", "test", "build", "run_process", "check_process",
               "stop_process", "vcs_commit", "vcs_push", "goal_update"}
WORK_KINDS = {"edit", "test", "build", "run_process", "vcs_commit", "vcs_push"}


# ---------------------------------------------------------------- questions

def direction_questions(f):
    q = {
        "direction": {
            "type": "choice",
            "instructions": ("Setting `proposed_step` aside, which direction should the agent move in next toward "
                             "`goal.current` and `task`, given `knowledge`, `workspace`, `goal` and `facts`?"),
            "criteria": DIRECTIONS,
        },
    }
    if f["has_todo_list"]:
        q["current_goal_done"] = {
            "type": "noul",
            "instructions": ("Do `knowledge`, `workspace` and `recent_steps` show that `goal.current` is fully done: "
                             "its changes made and, where code changed, verified by a passing test or build?"),
            "criteria": {"true": "Done and verified; the agent can move to the next goal.",
                         "false": "Work, verification or a fix is still outstanding for this goal."},
        }
    if f["unverified_edits"]:
        q["verification_due"] = {
            "type": "noul",
            "instructions": ("Should the files in `facts.unverified_edits` be verified with a test or build run "
                             "before the agent moves on?"),
            "criteria": {"true": "Yes: they changed behaviour or code that tests or a build would check.",
                         "false": "No: docs, config or trivial changes, or verification is not possible here."},
        }
    return q


def step_questions(f):
    q = direction_questions(f)
    q["step_sound"] = {
        "type": "noul",
        "instructions": "Given `knowledge`, `workspace` and `goal`, is `proposed_step` a sensible next action?",
        "criteria": {"true": "A careful engineer would take this step now.",
                     "false": "It is premature, redundant, mistaken, off-goal or needlessly risky right now."},
    }
    q["knowledge_sufficient"] = {
        "type": "noul",
        "instructions": ("Does the agent already know what `proposed_step` depends on (see `knowledge` and "
                         "`recent_steps`), without reading, searching or inspecting anything else first?"),
    }
    q["goal_aligned"] = {
        "type": "noul",
        "instructions": "Does `proposed_step` advance `goal.current` (or `task` when there is no todo list)?",
    }
    if f["recent_errors"]:
        q["repeats_failure"] = {
            "type": "noul",
            "instructions": ("Is `proposed_step` essentially the same action as a step in `recent_steps` whose "
                             "`status` is `error`, retried without a meaningful change in between?"),
            "criteria": {"true": "It retries a failed step unchanged.",
                         "false": "It is a new action, or a retry after a relevant change."},
        }
    return q


def report_questions(f, report):
    """Direction questions plus checks of the agent's own report and its next step."""
    q = direction_questions(f)
    q["claims_supported"] = {
        "type": "noul",
        "instructions": ("Are the claims in `agent_report` (what the agent says it learned, changed, finished and "
                         "verified) supported by the observed evidence in `knowledge`, `workspace` and "
                         "`recent_steps`?"),
        "criteria": {"true": "The evidence backs the report.",
                     "false": "The report claims knowledge, changes, completion or verification the evidence "
                              "does not show, or contradicts it."},
    }
    if report.get("next_step"):
        q["next_step_sound"] = {
            "type": "noul",
            "instructions": ("Given the evidence and `goal.current`, is `agent_report.next_step` a sensible next "
                             "action for the agent?"),
            "criteria": {"true": "A careful engineer would do this next.",
                         "false": "It is premature, redundant, mistaken, off-goal or needlessly risky right now."},
        }
    if report["knowledge"]["open_questions"] and report.get("next_step"):
        q["questions_block_step"] = {
            "type": "noul",
            "instructions": ("Must one of `agent_report.knowledge.open_questions` be answered before "
                             "`agent_report.next_step` can be done correctly?"),
        }
    return q


def judge_report(st, cfg):
    """set_state: judge the agent's own report together with the observed state.
    -> direction, a verdict on the agent's intended next step, and mismatches."""
    rep = st["report"]
    f = S.facts(st)
    t0 = time.time()
    resp = jev.ask(S.to_jev_state(st, cfg), report_questions(f, rep), cfg["model"], cfg["timeout_s"])
    answers = resp.get("answers", {})
    n = {k: v.get("noul") for k, v in answers.items() if v.get("type") == "noul"}
    d = direction_from(answers, f, st)
    mismatches = list(f.get("report_mismatches") or [])
    th = cfg["thresholds"]
    if n.get("claims_supported", 1.0) <= th["claims_unsupported_max"] and not mismatches:
        mismatches.append("the report claims things the observed evidence does not show (p=%.2f)"
                          % n["claims_supported"])
    if rep["believes_goal_done"] and (n.get("current_goal_done") or 1.0) <= th["report_goal_done_max"]:
        mismatches.append("believes the current goal is done, but the evidence says it is not (p=%.2f)"
                          % n["current_goal_done"])

    verdict, why = "go ahead", []
    if rep["next_step"]:
        if n.get("next_step_sound", 1.0) < 0.5:
            verdict = "reconsider"
            why.append("the intended next step does not look sensible now (p=%.2f)" % n["next_step_sound"])
        if n.get("questions_block_step", 0) >= 0.7:
            verdict = "reconsider"
            why.append("answer your open questions first")
        if mismatches and d["direction"] in ("run_verification", "fix_failure", "gather_knowledge"):
            verdict = "reconsider"
    else:
        verdict = "follow the direction"
    if mismatches:
        why.append("your report does not match the evidence")
    return {
        "direction": d,
        "summary": direction_text(d),
        "next_step": rep["next_step"] or None,
        "next_step_verdict": verdict,
        "why": why,
        "mismatches": mismatches,
        "answers": jev.summarize(answers),
        "ms": int((time.time() - t0) * 1000),
        "model": resp.get("model"),
    }


def stop_questions():
    return {"finished": {
        "type": "noul",
        "instructions": ("Has the agent fully completed `task`: every item in `goal.todos` done and code changes "
                         "verified by tests or a build where relevant?"),
        "criteria": {"true": "Complete; stopping now is right.",
                     "false": "Work, verification or a todo item is still outstanding."},
    }}


# ---------------------------------------------------------------- direction

def suggestions(direction, f, st):
    """Concrete next actions for a direction, from exact state."""
    k = st["knowledge"]
    out = []
    if direction == "plan_goals":
        out.append("write a todo list (TodoWrite / update_plan / todo_write / todowrite, or the MCP set_plan tool) "
                   "with exactly one item in_progress")
    elif direction == "gather_knowledge":
        for p in f.get("edit_targets_never_read") or []:
            out.append("read %s before editing it" % p)
        if f["current_goal"]:
            out.append("read what '%s' depends on" % f["current_goal"][:80])
    elif direction == "run_verification":
        last = k["tests"] or k["build"]
        if last:
            out.append("re-run: %s" % last["command"][:160])
        if f["unverified_edits"]:
            out.append("unverified since last edit: %s" % ", ".join(os.path.basename(p) for p in f["unverified_edits"][:6]))
    elif direction == "fix_failure":
        failed = [v for v in (k["tests"], k["build"]) if v and v["status"] == "fail"]
        if failed:
            tail = [ln for ln in failed[-1]["tail"].splitlines() if ln.strip()][-2:]
            out.append("failing: %s -> %s" % (failed[-1]["command"][:120], " | ".join(tail)[:200]))
        elif k["errors"]:
            out.append("last error in: %s" % k["errors"][-1]["step"][:160])
    elif direction == "check_process":
        out += ["check: %s" % c for c in (f["unchecked_processes"] or f["failed_processes"])[:3]]
    elif direction == "run_process":
        out += ["run: %s" % c for c in f["outstanding_required_runs"][:3]]
    elif direction == "edit_workspace":
        out += ["edit: %s" % p for p in f["outstanding_required_edits"][:5]]
    elif direction == "advance_goal":
        if f["current_goal"]:
            nxt = " and set '%s' in_progress" % f["next_goal"][:80] if f["next_goal"] else ""
            out.append("mark '%s' completed%s" % (f["current_goal"][:80], nxt))
    elif direction in ("finish", "ship"):
        if f["unverified_edits"]:
            out.append("verify first: %d edited file(s) untested" % len(f["unverified_edits"]))
    age = f.get("agent_report_age_steps")
    if direction in ("advance_goal", "finish", "fix_failure", "ship") and (age is None or age > 12):
        out.append("report your state with the MCP tool set_state before moving on")
    return out


def _override(f, direction, conf, probs, goal_done):
    """Hard rules that beat the model. The skill mandates a todo list, and a
    direction that contradicts exact facts falls back to the next-best one."""
    if not f["has_todo_list"] and (f["edited_files"] or f["unverified_edits"]
                                   or f.get("proposed_kind") in WORK_KINDS | {"goal_update"}):
        return "plan_goals", 1.0, "no todo list yet (required)"
    if f["last_verification_failed"] and direction in ("advance_goal", "ship", "finish"):
        return "fix_failure", 1.0, "the last test/build failed"
    ruled_out = set()
    if goal_done is not None and goal_done < 0.5:
        ruled_out.add("advance_goal")
    if f["has_todo_list"] and not f["all_goals_done"]:
        ruled_out.add("finish")
    if f["has_todo_list"] and conf < 0.8:
        ruled_out.add("plan_goals")
    if direction in ruled_out:
        rest = sorted(((k, p) for k, p in probs.items() if k not in ruled_out), key=lambda kv: -kv[1])
        if rest:
            return rest[0][0], rest[0][1], "%s ruled out by state" % direction
    return direction, conf, None


def direction_from(answers, f, st):
    d = answers.get("direction") or {}
    direction, conf = d.get("choice"), d.get("confidence") or 0.0
    probs = d.get("probabilities") or {}
    goal_done = (answers.get("current_goal_done") or {}).get("noul")
    direction, conf, rule = _override(f, direction, conf, probs, goal_done)
    alts = sorted(((k, p) for k, p in probs.items() if k != direction), key=lambda kv: -kv[1])[:3]
    return {
        "direction": direction,
        "confidence": round(conf, 2),
        "meaning": DIRECTIONS.get(direction, ""),
        "do": suggestions(direction, f, st),
        "rule": rule,
        "alternatives": {k: round(p, 2) for k, p in alts},
        "current_goal": f["current_goal"],
        "current_goal_done": (answers.get("current_goal_done") or {}).get("noul"),
        "goals_done": f["goals_done"],
    }


def direction_text(d):
    s = "next: %s (%.2f) - %s" % (d["direction"], d["confidence"], d["meaning"])
    if d.get("rule"):
        s += " [%s]" % d["rule"]
    if d["do"]:
        s += " Do: " + "; ".join(d["do"])
    return s


def get_direction(st, cfg):
    """Ask Jev which way to go from the current state (no proposed step)."""
    f = S.facts(st)
    t0 = time.time()
    resp = jev.ask(S.to_jev_state(st, cfg), direction_questions(f), cfg["model"], cfg["timeout_s"])
    d = direction_from(resp.get("answers", {}), f, st)
    d["ms"], d["model"] = int((time.time() - t0) * 1000), resp.get("model")
    d["answers"] = jev.summarize(resp.get("answers"))
    return d


# ------------------------------------------------------------------- policy

SEVERITY = {"none": 0, "advice": 1, "ask": 2, "deny": 3}


def decide_step(answers, f, st, cfg):
    """-> (severity, message, direction). severity: none | advice | ask | deny."""
    th = cfg["thresholds"]
    n = {k: v.get("noul") for k, v in answers.items() if v.get("type") == "noul"}
    d = direction_from(answers, f, st)
    kind = f.get("proposed_kind")
    findings = []

    if n.get("repeats_failure", 0) >= th["repeat_failure"]:
        findings.append(("deny", "this retries a step that just failed without a relevant change; diagnose the error"))
    if kind == "goal_update" and f.get("marks_current_goal_completed"):
        why = []
        if n.get("current_goal_done", 1.0) < th["goal_incomplete_max"]:
            why.append("its work does not look done (p=%.2f)" % n["current_goal_done"])
        if f["unverified_edits"] and n.get("verification_due", 0) >= th["verification_due"]:
            why.append("%d edited file(s) are unverified" % len(f["unverified_edits"]))
        if f["last_verification_failed"]:
            why.append("the last test/build failed")
        if why:
            findings.append(("deny", "don't mark '%s' completed yet: %s" % ((f["current_goal"] or "")[:80], "; ".join(why))))
    if kind in ("vcs_commit", "vcs_push"):
        if f["last_verification_failed"]:
            findings.append(("deny", "the last test/build failed; fix it before committing or pushing"))
        elif f["unverified_edits"] and n.get("verification_due", 0) >= th["ship_verification_due"]:
            findings.append(("deny", "verify first: %d edited file(s) untested since the last change"
                             % len(f["unverified_edits"])))
    if not f["has_todo_list"] and kind in WORK_KINDS:
        findings.append(("deny", "write the todo list first (required): one item per goal, one in_progress"))
    if f.get("edit_targets_never_read"):
        findings.append(("advice", "read %s before editing it" % ", ".join(f["edit_targets_never_read"][:3])))
    if (d["direction"] == "advance_goal" and kind != "goal_update"
            and n.get("current_goal_done", 0) >= th["goal_done_nudge"]):
        findings.append(("advice", "'%s' looks done: mark it completed and move to the next goal"
                         % (f["current_goal"] or "")[:80]))
    planning = kind == "goal_update" and not f.get("marks_current_goal_completed")
    writes_tests = kind == "edit" and f.get("edits_tests_only")
    if writes_tests and d["direction"] == "run_verification":
        planning = True  # writing tests is verification work, not a detour from it
    if (not planning and kind in TYPED_KINDS and d["direction"] in COMPATIBLE and kind not in COMPATIBLE[d["direction"]]
            and d["confidence"] >= th["move_conf"]
            and (n.get("step_sound", 1.0) < 0.5 or n.get("goal_aligned", 1.0) <= th["off_goal_max"])):
        findings.append(("advice", "this %s step is off-direction" % kind))
    if (not planning and n.get("knowledge_sufficient", 1.0) <= th["knowledge_gap_max"]
            and d["direction"] == "gather_knowledge"
            and d["confidence"] >= th["move_conf"] and kind not in ("read", "search", "vcs_read", "web")):
        findings.append(("advice", "something this step depends on is still unknown; look first"))

    if not findings:
        return "none", "", d
    findings.sort(key=lambda x: -SEVERITY[x[0]])
    severity = findings[0][0]
    msg = "; ".join(m for _, m in findings[:3]) + ". " + direction_text(d)
    return severity, msg, d


def judge_step(st, cfg, proposed):
    f = S.facts(st, proposed)
    t0 = time.time()
    resp = jev.ask(S.to_jev_state(st, cfg, proposed), step_questions(f), cfg["model"], cfg["timeout_s"])
    answers = resp.get("answers", {})
    severity, msg, d = decide_step(answers, f, st, cfg)
    return {"severity": severity, "message": msg, "direction": d, "ms": int((time.time() - t0) * 1000),
            "model": resp.get("model"), "answers": jev.summarize(answers)}


def judge_stop(st, cfg):
    """-> (block: bool, message). Only asks Jev when something looks open."""
    f = S.facts(st)
    open_items = []
    if f["has_todo_list"] and not f["all_goals_done"]:
        open_items.append("goals done %s" % f["goals_done"])
    if f["unverified_edits"]:
        open_items.append("%d edited file(s) unverified" % len(f["unverified_edits"]))
    if f["last_verification_failed"]:
        open_items.append("last test/build failed")
    if f["unchecked_processes"]:
        open_items.append("%d background process(es) never checked" % len(f["unchecked_processes"]))
    if not open_items:
        return False, "", None
    resp = jev.ask(S.to_jev_state(st, cfg), stop_questions(), cfg["model"], cfg["timeout_s"])
    p = (resp.get("answers", {}).get("finished") or {}).get("noul", 1.0)
    if p >= cfg["thresholds"]["finished_min"]:
        return False, "", p
    return True, "Not finished (p=%.2f): %s. Continue, or tell the user what is left and why." % (
        p, "; ".join(open_items)), p


def choose(task, context, options, cfg):
    """Pick the best of the agent's own candidate next steps."""
    opts = {"option_%d" % (i + 1): S.scrub(o)[:300] for i, o in enumerate(options)}
    opts["none"] = "None of the listed options is a sensible next step."
    q = {"best": {"type": "choice", "criteria": opts,
                  "instructions": "Which option is the best next step toward `task`, given `context`?"}}
    resp = jev.ask({"task": S.scrub(task)[:2000], "context": S.scrub(context)[:3000]}, q, cfg["model"], cfg["timeout_s"])
    ans = resp["answers"]["best"]

    def name(k):
        return "none of these" if k == "none" else options[int(k.split("_")[1]) - 1]
    return {"best": name(ans["choice"]), "confidence": ans.get("confidence"),
            "ranking": [(name(k), round(p, 2)) for k, p in sorted(ans.get("probabilities", {}).items(),
                                                                   key=lambda kv: -kv[1])]}
