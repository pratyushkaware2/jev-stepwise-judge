"""Minimal stdlib client for TypeSafe's System One API (Jev)."""
import json
import os
import urllib.error
import urllib.request

API_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/") + "/v1/systemone"


class JevUnavailable(Exception):
    """No key, network failure, timeout or a non-2xx answer."""


def api_key():
    for var in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        key = os.environ.get(var, "").strip()
        if key:
            return key
    for path in ("~/.config/typesafe/key", "~/.config/jev-stepwise-judge/key"):
        try:
            with open(os.path.expanduser(path)) as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            continue
    return ""


def ask(state, questions, model, timeout):
    """POST one batched request. Returns the decoded response. Never logs the key."""
    key = api_key()
    if not key:
        raise JevUnavailable("no TYPESAFE_API_KEY")
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise JevUnavailable("HTTP %s" % e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise JevUnavailable(type(e).__name__) from None


def summarize(answers):
    """Compact view of raw answers for logs: noul -> p, choice -> (choice, conf)."""
    out = {}
    for k, v in (answers or {}).items():
        if v.get("type") == "noul":
            out[k] = v.get("noul")
        elif v.get("type") == "choice":
            out[k] = {"choice": v.get("choice"), "confidence": v.get("confidence")}
        elif v.get("type") == "score":
            out[k] = {"score": v.get("score"), "confidence": v.get("confidence")}
    return out
