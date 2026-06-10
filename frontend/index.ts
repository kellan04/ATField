import { createCliRenderer, TextRenderable, BoxRenderable } from "@opentui/core"

// ============================================================================
// Types
// ============================================================================

type ChatRole = "user" | "assistant" | "tool" | "system"
type StreamPhase = "idle" | "thinking" | "tool_running" | "responding"

interface ActiveAssistantTurn {
  messageId: string
  thinking: string
  content: string
  toolCalls: Array<{
    id: string
    name: string
    args: Record<string, unknown>
    status: "running" | "done"
    result?: string
  }>
  status: StreamPhase
}

type AppPhase = "booting" | "ready" | "waiting_response" | "compacting" | "error"

interface AppState {
  phase: AppPhase
  inputValue: string
  activeTurn: ActiveAssistantTurn | null
  debug: boolean
  backendHealthy: boolean
}

type BackendMessage =
  | { type: "ready"; protocol_version?: string; message_id?: string }
  | { type: "event"; event: "thinking" | "content" | "tool_start" | "tool_result" | "compact_panic"; data?: string; id?: string; name?: string; args?: Record<string, unknown>; result?: string; message_id?: string }
  | { type: "response"; status: string; content?: string; reasoning?: string; message_id?: string }
  | { type: "session_saved"; message_id?: string }
  | { type: "pong"; message_id?: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown>; message_id?: string }

// ============================================================================
// ProcessManager
// ============================================================================

interface BackendProcess {
  start(): Promise<void>
  send(msg: object): void
  onMessage(cb: (msg: BackendMessage) => void): void
  onExit(cb: (code: number | null) => void): void
  stop(): Promise<void>
}

function createBackendProcess(): BackendProcess {
  let proc: ReturnType<typeof Bun.spawn> | null = null
  const messageCallbacks: Array<(msg: BackendMessage) => void> = []
  const exitCallbacks: Array<(code: number | null) => void> = []

  return {
    async start() {
      proc = Bun.spawn({
        cmd: [".venv/bin/python", "eva.py", "--tui"],
        cwd: "/Volumes/Work/code/python_program/eva",
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      })

      // Read stdout line by line (runs concurrently)
      const reader = proc.stdout.getReader()
      const dec = new TextDecoder()

      // Non-blocking read loop
      const readLoop = async () => {
        try {
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            const line = dec.decode(value)
            const lines = line.split("\n")
            for (const l of lines) {
              const trimmed = l.trim()
              if (!trimmed) continue
              try {
                const msg = JSON.parse(trimmed) as BackendMessage
                for (const cb of messageCallbacks) {
                  cb(msg)
                }
              } catch {
                // Ignore non-JSON lines
              }
            }
          }
        } catch {
          // Reader error
        }
      }
      readLoop()

      // Read stderr (debug output)
      const stderrReader = proc.stderr.getReader()
      const errDec = new TextDecoder()
      const errLoop = async () => {
        try {
          while (true) {
            const { done, value } = await stderrReader.read()
            if (done) break
            const errLine = errDec.decode(value)
            if (errLine.trim()) {
              console.error("[backend]", errLine.trim())
            }
          }
        } catch {
          // Ignore stderr errors
        }
      }
      errLoop()

      // Return immediately - stdin/stdout pipes are ready
    },

    send(msg: object) {
      if (proc && proc.stdin) {
        proc.stdin.write(JSON.stringify(msg) + "\n")
      }
    },

    onMessage(cb: (msg: BackendMessage) => void) {
      messageCallbacks.push(cb)
    },

    onExit(cb: (code: number | null) => void) {
      exitCallbacks.push(cb)
    },

    async stop() {
      if (proc) {
        proc.kill()
        proc.stdin.close()
        await proc.exited
        const exitCode = proc.exitCode
        for (const cb of exitCallbacks) {
          cb(exitCode)
        }
      }
    },
  }
}

// ============================================================================
// AppState
// ============================================================================

let state: AppState = {
  phase: "booting",
  inputValue: "",
  activeTurn: null,
  debug: false,
  backendHealthy: false,
}

