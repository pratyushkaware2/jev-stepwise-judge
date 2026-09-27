#!/usr/bin/env python3
"""Summarise benchmark arms from Harbor job directories.

    bench/analyze.py <jobs-dir> <prefix> [--json out.json]

<prefix> selects the jobs, e.g. "swe" reads <jobs-dir>/swe-{off,agent,joints,every_step}.
Per arm: solve rate (mean reward over all attempts), a paired bootstrap CI over tasks for the
difference from the baseline arm (off), a paired sign test on per-task mean reward, and how
the judge was used: set_state calls, judge notes seen by the agent (blocks and directions),
Jev calls (judgments.jsonl), tokens, agent time, and exceptions.

Trials whose verifier or agent log shows a network / infrastructure error are counted apart
("infra") and left out of solve rates, since they say nothing about the agent; rerun them
identically for every arm.
"""
import datetime as dt
import glob
import json
import math
import os
import random
import re
import sys
from collections import defaultdict

ARMS = ["off", "agent", "joints", "every_step"]
INFRA = re.compile(
    r"curl: \(\d+\)|failed to download|Temporary failure in name resolution|Could not resolve host|"
    r"Connection (reset|refused|timed out)|HTTPSConnectionPool|ReadTimeout|ConnectTimeout|"
    r"error sending request|No space left on device|502 Bad Gateway|503 Service Unavailable",
    re.I)


def _t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _read(path, limit=None):
    try:
        with open(path, errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


def blocked_steps(opencode_log):
    """Tool calls the judge refused: OpenCode reports them as errored tool parts carrying the note."""
    n = 0
    for line in opencode_log.splitlines():
        if '"tool_use"' not in line or "jev-stepwise-judge" not in line:
            continue
        try:
            state = (json.loads(line).get("part") or {}).get("state") or {}
        except ValueError:
            continue
        if state.get("status") == "error" and "jev-stepwise-judge" in str(state.get("error", "")):
            n += 1
    return n


def trial(tdir):
    d = json.load(open(os.path.join(tdir, "result.json")))
    task = os.path.basename(tdir.rstrip("/")).rsplit("__", 1)[0]
    reward = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exc = d.get("exception_info") or None
    a = d.get("agent_execution") or {}
    secs = (_t(a["finished_at"]) - _t(a["started_at"])).total_seconds() if a.get("finished_at") else None
    ctx = d.get("agent_result") or {}
    oc = _read(os.path.join(tdir, "agent", "opencode.txt"))
    verifier = _read(os.path.join(tdir, "verifier", "test-stdout.txt"))
    jl = os.path.join(tdir, "agent", "jev-stepwise-judge", "judgments.jsonl")
    jev_calls = sum(1 for _ in open(jl)) if os.path.exists(jl) else 0
    infra = bool(INFRA.search(verifier)) and reward != 1.0
    if exc and exc.get("exception_type") in ("NonZeroAgentExitCodeError",) and INFRA.search(exc.get("exception_message", "")):
        infra = True
    return {
        "task": task, "reward": float(reward) if reward is not None else 0.0, "has_reward": reward is not None,
        "exception": exc and exc.get("exception_type"), "infra": infra, "agent_s": secs,
        "in_tok": ctx.get("n_input_tokens") or 0, "out_tok": ctx.get("n_output_tokens") or 0,
        "tools": oc.count('"type":"tool_use"'),
        "set_state": oc.count("jev-stepwise-judge_set_state"),
        "judge_notes": oc.count("[jev-stepwise-judge]"),
        "blocks": blocked_steps(oc),
        "todo": oc.count('"tool":"todowrite"'),
        "jev_calls": jev_calls,
    }


def load(jobs, prefix):
    out = {}
    for arm in ARMS:
        rows = []
        pattern = [os.path.join(jobs, "%s-%s" % (prefix, arm), "*", "result.json"),
                   os.path.join(jobs, "%s-%s-a*" % (prefix, arm), "*", "result.json")]  # lockstep chunks
        for res in [r for pat in pattern for r in glob.glob(pat)]:
            try:
                rows.append(trial(os.path.dirname(res)))
            except (OSError, ValueError, KeyError):
                pass
        if rows:
            out[arm] = rows
    return out


def per_task(rows):
    m = defaultdict(list)
    for r in rows:
        if not r["infra"]:
            m[r["task"]].append(r["reward"])
    return {t: sum(v) / len(v) for t, v in m.items() if v}


def bootstrap_diff(a, b, n=10000, seed=0):
    tasks = sorted(set(a) & set(b))
    if not tasks:
        return None
    rnd = random.Random(seed)
    diffs = []
    for _ in range(n):
        s = [rnd.choice(tasks) for _ in tasks]
        diffs.append(sum(b[t] - a[t] for t in s) / len(s))
    diffs.sort()
    return (sum(b[t] - a[t] for t in tasks) / len(tasks), diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1], len(tasks))


