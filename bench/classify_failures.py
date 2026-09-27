#!/usr/bin/env python3
"""Label why each failed rollout failed, with one Jev call per trial.

    bench/classify_failures.py [results-dir] [--run swe] [--limit N] [--dry-run] [--redo]

Code computes the facts (which target tests still fail, regressions, files edited, whether tests
were run, how the run ended, the judge's blocks and reports, the last steps and the final diff);
Jev answers one typed choice question about the cause, and for judge arms the probability that
judge friction stopped the agent short of a fix. About $0.00004 per trial.

Appends to <results>/failure_labels.jsonl (skipping trials already labelled unless --redo); the
viewer (bench/viewer/serve.py) shows the label on each trial and lets you filter by it.
Needs TYPESAFE_API_KEY or ~/.config/typesafe/key.
"""
import argparse
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from jev_stepwise_judge import jev  # noqa: E402
import analyze  # noqa: E402

MODEL = "jev-1.13.0"
DIR = re.compile(r"^(?P<run>.+?)-(?P<arm>off|agent|joints|every_step)(?:-a(?P<attempt>\d+)-c(?P<chunk>\d+))?$")
TESTY = re.compile(r"pytest|runtests|tox |unittest|manage\.py test|python -m test|make test", re.I)

CAUSES = {
    "no_code_change": "Never made a code change that could fix the issue: it explored, then stopped or ran out.",
    "wrong_location": "Changed code, but not where the bug is: the target tests fail because the change misses the real cause.",
    "wrong_fix": "Changed the right code, but the logic does not do what the issue asks; the target tests fail.",
    "partial_fix": "Fixed part of the required behaviour: some target tests pass, others still fail.",
    "broke_existing": "The change breaks tests that passed before (regressions), whether or not the target tests pass.",
    "ran_out": "Stopped by a context overflow or the time limit while still working toward a fix.",
    "unverified_claim": "Declared the task done without running a test that would have shown the fix does not work.",
    "env_trouble": "Could not run the code or tests (imports, setup, tooling) and that derailed the attempt.",
}


def _short(v, n):
    s = v if isinstance(v, str) else json.dumps(v)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + " …"


def facts_for(tdir):
    res = json.load(open(os.path.join(tdir, "result.json")))
    stream = []
    for line in open(os.path.join(tdir, "agent", "opencode.txt"), errors="replace"):
        if line.strip().startswith("{"):
            try:
                stream.append(json.loads(line))
            except ValueError:
                pass
    tools = [e["part"] for e in stream if e.get("type") == "tool_use"]
    texts = [(e.get("part") or {}).get("text", "") for e in stream if e.get("type") == "text"]
    edited, diffs, test_runs = [], [], []
    for p in tools:
        st = p.get("state") or {}
        inp = st.get("input") or {}
        if p.get("tool") in ("edit", "write", "patch", "multiedit") and st.get("status") == "completed":
            f = inp.get("filePath") or inp.get("path")
            if f and f not in edited:
                edited.append(f)
            d = (st.get("metadata") or {}).get("diff")
            if d:
                diffs.append(d)
        if p.get("tool") == "bash" and TESTY.search(str(inp.get("command", ""))):
            test_runs.append({"command": _short(inp.get("command"), 160), "status": st.get("status"),
                              "output_tail": _short(str(st.get("output") or st.get("error") or "")[-600:], 400)})
    oc = "\n".join(json.dumps(e) for e in stream if e.get("type") == "error")
    exc = (res.get("exception_info") or {}).get("exception_type")
    ended = ("context_overflow" if "ContextOverflowError" in oc else "time_limit" if exc == "AgentTimeoutError"
             else "exception: " + exc if exc else "agent stopped on its own")
    rep = {}
    try:
        rep = next(iter(json.load(open(os.path.join(tdir, "verifier", "report.json"))).values()))
    except (OSError, ValueError, StopIteration):
        pass
    ts = rep.get("tests_status") or {}
    f2p, p2p = ts.get("FAIL_TO_PASS") or {}, ts.get("PASS_TO_PASS") or {}
    t = analyze.trial(tdir)
    traj = json.load(open(os.path.join(tdir, "agent", "trajectory.json")))
    task = next((s.get("message", "") for s in traj.get("steps", []) if s.get("source") == "user"), "")
    judg = []
    jl = os.path.join(tdir, "agent", "jev-stepwise-judge", "judgments.jsonl")
    if os.path.exists(jl):
        judg = [json.loads(x) for x in open(jl) if x.strip()]
    last_dir = next((j.get("direction") for j in reversed(judg) if j.get("direction")), None)
    return {
        "task": _short(task, 1500),
        "outcome": {
            "target_tests_failing": f2p.get("failure", [])[:10], "target_tests_passing": len(f2p.get("success", [])),
            "regressions": p2p.get("failure", [])[:10], "regression_count": len(p2p.get("failure", [])),
            "patch_applied": rep.get("patch_successfully_applied"), "ended": ended,
        },
        "work": {
            "files_edited": edited[:15], "successful_edits": len(diffs), "test_runs": len(test_runs),
            "last_test_runs": test_runs[-3:], "turns": sum(1 for e in stream if e.get("type") == "step_start"),
            "tool_calls": len(tools),
        },
        "judge": {"reports": t["set_state"], "blocked_steps": t["blocks"], "last_direction": last_dir},
        "recent_steps": [
            {"tool": p.get("tool"), "input": _short((p.get("state") or {}).get("input"), 160),
             "status": (p.get("state") or {}).get("status"),
             "result": _short(str((p.get("state") or {}).get("output") or (p.get("state") or {}).get("error") or "")[-300:], 200)}
            for p in tools[-15:]],
        "final_message": _short(texts[-1] if texts else "", 800),
        "final_diff": _short("\n".join(diffs[-2:]), 1500),
    }


