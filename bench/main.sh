#!/bin/bash
# The main run: SWE-bench Verified Mini, then the Terminal-Bench 2.0 subset, 4 arms x 3 attempts,
# arms in lockstep chunks of 25 tasks, 5 trials per arm at once (20 total), 128K context.
# Run on the bench VM inside tmux so it survives disconnects:
#
#   tmux new -d -s bench ~/jev-stepwise-judge/bench/main.sh
#
# Rerunning resumes: chunks already marked .complete are skipped. Stop between chunks with
# `touch ~/bench/STOP`. BENCH_CONTEXT must match the pod's vLLM --max-model-len.
cd ~/bench
export BENCH_CONTEXT=${BENCH_CONTEXT:-131072}
here=$(cd "$(dirname "$0")" && pwd)
"$here/run_lockstep.sh" swe swe-tasks.txt swe 25 5 3 >> main.log 2>&1 || exit 1
"$here/run_lockstep.sh" tb2 tb2-tasks.txt tb2 25 5 3 >> main.log 2>&1 || exit 1
touch main.done
