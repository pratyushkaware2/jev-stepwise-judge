---
name: jev-stepwise-judge
description: >
  Work step by step under a Jev judge that tracks your agent state (knowledge,
  workspace, goals) and tells you which direction to move in next. Use for any
  multi-step coding task when jev-stepwise-judge is installed, when you see
  "[jev-stepwise-judge]" notes, or when unsure whether the next step is right,
  whether to verify, or whether the current goal is finished. Mandates a todo list.
---

# jev-stepwise-judge

A TypeSafe **Jev** judge watches every prompt, tool call and result, and keeps
your **agent state**:

| State | What it tracks |
| --- | --- |
| **Knowledge** | files you have read, the latest test and build output, background processes and whether you checked them, recent errors |
| **Workspace** | edits you made, edits not yet verified by a test or build, and the edits and runs the task *requires* (declared with `set_plan`) |
| **Goal** | your todo list: the current goal, progress, and when it is time to move on |

From that state Jev picks a **direction** for your next move, and judges each
step before it runs. You work; the judge keeps you pointed the right way.

## Rules

1. **Keep a todo list. It is mandatory.** Before the first edit, write one with your
   agent's todo tool: `TodoWrite` / `TaskCreate` (Claude Code), `update_plan`
   (Codex), `todo_write` (Grok), `todowrite` (OpenCode). If your agent has
   none, use the MCP tool `set_plan` with `todos`.
   - One item per goal, phrased as an outcome ("parser keeps trailing tokens").
   - Exactly one item `in_progress`.
   - Mark an item `completed` only when its changes are made **and verified**
     (tests or build pass). Then set the next item `in_progress` in the same update.
   - Update the list when the plan changes, instead of working off-list.
   Without a list, the judge blocks edits, runs and commits in enforce mode.
2. **Declare required workspace changes** once you know them: `set_plan` with
   `required_edits` (files that must change) and `required_runs` (tests, builds,
   migrations that must run). They are ticked off automatically as you work.
3. **Read before you edit, verify after you edit.** Run the relevant tests or
   build after changes and before marking a goal done, committing or finishing.
   Check on background processes you started.
4. **Act on `[jev-stepwise-judge]` notes.** They read
   `next: <direction> (<confidence>) - <meaning> Do: <concrete actions>`.
   Follow the direction, or say in one line why you are not. Explicit
   instructions from the user outrank the judge.
5. **After a denial, change course.** Do not retry the same call. Do what the
   direction says (usually: fix the failure, verify, or update the todo list).
6. **Ask for a direction at the joints.** Call `get_direction` when you finish a
   goal, after a failure, when a plan changes, or when unsure what comes next. Call
   `judge_step` before an irreversible or outward-facing step (push, deploy,
   delete, publish). Call `choose_next` when you have several reasonable options.

## Directions

| Direction | Do this |
| --- | --- |
| `plan_goals` | Write or fix the todo list first |
| `gather_knowledge` | Read, search, or inspect output before changing anything |
| `edit_workspace` | Make the edits the current goal still needs |
| `run_verification` | Run tests / build / lint on the unverified edits |
| `fix_failure` | Diagnose and fix the latest failing test, build or process |
| `run_process` | Start or restart a process the work needs |
| `check_process` | Check a background process you started |
| `advance_goal` | Mark the current goal completed; start the next one |
| `ship` | Commit / push verified work, if the task asks for it |
| `ask_user` | Stop and ask: a decision, information or consent is needed |
| `finish` | Everything is done and verified: report back |

## Tools

MCP server `jev-stepwise-judge`:

- `get_direction`: where to go next, with confidence, concrete actions, and alternatives.
- `get_state`: your tracked state (local only).
- `judge_step {description, tool?, input?}`: judge a step before taking it.
- `choose_next {options[], context?}`: rank candidate next steps.
- `set_plan {todos?, required_edits?, required_runs?}`: declare goals and required changes.

Without MCP, use the CLI: `jev-stepwise-judge direction`, `jev-stepwise-judge
check "<step>"`, `jev-stepwise-judge next --task "<task>" "<option>" "<option>"`.

## Notes

- Jev judges; it does not write code. Its answers are calibrated probabilities,
  not certainty. Low confidence means the choice is close, not that it is wrong.
- Steps touching secrets or configured sensitive paths are never sent to Jev.
  Never paste secrets into tool calls.
- Setup (by a human): `git clone https://github.com/pratyushkaware2/jev-stepwise-judge`,
  set `TYPESAFE_API_KEY`, run `bin/jev-stepwise-judge install`.