def sign_test(a, b):
    """Two-sided exact sign test on tasks where the per-task mean rewards differ."""
    tasks = set(a) & set(b)
    up = sum(1 for t in tasks if b[t] > a[t])
    down = sum(1 for t in tasks if b[t] < a[t])
    n = up + down
    if n == 0:
        return up, down, 1.0
    k = min(up, down)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return up, down, min(1.0, 2 * p)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def main():
    jobs, prefix = sys.argv[1], sys.argv[2]
    data = load(jobs, prefix)
    base = per_task(data.get("off", []))
    summary = {}
    print("%s: %s" % (prefix, jobs))
    print("%-11s %6s %6s %6s %8s %22s %12s %7s %7s %7s %7s %8s %9s" % (
        "arm", "trials", "infra", "exc", "solve%", "diff vs off [95% CI]", "sign +/-/p", "agent_s",
        "set_st", "notes", "blocks", "jev", "in_tok"))
    for arm in ARMS:
        rows = data.get(arm)
        if not rows:
            continue
        clean = [r for r in rows if not r["infra"]]
        pt = per_task(rows)
        solve = mean([r["reward"] for r in clean]) * 100
        diff = bootstrap_diff(base, pt) if arm != "off" and base else None
        sign = sign_test(base, pt) if arm != "off" and base else None
        summary[arm] = {
            "trials": len(rows), "infra": sum(r["infra"] for r in rows),
            "exceptions": sum(1 for r in rows if r["exception"]),
            "solve_rate": solve / 100, "tasks": len(pt),
            "diff_vs_off": diff and {"mean": diff[0], "ci95": [diff[1], diff[2]], "tasks": diff[3]},
            "sign_test": sign and {"better": sign[0], "worse": sign[1], "p": sign[2]},
            "agent_s": mean([r["agent_s"] for r in clean]),
            "set_state": mean([r["set_state"] for r in clean]), "judge_notes": mean([r["judge_notes"] for r in clean]),
            "blocks": mean([r["blocks"] for r in clean]), "jev_calls": mean([r["jev_calls"] for r in clean]),
            "jev_calls_total": sum(r["jev_calls"] for r in rows),
            "in_tok": mean([r["in_tok"] for r in clean]), "out_tok": mean([r["out_tok"] for r in clean]),
            "tools": mean([r["tools"] for r in clean]), "todo": mean([r["todo"] for r in clean]),
            "exception_types": sorted({r["exception"] for r in rows if r["exception"]}),
        }
        s = summary[arm]
        dtxt = "%+.1f [%+.1f, %+.1f]" % (diff[0] * 100, diff[1] * 100, diff[2] * 100) if diff else "-"
        stxt = "%d/%d/%.2f" % sign if sign else "-"
        print("%-11s %6d %6d %6d %7.1f%% %22s %12s %7.0f %7.1f %7.1f %7.1f %8.1f %9.0f" % (
            arm, s["trials"], s["infra"], s["exceptions"], solve, dtxt, stxt, s["agent_s"], s["set_state"],
            s["judge_notes"], s["blocks"], s["jev_calls"], s["in_tok"]))
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as f:
            json.dump(summary, f, indent=1)


if __name__ == "__main__":
    main()
