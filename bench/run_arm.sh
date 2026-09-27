#!/bin/bash
# Run one benchmark arm as a Harbor job on the bench VM.
#
#   bench/run_arm.sh <swe|tb2> <off|agent|joints|every_step> <job-name> <tasks-file> [n_concurrent] [attempts]
#
# Arms differ only in --ak jev=...; everything else (OpenCode version, model, mounts,
# timeouts) is identical. Run the four arms side by side so GPU load and network
# conditions hit every arm alike. Expects on the VM:
#   ~/bench/agent-cache            Node + OpenCode + Python (see bench/harbor_agent.py)
#   ~/.config/jev-bench/vllm_key   vLLM API key;  ~/.config/jev-bench/pod_id  Runpod pod id
#   ~/.config/typesafe/key         TypeSafe key (copied into judge-arm containers only)
# No automatic retries: a retried agent failure would be an extra attempt. Infrastructure
# failures are classified and rerun afterwards, identically for every arm.
set -euo pipefail
bench=$1 arm=$2 job=$3 tasks=$4 n=${5:-5} k=${6:-1}

export PATH=$HOME/.local/bin:$PATH
export PYTHONPATH=$HOME/jev-stepwise-judge
cd ~/bench

key=$(cat ~/.config/jev-bench/vllm_key)
pod=$(cat ~/.config/jev-bench/pod_id)
version=$(cat agent-cache/OPENCODE_VERSION)
context=${BENCH_CONTEXT:-131072}  # must match vLLM --max-model-len on the pod
mounts='[{"type":"bind","source":"'"$HOME"'/bench/agent-cache","target":"/opt/agent-cache","read_only":true}]'
config='{"provider":{"vllm":{"npm":"@ai-sdk/openai-compatible","name":"vLLM","options":{"baseURL":"https://'"$pod"'-8000.proxy.runpod.net/v1","apiKey":"{env:VLLM_API_KEY}"},"models":{"qwen3-coder":{"name":"Qwen3-Coder-30B-A3B-Instruct-FP8","tool_call":true,"limit":{"context":'"$context"',"output":8192}}}}}}'

case $bench in
    swe) dataset=swe-bench/swe-bench-verified; prefix=swe-bench/ ;;
    tb2) dataset=terminal-bench@2.0; prefix= ;;
    *) echo "unknown benchmark: $bench" >&2; exit 2 ;;
esac
include=()
while read -r t; do [ -n "$t" ] && include+=(-i "$prefix$t"); done < "$tasks"

exec harbor run -d "$dataset" "${include[@]}" \
    -a bench.harbor_agent:JevOpenCode --ak "jev=$arm" --ak "version=$version" --ak "opencode_config=$config" \
    -m vllm/qwen3-coder --ae "VLLM_API_KEY=$key" --mounts "$mounts" \
    -n "$n" -k "$k" --no-delete --max-retries 0 -y -q -o jobs --job-name "$job"