def questions(judge_arm):
    q = {"failure_cause": {
        "type": "choice",
        "instructions": ("The agent's fix for `task` did not pass the tests (see `outcome`). Given `outcome`, `work`, "
                         "`recent_steps`, `final_message` and `final_diff`, what is the main reason it failed?"),
        "criteria": CAUSES,
    }}
    if judge_arm:
        q["judge_friction"] = {
            "type": "noul",
            "instructions": ("The agent ran under a judge that can block steps until it reports its state "
                             "(`judge.blocked_steps`, `judge.reports`). Did that friction use up the turns, context or "
                             "time the agent needed, so it stopped short of a fix it was close to?"),
            "criteria": {"true": "Yes: the blocks and reports displaced work the agent was about to do.",
                         "false": "No: the failure comes from the agent's own understanding or fix, not the judge."},
        }
    return q


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="?", default=os.path.join(HERE, "results"))
    ap.add_argument("--run", default="swe")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="print the first request instead of calling Jev")
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    out_path = os.path.join(a.results, "failure_labels.jsonl")
    done = set()
    if os.path.exists(out_path) and not a.redo:
        done = {json.loads(x).get("trial_dir") for x in open(out_path) if x.strip()}
    todo = []
    for jdir in sorted(glob.glob(os.path.join(a.results, "jobs", "*"))):
        m = DIR.match(os.path.basename(jdir))
        if not m or m.group("run") != a.run:
            continue
        for rj in sorted(glob.glob(os.path.join(jdir, "*", "result.json"))):
            tdir = os.path.dirname(rj)
            rel = os.path.relpath(tdir, a.results)
            if rel in done:
                continue
            t = analyze.trial(tdir)
            if t["reward"] >= 1 or t["infra"]:
                continue
            todo.append((rel, tdir, m.group("arm")))
    if a.limit:
        todo = todo[:a.limit]
    print("%d failed trials to label" % len(todo), flush=True)
    mode = "w" if a.redo else "a"
    with open(out_path, mode) as out:
        for i, (rel, tdir, arm) in enumerate(todo, 1):
            state = facts_for(tdir)
            q = questions(arm != "off")
            if a.dry_run:
                print(json.dumps({"state": state, "questions": q, "model": MODEL}, indent=1)[:6000])
                return
            try:
                resp = jev.ask(state, q, MODEL, 30)
            except jev.JevUnavailable as e:
                print("  %s: Jev unavailable (%s)" % (rel, e), flush=True)
                continue
            ans = resp.get("answers") or {}
            fc = ans.get("failure_cause") or {}
            rec = {"trial_dir": rel, "arm": arm, "task": os.path.basename(tdir).rsplit("__", 1)[0],
                   "cause": fc.get("choice"), "confidence": fc.get("confidence"),
                   "judge_friction": (ans.get("judge_friction") or {}).get("noul"),
                   "answers": ans, "facts": {k: state[k] for k in ("outcome", "work", "judge")}}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            if i % 25 == 0 or i == len(todo):
                print("  %d/%d" % (i, len(todo)), flush=True)


if __name__ == "__main__":
    main()
