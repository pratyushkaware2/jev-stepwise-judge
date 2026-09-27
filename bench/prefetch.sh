#!/bin/bash
# Build every task image once and check every reference solution, then write the task lists.
# Run on the bench VM before the first benchmark run (~45 min; no GPU needed):
#
#   bash ~/jev-stepwise-judge/bench/prefetch.sh
#
# The oracle agent replays each task's reference solution: tasks it does not solve are broken or
# flaky here and are left out. --no-delete keeps the images, so real runs never download them.
# Writes ~/bench/swe-tasks.txt (SWE-bench Verified Mini, oracle passes) and ~/bench/tb2-tasks.txt
# (50 Terminal-Bench 2.0 tasks sampled with seed 0 from the oracle passes).
set -euo pipefail
export PATH=$HOME/.local/bin:$PATH
cd ~/bench

# SWE-bench Verified Mini (50 instances, django + sphinx), published as a HF dataset
curl -fsSL "https://datasets-server.huggingface.co/rows?dataset=MariusHobbhahn/swe-bench-verified-mini&config=default&split=test&offset=0&length=100" |
    jq -r '.rows[].row.instance_id' > swe-mini-ids.txt

swe_args=()
while read -r t; do swe_args+=(-i "swe-bench/$t"); done < swe-mini-ids.txt
harbor run -d swe-bench/swe-bench-verified -a oracle "${swe_args[@]}" -n 16 --no-delete --max-retries 2 -y -q \
    -o jobs --job-name prefetch-swe-mini > prefetch-swe-mini.log 2>&1 || true
harbor run -d terminal-bench@2.0 -a oracle -n 12 --no-delete --max-retries 2 -y -q \
    -o jobs --job-name prefetch-tb2 > prefetch-tb2.log 2>&1 || true

python3 - <<'PY'
import glob, json, random

def passes(job):
    ok = set()
    for f in glob.glob("jobs/%s/*/result.json" % job):
        d = json.load(open(f))
        r = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if r == 1.0 and not d.get("exception_info"):
            ok.add(f.split("/")[2].rsplit("__", 1)[0])
    return sorted(ok)

swe = passes("prefetch-swe-mini")
tb2 = passes("prefetch-tb2")
open("swe-tasks.txt", "w").write("\n".join(swe) + "\n")
open("tb2-oracle-pass.txt", "w").write("\n".join(tb2) + "\n")
open("tb2-tasks.txt", "w").write("\n".join(sorted(random.Random(0).sample(tb2, min(50, len(tb2))))) + "\n")
print("SWE oracle passes:", len(swe), " TB2 oracle passes:", len(tb2), "(50 sampled)")
PY
touch prefetch.done
