"""Classify a tool call into a step kind, and read what its result says.

Pure code, no model calls: exact facts (which file, which command, did it
exit non-zero) belong in code; Jev gets the semantic questions.

Step kinds:
  read, search, web, vcs_read          knowledge-gathering
  edit                                 workspace change
  test, build                          verification (build includes lint/type-check)
  run_process, check_process, stop_process
  vcs_commit, vcs_push                 shipping
  goal_update                          todo / plan list change
  delegate, external, shell, meta      everything else
"""
import json
import re
import shlex

# tool names, lower-cased, across Claude Code, Codex, Grok CLI and OpenCode
READ_TOOLS = {"read", "read_file", "notebookread", "view", "view_image", "readmcpresourcetool"}
SEARCH_TOOLS = {"grep", "glob", "ls", "list", "list_dir", "list_directory", "grep_search", "file_search",
                "codebase_search", "search", "find", "toolsearch", "listmcpresourcestool", "memory_search",
                "search_tool", "list_tools"}
WEB_TOOLS = {"webfetch", "websearch", "web_fetch", "web_search", "fetch"}
EDIT_TOOLS = {"edit", "multiedit", "write", "notebookedit", "apply_patch", "search_replace", "write_file",
              "create_file", "edit_file", "patch", "str_replace", "str_replace_editor", "replace"}
SHELL_TOOLS = {"bash", "shell", "exec_command", "run_terminal_command", "terminal", "run_command",
               "local_shell", "execute", "run_shell_command", "unified_exec"}
TODO_TOOLS = {"todowrite", "todo_write", "update_plan", "taskcreate", "taskupdate"}
PROC_CHECK_TOOLS = {"bashoutput", "taskoutput", "get_command_or_subagent_output", "write_stdin"}
PROC_STOP_TOOLS = {"killshell", "killbash", "taskstop", "kill_command_or_subagent"}
DELEGATE_TOOLS = {"task", "agent", "spawn_subagent"}
META_TOOLS = {"todoread", "tasklist", "taskget", "askuserquestion", "exitplanmode", "enterplanmode", "skill",
              "sendusermessage", "sendusefile"}

TEST_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|karma|playwright test|cypress run|rspec|"
    r"phpunit|go test|cargo (?:test|nextest)|swift test|ctest|bun test|deno test|dotnet test|mix test|"
    r"(?:npm|pnpm|yarn|bun)(?: run)? test\b|make (?:test|check)\b|gradlew? test|mvn test|"
    r"xcodebuild\b[^|;&]*\btest\b)|\bpython3? (?:-m )?\S*test\S*\.py\b")
BUILD_RE = re.compile(
    r"\b(make\b(?! (?:test|check))|cmake --build|ninja\b|cargo (?:build|check|clippy)|go (?:build|vet)|tsc\b|"
    r"(?:npm|pnpm|yarn|bun) run (?:build|lint|typecheck|check|compile)|(?:pnpm|yarn) (?:build|lint)|"
    r"xcodebuild\b|swift build|gradlew? (?:build|assemble|compile\w*)|mvn (?:package|compile|verify)|"
    r"ruff\b|flake8|pylint|mypy|pyright|eslint|biome\b|prettier --check|shellcheck|"
    r"python3? -m (?:py_compile|compileall)|node --check|bash -n|zsh -n|vite build|next build|webpack)")
READ_CMD_RE = re.compile(r"^\s*(cat|head|tail(?! -f)|less|more|bat|wc|stat|file|jq|nl|od|xxd|sed -n|awk)\b")
SEARCH_CMD_RE = re.compile(r"^\s*(ls|tree|find|fd|rg|grep|ag|ack|locate|which|type|pwd|du)\b")
VCS_READ_RE = re.compile(r"^\s*git (status|diff|log|show|branch|blame|remote -v|rev-parse|ls-files)\b")
PROC_CHECK_RE = re.compile(r"^\s*(ps|pgrep|lsof|top|jobs|tail -f|curl\s[^|;&]*(localhost|127\.0\.0\.1|0\.0\.0\.0))\b")
PROC_STOP_RE = re.compile(r"^\s*(kill|pkill|killall)\b")
SERVER_RE = re.compile(r"\b(npm|pnpm|yarn|bun) run (dev|start|serve|watch)\b|\b(uvicorn|gunicorn|flask run|"
                       r"rails s|http\.server|next dev|vite\b(?! build)|nodemon|docker compose up)\b")
PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")
PATH_KEYS = ("file_path", "filePath", "path", "target_file", "notebook_path", "filename", "file", "target")

