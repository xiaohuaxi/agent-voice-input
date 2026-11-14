[English](README.md) | [简体中文](README.zh-CN.md)

# Agent Terminal Title & Session Registry Integrations

`agent-voice-input` can bind to the active foreground coding agent session in **O(1) time** across multiple concurrent terminal tabs/windows, extracting clean recent dialogue and `git status` file hotwords for speech recognition and Stage 2 refinement while strictly ignoring background subagents and non-interactive child processes.

All integrations below are **optional enhancements**—without them, `agent-voice-input` automatically falls back to `/proc` foreground interactive root-session scanning and window-title directory disambiguation.

---

## 1. Codex CLI (`~/.codex/config.toml`)

Merge `terminal_title` from [`codex/config.toml`](codex/config.toml) into the `[tui]` table of `~/.codex/config.toml`:

```toml
[tui]
terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]
```

Codex appends the root session's `thread-id` UUID to the terminal title, allowing `agent-voice-input` to locate the exact `rollout-*.jsonl` file immediately.

---

## 2. Claude Code (`~/.claude/settings.json`)

Ensure `agent-voice-input` is on your `PATH` (or use its full path; see [`claude/voice-title.sh`](claude/voice-title.sh) for a `~/.local/bin` wrapper), and add `SessionStart` + `UserPromptSubmit` hooks in `~/.claude/settings.json` following [`claude/settings.hooks.json`](claude/settings.hooks.json):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "agent-voice-input session-hook --cli claude"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "agent-voice-input session-hook --cli claude"
          }
        ]
      }
    ]
  }
}
```

What the hook does:
1. Automatically ignores background subagents carrying `agent_id` or `isSidechain`;
2. Atomically writes root session metadata to `$XDG_RUNTIME_DIR/agent-voice-input/sessions/claude-<id8>.json` (`0600` permissions);
3. Writes an OSC 2 title (`claude · <short_cwd> [#<id8>]`) directly to the controlling TTY character device while keeping `stdout` empty so Claude's prompt context is never polluted.

---

## 3. Antigravity CLI (`~/.gemini/hooks.json`)

Following [`gemini/hooks.json`](gemini/hooks.json), add `voice-session-title` to `~/.gemini/hooks.json`:

```json
{
  "voice-session-title": {
    "SessionStart": [
      {
        "type": "command",
        "command": "agent-voice-input session-hook --cli agy",
        "timeout": 5
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "agent-voice-input session-hook --cli agy",
        "timeout": 5
      }
    ]
  }
}
```

The hook checks `presence/<cid>.lock` file locks and the opening JSONL record to filter out background subagents and headless `agy -p` invocations, updating the controlling TTY title (`agy · <short_cwd> [#<cid8>]`) only for foreground interactive sessions and returning a clean `{}` JSON response.
