#!/bin/bash
# Watch a run from your own machine: every 15 min sync results and log cost and vLLM health.
# Exits (so a waiting agent or shell wakes up) when <done-file> appears on the VM, the budget guard
# fires, the vLLM endpoint fails two checks in a row, or the VM is unreachable three times.
#
#   bench/supervise.sh main.done
set -uo pipefail
host=${BENCH_HOST:-jev-bench}
here=$(cd "$(dirname "$0")" && pwd)
target=${1:?usage: supervise.sh <done-file under ~/bench>}
unreach=0 vdown=0
while true; do
    st=$(ssh -o ConnectTimeout=60 "$host" "cd ~/bench; [ -f BUDGET_EXCEEDED ] && echo BUDGET; [ -f $target ] && echo DONE; \
        K=\$(cat ~/.config/jev-bench/vllm_key); \
        curl -s -m 20 -o /dev/null -w 'vllm=%{http_code}' -H \"Authorization: Bearer \$K\" \
            https://\$(cat ~/.config/jev-bench/pod_id)-8000.proxy.runpod.net/v1/models; echo; \
        python3 ~/jev-stepwise-judge/bench/cost.py | tail -1" 2>/dev/null)
    if [ -z "$st" ]; then unreach=$((unreach + 1)); else unreach=0; fi
    echo "$(date -u +%T) $(echo "$st" | tr '\n' ' ')"
    case "$st" in
        *BUDGET*) "$here/sync_results.sh"; echo "EXIT budget"; exit 0 ;;
        *DONE*) "$here/sync_results.sh"; echo "EXIT done"; exit 0 ;;
    esac
    case "$st" in *vllm=200*) vdown=0 ;; "") ;; *) vdown=$((vdown + 1)) ;; esac
    [ $vdown -ge 2 ] && { "$here/sync_results.sh"; echo "EXIT vllm-down"; exit 0; }
    [ $unreach -ge 3 ] && { echo "EXIT vm-unreachable"; exit 0; }
    "$here/sync_results.sh" >/dev/null 2>&1
    sleep 900
done