PASS_RE = re.compile(r"(?i)\b(\d+ passed(?!.*\b[1-9]\d* failed)|all tests passed|tests? passed|build succeeded|"
                     r"build successful|compiled successfully|0 errors?\b|no issues found|success: no issues|"
                     r"finished .*target|^ok\b|\bOK$)", re.M)
FAIL_RE = re.compile(r"(?i)(\b[1-9]\d* (failed|errors?)\b|\bFAILED\b|\bFAIL\b|error\[E\d+\]|build failed|"
                     r"traceback \(most recent call last\)|\bpanicked\b|npm err!|exit code [1-9]|"
                     r"command not found|syntaxerror|compilation failed)")


def _command_of(tool_input):
    if not isinstance(tool_input, dict):
        return tool_input if isinstance(tool_input, str) else ""
    cmd = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(cmd, list):
        # codex style ["bash", "-lc", "<script>"]
        if len(cmd) >= 3 and cmd[1] in ("-lc", "-c"):
            return cmd[2]
        return " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else ""


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")


def is_test_path(path):
    return bool(TEST_PATH_RE.search(path or ""))


def input_paths(tool_input):
    paths = []
    if isinstance(tool_input, dict):
        for k in PATH_KEYS:
            v = tool_input.get(k)
            if isinstance(v, str) and v:
                paths.append(v)
        for e in tool_input.get("edits", []) if isinstance(tool_input.get("edits"), list) else []:
            if isinstance(e, dict) and isinstance(e.get("file_path"), str):
                paths.append(e["file_path"])
    for s in _strings(tool_input):
        paths.extend(m.strip() for m in PATCH_FILE_RE.findall(s))
    return list(dict.fromkeys(paths))


def _command_paths(cmd):
    try:
        toks = shlex.split(cmd.split("|")[0].split("&&")[0], posix=True)
    except ValueError:
        toks = cmd.split()
    return [t for t in toks[1:] if not t.startswith("-") and ("/" in t or re.search(r"\.\w{1,6}$", t))][:5]


def _segments(cmd):
    return [s for s in re.split(r"&&|\|\||;|\n", cmd) if s.strip()]


def classify(tool, tool_input):
    """-> {"kind", "paths", "command", "background"}"""
    name = (tool or "").lower()
    base = name.split("__")[-1] if name.startswith("mcp__") else name
    step = {"kind": "shell", "paths": input_paths(tool_input), "command": None, "background": False}

    if "jev-stepwise-judge" in name or "jev_stepwise_judge" in name:
        step["kind"] = "meta"  # calls to the judge itself are never judged
    elif name in TODO_TOOLS:
        step["kind"] = "goal_update"
    elif name in READ_TOOLS:
        step["kind"] = "read"
    elif name in SEARCH_TOOLS:
        step["kind"] = "search"
    elif name in WEB_TOOLS:
        step["kind"] = "web"
    elif name in EDIT_TOOLS:
        step["kind"] = "edit"
    elif name in PROC_CHECK_TOOLS:
        step["kind"] = "check_process"
    elif name in PROC_STOP_TOOLS:
        step["kind"] = "stop_process"
    elif name in DELEGATE_TOOLS:
        step["kind"] = "delegate"
    elif name in META_TOOLS:
        step["kind"] = "meta"
    elif name.startswith("mcp__") or "__" in name:
        step["kind"] = "search" if re.match(r"(read|get|list|search|find|query|fetch|status)", base) else "external"
    elif name in SHELL_TOOLS or _command_of(tool_input):
        cmd = _command_of(tool_input)
        step["command"] = cmd
        if "apply_patch" in cmd and PATCH_FILE_RE.search(cmd):
            step["kind"] = "edit"
            return step
        bg = isinstance(tool_input, dict) and bool(tool_input.get("run_in_background") or tool_input.get("background"))
        bg = bg or bool(re.search(r"(^|[^&])&\s*$", cmd)) or cmd.lstrip().startswith("nohup ")
        step["background"] = bg
        segs = _segments(cmd)
        if re.search(r"\bgit push\b", cmd):
            step["kind"] = "vcs_push"
        elif re.search(r"\bgit commit\b", cmd):
            step["kind"] = "vcs_commit"
        elif TEST_RE.search(cmd):
            step["kind"] = "test"
        elif BUILD_RE.search(cmd):
            step["kind"] = "build"
        elif bg or SERVER_RE.search(cmd):
            step["kind"] = "run_process"
        elif segs and all(PROC_CHECK_RE.match(s) for s in segs):
            step["kind"] = "check_process"
        elif segs and PROC_STOP_RE.match(segs[0]):
            step["kind"] = "stop_process"
        elif segs and all(VCS_READ_RE.match(s) for s in segs):
            step["kind"] = "vcs_read"
        elif segs and all(READ_CMD_RE.match(s) or SEARCH_CMD_RE.match(s) or VCS_READ_RE.match(s) for s in segs):
            step["kind"] = "read" if READ_CMD_RE.match(segs[0]) else "search"
            step["paths"] = step["paths"] or _command_paths(cmd)
        elif re.search(r"(^|\s)(>|>>|tee )\s*\S", cmd) or re.match(r"\s*(sed -i|perl -pi|mv|cp|rm|mkdir|touch)\b", cmd):
            step["kind"] = "edit"
            step["paths"] = step["paths"] or _command_paths(cmd)
    return step


