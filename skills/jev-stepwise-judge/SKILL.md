---
name: jev-stepwise-judge
description: >
  Work step by step with a Jev judge that records your agent state (knowledge,
  workspace, goals) and, when you report your own view with set_state, tells you
  which direction to move next and whether your intended step is sound. Use for
  any multi-step coding task when jev-stepwise-judge is installed, when you see
  "[jev-stepwise-judge]" messages, or when unsure whether to verify, move on, or
  finish. Mandates a todo list.
---

# jev-stepwise-judge

Hooks record your **agent state** from every prompt, tool call and result:

| State | What is recorded |
| --- | --- |
| **Knowledge** | files you read, the latest test and build output, background processes and whether you checked them, recent errors |
| **Workspace** | edits you made, edits with no passing test or build since, and the edits and runs the task requires |
| **Goal** | your todo list: the current goal and progress |

Recording is silent and free. **You decide when to be judged.** Call the MCP tool
`set_state` with *your own* view of that state and the step you intend to take.
TypeSafe **Jev** checks your report against the recorded evidence and returns:

- a **direction** to move in, with concrete actions;
- a **verdict** on your intended next step;
- any **mismatches** between what you claim and what actually happened.

## Rules

1. **Keep a todo list. It is mandatory.** Before the first edit, write one with your
   agent's todo tool: `TodoWrite` / `TaskCreate` (Claude Code), `update_plan`
   (Codex), `todo_write` (Grok), `todowrite` (OpenCode). If your agent has none,
   use the MCP tool `set_plan` with `todos`.
   - One item per goal, phrased as an outcome ("parser keeps trailing tokens").
   - Exactly one item `in_progress`.
   - Mark an item `completed` only when its changes are made **and verified**
     (tests or build pass). Then set the next item `in_progress` in the same update.
   - Update the list when the plan changes, instead of working off-list.
2. **Report your state at the joints** with `set_state`, in particular:
   - once you understand the problem, before the first edit;
   - when you believe the current goal is done, **before** marking it completed;
   - after a failure you cannot immediately explain;
   - before committing, pushing or finishing;
   - whenever you are unsure what to do next.

   Be honest. Set `believes_verified` only if tests or a build passed *after* your
   last edit. The judge checks, and a mismatch costs you a detour.
3. **Follow the verdict.** If the verdict is `reconsider` or there are mismatches,
   move in the returned direction instead. Explicit instructions from the user
   outrank the judge.
4. **When a step is blocked because new results arrived since your last report**,
   your setup requires a fresh report. Call `set_state`, then retry the step if the
   verdict allows it. A report covers every step you send until the next result comes
   back (steps sent together in one batch share it), and todo-list updates don't use
   it up.
5. **Read before you edit, verify after you edit.** Check on background processes
   you started.
6. **After a denial, change course.** Do not retry the same call unchanged.

## `set_state`

```json
{
  "current_goal": "Fix parse() dropping trailing tokens",
  "knowledge": {"learned": ["split(',') keeps an empty last token"],
                "open_questions": ["are empty middle tokens valid?"]},
  "workspace": {"changed": ["parse.py strips a trailing comma"],
                "still_required": ["regression test"],
                "to_run": ["python3 -m pytest -q"]},
  "believes_goal_done": false,
  "believes_verified": false,
  "next_step": "run python3 -m pytest -q"
}
```

Commands in `workspace.to_run` are tracked until they succeed.

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

## Other tools

- `get_state`: the recorded state as the judge sees it (local, free).
- `get_direction`: a direction from the recorded state alone (no self-report).
- `judge_step {description}`: judge one specific step before taking it.
- `choose_next {options[]}`: rank 2-8 candidate next steps.
- `set_plan {todos?, required_edits?, required_runs?}`: declare goals and required changes.

Without MCP, use the CLI: `jev-stepwise-judge direction`,
`jev-stepwise-judge check "<step>"`.

## Notes

- Jev judges; it does not write code. Its answers are calibrated probabilities.
  Low confidence means the choice is close, not that it is wrong.
- Steps touching secrets or configured sensitive paths are never sent to Jev.
  Never paste secrets into tool calls or reports.
- Setup (by a human): `git clone https://github.com/pratyushkaware2/jev-stepwise-judge`,
  set `TYPESAFE_API_KEY`, run `bin/jev-stepwise-judge install`.
