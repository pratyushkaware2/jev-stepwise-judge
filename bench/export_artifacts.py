#!/usr/bin/env python3
"""Index synced benchmark results for analysis of the judge.

    bench/export_artifacts.py [results-dir]      (default: bench/results)

Reads <results>/jobs/ (as copied by sync_results.sh) and writes, next to it:

    trials.csv            one row per trial: run, arm, attempt, task, reward, exception, judge usage, tokens
    judge_events.jsonl    every judge log event (judgments.jsonl) with its trial's run/arm/task/reward
    summary-<run>.txt     bench/analyze.py output per run
    INDEX.md              what is where
"""
import csv
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402

RUNS = {  # job-dir prefix -> description
    "swe": "128K context, testbed env active (BASH_ENV), RTX PRO 6000, lockstep chunks of 25, 5/arm (main run)",
    "envbug-swe": "128K context; env bug: agent shells ran in conda base, not the testbed env",
    "envbug-swe64k": "64K context, A100; env bug",
    "envbug-overload-swe": "64K, A100, GPU overloaded; env bug",
    "envbug-smoke-swe": "64K smoke test, 4 tasks; env bug",
}
DIR = re.compile(r"^(?P<run>.+?)-(?P<arm>off|agent|joints|every_step)(?:-a(?P<attempt>\d+)-c(?P<chunk>\d+))?$")


def _jsonl(path):
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def main():
    res = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
    jobs = os.path.join(res, "jobs")
    rows, events = [], []
    for jdir in sorted(glob.glob(os.path.join(jobs, "*"))):
        m = DIR.match(os.path.basename(jdir))
        if not m or m.group("run") not in RUNS:
            continue
        for rj in sorted(glob.glob(os.path.join(jdir, "*", "result.json"))):
            tdir = os.path.dirname(rj)
            try:
                t = analyze.trial(tdir)
            except (OSError, ValueError, KeyError):
                continue
            oc = analyze._read(os.path.join(tdir, "agent", "opencode.txt"))
            jl = _jsonl(os.path.join(tdir, "agent", "jev-stepwise-judge", "judgments.jsonl"))
            reports = [e for e in jl if e.get("event") == "set_state"]
            row = {
                "run": m.group("run"), "arm": m.group("arm"), "attempt": m.group("attempt") or "",
                "chunk": m.group("chunk") or "", "task": t["task"], "reward": t["reward"],
                "exception": t["exception"] or "", "infra": int(t["infra"]),
                "context_overflow": int("ContextOverflowError" in oc),
                "agent_s": round(t["agent_s"] or 0), "tool_calls": t["tools"], "todo_writes": t["todo"],
                "set_state_calls": t["set_state"], "blocked_steps": t["blocks"], "judge_notes": t["judge_notes"],
                "jev_calls": t["jev_calls"],
                "verdict_go": sum(1 for e in reports if e.get("verdict") == "go ahead"),
                "verdict_reconsider": sum(1 for e in reports if e.get("verdict") == "reconsider"),
                "reports_with_mismatch": sum(1 for e in reports if e.get("mismatches")),
                "in_tok": t["in_tok"], "out_tok": t["out_tok"],
                "trial_dir": os.path.relpath(tdir, res),
            }
            rows.append(row)
            for e in jl:
                events.append(dict(e, run=row["run"], arm=row["arm"], attempt=row["attempt"], task=row["task"],
                                   trial_reward=row["reward"], trial_dir=row["trial_dir"]))
    if not rows:
        sys.exit("no trials found under %s" % jobs)
    with open(os.path.join(res, "trials.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(res, "judge_events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    for run in RUNS:
        if any(r["run"] == run for r in rows):
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sys.argv = ["analyze.py", jobs, run, "--json", os.path.join(res, "summary-%s.json" % run)]
                analyze.main()
            with open(os.path.join(res, "summary-%s.txt" % run), "w") as f:
                f.write(RUNS[run] + "\n" + buf.getvalue())
    counts = {}
    for r in rows:
        counts.setdefault(r["run"], {}).setdefault(r["arm"], 0)
        counts[r["run"]][r["arm"]] += 1
    with open(os.path.join(res, "INDEX.md"), "w") as f:
        f.write("# Benchmark artifacts\n\n| run | what | trials per arm (off/agent/joints/every_step) |\n| --- | --- | --- |\n")
        for run, desc in RUNS.items():
            c = counts.get(run, {})
            f.write("| `%s` | %s | %s |\n" % (run, desc, " / ".join(str(c.get(a, 0)) for a in analyze.ARMS)))
        f.write(INDEX_BODY)
    print("%d trials, %d judge events -> %s" % (len(rows), len(events), res))


INDEX_BODY = """
## Files here

- `trials.csv`: one row per trial (reward, exception, context overflow, judge usage, tokens, `trial_dir`).
- `judge_events.jsonl`: every judge event across all trials, tagged with run/arm/task/trial reward.
  `event=PreToolUse` with `blocked=true` is a step refused for a missing report (`required=set_state`);
  `event=set_state` is a judged report: `direction`, `verdict` (go ahead / reconsider), `mismatches`, `ms`.
- `summary-<run>.txt/.json`: per-arm solve rate, paired bootstrap CI vs baseline, sign test, judge usage.
- `cost-latest.txt`, `cost-ledger.json`: infrastructure cost.
- `swe-tasks.txt`, `tb2-tasks.txt`: task lists (TB2 was prepared but not run).

## Inside a trial directory (`jobs/<run>-<arm>[-a<attempt>-c<chunk>]/<task>__<id>/`)

- `result.json`: Harbor's record (timings, reward, exception, token counts).
- `agent/opencode.txt`: OpenCode's JSON event stream (every model turn, tool call and output; judge
  notes and blocks appear inside tool outputs/errors as `[jev-stepwise-judge] ...`).
- `agent/trajectory.json`: the same run as an ATIF trajectory.
- `agent/jev-stepwise-judge/judgments.jsonl`: the judge's log for this trial (judge arms only).
- `agent/jev-stepwise-judge/sessions/*.json`: the judge's recorded agent state at the end (knowledge,
  workspace, goals, last report, steps).
- `agent/opencode/xdg-data/opencode/log/`: OpenCode's own log.
- `verifier/test-stdout.txt`, `verifier/report.json`, `verifier/reward.txt`: the test run and verdict.

Runs prefixed `envbug-` ran with the agent's shell outside SWE-bench's `testbed` conda env (OpenCode's
non-interactive bash never read ~/.bashrc), so ~1/3 of trials hit ModuleNotFoundError; `swe` sets
BASH_ENV=$HOME/.bashrc as mini-SWE-agent does. Judge version: `swe`, `envbug-swe`, `envbug-swe64k` used
commit 36e439b; `envbug-overload-swe` and `envbug-smoke-swe` used 0.2.0 (be138cf).
"""

if __name__ == "__main__":
    main()
