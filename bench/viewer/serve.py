#!/usr/bin/env python3
"""Local viewer for benchmark results: trial list, rollout traces and the judge's side of them.

    python3 bench/viewer/serve.py [results-dir] [--port 8765]

Opens nothing on the network: binds 127.0.0.1 and reads only files under the results directory
(the folder bench/sync_results.sh fills: <dir>/jobs/<run>-<arm>[-a<n>-c<n>]/<trial>/...).
Another results folder can be loaded from the page. Standard library only.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import analyze  # noqa: E402

RUN_NOTES = {
    "swe": "128K context, testbed env active (BASH_ENV), RTX PRO 6000, lockstep chunks of 25, 5 per arm",
    "envbug-swe": "128K context. Env bug: agent shells ran in conda base, not the testbed env",
    "envbug-swe64k": "64K context, A100. Env bug: agent shells ran in conda base, not the testbed env",
    "envbug-overload-swe": "64K, A100, GPU overloaded. Env bug: agent shells ran in conda base",
    "envbug-smoke-swe": "64K smoke test, 4 tasks. Env bug: agent shells ran in conda base",
}
DIR = re.compile(r"^(?P<run>.+?)-(?P<arm>off|agent|joints|every_step)(?:-a(?P<attempt>\d+)-c(?P<chunk>\d+))?$")
MARK = "[jev-stepwise-judge]"
MAX_TEXT = 60000


def _ms(iso):
    if not iso:
        return None
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp() * 1000)


def _json(path, default=None):
    try:
        with open(path, errors="replace") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _jsonl(path):
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _cap(s):
    s = s if isinstance(s, str) else json.dumps(s, indent=1)
    return (s[:MAX_TEXT] + "\n… [%d more characters]" % (len(s) - MAX_TEXT)) if len(s) > MAX_TEXT else s


class Results:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.jobs = os.path.join(self.root, "jobs") if os.path.isdir(os.path.join(self.root, "jobs")) else self.root
        self.index = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ index
    def build_index(self):
        cache_path = os.path.join(self.root, ".viewer-index.json")
        cache = _json(cache_path, {}) or {}
        trials, fresh = [], {}
        for jdir in sorted(glob.glob(os.path.join(self.jobs, "*"))):
            m = DIR.match(os.path.basename(jdir))
            if not m:
                continue
            for rj in sorted(glob.glob(os.path.join(jdir, "*", "result.json"))):
                tdir = os.path.dirname(rj)
                rel = os.path.relpath(tdir, self.root)
                stamp = "%d" % os.path.getmtime(rj)
                hit = cache.get(rel)
                if hit and hit.get("_stamp") == stamp:
                    row = hit
                else:
                    try:
                        t = analyze.trial(tdir)
                    except (OSError, ValueError, KeyError):
                        continue
                    oc = analyze._read(os.path.join(tdir, "agent", "opencode.txt"))
                    row = {
                        "id": rel, "run": m.group("run"), "arm": m.group("arm"),
                        "attempt": int(m.group("attempt") or 0), "task": t["task"],
                        "reward": t["reward"], "exception": t["exception"], "infra": t["infra"],
                        "overflow": "ContextOverflowError" in oc, "agent_s": round(t["agent_s"] or 0),
                        "tools": t["tools"], "set_state": t["set_state"], "blocks": t["blocks"],
                        "jev_calls": t["jev_calls"], "in_tok": t["in_tok"], "_stamp": stamp,
                    }
                trials.append(row)
                fresh[row["id"]] = row
        try:
            with open(cache_path, "w") as f:
                json.dump(fresh, f)
        except OSError:
            pass
        labels = {}
        for path in sorted(glob.glob(os.path.join(self.root, "failure_labels*.jsonl"))):
            for rec in _jsonl(path):
                labels[rec.get("trial_dir")] = rec
        for row in trials:
            if row["id"] in labels:
                row["label"] = labels[row["id"]]
        runs = {}
        for run in sorted({r["run"] for r in trials}):
            summ = _json(os.path.join(self.root, "summary-%s.json" % run))
            if summ is None:
                data = analyze.load(self.jobs, run)
                summ = self._summary(data)
            runs[run] = {"note": RUN_NOTES.get(run, ""), "summary": summ}
        self.index = {"root": self.root, "runs": runs, "trials": trials}
        return self.index

    @staticmethod
    def _summary(data):
        base = analyze.per_task(data.get("off", []))
        out = {}
        for arm, rows in data.items():
            clean = [r for r in rows if not r["infra"]]
            pt = analyze.per_task(rows)
            d = analyze.bootstrap_diff(base, pt, n=2000) if arm != "off" and base else None
            out[arm] = {"trials": len(rows), "solve_rate": analyze.mean([r["reward"] for r in clean]),
                        "diff_vs_off": d and {"mean": d[0], "ci95": [d[1], d[2]]}}
        return out

    # ------------------------------------------------------------------ trace
    def trial_path(self, rel):
        p = os.path.abspath(os.path.join(self.root, rel))
        if not p.startswith(self.root + os.sep) or not os.path.isfile(os.path.join(p, "result.json")):
            raise ValueError("unknown trial")
        return p

    def trace(self, rel):
        t = self.trial_path(rel)
        res = _json(os.path.join(t, "result.json"), {})
        ev = []

        phases = []
        for key, name in (("environment_setup", "Environment setup"), ("agent_setup", "Agent setup"),
                          ("agent_execution", "Agent run"), ("verifier", "Tests")):
            p = res.get(key) or {}
            if p.get("started_at"):
                phases.append({"name": name, "start": _ms(p["started_at"]), "end": _ms(p.get("finished_at"))})

        stream = _jsonl(os.path.join(t, "agent", "opencode.txt"))
        traj = _json(os.path.join(t, "agent", "trajectory.json"), {}) or {}
        first_ts = min([e.get("timestamp") for e in stream if e.get("timestamp")] or [0]) or None
        agent_start = next((p["start"] for p in phases if p["name"] == "Agent run"), None)
        prompt = next((s.get("message") for s in traj.get("steps", []) if s.get("source") == "user"), None)
        if prompt:
            ev.append({"t": (first_ts or agent_start or 0) - 1, "kind": "user", "text": _cap(prompt)})

        finish_by_msg = {}
        for e in stream:
            if e.get("type") == "step_finish":
                p = e.get("part") or {}
                tok = p.get("tokens") or {}
                finish_by_msg[p.get("messageID")] = {
                    "context": (tok.get("input") or 0) + ((tok.get("cache") or {}).get("read") or 0),
                    "output": tok.get("output") or 0, "reason": p.get("reason")}
        first_in_msg = {}  # a turn starts at its message's earliest part, not at the step_start record
        for e in stream:
            p = e.get("part") or {}
            tm = (p.get("state") or {}).get("time") or p.get("time") or {}
            t0 = tm.get("start") or e.get("timestamp")
            mid = p.get("messageID")
            if mid and t0:
                first_in_msg[mid] = min(first_in_msg.get(mid, t0), t0)
        turn = 0
        for e in stream:
            typ, p, ts = e.get("type"), e.get("part") or {}, e.get("timestamp")
            if typ == "step_start":
                turn += 1
                ev.append({"t": min(ts, first_in_msg.get(p.get("messageID"), ts)), "kind": "turn", "n": turn,
                           **finish_by_msg.get(p.get("messageID"), {})})
            elif typ in ("text", "reasoning"):
                txt = p.get("text") or ""
                if txt.strip():
                    ev.append({"t": (p.get("time") or {}).get("start") or ts,
                               "kind": "text" if typ == "text" else "thinking", "text": _cap(txt)})
            elif typ == "tool_use":
                st = p.get("state") or {}
                tm = st.get("time") or {}
                tool = p.get("tool", "")
                item = {"t": tm.get("start") or ts, "end": tm.get("end"), "kind": "tool", "tool": tool,
                        "status": st.get("status"), "title": st.get("title") or "", "input": st.get("input"),
                        "call": p.get("callID")}
                out = st.get("output")
                err = st.get("error")
                if tool.endswith("set_state") and "jev-stepwise-judge" in tool:
                    item["kind"] = "judge_report"
                    try:
                        item["reply"] = json.loads(out) if isinstance(out, str) else out
                    except ValueError:
                        item["reply_text"] = _cap(out or err or "")
                elif tool.startswith("jev-stepwise-judge"):
                    item["kind"] = "judge_tool"
                    item["output"] = _cap(out if out is not None else err or "")
                else:
                    body = out if out is not None else ""
                    if err and MARK in str(err):
                        item["blocked"] = str(err).split(MARK, 1)[1].strip()
                    elif err:
                        item["error"] = _cap(str(err))
                    if isinstance(body, str) and MARK in body:
                        head, note = body.split(MARK, 1)
                        item["judge_note"] = note.strip()
                        body = head.rstrip()
                    item["output"] = _cap(body)
                    diff = (st.get("metadata") or {}).get("diff")
                    if diff:
                        item["diff"] = _cap(diff)
                ev.append(item)
            elif typ == "error":
                er = e.get("error") or {}
                ev.append({"t": ts, "kind": "error", "name": er.get("name", "Error"),
                           "text": _cap((er.get("data") or {}).get("message") or json.dumps(er))})

        # judge log: latency for reports; anything else becomes its own event
        judg = _jsonl(os.path.join(t, "agent", "jev-stepwise-judge", "judgments.jsonl"))
        reports = [e for e in ev if e["kind"] == "judge_report"]
        ri = 0
        for j in judg:
            if j.get("event") == "set_state" and ri < len(reports):
                reports[ri]["ms"] = j.get("ms")
                reports[ri]["log"] = j
                ri += 1
            elif j.get("event") == "PreToolUse" and j.get("blocked"):
                continue  # already shown on the blocked tool call
            else:
                ev.append({"t": _ms(j.get("ts")), "kind": "judge_event", "log": j})

        exc = res.get("exception_info")
        if exc:
            ev.append({"t": _ms(exc.get("occurred_at")) or (phases[-1]["end"] if phases else 0),
                       "kind": "exception", "name": exc.get("exception_type"),
                       "text": _cap(exc.get("exception_message", ""))})

        ver = {"reward": ((res.get("verifier_result") or {}).get("rewards") or {}).get("reward")}
        rep = _json(os.path.join(t, "verifier", "report.json"), {}) or {}
        inst = next(iter(rep.values()), {}) if rep else {}
        ts_ = inst.get("tests_status") or {}
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS"):
            if k in ts_:
                ver[k] = {"passed": ts_[k].get("success", []), "failed": ts_[k].get("failure", [])}
        ver["patch_applied"] = inst.get("patch_successfully_applied")
        stdout = analyze._read(os.path.join(t, "verifier", "test-stdout.txt"))
        ver["stdout_tail"] = "\n".join(stdout.splitlines()[-80:])
        vp = next((p for p in phases if p["name"] == "Tests"), None)
        ev.append({"t": (vp or {}).get("start") or (ev[-1]["t"] if ev else 0), "kind": "verifier", **ver})

        ev = [e for e in ev if e.get("t") is not None]
        others = [e["t"] for e in ev if e["kind"] != "user"]
        for e in ev:  # the task comes first, whatever the clocks say
            if e["kind"] == "user" and others:
                e["t"] = min(others) - 1
        ev.sort(key=lambda e: (e["t"], 0 if e["kind"] == "turn" else 1))
        origin = min([p["start"] for p in phases if p["name"] == "Agent run"] or [ev[0]["t"] if ev else 0])
        m = DIR.match(os.path.basename(os.path.dirname(t)))
        return {"id": rel, "task": os.path.basename(t).rsplit("__", 1)[0], "run": m and m.group("run"),
                "arm": m and m.group("arm"), "attempt": m and m.group("attempt"), "origin": origin,
                "phases": phases, "events": ev,
                "tokens": {"in": (res.get("agent_result") or {}).get("n_input_tokens"),
                           "out": (res.get("agent_result") or {}).get("n_output_tokens")},
                "label": next((r for path in sorted(glob.glob(os.path.join(self.root, "failure_labels*.jsonl")))
                               for r in _jsonl(path) if r.get("trial_dir") == rel), None)}


STATE = {"results": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        r = STATE["results"]
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/index":
                with r.lock:
                    return self._send(200, r.index or r.build_index())
            if u.path == "/api/trace":
                return self._send(200, r.trace(parse_qs(u.query).get("id", [""])[0]))
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/load":
            path = os.path.expanduser(body.get("path", ""))
            if not os.path.isdir(path):
                return self._send(400, {"error": "No folder at %s" % path})
            res = Results(path)
            with res.lock:
                idx = res.build_index()
            if not idx["trials"]:
                return self._send(400, {"error": "No trials found in %s (expected jobs/<run>-<arm>/...)" % path})
            STATE["results"] = res
            return self._send(200, idx)
        if u.path == "/api/refresh":
            r = STATE["results"]
            with r.lock:
                return self._send(200, r.build_index())
        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="?", default=os.path.join(os.path.dirname(HERE), "results"))
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    STATE["results"] = Results(a.results)
    print("indexing %s ..." % STATE["results"].root, flush=True)
    n = len(STATE["results"].build_index()["trials"])
    print("%d trials. Open http://127.0.0.1:%d" % (n, a.port), flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
