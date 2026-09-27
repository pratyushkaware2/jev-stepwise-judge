#!/usr/bin/env python3
"""Running cost of the benchmark infrastructure, and a budget guard.

Costs come from a ledger of billable intervals at list prices (GCP and Runpod bill per
second), plus the bench VM's internet egress read from its network counters:

    ~/bench/cost-ledger.json
    {"budget": 100, "stop_at": 95,
     "fixed": [{"what": "...", "usd": 0.05}],
     "intervals": [{"what": "jev-bench compute", "rate": 0.844, "start": "2026-09-27T07:33:00Z", "end": null}]}

An interval with "end": null is still running. Close it (set "end") when the resource stops.

    cost.py            print the breakdown
    cost.py --json     same, as JSON
    cost.py --guard    also: at or above stop_at, write ~/bench/BUDGET_EXCEEDED, interrupt the
                       Harbor jobs so finished trials stay on disk, and power the VM off
                       (a stopped GCE VM bills only its disk). Run from a systemd timer.
"""
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
LEDGER = os.path.join(HOME, "bench", "cost-ledger.json")
EGRESS_STATE = os.path.join(HOME, "bench", "egress-state.json")
FLAG = os.path.join(HOME, "bench", "BUDGET_EXCEEDED")
NIC = "ens4"
EGRESS_USD_PER_GB = 0.12  # GCP premium tier, North America internet egress


def _t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def egress_gb():
    """Cumulative tx bytes on the NIC, carried across reboots (counters reset at boot)."""
    try:
        with open("/sys/class/net/%s/statistics/tx_bytes" % NIC) as f:
            now = int(f.read())
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
    except OSError:
        return 0.0
    try:
        with open(EGRESS_STATE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"boot": boot, "base": 0, "last": 0}
    if st.get("boot") != boot:
        st = {"boot": boot, "base": st.get("base", 0) + st.get("last", 0), "last": 0}
    st["last"] = now
    with open(EGRESS_STATE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(EGRESS_STATE + ".tmp", EGRESS_STATE)
    return (st["base"] + now) / 1e9


def compute():
    with open(LEDGER) as f:
        led = json.load(f)
    now = dt.datetime.now(dt.timezone.utc)
    rows = [(x["what"], float(x["usd"])) for x in led.get("fixed", [])]
    for iv in led.get("intervals", []):
        end = _t(iv["end"]) if iv.get("end") else now
        hours = max(0.0, (end - _t(iv["start"])).total_seconds() / 3600)
        rows.append(("%s (%.2f h @ $%.3f/h%s)" % (iv["what"], hours, iv["rate"], "" if iv.get("end") else ", running"),
                     hours * iv["rate"]))
    gb = egress_gb()
    rows.append(("jev-bench internet egress (%.2f GB @ $%.2f/GB)" % (gb, EGRESS_USD_PER_GB), gb * EGRESS_USD_PER_GB))
    total = sum(u for _, u in rows)
    return {"at": now.isoformat(timespec="seconds"), "rows": rows, "total": total,
            "budget": led.get("budget", 100), "stop_at": led.get("stop_at", 95)}


def guard(c):
    if c["total"] < c["stop_at"] or os.path.exists(FLAG):
        return
    with open(FLAG, "w") as f:
        f.write("%s total $%.2f >= stop_at $%.2f\n" % (c["at"], c["total"], c["stop_at"]))
    out = subprocess.run(["pgrep", "-f", "bin/harbor run"], capture_output=True, text=True).stdout.split()
    for pid in out:
        try:
            os.kill(int(pid), signal.SIGINT)
        except (OSError, ValueError):
            pass
    time.sleep(180)  # let Harbor write results for the trials it is closing
    subprocess.run(["sync"])
    subprocess.run(["sudo", "systemctl", "poweroff"])


def main():
    c = compute()
    if "--json" in sys.argv:
        print(json.dumps(c, indent=1))
    else:
        for what, usd in c["rows"]:
            print("%8.2f  %s" % (usd, what))
        print("%8.2f  TOTAL (budget $%.0f, guard stops at $%.0f)" % (c["total"], c["budget"], c["stop_at"]))
    if "--guard" in sys.argv:
        guard(c)


if __name__ == "__main__":
    main()