// ============================================================================
// Renderer Setup
// ============================================================================

const renderer = await createCliRenderer({
  screenMode: "split-footer",
  externalOutputMode: "passthrough",
  consoleMode: "disabled",
  exitOnCtrlC: true,
  targetFps: 30,
  footerHeight: 3,
})

// Protocol state: ignore non-JSON stdout until handshake complete
let handshakeDone = false

// Footer Input Area
const footerBox = new BoxRenderable(renderer, {
  id: "footer",
  position: "absolute",
  bottom: 0,
  width: "100%",
  height: 3,
  backgroundColor: "#1a1a2e",
  flexDirection: "row",
  alignItems: "center",
  borderTop: "2px solid #7ee8fa",
})

const statusText = new TextRenderable(renderer, {
  id: "status",
  content: "EVA",
  fg: "#7ee8fa",
  attributes: 1,
})

footerBox.add(statusText)

// Help text - keyboard shortcuts hint
const helpText = new TextRenderable(renderer, {
  id: "help",
  content: "[ctrl+l clear  ctrl+d debug  ctrl+c quit]",
  fg: "#555555",
})

footerBox.add(helpText)

const spacerText = new TextRenderable(renderer, {
  id: "spacer",
  content: "  ",
  fg: "#1a1a2e",
})

footerBox.add(spacerText)

const inputText = new TextRenderable(renderer, {
  id: "input",
  content: "> ",
  fg: "#e8d5b7",
})

footerBox.add(inputText)

renderer.root.add(footerBox)

// ============================================================================
// Message Rendering
// ============================================================================

let msgCounter = 0

// Thinking animation state
let thinkingDots = 0
let thinkingInterval: ReturnType<typeof setInterval> | null = null

const THINKING_FRAMES = ["   ", ".  ", " . ", "  .", " . ", ".  "]

function startThinkingAnimation() {
  thinkingDots = 0
  thinkingInterval = setInterval(() => {
    thinkingDots = (thinkingDots + 1) % THINKING_FRAMES.length
    updateStatus(`EVA | Thinking${THINKING_FRAMES[thinkingDots]}`)
  }, 400)
}

function stopThinkingAnimation() {
  if (thinkingInterval) {
    clearInterval(thinkingInterval)
    thinkingInterval = null
  }
}

function appendToScrollback(role: ChatRole, text: string) {
  const icon = role === "user" ? "👤" : role === "assistant" ? "🤖" : role === "tool" ? "🔧" : "⚙️"
  const color = role === "user" ? "#7ee8fa" : role === "assistant" ? "#e8d5b7" : role === "tool" ? "#b8a9c9" : "#90EE90"

  // Separator before assistant message
  if (role === "assistant") {
    renderer.writeToScrollback((ctx) => {
      const sep = new TextRenderable(renderer, {
        content: "─".repeat(Math.min(ctx.width, 60)) + "\n",
        fg: "#333333",
      })
      const block = new BoxRenderable(renderer, {
        position: "relative",
        width: ctx.width,
        flexDirection: "column",
        marginTop: 4,
      })
      block.add(sep)
      return { root: block, width: ctx.width, height: 1, startOnNewLine: true, trailingNewline: true }
    })
  }

  renderer.writeToScrollback((ctx) => {
    const label = new TextRenderable(renderer, {
      content: `${icon} ${role}\n`,
      fg: color,
      attributes: 1,
    })
    const content = new TextRenderable(renderer, {
      content: text,
      fg: color,
    })
    const block = new BoxRenderable(renderer, {
      position: "relative",
      width: ctx.width,
      flexDirection: "column",
      marginTop: 4,
      marginBottom: 4,
    })
    block.add(label)
    block.add(content)
    return { root: block, width: ctx.width, height: 1, startOnNewLine: true, trailingNewline: true }
  })
}

function updateStatus(text: string) {
  statusText.content = text
}

function updateInput() {
  inputText.content = `> ${state.inputValue}`
}

// ============================================================================
// Backend Integration
// ============================================================================

const backend = createBackendProcess()

