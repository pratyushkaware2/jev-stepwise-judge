"""Configuration: built-in defaults, overridden by a user config file and env vars."""
import json
import os

DEFAULTS = {
    # off | shadow (log only) | advise (notes to the agent, never blocks) | enforce
    "mode": "advise",
    "model": "jev-1.13.0",
    "timeout_s": 3.0,
    "recent_steps": 8,
    "max_field_chars": 600,
    # step kinds that are recorded into the agent state but never sent to Jev
    "skip_kinds": ["read", "search", "web", "vcs_read", "meta"],
    # regexes over paths/commands; a matching step is never sent to Jev, and
    # matching paths are masked in every state that is sent
    "sensitive_path_patterns": [
        r"(^|/)\.ssh/", r"(^|/)\.aws/", r"(^|/)\.gnupg/", r"(^|/)\.netrc\b",
        r"(^|/)\.env(\.[\w-]+)?$", r"(^|/)\.env\b", r"\bid_(rsa|ed25519|ecdsa)\b",
        r"(^|/)\.config/typesafe/", r"credentials(\.json)?\b", r"\.pem\b", r"\.p12\b",
    ],
    # your own additions (kept out of the defaults so they stay private)
    "extra_sensitive_path_patterns": [],
    # When the hooks call Jev on their own. The hooks always record state (local,
    # no network); by default the agent decides when to be judged (MCP set_state).
    #   off         record only; judgement happens when the agent calls set_state / get_direction
    #   gates       also judge the high-stakes moments: marking a goal completed,
    #               commit / push, and stopping with work open
    #   every_step  judge every non-read step and push directions after tests, builds,
    #               goal changes and failures
    "auto_judge": "off",
    # How often the agent itself must report its state (MCP set_state) before acting.
    # Enforced by the hooks in code (no Jev call); a missing report blocks the step
    # in advise and enforce modes alike, with instructions for what to report.
    #   agent       never required; the agent calls set_state when it judges useful
    #   joints      required before marking a goal completed, commit / push, and stopping
    #   every_step  required before every acting step (edit, run, test, build, commit,
    #               completing a goal); reads, searches and plain todo planning are free
    "require_set_state": "agent",
    # ask Jev whether a test/build output passed when exit status is unknown
    # (only when auto_judge is not off)
    "classify_unknown_results": True,
    # with auto_judge gates / every_step: judge an attempt to stop while work is open
    "stop_gate": True,
    # agents that get the one-time "keep a todo list / call set_state" reminder at the
    # first prompt of a session (the skill's rules in one line). Empty list = nobody.
    "remind_agents": ["claude", "codex", "grok", "opencode"],
    "thresholds": {},
}

THRESHOLDS = {
    "repeat_failure": 0.85,        # retries a failed step unchanged -> deny
    "goal_incomplete_max": 0.5,    # marking a goal done while goal_complete is below this -> gate
    "verification_due": 0.7,       # unverified edits should be tested/built first
    "ship_verification_due": 0.6,  # ... before a commit / push
    "goal_done_nudge": 0.85,       # current goal looks done but the agent keeps going
    "knowledge_gap_max": 0.3,      # knowledge_sufficient at or below -> gather first
    "off_goal_max": 0.2,           # goal_aligned at or below -> off-goal
    "move_conf": 0.7,              # confidence needed in next_move to give advice
    "finished_min": 0.5,           # stop gate: finished below this -> not done
    "push_conf": 0.65,             # push a direction note after key events only above this
    "claims_unsupported_max": 0.2,  # set_state: flag the report only when Jev is confident it is unsupported
    "report_goal_done_max": 0.2,    # set_state: flag "believes done" only when Jev is confident it is not
}


def state_dir():
    return os.path.expanduser(os.environ.get("JEV_STEPWISE_STATE", "~/.local/state/jev-stepwise-judge"))


def config_path():
    return os.path.expanduser(os.environ.get("JEV_STEPWISE_CONFIG", "~/.config/jev-stepwise-judge/config.json"))


PROJECT_FILE = ".jev-stepwise-judge.json"
# what a project file may set; sensitive patterns can only be added, never removed
PROJECT_KEYS = {"mode", "require_set_state", "auto_judge", "stop_gate", "skip_kinds", "thresholds",
                "classify_unknown_results", "recent_steps", "remind_agents"}


def project_config(cwd):
    """Nearest .jev-stepwise-judge.json from cwd upwards (stops at $HOME or /)."""
    if not cwd:
        return None, {}
    home = os.path.expanduser("~")
    d = os.path.abspath(cwd)
    while True:
        path = os.path.join(d, PROJECT_FILE)
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                return path, data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return path, {}
        parent = os.path.dirname(d)
        if d in (home, parent):
            return None, {}
        d = parent


def load(cwd=None):
    """Defaults < user config < project file (nearest to cwd) < environment."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(config_path()) as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update(user)
    except (OSError, ValueError):
        pass
    path, proj = project_config(cwd)
    if proj:
        cfg["project_config"] = path
        for k, v in proj.items():
            if k in PROJECT_KEYS:
                cfg[k] = dict(cfg.get(k) or {}, **v) if k == "thresholds" and isinstance(v, dict) else v
        extra = proj.get("extra_sensitive_path_patterns")
        if isinstance(extra, list):
            cfg["extra_sensitive_path_patterns"] = list(cfg.get("extra_sensitive_path_patterns") or []) + extra
    th = dict(THRESHOLDS)
    th.update(cfg.get("thresholds") or {})
    cfg["thresholds"] = th
    cfg["sensitive"] = list(cfg.get("sensitive_path_patterns") or []) + list(cfg.get("extra_sensitive_path_patterns") or [])
    if os.environ.get("JEV_STEPWISE_REQUIRE"):
        cfg["require_set_state"] = os.environ["JEV_STEPWISE_REQUIRE"]
    if os.environ.get("JEV_STEPWISE_AUTO"):
        cfg["auto_judge"] = os.environ["JEV_STEPWISE_AUTO"]
    if os.environ.get("JEV_STEPWISE_MODE"):
        cfg["mode"] = os.environ["JEV_STEPWISE_MODE"]
    if os.environ.get("JEV_STEPWISE_DISABLE"):
        cfg["mode"] = "off"
    return cfg
