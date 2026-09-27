# Benchmark: does the judge help a weaker coding model?

Four arms on the same tasks, same model, same OpenCode build; they differ only in the judge:

| Arm | Agent gets |
| --- | --- |
| `off` | plain OpenCode (baseline) |
| `agent` | skill + judge, `require_set_state=agent` (the agent decides when to report) |
| `joints` | skill + judge, `require_set_state=joints` (report before completing a goal, commit, stop) |
| `every_step` | skill + judge, `require_set_state=every_step` (report before every acting step) |

Tasks: SWE-bench Verified Mini (50) and a seeded 50-task subset of Terminal-Bench 2.0, 3 attempts
each, run with [Harbor](https://www.harborframework.com). Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8`
served by vLLM.

## Architecture

```
your machine ──ssh (IAP)──▶ GCP VM "jev-bench"  (24 vCPU / 96 GB / 300 GB)
  sync_results.sh             Docker + Harbor: 20 task containers at once
  supervise.sh                ~/bench/agent-cache ──ro mount──▶ /opt/agent-cache in every container
                              budget guard (cost.py --guard, every 5 min)
                                     │ HTTPS (OpenAI-compatible, API key)
                                     ▼
                              Runpod pod: vLLM on 1x RTX PRO 6000 96GB (128K context, FP8 KV)
```

Task containers cannot run on Runpod (pods are containers themselves: no Docker or Compose inside),
so the GPU only serves the model and a plain VM runs Docker.

| File | What it does |
| --- | --- |
| `infra/gcp_vm.sh` | create / stop / start / delete the VM; prints the IAP SSH config |
| `infra/vm_bootstrap.sh` | Docker, uv, Harbor (pinned), log caps, budget guard, vLLM key |
| `infra/build_agent_cache.sh` | Node, pinned OpenCode, its config deps, ripgrep, standalone Python |
| `infra/runpod_vllm_pod.json` | the vLLM pod spec (GPU, image, vLLM flags) |
| `prefetch.sh` | builds every task image, runs each reference solution, writes task lists |
| `harbor_agent.py` | Harbor agent: OpenCode with or without the judge (`--ak jev=...`) |
| `run_arm.sh` | one arm as one Harbor job |
| `run_lockstep.sh` | all four arms per chunk of tasks, next chunk when all are done |
| `main.sh` | the full run (both benchmarks) |
| `cost.py` | running cost from a ledger + budget guard |
| `analyze.py` | per-arm solve rate, paired CIs vs baseline, judge usage, infra failures |
| `sync_results.sh`, `supervise.sh` | copy results home; watch a run and exit on done / trouble |

## The setup that works (and why)

| Setting | Value | Why |
| --- | --- | --- |
| GPU | 1x RTX PRO 6000 96GB, Runpod community (~$1.69/h) | FP8 KV cache holds ~1.2M tokens: 20 agents at 128K with ~90% prefix-cache hits. About half the cost per trial of an A100 80GB (458K-token KV, ~10 agents); H100/H200 only add per-turn speed at 1.4-1.7x the price, since the VM caps concurrency anyway |
| vLLM | v0.30.0, `--max-model-len 131072 --kv-cache-dtype fp8 --max-num-seqs 32 --enable-prefix-caching` | at 64K, ~9% of judge-arm trials hit ContextOverflowError vs ~1% baseline (the judge's MCP tools and instructions add ~1.9K tokens up front) |
| Concurrency | 5 per arm, 20 total | keep the agents' working contexts inside the KV cache. Over-subscribing thrashes the prefix cache: at 24 agents on an A100, hit rate fell from 96% to 32%, each model call took ~105 s, trials slowed 5x and hit wall-clock timeouts |
| Scheduling | lockstep chunks of 25 tasks | free-running jobs let the fast baseline finish first, after which judge arms get an idle GPU (fewer timeouts). Lockstep keeps load equal; below GPU saturation big chunks are fine and waste less time on stragglers |
| VM | GCP e2-custom-24-96GB, 300 GB pd-balanced, Debian 13 (~$0.89/h) | 20 containers plus Terminal-Bench builds; all images stay on disk |
| Harbor | 0.23.0, `--no-delete`, no automatic retries | the default `--delete` removes the task image after every trial (re-download each time); a retried agent failure would be an extra attempt |
| Agent cache | read-only mount in every arm | agent setup 2-4 s instead of ~270 s, no per-trial npm / GitHub downloads, and judge arms never get a `python3` the baseline lacks |

Verifiers still download Python packages at scoring time (SWE-bench: `uv run parser.py` pulls
swebench/datasets, ~90 MB; Terminal-Bench: the uv installer). A network error there scores 0, not an
exception. `analyze.py` counts such trials as `infra` and leaves them out; rerun them for every arm.

## Reproduce

1. **VM.** `GCP_PROJECT=... bench/infra/gcp_vm.sh create`, add the printed block to `~/.ssh/config`.
   Copy this repository to `~/jev-stepwise-judge` on the VM (e.g. `rsync -az --exclude .git ./ jev-bench:jev-stepwise-judge/`),
   then on the VM: `bash ~/jev-stepwise-judge/bench/infra/vm_bootstrap.sh`, log in again, and
   `bash ~/jev-stepwise-judge/bench/infra/build_agent_cache.sh`.
2. **Keys.** Copy your TypeSafe key to the VM's `~/.config/typesafe/key` (mode 600); it is copied only
   into judge-arm containers and never lands in Harbor's job config. The bootstrap generated the vLLM
   key in `~/.config/jev-bench/vllm_key`.
3. **Images and task lists.** On the VM, in tmux: `bash ~/jev-stepwise-judge/bench/prefetch.sh` (~45 min).
4. **GPU.** Create the pod from `infra/runpod_vllm_pod.json` (Runpod console, REST v2, or the Runpod
   MCP `create-pod` tool) with the vLLM key filled in, then on the VM `echo <POD_ID> > ~/.config/jev-bench/pod_id`.
   Wait for `curl -H "Authorization: Bearer $(cat ~/.config/jev-bench/vllm_key)" https://<POD_ID>-8000.proxy.runpod.net/v1/models`.
5. **Cost ledger.** Copy `cost-ledger.example.json` to `~/bench/cost-ledger.json` with real start
   times and your budget; `python3 bench/cost.py` prints the running total, the guard stops the run
   and powers the VM off at `stop_at`. Close an interval (`"end"`) when you stop that resource.
6. **Run.** `tmux new -d -s bench ~/jev-stepwise-judge/bench/main.sh`. Watch from home with
   `bench/supervise.sh main.done`.
7. **Analyse.** `bench/sync_results.sh && bench/analyze.py bench/results/jobs swe` (or `tb2`).
8. **Tear down.** Stop the pod (it bills until stopped or terminated) and `gcp_vm.sh stop` (disk only)
   or `delete`.

## Lessons (each cost time once)

- Measure GPU load with vLLM's `/metrics` over a window: prefix-cache hit rate, time to first token,
  queue time and preemptions. Requests "running" says little; the KV cache is the real limit.
- Stop runs with `pkill -f "[b]in/harbor run"`: a plain `-f "bin/harbor run"` also matches the shell
  running the pkill (over SSH that kills your own session, or a later run if the command lingers).
- A killed Harbor job can still leave a job-level `result.json`; `run_lockstep.sh` only trusts its own
  `.complete` marker. Trial dirs are root-owned; remove them with `sudo`.
- Runpod hosts report their CUDA version; `vllm/vllm-openai` v0.30 needs >= 12.9 (`minCudaVersion`).
- uv's `cpython-3.12-...` alias is a symlink to an absolute host path; mount the versioned directory.
- OpenCode quietly downloads ripgrep and npm-installs its plugin package in every trial; cache both.
