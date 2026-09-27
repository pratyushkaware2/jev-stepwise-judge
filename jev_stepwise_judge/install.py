"""Idempotent installer: hooks + MCP server + skill for each agent, all user-level.

Every JSON file it edits is backed up first (<file>.bak-jev-stepwise-<time>).
Entries are recognised by the string "jev-stepwise-judge" in their command, so
re-running replaces them and `uninstall` removes exactly them.
"""
import json
import os
import shutil
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin", "jev-stepwise-judge")
SKILL_SRC = os.path.join(ROOT, "skills", "jev-stepwise-judge")
PLUGIN_SRC = os.path.join(ROOT, "integrations", "opencode", "jev-stepwise-judge.ts")
MARK = "jev-stepwise-judge"
HOME = os.path.expanduser("~")

# one identical command everywhere: Grok also reads Claude's hooks and
# de-duplicates identical handlers; the agent is detected from the payload
HOOK_CMD = '"%s" hook' % BIN
EVENTS = {
    "claude": {"UserPromptSubmit": 5, "PreToolUse": 15, "PostToolUse": 15, "PostToolUseFailure": 15, "Stop": 15},
    "grok": {"UserPromptSubmit": 5, "PreToolUse": 15, "PostToolUse": 15, "PostToolUseFailure": 15, "Stop": 15},
    "codex": {"UserPromptSubmit": 5, "PreToolUse": 15, "PostToolUse": 15, "Stop": 15},
}
PATHS = {
    "claude": os.path.join(HOME, ".claude", "settings.json"),
    "codex": os.path.join(HOME, ".codex", "hooks.json"),
    "grok": os.path.join(HOME, ".grok", "hooks", "jev-stepwise-judge.json"),
    "opencode_cfg": os.path.join(HOME, ".config", "opencode", "opencode.jsonc"),
    "opencode_plugin": os.path.join(HOME, ".config", "opencode", "plugins", "jev-stepwise-judge.ts"),
}
SKILL_DIRS = [os.path.join(HOME, ".claude", "skills"), os.path.join(HOME, ".agents", "skills"),
              os.path.join(HOME, ".config", "opencode", "skills")]


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path, data, dry):
    if dry:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, "%s.bak-jev-stepwise-%s" % (path, time.strftime("%Y%m%d%H%M%S")))
    tmp = path + ".tmp-jev"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _strip(hooks):
    """Remove our handlers from a hooks mapping, dropping emptied groups."""
    for event in list(hooks):
        groups = []
        for g in hooks[event] if isinstance(hooks[event], list) else []:
            hs = [h for h in g.get("hooks", []) if MARK not in str(h.get("command", ""))]
            if hs:
                groups.append(dict(g, hooks=hs))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    return hooks


def hooks_file(agent, remove, dry, log):
    path = PATHS[agent]
    data = _read_json(path, {})
    hooks = _strip(dict(data.get("hooks") or {}))
    if not remove:
        for event, timeout in EVENTS[agent].items():
            hooks.setdefault(event, []).append(
                {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": timeout}]})
    if agent == "grok" and remove and not hooks:
        if not dry and os.path.exists(path):
            os.remove(path)
        log("removed %s" % path)
        return
    data["hooks"] = hooks
    _write_json(path, data, dry)
    log("%s hooks %s %s" % (agent, "removed from" if remove else "written to", path))


def _run(cmd, dry, log):
    log("$ " + " ".join(cmd))
    if dry:
        return 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        log("  failed: %s" % e)
        return 1
    if r.returncode:
        log("  exit %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[:300]))
    return r.returncode


def mcp(agent, remove, dry, log):
    exe = shutil.which(agent)
    if not exe:
        log("%s not found on PATH; skipping its MCP registration" % agent)
        return
    if agent == "claude":
        _run([exe, "mcp", "remove", "-s", "user", MARK], dry, lambda m: None)
        if not remove:
            _run([exe, "mcp", "add", "-s", "user", MARK, "--", BIN, "mcp"], dry, log)
    elif agent == "codex":
        _run([exe, "mcp", "remove", MARK], dry, lambda m: None)
        if not remove:
            _run([exe, "mcp", "add", MARK, "--", BIN, "mcp"], dry, log)
    elif agent == "grok":
        _run([exe, "mcp", "remove", MARK], dry, lambda m: None)
        if not remove:
            _run([exe, "mcp", "add", "-s", "user", MARK, BIN, "--", "mcp"], dry, log)


def opencode(remove, dry, log):
    cfg_path = PATHS["opencode_cfg"]
    try:
        data = _read_json(cfg_path, {})
    except ValueError:
        log("%s is not plain JSON (comments?); add this by hand:\n  \"mcp\": {\"%s\": {\"type\": \"local\", "
            "\"command\": [\"%s\", \"mcp\"]}}" % (cfg_path, MARK, BIN))
        data = None
    if data is not None:
        servers = dict(data.get("mcp") or {})
        servers.pop(MARK, None)
        if not remove:
            servers[MARK] = {"type": "local", "command": [BIN, "mcp"], "enabled": True}
        if servers:
            data["mcp"] = servers
        else:
            data.pop("mcp", None)
        _write_json(cfg_path, data, dry)
        log("opencode MCP %s %s" % ("removed from" if remove else "written to", cfg_path))
    plugin = PATHS["opencode_plugin"]
    if remove:
        if os.path.exists(plugin) and not dry:
            os.remove(plugin)
        log("removed %s" % plugin)
        return
    with open(PLUGIN_SRC) as f:
        src = f.read().replace("__JEV_STEPWISE_BIN__", BIN)
    if not dry:
        os.makedirs(os.path.dirname(plugin), exist_ok=True)
        with open(plugin, "w") as f:
            f.write(src)
    log("opencode plugin written to %s" % plugin)


def skill(remove, dry, log):
    for d in SKILL_DIRS:
        if not os.path.isdir(os.path.dirname(d)):
            continue
        link = os.path.join(d, MARK)
        if os.path.islink(link) or os.path.exists(link):
            if os.path.islink(link) and not dry:
                os.remove(link)
            elif not os.path.islink(link):
                log("%s exists and is not a symlink; left alone" % link)
                continue
        if not remove and not dry:
            os.makedirs(d, exist_ok=True)
            os.symlink(SKILL_SRC, link)
        log("skill %s %s" % ("unlinked from" if remove else "linked into", d))


def run(agents, remove=False, dry=False, log=print):
    agents = agents or ["claude", "codex", "grok", "opencode"]
    if not remove and not os.access(BIN, os.X_OK):
        os.chmod(BIN, 0o755)
    for a in agents:
        if a in ("claude", "codex", "grok"):
            hooks_file(a, remove, dry, log)
            mcp(a, remove, dry, log)
        elif a == "opencode":
            opencode(remove, dry, log)
        else:
            log("unknown agent: %s" % a)
    skill(remove, dry, log)
    if not remove and "codex" in agents:
        log("codex: open codex and run /hooks to trust the new hooks (codex skips untrusted hooks)")
    return 0