backend.onMessage((msg) => {
  if (msg.type === "ready") {
    state.phase = "ready"
    state.backendHealthy = true
    if (msg.protocol_version) {
      updateStatus(`EVA | Ready v${msg.protocol_version}`)
    } else {
      updateStatus("EVA | Ready")
    }
  } else if (msg.type === "pong") {
    // Health check passed
  } else if (msg.type === "event") {
    if (msg.event === "thinking" && msg.data !== undefined) {
      if (!thinkingInterval) {
        startThinkingAnimation()
      }
    } else if (msg.event === "content" && msg.data !== undefined) {
      stopThinkingAnimation()
      if (!state.activeTurn) {
        state.activeTurn = {
          messageId: `msg_${++msgCounter}`,
          thinking: "",
          content: "",
          toolCalls: [],
          status: "responding",
        }
      }
      state.activeTurn.content += msg.data
    } else if (msg.event === "tool_start" && msg.id && msg.name) {
      if (!state.activeTurn) {
        state.activeTurn = {
          messageId: `msg_${++msgCounter}`,
          thinking: "",
          content: "",
          toolCalls: [],
          status: "tool_running",
        }
      }
      state.activeTurn.toolCalls.push({
        id: msg.id,
        name: msg.name,
        args: msg.args ?? {},
        status: "running",
      })
      updateStatus(`🔧 ${msg.name}`)
    } else if (msg.event === "tool_result" && msg.id && msg.result !== undefined) {
      if (state.activeTurn) {
        const tc = state.activeTurn.toolCalls.find((t) => t.id === msg.id)
        if (tc) {
          tc.status = "done"
          tc.result = msg.result
        }
      }
    } else if (msg.event === "compact_panic") {
      updateStatus("⚠️ Memory compacting...")
      state.phase = "compacting"
    }
  } else if (msg.type === "response") {
    stopThinkingAnimation()
    if (state.activeTurn) {
      const text = state.activeTurn.content || state.activeTurn.thinking || ""
      if (text) {
        appendToScrollback("assistant", text)
      }
      for (const tc of state.activeTurn.toolCalls) {
        const toolText = `🔧 ${tc.name}\n   ${tc.result ?? "(running)"}`
        appendToScrollback("tool", toolText)
      }
      state.activeTurn = null
    }
    state.phase = "ready"
    updateStatus("EVA | Ready v1.0")
  } else if (msg.type === "session_saved") {
    updateStatus("EVA | Session saved")
  }
})

backend.onExit((code) => {
  state.phase = "error"
  state.backendHealthy = false
  updateStatus(`❌ Backend exited: ${code}`)
})

// ============================================================================
// Keyboard Input
// ============================================================================

renderer.keyInput.on("keypress", (key) => {
  // Ctrl+L: clear conversation
  if (key.ctrl && key.name === "l") {
    renderer.clear()
    return
  }

  // Ctrl+D: toggle debug mode
  if (key.ctrl && key.name === "d") {
    state.debug = !state.debug
    updateStatus(state.debug ? "EVA | Debug ON" : "EVA | Ready v1.0")
    return
  }

  if (key.name === "return") {
    if (state.phase === "ready" && state.inputValue.trim()) {
      const text = state.inputValue.trim()
      state.inputValue = ""
      updateInput()
      appendToScrollback("user", text)
      state.phase = "waiting_response"
      updateStatus("⏳ Thinking...")
      backend.send({ type: "user_message", content: text })
    }
    return
  }

  // Backspace
  if (key.name === "backspace") {
    if (state.inputValue.length > 0) {
      state.inputValue = state.inputValue.slice(0, -1)
      updateInput()
    }
    return
  }

  // Printable characters - append if printable
  if (key.sequence && key.sequence.length === 1 && key.sequence.charCodeAt(0) >= 32) {
    state.inputValue += key.sequence
    updateInput()
  }
})

// ============================================================================
// Start
// ============================================================================

// Start backend and send init
backend.start().then(() => {
  // Backend is now spawned, send init
  backend.send({ type: "init" })
}).catch((err) => {
  console.error("Failed to start backend:", err)
  state.phase = "error"
  updateStatus("❌ Failed to start eva.py --tui")
})