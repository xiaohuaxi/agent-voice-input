**English** | [简体中文](README.zh-CN.md)

# Agent Terminal Title & Session Registry Integrations

> [!TIP]
> **~80% of daily workflows need zero hooks**: macOS Ghostty / `Terminal.app` / `iTerm2`, local or remote `tmux` on any platform (no `~/.tmux.conf` edits), all desktop apps, and WSL2 single-window or distinct-directory windows bind accurately out of the box.

Optional hooks are mainly useful when you run **multiple standalone non-`tmux` windows in the same project directory**—for example, Windows Terminal on WSL2 without `tmux`, or macOS terminals without a `tty` query API (WezTerm, Kitty, Alacritty, Warp). With hooks enabled, `agent-voice-input` binds to the foreground root session in **O(1) time** while strictly ignoring background subagents and headless child processes.

For the complete local, desktop IDE, and SSH remote routing matrix, see [`docs/context-routing.md`](../docs/context-routing.md).

---

## Recommended: Safe One-Command Setup (`agent-voice-input integrate`)

Instead of hand-editing JSON/TOML files (or letting an AI agent patch them manually), use the built-in non-interactive command:

```bash
agent-voice-input integrate --status     # Read-only check: detected CLIs, current hook state, and whether hooks are needed
agent-voice-input integrate --dry-run    # Preview exact changes without modifying any file
agent-voice-input integrate              # Safely install hooks for all detected CLIs
```

Supported flags:
- `--cli all|claude|codex|agy`: Target a specific CLI (default: `all` detected CLIs).
- `--uninstall`: Idempotently remove only `agent-voice-input` entries (leaving all other user hooks intact).
- `--json`: Machine-readable output for AI agents and scripts.

**Built-in safety guarantees**:
1. **Dotfiles symlink preservation**: If a target config (e.g., `~/.claude/settings.json`) is a symlink managed by Stow, chezmoi, or a dotfiles repo, `integrate` resolves it and atomically writes to the real target without replacing the symlink.
2. **Clean out-of-tree backups**: Pre-edit snapshots are saved under `~/.cache/agent-voice-input/integrate-backups/` (`0700`/`0600`), never leaving untracked `.bak` files inside dotfiles git repositories.
3. **Idempotent merge & comment-safe TOML**: Existing user hooks (such as `PreToolUse` / `Stop`) and Codex TOML comments/tables are preserved; Codex edits are validated via `parse_toml()` before atomic replacement.
4. **Zero extra `.sh` files & uninstall-safe inline commands**: Claude Code and AGY use self-contained inline `sh -c` commands with `PATH` / `$HOME/.local/bin` fallback—if `agent-voice-input` is later uninstalled, hooks fail silently without errors, Claude `stdout` stays empty, and AGY hooks always emit `{}`.

---

## Manual Configuration Examples

If you prefer managing configs by hand (for example, inside a shared dotfiles repo), the sections below show the exact equivalent configuration for each CLI.

### 1. Codex CLI (`~/.codex/config.toml`)

Merge `terminal_title` from [`codex/config.toml`](codex/config.toml) into the `[tui]` table of `~/.codex/config.toml` (or run `agent-voice-input integrate --cli codex`):

```toml
[tui]
terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]
```

Codex appends the root session's `thread-id` UUID to the terminal title, allowing `agent-voice-input` to locate the exact `rollout-*.jsonl` file immediately.

---

### 2. Claude Code (`~/.claude/settings.json`)

Add `SessionStart` and `UserPromptSubmit` hooks in `~/.claude/settings.json` (or run `agent-voice-input integrate --cli claude`; see also [`claude/settings.hooks.json`](claude/settings.hooks.json) and [`claude/voice-title.sh`](claude/voice-title.sh)). The recommended inline command checks `PATH` and `$HOME/.local/bin`, runs silently, and never errors if the binary is absent:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] && \"$b\" session-hook --cli claude --quiet >/dev/null 2>&1 || true'"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] && \"$b\" session-hook --cli claude --quiet >/dev/null 2>&1 || true'"
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

### 3. Antigravity CLI (`~/.gemini/config/hooks.json`)

Add `voice-session-title` to `~/.gemini/config/hooks.json` (or run `agent-voice-input integrate --cli agy`; see also [`gemini/hooks.json`](gemini/hooks.json); legacy `~/.gemini/hooks.json` and `~/.gemini/settings.json` remain recognized by `doctor`). The recommended inline command guarantees `{}` is always printed even if `agent-voice-input` is uninstalled:

```json
{
  "voice-session-title": {
    "SessionStart": [
      {
        "type": "command",
        "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; if [ -x \"$b\" ]; then \"$b\" session-hook --cli agy --quiet >/dev/null 2>&1 || true; fi; printf \"{}\\n\"'",
        "timeout": 5
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; if [ -x \"$b\" ]; then \"$b\" session-hook --cli agy --quiet >/dev/null 2>&1 || true; fi; printf \"{}\\n\"'",
        "timeout": 5
      }
    ]
  }
}
```

The hook checks `presence/<cid>.lock` file locks, the opening JSONL record, and the parent process chain (`/proc` on Linux or `ps` on macOS) to filter out background subagents and headless `agy -p` invocations—recording the normalized `tty` (and `tmux_pane` metadata when inside `tmux`) in `<runtime_dir>/sessions/agy-<cid8>.json`, updating the controlling TTY / pane title (`agy · <short_cwd> [#<cid8>]`) without changing pane focus only for foreground interactive sessions, and always returning a clean `{}` JSON response.

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
