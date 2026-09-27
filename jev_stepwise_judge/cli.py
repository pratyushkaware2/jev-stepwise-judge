"""Command line: jev-stepwise-judge <command>

  hook                      hook entry point (stdin: hook JSON); used by agent configs
  mcp                       MCP server over stdio; used by agent configs
  direction [--session ID]  ask Jev which way to go from a tracked session
  state [--session ID]      print a tracked session's state (local)
  check STEP [--session ID] judge a step you are considering, e.g. check "git push"
  next --task T [--context C] OPT OPT...
                            rank candidate next steps
  install [AGENT...] [--dry-run]   hooks + MCP + skill for claude codex grok opencode
  uninstall [AGENT...]      remove exactly what install added
  stats                     summarise the judgment log
  doctor                    check key, API, config and an offline hook round-trip
"""
import argparse
import json
import os
import sys
from collections import Counter

from . import __version__, config, hooks, install, jev, judge, mcp_server, state as S, steps


def _session(args):
    st = S.find_session(None, os.getcwd(), getattr(args, "session", None))
    if st is None:
        sys.exit("no tracked session for %s (hooks installed? any tool calls yet?)" % os.getcwd())
    return st


def cmd_direction(args):
    d = judge.get_direction(_session(args), config.load())
    print(judge.direction_text(d))
    print(json.dumps({k: d[k] for k in ("alternatives", "current_goal", "current_goal_done", "goals_done", "ms")}))


def cmd_state(args):
    st = _session(args)
    print(json.dumps({"session": st["session"], "agent": st["agent"], "task": st["task"][:300],
                      "facts": S.facts(st), "todos": st["goal"]["todos"],
                      "tests": st["knowledge"]["tests"], "build": st["knowledge"]["build"],
                      "processes": st["knowledge"]["processes"]}, indent=1, default=str))


def cmd_check(args):
    server = mcp_server.Server()
    server.pids = []
    print(json.dumps(server.judge_step({"description": args.step, "session": args.session}), indent=1, default=str))


def cmd_next(args):
    r = judge.choose(args.task, args.context, args.options, config.load())
    print("best: %s (confidence %.2f)" % (r["best"], r["confidence"] or 0))
    for name, p in r["ranking"]:
        print("  %.2f  %s" % (p, name[:100]))


def cmd_stats(_args):
    path = os.path.join(config.state_dir(), "judgments.jsonl")
    sev, dirs, errs, ms, n = Counter(), Counter(), Counter(), [], 0
    try:
        with open(path) as f:
            for line in f:
                e = json.loads(line)
                n += 1
                if "error" in e:
                    errs[e["error"]] += 1
                if e.get("severity"):
                    sev[e["severity"]] += 1
                if e.get("direction") or e.get("pushed"):
                    dirs[e.get("direction") or e.get("pushed")] += 1
                if "ms" in e:
                    ms.append(e["ms"])
    except OSError:
        print("no log yet:", path)
        return
    ms.sort()
    print("entries %d | verdicts %s" % (n, dict(sev)))
    print("directions %s" % dict(dirs))
    print("errors %s" % (dict(errs) or "none"))
    if ms:
        print("jev latency ms p50 %d p90 %d" % (ms[len(ms) // 2], ms[int(len(ms) * 0.9)]))


def cmd_doctor(_args):
    cfg = config.load()
    print("version %s | mode %s | model %s | config %s" % (__version__, cfg["mode"], cfg["model"], config.config_path()))
    print("key:", "found" if jev.api_key() else "MISSING (set TYPESAFE_API_KEY)")
    try:
        r = jev.ask("A test run just passed.", {"ok": {"type": "noul", "instructions": "Did the test run pass?"}},
                    cfg["model"], cfg["timeout_s"])
        print("api: ok (%s, ok=%.2f)" % (r.get("model"), r["answers"]["ok"]["noul"]))
    except jev.JevUnavailable as e:
        print("api: FAILED (%s)" % e)
    st = S.new_state("doctor", "doctor", "/tmp")
    S.on_prompt(st, "Fix the failing parser test.")
    rec = S.on_pre(st, "Bash", {"command": "python3 -m pytest -q"}, "t1")
    S.on_post(st, "Bash", "t1", "PostToolUse", {"stdout": "1 failed, 3 passed", "exit_code": 1})
    f = S.facts(st)
    print("offline state: step kind=%s, tests=%s, last_verification_failed=%s"
          % (rec["kind"], st["knowledge"]["tests"]["status"], f["last_verification_failed"]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hook":
        return hooks.main(argv[1:])
    if argv and argv[0] == "mcp":
        return mcp_server.main(argv[1:])
    ap = argparse.ArgumentParser(prog="jev-stepwise-judge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("direction", "state"):
        p = sub.add_parser(name)
        p.add_argument("--session")
    p = sub.add_parser("check")
    p.add_argument("step")
    p.add_argument("--session")
    p = sub.add_parser("next")
    p.add_argument("--task", required=True)
    p.add_argument("--context", default="")
    p.add_argument("options", nargs="+")
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("agents", nargs="*", help="claude codex grok opencode (default: all)")
        p.add_argument("--dry-run", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("doctor")
    args = ap.parse_args(argv)
    if args.cmd in ("install", "uninstall"):
        return install.run(args.agents, remove=args.cmd == "uninstall", dry=args.dry_run)
    fn = {"direction": cmd_direction, "state": cmd_state, "check": cmd_check, "next": cmd_next,
          "stats": cmd_stats, "doctor": cmd_doctor}.get(args.cmd)
    if fn is None:
        ap.print_help()
        return 64
    try:
        fn(args)
    except jev.JevUnavailable as e:
        sys.exit("Jev unavailable: %s" % e)
    return 0
