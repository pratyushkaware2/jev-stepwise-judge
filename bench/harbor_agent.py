"""Harbor agent for the jev-stepwise-judge benchmark: OpenCode, with or without the judge.

Every arm uses this class so the baseline differs from the judge arms only in the judge:

    -a bench.harbor_agent:JevOpenCode --ak jev=off           # A: plain OpenCode
    -a bench.harbor_agent:JevOpenCode --ak jev=agent         # B: require_set_state=agent
    -a bench.harbor_agent:JevOpenCode --ak jev=joints        # C: require_set_state=joints
    -a bench.harbor_agent:JevOpenCode --ak jev=every_step    # D: require_set_state=every_step

plus, for every arm: --ak version=<agent-cache/OPENCODE_VERSION> and the --mounts shown at AGENT_CACHE.

For the judge arms, install() copies this checkout into the task container, puts the
TypeSafe key there (read from the host's ~/.config/typesafe/key, so it never lands in
Harbor's job config), and runs `jev-stepwise-judge install opencode` (plugin + skill).
The MCP server goes into the opencode.json that OpenCode.run() writes on every trial.
The judge's state and judgments.jsonl go to /logs/agent/jev-stepwise-judge, which Harbor
copies into the trial directory.
"""
import shlex
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from harbor.agents.installed.opencode import OpenCode, OpenCodeOptions
from harbor.environments.base import BaseEnvironment

REPO = Path(__file__).resolve().parent.parent
KEY_FILE = Path("~/.config/typesafe/key").expanduser()
REMOTE_REPO = "/opt/jev-stepwise-judge"
REMOTE_BIN = REMOTE_REPO + "/bin/jev-stepwise-judge"
STATE_DIR = "/logs/agent/jev-stepwise-judge"
# Node + OpenCode built once on the host and bind-mounted read-only into every task container:
#   --mounts '[{"type":"bind","source":"<host>/agent-cache","target":"/opt/agent-cache","read_only":true}]'
AGENT_CACHE = "/opt/agent-cache"
CACHE_PYTHON = AGENT_CACHE + "/python/bin/python3"
MARK = "jev-stepwise-judge"
# what goes into the container: the package, its entry point, the plugin and the skill
SHIP = ["bin", "jev_stepwise_judge", "integrations", "skills"]

Jev = Literal["off", "agent", "joints", "every_step"]


class JevOpenCodeOptions(OpenCodeOptions):
    jev: Jev = Field(default="off", description="off = baseline; else the require_set_state level.")


class JevOpenCode(OpenCode):
    options_model = JevOpenCodeOptions

    def __init__(self, *args, jev: Jev = "off", opencode_config: dict[str, Any] | None = None, **kwargs):
        config = dict(opencode_config or {})
        if jev != "off":
            mcp = dict(config.get("mcp") or {})
            mcp[MARK] = {"type": "local", "command": [REMOTE_BIN, "mcp"], "enabled": True}
            config["mcp"] = mcp
        super().__init__(*args, jev=jev, opencode_config=config, **kwargs)
        self._jev: Jev = jev
        # Trial applies extra_env to every exec in the agent phase, so set it before the trial starts.
        self._extra_env.setdefault("JEV_STEPWISE_STATE", STATE_DIR)
        # SWE-bench images activate the repo's environment in ~/.bashrc (`conda activate testbed`), and
        # the official harness runs `bash -c 'source ~/.bashrc && ...'`. OpenCode's bash tool runs
        # non-interactive `bash -c`, which never reads ~/.bashrc, so without this every agent command ran
        # in conda's base env: no repo dependencies, not even pytest. BASH_ENV makes non-interactive bash
        # source it (bash expands $HOME itself). Stock Ubuntu .bashrc files return early when
        # non-interactive, so images without such a setup are unaffected. Same for every arm.
        self._extra_env.setdefault("BASH_ENV", "$HOME/.bashrc")
        if jev != "off":
            self._extra_env["JEV_STEPWISE_REQUIRE"] = jev

    async def install(self, environment: BaseEnvironment) -> None:
        await self._install_opencode(environment)
        if self._jev == "off":
            return
        if not KEY_FILE.is_file():
            raise FileNotFoundError(f"TypeSafe key not found at {KEY_FILE}")
        # The judge runs on the cache's standalone Python, never the task image's: installing
        # python3 only in judge arms would hand those agents a tool the baseline lacks.
        py = CACHE_PYTHON
        if (await environment.exec(command=f"{py} --version", user="root")).return_code != 0:
            self.logger.warning("cache python not usable here; using the image's python3 (arms may differ)")
            await self.ensure_system_dependencies(environment, ("python3",))
            py = "python3"
        await self.exec_as_root(environment, f"rm -rf {REMOTE_REPO} && mkdir -p {REMOTE_REPO}")
        for name in SHIP:
            await environment.upload_dir(REPO / name, f"{REMOTE_REPO}/{name}")
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "key"
            staged.write_bytes(KEY_FILE.read_bytes())
            await environment.upload_file(staged, "/tmp/jev-typesafe-key")
        interp = shlex.quote(py if py.startswith("/") else "/usr/bin/env python3")
        await self.exec_as_root(
            environment,
            f"sed -i '1s|.*|#!'{interp}'|' {REMOTE_BIN} && "
            f"chmod -R a+rX {REMOTE_REPO} && chmod 755 {REMOTE_BIN}",
        )
        await self.exec_as_agent(
            environment,
            "set -e; mkdir -p ~/.config/typesafe; chmod 700 ~/.config/typesafe; "
            "install -m 600 /tmp/jev-typesafe-key ~/.config/typesafe/key; rm -f /tmp/jev-typesafe-key; "
            f"{REMOTE_BIN} install opencode; "
            # the MCP entry lives in the opencode.json that run() writes; drop the installer's copy
            "rm -f ~/.config/opencode/opencode.jsonc; "
            f"{REMOTE_BIN} doctor",
        )

    async def _install_opencode(self, environment: BaseEnvironment) -> None:
        """Link Node + OpenCode from the read-only host cache mounted at AGENT_CACHE.

        Harbor's own install downloads nvm, Node and OpenCode in every trial (~4.5 min, and a
        network failure point). Falls back to it when the cache is not mounted or does not run
        here (e.g. musl images); pass --ak version=<the cache's version> so both paths match.
        """
        probe = await environment.exec(
            command=f"{AGENT_CACHE}/node/bin/node --version && {AGENT_CACHE}/npm/bin/opencode --version",
            user="root",
        )
        if probe.return_code != 0:
            self.logger.warning("agent cache not usable here; falling back to a network install")
            await super().install(environment)
            return
        await self.ensure_system_dependencies(environment, ("bash", "coreutils"))
        await self.exec_as_root(
            environment,
            f"ln -sf {AGENT_CACHE}/node/bin/node /usr/local/bin/node && "
            f"ln -sf {AGENT_CACHE}/npm/bin/opencode /usr/local/bin/opencode && "
            # OpenCode otherwise downloads ripgrep from GitHub on first use in every trial
            f"ln -sf {AGENT_CACHE}/ripgrep/rg /usr/local/bin/rg && opencode --version",
        )
        # ... and npm-installs @opencode-ai/plugin (63 MB) into its config dir on every start
        await self.exec_as_agent(
            environment,
            f"mkdir -p ~/.config/opencode && cp -a {AGENT_CACHE}/opencode-config/. ~/.config/opencode/",
        )


__all__ = ["JevOpenCode"]