# ------------------------------------------------------------------ goals

STATUS_MAP = {"done": "completed", "complete": "completed", "completed": "completed", "finished": "completed",
              "in_progress": "in_progress", "in-progress": "in_progress", "inprogress": "in_progress",
              "active": "in_progress", "doing": "in_progress", "pending": "pending", "todo": "pending",
              "not_started": "pending", "cancelled": "cancelled", "canceled": "cancelled", "skipped": "cancelled"}


def _norm_status(s):
    return STATUS_MAP.get(str(s or "pending").lower().replace(" ", "_"), "pending")


def apply_goal_update(tool, tool_input, todos):
    """Return the todo list after this call. Handles full-list writes (TodoWrite,
    todo_write, todowrite, update_plan) and incremental TaskCreate/TaskUpdate."""
    name = (tool or "").lower()
    todos = [dict(t) for t in (todos or [])]
    if not isinstance(tool_input, dict):
        return todos
    items = tool_input.get("todos")
    if items is None:
        items = tool_input.get("plan")
    if isinstance(items, list):
        new = []
        for i, t in enumerate(items):
            if not isinstance(t, dict):
                continue
            content = t.get("content") or t.get("step") or t.get("title") or t.get("subject") or ""
            new.append({"id": str(t.get("id", i + 1)), "content": str(content)[:200],
                        "status": _norm_status(t.get("status"))})
        if tool_input.get("merge") and todos:
            by_id = {t["id"]: t for t in todos}
            for t in new:
                if t["id"] in by_id:
                    by_id[t["id"]].update({k: v for k, v in t.items() if v})
                else:
                    todos.append(t)
            return todos
        return new
    if name == "taskcreate":
        todos.append({"id": str(len(todos) + 1), "content": str(tool_input.get("subject", ""))[:200],
                      "status": "pending"})
    elif name == "taskupdate":
        tid = str(tool_input.get("taskId", tool_input.get("id", "")))
        for t in todos:
            if t["id"] == tid:
                if tool_input.get("status"):
                    t["status"] = _norm_status(tool_input["status"])
                if tool_input.get("subject"):
                    t["content"] = str(tool_input["subject"])[:200]
    return todos


def current_goal(todos):
    for status in ("in_progress", "pending"):
        for t in todos or []:
            if t.get("status") == status:
                return t
    return None


def newly_completed(before, after):
    was = {t["id"]: t.get("status") for t in before or []}
    return [t for t in after or [] if t.get("status") == "completed" and was.get(t["id"]) != "completed"]


# ----------------------------------------------------------------- results

def result_text(response, limit=4000):
    if response is None:
        return ""
    if isinstance(response, str):
        text = response
    elif isinstance(response, dict):
        parts = [response.get(k) for k in ("stdout", "stderr", "output", "result", "content", "error", "message")]
        text = "\n".join(p if isinstance(p, str) else json.dumps(p, default=str) for p in parts if p)
        if not text:
            text = json.dumps(response, default=str)
    else:
        text = json.dumps(response, default=str)
    return text[-limit:]


def looks_failed(event, response):
    if event == "PostToolUseFailure":
        return True
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError") or response.get("interrupted"):
            return True
        if response.get("success") is False:
            return True
        for k in ("exit_code", "exitCode", "returncode", "status_code"):
            if isinstance(response.get(k), int) and response[k] != 0:
                return True
    return False


def verification_status(event, response):
    """pass | fail | unknown for a test/build step."""
    if looks_failed(event, response):
        return "fail"
    if isinstance(response, dict):
        for k in ("exit_code", "exitCode", "returncode"):
            if response.get(k) == 0:
                return "pass"
    text = result_text(response)
    if FAIL_RE.search(text):
        return "fail"
    if PASS_RE.search(text):
        return "pass"
    return "unknown"
