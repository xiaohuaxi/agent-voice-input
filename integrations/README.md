[English](README.md) | [简体中文](README.zh-CN.md)

# Agent Terminal Title & Session Registry Integrations

`agent-voice-input` can bind to the active foreground coding agent session in **O(1) time** across multiple concurrent terminal tabs/windows, extracting clean recent dialogue and `git status` file hotwords for speech recognition and Stage 2 refinement while strictly ignoring background subagents and non-interactive child processes.

All integrations below are **optional enhancements**:
- **Inside `tmux`**, active-pane tracking works out of the box on both Linux/WSL2 and macOS without modifying `~/.tmux.conf` (matching the focused attached client's active pane while isolating sibling panes).
- On **macOS + Ghostty**, `agent-voice-input` already reads the focused split's `tty` and `cwd` to match each split's active `codex`, `claude`, or `agy` session without hooks (including when Ghostty attaches to `tmux`).
- On **Linux / WSL2**, without hooks it automatically falls back to `/proc` foreground interactive root-session scanning and window-title / directory disambiguation.
- For the full local, desktop IDE, and SSH remote routing matrix, see [`docs/context-routing.md`](../docs/context-routing.md).

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
1. Automatically ignores background subagents carrying `agent_id` or `isSidechain` as well as headless `claude -p` / `--print` invocations (via `/proc` on Linux or `ps` parent-chain inspection on macOS);
2. Atomically writes root session metadata (including `agent_pid`/`pid`, normalized controlling `tty`, and `tmux_pane` / `tmux_socket` when inside `tmux`) to `<runtime_dir>/sessions/claude-<id8>.json` (`0600` permissions);
3. Walks the parent process chain to locate the controlling TTY character device (`/dev/pts/*` or `/dev/ttys*`) and writes an OSC 2 title (`claude · <short_cwd> [#<id8>]`)—and syncs the target pane's `#{pane_title}` inside `tmux` without switching pane focus—while keeping `stdout` empty so Claude's prompt context is never polluted.

---

## 3. Antigravity CLI (`~/.gemini/config/hooks.json`)

Following [`gemini/hooks.json`](gemini/hooks.json), add `voice-session-title` to `~/.gemini/config/hooks.json` (legacy `~/.gemini/hooks.json` and `~/.gemini/settings.json` remain recognized by `doctor`):

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

The hook checks `presence/<cid>.lock` file locks, the opening JSONL record, and the parent process chain (`/proc` on Linux or `ps` on macOS) to filter out background subagents and headless `agy -p` invocations—recording the normalized `tty` (and `tmux_pane` metadata when inside `tmux`) in `<runtime_dir>/sessions/agy-<cid8>.json`, updating the controlling TTY / pane title (`agy · <short_cwd> [#<cid8>]`) without changing pane focus only for foreground interactive sessions, and returning a clean `{}` JSON response.

---

## 4. `tmux` Split Panes (Zero `~/.tmux.conf` Changes Needed)

`agent-voice-input` tracks `tmux` multi-pane workflows out of the box without enabling `set-titles on` or modifying `~/.tmux.conf`:

- **Active-Pane Locking**: Even when the outer terminal window title stays `tmux` or a shell default, the daemon queries the active attached `tmux` client's current active pane (matching the outer `window_tty`/`window_pid` directly on macOS + Ghostty) and resolves sessions strictly within that pane:
  1. **With Hooks / Codex Title UUID**: Matches `[#tag]` / UUID in `#{pane_title}` or `tmux_pane` in the runtime session registry, verifying the recorded `agent_pid` and pane process tree are still alive so exited sessions never leave stale matches.
  2. **Without Hooks**: Scopes interactive root-session discovery strictly to the active pane (`/proc` descendant process tree on Linux/WSL2, or `ps`/`lsof` controlling-TTY inspection on macOS), keeping background teammate panes and empty shell panes isolated.
- **Multi-Tab / Split Coexistence**: If `tmux` stays attached in one terminal tab/split while you switch to a regular non-`tmux` tab/split running another agent, outer `window_tty` matching (Ghostty), `[#tag]` hints, CLI/spinner/directory titles, and client activity comparison keep the foreground non-`tmux` session from being hijacked by the background `tmux` session.

---

## 5. SSH Remote Linux Hosts (Zero-Install + Optional Title Hook)

When dictating from a local macOS or Windows+WSL2 machine into a headless remote Linux server over SSH:

1. **Whitelist the host locally** in `~/.config/agent-voice-input/config.toml`:
   ```toml
   [remote]
   hosts = ["devbox"]
   ```
2. **Zero-Install Mode (Default)**: Nothing needs to be installed on the remote server (`python3` 3.10+ stdlib only). Standard SSH shell/tmux window titles (`user@devbox: ~/repos/my-app`), active attached remote `tmux` panes, and live remote `/proc` interactive sessions are matched automatically—while unconfigured SSH/Remote-IDE windows are strictly isolated from leaking local background sessions.
3. **Remote `tmux` Outer Title Forwarding (Recommended when running multiple SSH tabs)**: Add 2 lines to `~/.tmux.conf` on the remote host so the outer local terminal receives `[@<short_hostname>]` and pane titles:
   ```tmux
   set -g set-titles on
   set -g set-titles-string '#T [@#h]'
   ```
4. **Optional Title-Tag Multi-Session Disambiguation**:
   - If `agent-voice-input` is already on the remote host, `agent-voice-input session-hook` automatically appends `[@<short_hostname>]` (or `$AGENT_VOICE_INPUT_HOST_TAG`) when `$SSH_CONNECTION` is present.
   - Without installing `agent-voice-input` on the remote host, you can copy the single standalone script [`remote/remote-title-hook.sh`](remote/remote-title-hook.sh) to `~/.local/bin/remote-title-hook.sh` (`chmod +x`) and wire it into Claude Code (`~/.local/bin/remote-title-hook.sh claude`) or Antigravity CLI (`~/.local/bin/remote-title-hook.sh agy`).
