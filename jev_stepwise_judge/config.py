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
    # ask Jev whether a test/build output passed when exit status is unknown
    "classify_unknown_results": True,
    # judge the agent's attempt to stop while goals or verification are open
    "stop_gate": True,
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
}


def state_dir():
    return os.path.expanduser(os.environ.get("JEV_STEPWISE_STATE", "~/.local/state/jev-stepwise-judge"))


def config_path():
    return os.path.expanduser(os.environ.get("JEV_STEPWISE_CONFIG", "~/.config/jev-stepwise-judge/config.json"))


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(config_path()) as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update(user)
    except (OSError, ValueError):
        pass
    th = dict(THRESHOLDS)
    th.update(cfg.get("thresholds") or {})
    cfg["thresholds"] = th
    cfg["sensitive"] = list(cfg.get("sensitive_path_patterns") or []) + list(cfg.get("extra_sensitive_path_patterns") or [])
    if os.environ.get("JEV_STEPWISE_MODE"):
        cfg["mode"] = os.environ["JEV_STEPWISE_MODE"]
    if os.environ.get("JEV_STEPWISE_DISABLE"):
        cfg["mode"] = "off"
    return cfg
