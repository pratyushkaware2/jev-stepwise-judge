#!/bin/bash
# One-time setup of the bench VM (Debian 13). Run on the VM as the user that runs the benchmark,
# from a checkout of this repository at ~/jev-stepwise-judge:
#
#   bash ~/jev-stepwise-judge/bench/infra/vm_bootstrap.sh
#
# Installs Docker CE (Docker's apt repo), uv and a pinned Harbor, caps container logs, and enables
# the budget guard (bench/cost.py --guard every 5 min; it may power the VM off, hence the sudoers
# rule). Afterwards: put the TypeSafe key in ~/.config/typesafe/key (mode 600), build the agent
# cache (bench/infra/build_agent_cache.sh) and write ~/bench/cost-ledger.json.
set -euo pipefail
HARBOR_VERSION=${HARBOR_VERSION:-0.23.0}
REPO=${REPO:-$HOME/jev-stepwise-judge}

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl git python3 jq tmux rsync >/dev/null

# Docker CE
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf "Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: %s\nComponents: stable\nSigned-By: /etc/apt/keyrings/docker.asc\n" \
    "$(. /etc/os-release && echo "$VERSION_CODENAME")" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
sudo usermod -aG docker "$USER"
echo '{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"3"},"builder":{"gc":{"enabled":true,"defaultKeepStorage":"20GB"}}}' \
    | sudo tee /etc/docker/daemon.json >/dev/null
sudo systemctl restart docker

# uv + Harbor
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
"$HOME/.local/bin/uv" tool install "harbor==$HARBOR_VERSION"

mkdir -p "$HOME/bench" "$HOME/.config/jev-bench" "$HOME/.config/typesafe"
chmod 700 "$HOME/.config/jev-bench" "$HOME/.config/typesafe"
[ -f "$HOME/.config/jev-bench/vllm_key" ] || {
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$HOME/.config/jev-bench/vllm_key"
    chmod 600 "$HOME/.config/jev-bench/vllm_key"
}

# Budget guard
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" | sudo tee /etc/sudoers.d/bench-poweroff >/dev/null
sudo chmod 440 /etc/sudoers.d/bench-poweroff
sudo tee /etc/systemd/system/bench-budget-guard.service >/dev/null <<EOF
[Unit]
Description=Benchmark budget guard: stop the run and power off at the budget limit
[Service]
Type=oneshot
User=$USER
ExecStart=/usr/bin/python3 $REPO/bench/cost.py --guard
TimeoutStartSec=600
EOF
sudo tee /etc/systemd/system/bench-budget-guard.timer >/dev/null <<EOF
[Unit]
Description=Check the benchmark budget every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now bench-budget-guard.timer

echo "done. Log out and back in (docker group). vLLM key: ~/.config/jev-bench/vllm_key"
