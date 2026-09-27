#!/bin/bash
# Run all four arms in lockstep: each chunk of tasks runs in every arm at once, and the next
# chunk starts only when all four are done. No arm ever runs under a different load than the
# others (a free-running job per arm lets the fast baseline finish early, after which the slower
# judge arms get an idle GPU and fewer wall-clock timeouts).
#
#   bench/run_lockstep.sh <swe|tb2> <tasks-file> <tag> [chunk=6] [per-arm concurrency=3] [attempts=3]
#
# Jobs are named <tag>-<arm>-a<attempt>-c<chunk>; bench/analyze.py <jobs> <tag> reads them.
# Stops between chunks if ~/bench/BUDGET_EXCEEDED or ~/bench/STOP exists; rerunning resumes
# (a chunk is skipped only when all four of its jobs exited cleanly and were marked .complete).
set -uo pipefail
bench=$1 tasks=$2 tag=$3 chunk=${4:-6} n=${5:-3} attempts=${6:-3}
here=$(cd "$(dirname "$0")" && pwd)
# a chunk size can be changed for a run that has not started yet: echo 25 > ~/bench/chunk-<tag>.override
[ -f "$HOME/bench/chunk-$tag.override" ] && chunk=$(cat "$HOME/bench/chunk-$tag.override")
cd ~/bench
mkdir -p chunks
mapfile -t all < <(grep -v '^\s*$' "$tasks")
arms=(off agent joints every_step)

for ((a = 1; a <= attempts; a++)); do
    for ((i = 0; i * chunk < ${#all[@]}; i++)); do
        [ -f BUDGET_EXCEEDED ] || [ -f STOP ] && { echo "stopping before $tag a$a c$i"; exit 1; }
        c=$(printf "%02d" "$i")
        list=chunks/$tag-c$c.txt
        printf '%s\n' "${all[@]:i*chunk:chunk}" > "$list"
        done_all=1
        for arm in "${arms[@]}"; do
            [ -f "jobs/$tag-$arm-a$a-c$c/.complete" ] || done_all=0
        done
        [ $done_all = 1 ] && continue
        echo "$(date -u +%T) $tag attempt $a chunk $c ($(wc -l < "$list") tasks)"
        pids=()
        for arm in "${arms[@]}"; do
            # trial dirs are root-owned when a run was killed, so remove leftovers with sudo
            sudo rm -rf "jobs/$tag-$arm-a$a-c$c"
            ( "$here/run_arm.sh" "$bench" "$arm" "$tag-$arm-a$a-c$c" "$list" "$n" 1 > "logs-$tag-$arm.txt" 2>&1 \
              && touch "jobs/$tag-$arm-a$a-c$c/.complete" ) &
            pids+=($!)
        done
        wait "${pids[@]}"
    done
done
touch "$tag.done"
