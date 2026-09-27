# jev-stepwise-judge

**A step-by-step judge for coding agents. It tracks what the agent knows, what it
has changed, and where it is in its todo list, and tells it which direction to
move next.** Powered by [TypeSafe Jev](https://typesafe.ai/), which answers typed
questions with calibrated probabilities in about 0.2 s for about $0.00004 a call.

Works with **Claude Code**, **Codex**, **Grok CLI** and **OpenCode**. It is written
in pure Python with the standard library only, with no dependencies and no build step.

```
you: "parse() drops trailing tokens when input ends with a comma. Fix it and add a regression test."

  * edit before writing a todo list
    -> DENY  write the todo list first (required) ... next: plan_goals (1.00)
  * write the todo list
    -> NOTE  next: gather_knowledge (0.90) - Do: read what 'Fix parse() ...' depends on
  * mark goal 1 completed without testing
    -> DENY  don't mark 'Fix parse() ...' completed yet: its work does not look done (p=0.08);
             1 edited file(s) are unverified. next: run_verification (0.75) - Do: unverified: parse.py
  * run tests (they fail)
    -> NOTE  next: fix_failure (0.94) - Do: failing: pytest -> AssertionError: ['a','b',''] != ['a','b']
  * commit and push while tests fail
    -> DENY  the last test/build failed; fix it before committing or pushing
  * run tests (they pass)
    -> NOTE  next: advance_goal (0.96) - Do: mark 'Fix parse() ...' completed and set
             'Add a regression test' in_progress
  * try to stop with goal 2 open
    -> BLOCK Not finished (p=0.32): goals done 1/2
```

That transcript is real output from [`examples/simulated_session.py`](examples/simulated_session.py)
against the live API. It uses enforce mode and `auto_judge=every_step`, so every step is judged; the p50 latency
was 173 ms. By default the judge is quieter: it records state silently, and **the agent decides when to be
judged** by reporting its own view with `set_state`:

```
set_state {current_goal: "Fix parse() trailing comma", believes_goal_done: true, believes_verified: true,
           next_step: "mark it completed and start the regression test"}      # but the last pytest run failed
->  next: fix_failure (0.97) - Do: failing: pytest -> AssertionError: [''] != []
    next_step_verdict: reconsider (the intended next step does not look sensible now, p=0.09)
    mismatches: claims its work is verified, but the last test/build failed;
                believes the current goal is done, but the evidence says it is not (p=0.03)
```

## The idea: agent state → direction

An agent's next move depends on three kinds of state, and the judge tracks all three
from the agent's own hook events:

| State | Tracked from | Examples |
| --- | --- | --- |
| **Knowledge**: what the agent has observed | reads, searches, tool results | files read; latest test and build output and whether it passed; background processes and whether they were checked; recent errors |
| **Workspace**: what changed and what still must | edits, runs, `set_plan` | files edited; edits not verified since they were made; required edits and runs the agent declared, ticked off automatically |
| **Goal**: where the agent is in the task | the todo list (mandatory) | current goal, progress, whether the current goal is done, when to move on |

Exact facts are computed in code: which edits are unverified, whether the last test
failed, which declared runs are outstanding, whether the step marks the current goal
complete. Jev gets those facts plus the state, and answers narrow typed questions in
one batched request:

- `direction`, a choice of `plan_goals`, `gather_knowledge`, `edit_workspace`,
  `run_verification`, `fix_failure`, `run_process`, `check_process`, `advance_goal`,
  `ship`, `ask_user` or `finish`
- `current_goal_done`: is the current todo item done and verified?
- `verification_due`: should the unverified edits be tested or built first?
- `step_sound`, `knowledge_sufficient`, `goal_aligned` and `repeats_failure` for the proposed step

The policy lives in code ([`judge.py`](jev_stepwise_judge/judge.py)). It turns
those answers into a **direction** with concrete actions (`re-run: pytest -q`, `mark
'X' completed and set 'Y' in_progress`, `read src/a.py before editing it`) and a
verdict for the step:

| Rule | Verdict (enforce) |
| --- | --- |
| No todo list and the step edits, runs, builds or ships | deny → `plan_goals` |
| Marks the current goal completed while it isn't done, edits are unverified, or the last check failed | deny (goal gate) |
| Commit/push after a failed test/build, or with unverified edits | deny (ship gate) |
| Retries a failed step unchanged | deny |
| Edits a file it never read | advice |
| Current goal looks done but the agent keeps working on it | advice → `advance_goal` |
| Step goes against a confident direction and looks unsound or off-goal | advice |
| Stops with open goals, unverified edits, or a failed check | block once (stop gate) |

Code also overrules Jev when the facts contradict it. It never suggests `advance_goal` for a goal
Jev itself rates as not done, never `finish` with open todos, and always `fix_failure`
instead of moving on after a red run.

## How it plugs in

```
agent ──hooks──▶ jev-stepwise-judge hook ──▶ recorded state (per session, ~/.local/state)
  │                     (records every prompt, step and result; local, no network)
  │
  └──MCP──▶ set_state {the agent's own view + intended next step}
                 └─▶ Jev: report vs evidence ─▶ direction + next-step verdict + mismatches
            get_state · get_direction · judge_step · choose_next · set_plan
```

- **Hooks** see every prompt, tool call and result, so the recorded state is exact and
  costs nothing. Claude Code, Codex and Grok CLI use Claude-style hook JSON. OpenCode
  gets a small plugin shim.
- **`set_state`** is the agent's own account: what it learned and still doesn't know,
  what it changed and still must change or run, whether it believes the goal is done and
  verified, and its intended next step. The report is judged *against* the recorded evidence.
  Exact contradictions are caught in code, for example "claims verified, but the last test
  failed". Jev judges the rest: whether the claims are supported, whether the goal is really
  done, whether the next step is sound, and which direction to take.
- **The MCP server** also offers `get_direction` (recorded state only), `judge_step`,
  `choose_next` and `set_plan`. It finds its own session through the agent process it
  shares with the hooks.

### When judging happens

Two settings in `~/.config/jev-stepwise-judge/config.json`:

| `require_set_state` | The agent must report (`set_state`) … |
| --- | --- |
| `agent` (default) | never required. The agent decides when a direction is useful |
| `joints` | before marking a goal completed, committing / pushing, and stopping |
| `every_step` | before every acting step (edit, run, test, build, commit, completing a goal). Reads, searches and plain todo planning stay free |

The mandate is checked in code by the hooks (no Jev call). A missing report **blocks** the
step, in advise and enforce mode alike, with instructions for what to report.

| `auto_judge` | The hooks call Jev on their own … |
| --- | --- |
| `off` (default) | never. The hooks only record state |
| `gates` | at the high-stakes moments: marking a goal completed, commit / push, stopping with work open |
| `every_step` | on every non-read step, and they push a direction after tests, builds, goal changes and failures |

Environment overrides: `JEV_STEPWISE_REQUIRE`, `JEV_STEPWISE_AUTO`, `JEV_STEPWISE_MODE`.
- **The skill** ([`skills/jev-stepwise-judge/SKILL.md`](skills/jev-stepwise-judge/SKILL.md))
  tells the agent the rules. **A todo list is mandatory**: one goal per item, one
  in_progress, and an item is completed only when done and verified. The skill also covers
  how to read `[jev-stepwise-judge]` notes and when to call the MCP tools.

## Install

```bash
git clone https://github.com/pratyushkaware2/jev-stepwise-judge.git ~/jev-stepwise-judge
cd ~/jev-stepwise-judge
export TYPESAFE_API_KEY=...          # from console.typesafe.ai; or put it in ~/.config/typesafe/key (mode 600)
bin/jev-stepwise-judge doctor        # key, API, offline state check
bin/jev-stepwise-judge install --dry-run   # show what would change
bin/jev-stepwise-judge install       # hooks + MCP + skill for claude codex grok opencode
```

`install [claude codex grok opencode]` is idempotent, backs up every JSON file it
edits (`*.bak-jev-stepwise-<time>`), and `uninstall` removes exactly what it added.

| Agent | Hooks | MCP | Skill |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `claude mcp add -s user` | `~/.claude/skills/` |
| Codex | `~/.codex/hooks.json`. **Then run `/hooks` in Codex to trust them.** | `codex mcp add` | `~/.agents/skills/` |
| Grok CLI | `~/.grok/hooks/jev-stepwise-judge.json` (Grok also reads Claude's hooks; identical handlers are de-duplicated) | `grok mcp add -s user` | `~/.agents/skills/` |
| OpenCode | plugin `~/.config/opencode/plugins/jev-stepwise-judge.ts` | `opencode.jsonc` → `mcp` | `~/.config/opencode/skills/` |

Agent differences, handled for you:

- Codex and OpenCode have no hook-driven "ask", so an ask becomes a deny that tells the
  model to confirm with the user.
- Codex drops `PreToolUse` context, so advice arrives with the tool result instead.
- OpenCode notes are appended to the tool output.

## Modes

`mode` sets what happens to the verdicts that Jev-based judging produces (`auto_judge`):

| Mode | Behaviour |
| --- | --- |
| `advise` (default) | Directions and advice reach the agent; nothing is blocked (denies become advice) |
| `enforce` | Deny rules block the step; the stop gate blocks one stop |
| `shadow` | Judge and log only; the agent sees nothing |
| `off` | Disabled |

Set it in `~/.config/jev-stepwise-judge/config.json` (`"mode": "enforce"`), per shell
with `JEV_STEPWISE_MODE=enforce`, or turn it off with `JEV_STEPWISE_DISABLE=1`. Start in
`advise` or `shadow`, read `jev-stepwise-judge stats`, then enforce.

## Configuration

`~/.config/jev-stepwise-judge/config.json` overrides any default in
[`config.py`](jev_stepwise_judge/config.py). See [`config.example.json`](config.example.json).

- `thresholds` tunes every cut-off.
- `skip_kinds` lists step kinds that are recorded but never judged (by default reads,
  searches and web lookups).
- `extra_sensitive_path_patterns` holds your private regexes.
- `require_set_state` and `auto_judge` set when judging happens (see above).

## Privacy

What leaves the machine is a compact, redacted state: task text, todo items, file
paths, command lines, and the last few hundred characters of test and build output.

- Secrets are scrubbed: bearer tokens, `KEY=…`, `sk-…`/`ghp_…`-style tokens and long
  base64 runs.
- Steps that touch a sensitive path pattern are **never sent**, and those paths are
  masked in every state that is. The defaults cover `.ssh`, `.aws`, `.env`, keys and
  credentials; add your own with `extra_sensitive_path_patterns`.
- File *contents* the agent reads are never sent, only their paths.
- Everything fails open: no key, a timeout or an API error leaves the agent untouched.

The API key is read from `TYPESAFE_API_KEY` (or `JEV_API_KEY`, or
`~/.config/typesafe/key`). It is never logged.

## CLI

```
jev-stepwise-judge direction          # which way now, for the session in this directory
jev-stepwise-judge state              # the tracked state (local)
jev-stepwise-judge check "git push"   # judge a step before taking it
jev-stepwise-judge next --task "..." "option A" "option B"
jev-stepwise-judge stats              # verdicts, directions, latency from the log
```

## Tests

```bash
python3 -m unittest discover -s tests -v       # offline; Jev is faked
python3 examples/simulated_session.py          # live; needs a key
```

## Prior art

Several projects already put Jev behind coding agents:

- [leepokai/jev-guard](https://github.com/leepokai/jev-guard) does security triage of
  every tool call and prompt-injection scanning.
- [jonathanavis96/jev-kit](https://github.com/jonathanavis96/jev-kit) provides a tool-call
  guard for Claude Code, plus Belay (sends unverified "done" back).

jev-stepwise-judge is about *progress* rather than safety. It models the agent's
knowledge, workspace and goal state, and answers "which direction next?" Run it
alongside a safety guard if you want both.

## License

MIT
