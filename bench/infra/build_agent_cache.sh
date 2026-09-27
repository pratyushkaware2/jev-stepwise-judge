#!/bin/bash
# Build the agent cache that bench/harbor_agent.py bind-mounts read-only at /opt/agent-cache in
# every task container (all arms alike):
#
#   node/            Node 22 (official linux-x64 build)
#   npm/bin/opencode OpenCode, pinned; OPENCODE_VERSION holds the version (pass it as --ak version=)
#   opencode-config/ prebuilt ~/.config/opencode deps (@opencode-ai/plugin), copied in per trial
#   ripgrep/rg       OpenCode would otherwise download it from GitHub in every trial
#   python -> pyinst/cpython-3.12.x-...  standalone Python for the judge, so judge arms never add a
#                    python3 to the task image that the baseline would not have
#
# Without it every trial spends ~4.5 min downloading nvm, Node and OpenCode, plus npm and GitHub
# fetches, each a failure point. Run on the bench VM:  bash bench/infra/build_agent_cache.sh
set -euo pipefail
C=${AGENT_CACHE_DIR:-$HOME/bench/agent-cache}
OPENCODE_VERSION=${OPENCODE_VERSION:-1.18.32}
NODE_MAJOR=${NODE_MAJOR:-22}
PYTHON_VERSION=${PYTHON_VERSION:-3.12}
RIPGREP_VERSION=${RIPGREP_VERSION:-15.1.0}
mkdir -p "$C"
cd "$C"

node=$(curl -fsSL https://nodejs.org/dist/index.json |
    python3 -c "import json,sys; print(next(r['version'] for r in json.load(sys.stdin) if r['version'].startswith('v$NODE_MAJOR.')))")
rm -rf node "node-$node-linux-x64"
curl -fsSL "https://nodejs.org/dist/$node/node-$node-linux-x64.tar.xz" | tar xJ
mv "node-$node-linux-x64" node
export PATH=$C/node/bin:$PATH

npm i -g --prefix "$C/npm" "opencode-ai@$OPENCODE_VERSION" >/dev/null
echo "$OPENCODE_VERSION" > OPENCODE_VERSION

rm -rf opencode-config && mkdir opencode-config
printf '{\n  "dependencies": {\n    "@opencode-ai/plugin": "%s"\n  }\n}\n' "$OPENCODE_VERSION" > opencode-config/package.json
(cd opencode-config && npm install --no-audit --no-fund >/dev/null)

mkdir -p ripgrep
curl -fsSL "https://github.com/BurntSushi/ripgrep/releases/download/$RIPGREP_VERSION/ripgrep-$RIPGREP_VERSION-x86_64-unknown-linux-musl.tar.gz" |
    tar xz -C ripgrep --strip-components=1

rm -rf pyinst python
UV_PYTHON_INSTALL_DIR=$C/pyinst "$HOME/.local/bin/uv" python install "$PYTHON_VERSION" >/dev/null
# link the concrete versioned dir: uv's minor-version alias is a symlink to an absolute host path,
# which does not exist inside the containers
ln -s "$(find pyinst -maxdepth 1 -type d -name "cpython-$PYTHON_VERSION.*" | head -1)" python

node/bin/node --version; npm/bin/opencode --version; ripgrep/rg --version | head -1; python/bin/python3 --version
du -sh "$C"
