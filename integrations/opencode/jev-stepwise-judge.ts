// jev-stepwise-judge for OpenCode. Installed by `jev-stepwise-judge install opencode`,
// which replaces __JEV_STEPWISE_BIN__ with the absolute path of bin/jev-stepwise-judge.
//
// OpenCode has plugins rather than shell hooks, so this shim turns plugin events into
// the same Claude-style hook payloads the judge reads from every other agent:
//   chat.message        -> UserPromptSubmit
//   tool.execute.before -> PreToolUse  (a deny throws, which the model sees as a tool error)
//   tool.execute.after  -> PostToolUse (direction notes are appended to the tool output)
// Any failure fails open.
import { spawnSync } from "node:child_process"

const BIN = "__JEV_STEPWISE_BIN__"
const TIMEOUT_MS = 15000

function judge(payload: Record<string, unknown>): any {
  try {
    const r = spawnSync(BIN, ["hook", "--agent", "opencode"], {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: TIMEOUT_MS,
    })
    const out = (r.stdout || "").trim()
    return out ? JSON.parse(out) : null
  } catch {
    return null
  }
}

export const JevStepwiseJudge = async ({ directory }: { directory: string }) => ({
  "chat.message": async (input: any, output: any) => {
    const text = (output?.parts || [])
      .filter((p: any) => p?.type === "text" && !p?.synthetic)
      .map((p: any) => p.text)
      .join("\n")
    if (text.trim()) {
      judge({ hook_event_name: "UserPromptSubmit", session_id: input.sessionID, cwd: directory, prompt: text })
    }
  },

  "tool.execute.before": async (input: any, output: any) => {
    const res = judge({
      hook_event_name: "PreToolUse",
      session_id: input.sessionID,
      tool_use_id: input.callID,
      cwd: directory,
      tool_name: input.tool,
      tool_input: output?.args ?? {},
    })
    const hso = res?.hookSpecificOutput
    if (hso?.permissionDecision === "deny") {
      throw new Error(hso.permissionDecisionReason || "blocked by jev-stepwise-judge")
    }
  },

  "tool.execute.after": async (input: any, output: any) => {
    const res = judge({
      hook_event_name: "PostToolUse",
      session_id: input.sessionID,
      tool_use_id: input.callID,
      cwd: directory,
      tool_name: input.tool,
      tool_response: { output: output?.output, metadata: output?.metadata },
    })
    const note = res?.hookSpecificOutput?.additionalContext
    if (note && typeof output?.output === "string") {
      output.output += "\n\n" + note
    }
  },
})
