// l0wagent's opencode plugin: mid-session delivery, the opencode side of Claude Code's
// PostToolUse hook. After every tool call it runs the same `assistant-hook` (which asks
// control for anything new: answers, decisions, the owner's messages) and appends what it
// returns to the tool's output, where the model reads it. It must never break a session:
// any failure leaves the output as it was.
import { spawnSync } from "node:child_process"

const HOOK = process.env.L0W_HOOK || "/usr/local/bin/assistant-hook"

export const L0wDelivery = async () => ({
  "tool.execute.after": async (_input, output) => {
    try {
      const r = spawnSync(HOOK, [], { input: "{}", timeout: 10000, encoding: "utf8" })
      const out = (r.stdout || "").trim()
      if (!out) return
      const text = JSON.parse(out)?.hookSpecificOutput?.additionalContext
      if (text) output.output = `${output.output ?? ""}\n\n${text}`
    } catch {
      // never break the session
    }
  },
})
