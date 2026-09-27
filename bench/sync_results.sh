#!/bin/bash
# Copy Harbor results and the cost ledger from the bench VM to bench/results/ (gitignored).
# Run from your own machine:  bench/sync_results.sh
# Trial dirs hold root-owned files from the containers, hence `sudo rsync` on the VM side.
# OpenCode's git snapshots are skipped (large, not needed for analysis).
set -euo pipefail
host=${BENCH_HOST:-jev-bench}
dest=${BENCH_RESULTS:-$(cd "$(dirname "$0")" && pwd)/results}
mkdir -p "$dest"
rsync -az --rsync-path="sudo rsync" --exclude 'xdg-data/opencode/snapshot' -e "ssh -o ConnectTimeout=60" \
    "$host:bench/jobs/" "$dest/jobs/"
rsync -az -e "ssh -o ConnectTimeout=60" "$host:bench/cost-ledger.json" "$host:bench/*-tasks.txt" "$dest/" 2>/dev/null || true
ssh -o ConnectTimeout=60 "$host" 'python3 ~/jev-stepwise-judge/bench/cost.py' > "$dest/cost-latest.txt" 2>/dev/null || true
date -u +%FT%TZ > "$dest/last-sync.txt"
