# AI Agent Setup & Troubleshooting Guide (`agent-voice-input`)

> **Audience**: Coding agents (Claude Code, Codex, Antigravity CLI `agy`, Cursor, etc.) installing, configuring, or troubleshooting `agent-voice-input` for a user.  
> **Design Contract**: Every `agent-voice-input` subcommand is **100% non-interactive** (zero `y/n` prompts) and supports `--json` on diagnostic/integration commands.

## Hard Safety Rules (Read First)

1. **Never leak secrets or guess keys**: Never print, echo, log, commit, or fabricate API keys. Ask the user to provide/enter their own key, and store credentials only in `0600` `~/.agent-voice-input/.env` (or `$AGENT_VOICE_INPUT_HOME/.env`).
2. **Never run silent `sudo` or mutate shell rc unasked**: Do not run `sudo` behind the user's back or silently edit `~/.zshrc`/`~/.bashrc`. If `ffmpeg` requires `sudo apt install` on WSL2, print the exact one-liner for the user to run (or ask explicit permission first).
3. **Never enable Codex Fast mode**: Do not set `service_tier="fast"` or enable Fast mode via any config, flag, or environment variable.
4. **Never hand-edit third-party CLI hook files**: Do **not** manually patch `~/.claude/settings.json`, `~/.gemini/config/hooks.json`, or `~/.codex/config.toml`. Always use `agent-voice-input integrate`.
5. **macOS SSH / `launchd` daemon rule**: Never run `agent-voice-input start` or `restart` over SSH (`$SSH_CONNECTION` / `$SSH_TTY` set) or via a `launchd` LaunchAgent on macOS. Daemons started under `sshd` or `launchd` lack GUI terminal Microphone/Automation TCC attribution and record all-zero silence; instruct the user to run `start` in their local GUI terminal (e.g., Ghostty or `Terminal.app`).

---

## Deterministic Setup Decision Tree

Follow these steps in order—keep the same `uv` install path for both Python and non-Python users so there are no PEP 668 or system Python 3.9 detours.

### Step 1: Detect Host Platform

Check `uname -s` and `/proc/version`:
- **macOS (Apple Silicon / Intel)**: Full daemon + Retina HUD + system mic. If `$SSH_CONNECTION` or `$SSH_TTY` is set, you may complete Steps 2–4 and `doctor --ping --json`, then stop before `start` and instruct the user to run `agent-voice-input start` in a local GUI terminal.
- **Windows + WSL2** (`/proc/version` matches `microsoft` / `WSL` indicates WSL; confirm the distro is **WSL2**, not WSL1, before running `start`): Full daemon + WASAPI capture + Per-Monitor V2 HUD. Windows `powershell.exe` is auto-resolved even when `appendWindowsPath` is off.
- **Native Linux Desktop / Standalone Windows (no WSL2)**: Global hotkey daemon and `toggle` are **not supported natively** yet. Native Linux only supports one-shot file transcription without local hotkeys/mic (`agent-voice-input once --file <wav> --dry-run` — still calls your configured cloud ASR/refiner provider; `--dry-run` only skips pasting) or serving as a headless SSH remote target (needs `python3 >= 3.10` only—do not install the package on remote SSH hosts unless explicitly requested). Inform the user and stop if they expect a local hotkey daemon.

### Step 2: Install CLI via `install.sh` or `uv` & Verify Host Tools

1. **Install the package** (one deterministic path—handles Python `< 3.10`, PEP 668, and Ubuntu missing `python3-venv`):
   - First locate `uv` across `PATH` and official user install directories (`$UV_INSTALL_DIR`, `$XDG_BIN_HOME`, `$XDG_DATA_HOME/../bin`, `$HOME/.local/bin`, `$HOME/.cargo/bin`, `/opt/homebrew/bin`, `/usr/local/bin`), assigning the discovered path to `UV_BIN` and always invoking `"$UV_BIN"`:
     ```bash
     UV_BIN="$(command -v uv 2>/dev/null || true)"
     for c in "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/uv}" "${XDG_BIN_HOME:+$XDG_BIN_HOME/uv}" "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin/uv}" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
       [ -n "$UV_BIN" ] && break
       [ -n "$c" ] && [ -x "$c" ] && UV_BIN="$c"
     done
     ```
   - If `UV_BIN` is found, install via `"$UV_BIN"`:
     ```bash
     "$UV_BIN" tool install --upgrade --python '>=3.10' agent-voice-input
     ```
   - Otherwise, download and run the repository's thin auditable installer (installs user-level `uv` with `UV_NO_MODIFY_PATH=1`, installs `agent-voice-input`, and checks host tools without `sudo`; download-then-execute preserves non-zero failure exit codes):
     ```bash
     (tmp=$(mktemp) && trap 'rm -f "$tmp"' EXIT && curl -fsSL https://raw.githubusercontent.com/xiaohuaxi/agent-voice-input/main/install.sh -o "$tmp" && sh "$tmp")
     ```
   - *(Source-checkout alternative for zero-dependency local dev: symlink `<repo>/bin/agent-voice-input` into `~/.local/bin/agent-voice-input` if `python3 >= 3.10` is already present.)*
2. **Verify installed binary & `PATH`**:
   - Resolve the tool bin directory via `BIN_DIR="$("$UV_BIN" tool dir --bin)"` (default `~/.local/bin` if `UV_BIN` was installed by `install.sh`; re-resolve `UV_BIN` via the same directory list above if needed) and confirm `"$BIN_DIR/agent-voice-input" --version` succeeds.
   - If `BIN_DIR` is not in `PATH`, use `"$BIN_DIR/agent-voice-input"` for remaining commands and print a one-liner with `BIN_DIR` expanded to the actual resolved directory path (leaving `$PATH` literal for their shell rc, e.g., `printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"` — never write literal `$BIN_DIR` into `~/.zshrc` or `~/.bashrc`).
   - If `command -v agent-voice-input` points to a stale path (e.g., an old symlink), alert the user.
3. **Check host prerequisites (`ffmpeg` and macOS `swiftc`)**:
   - **macOS**: Check `ffmpeg` (`PATH`, `/opt/homebrew/bin/ffmpeg`, `/usr/local/bin/ffmpeg`) and Command Line Tools via `xcode-select -p >/dev/null 2>&1 && xcrun --find swiftc >/dev/null 2>&1` (do not rely on `[ -x /usr/bin/swiftc ]`, which is an executable stub on fresh Macs without CLT).
     - If `ffmpeg` is missing and `brew` is available, install with `brew install ffmpeg` (no `sudo`) or ask the user to run it; if `brew` is absent, give the `brew install ffmpeg` / Homebrew link.
     - If `swiftc` is missing, instruct the user to run `xcode-select --install` (GUI prompt) and resume once installed.
   - **WSL2 (Ubuntu/Debian)**: Check `command -v ffmpeg`. If missing, do **not** run silent `sudo`; give the user the exact command:
     ```bash
     sudo apt update && sudo apt install -y ffmpeg
     ```

### Step 3: Initialize & Respect Keyboard Defaults

```bash
agent-voice-input init
```

Optional non-interactive flags: `--asr <provider>`, `--refiner <provider>`, `--hotkey <key>`, `--force` (`--force` overwrites `config.toml` only; always preserves `.env`, `hotwords/personal.txt`, and `.gitignore`).

- **MacBook built-in keyboards**: Auto-detected to `key = "RCommand"` (Right Command). Avoid `ROption` (Doubao IME), `Fn` (macOS 🌐 Globe key unless set to Do Nothing), and `RShift` (Chinese IME toggle).
- **Desktop Macs** (Mac mini, Mac Studio, iMac, or external 87/104-key keyboard) & **Windows/WSL2**: Keep `key = "RControl"`. Do **not** overwrite `RControl` to `RCommand` on desktop Macs or external keyboards.

Read `init`'s stdout carefully: it runs a zero-network local check of existing env keys and local `codex` CLI login and tells you immediately if the pipeline is ready.

### Step 4: Credentials, Provider Routing & Proxy

- **`asr.provider = "auto"`** priority: `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow`.
- **`refiner.provider = "auto"`** priority: local `codex` CLI in `chatgpt` subscription login mode first (never enables Fast mode), then API-key providers `openrouter -> deepseek -> dashscope -> gemini -> volcengine -> openai -> groq -> siliconflow`, then `codex` in `keyring` / `api_key` mode, else `"off"`. Set `refiner.provider = "codex"` explicitly to pin Codex when other API keys are configured. (`agy` is explicit opt-in only via `refiner.provider = "agy"`.)
- **OpenRouter defaults**: Two-stage `microsoft/mai-transcribe-2` (~`$0.10/hr`, native `keyterms`) + `google/gemini-3.8-flash` (`effort = "low"`). Prefer two-stage over single-hop on OpenRouter.

**Credential checklist**:
1. If `init` already found a valid Stage 1 key (and optionally local `codex` login), skip asking for more keys unless the user requested a specific provider.
2. Otherwise, ask the user which single Stage 1 provider they want and guide them to put their key into `~/.agent-voice-input/.env` (**remind them to remove the leading `#` comment character** and keep `0600` permissions):
   - **Mainland China**: `DASHSCOPE_API_KEY` (strongest CN/EN two-stage, 150-term / 1,800-char hotwords), `DEEPSEEK_API_KEY` (budget 1-key two-stage), or `DOUBAO_SPEECH_API_KEY` (50-term / 1,000-char hotwords, single-header `X-Api-Key`).
   - **Global / Overseas**: `OPENROUTER_API_KEY` (two-stage `microsoft/mai-transcribe-2` + `google/gemini-3.8-flash`), `OPENAI_API_KEY` (`gpt-transcribe` + `gpt-6-luna`), or `GEMINI_API_KEY` (native single-hop).
   - *Gotcha*: `ARK_API_KEY` (`volcengine`) is **Stage 2 only** and cannot drive Stage 1 ASR.
3. **Proxy**: Pure-stdlib HTTP client supports `HTTP_PROXY` / `HTTPS_PROXY` (`http://...`) plus `NO_PROXY=127.0.0.1,localhost`. If only `ALL_PROXY=socks5://...` is set, configure the proxy's HTTP mixed port in `HTTPS_PROXY` before `doctor --ping` and `start`/`restart`.

### Step 5: Optional CLI Hooks (Do Not Over-Configure)

Inspect read-only status first: `agent-voice-input integrate --status --json`.

- **SKIP `integrate` (~80% of setups)**: macOS Ghostty / `Terminal.app` / `iTerm2`, any `tmux` session, all desktop apps (Antigravity 2.0, ChatGPT/Codex Desktop, Claude Desktop, VS Code, Cursor, browsers, IM), and WSL2 single-window or distinct-directory windows.
- **RUN `integrate` only when needed**: WSL2 Windows Terminal without `tmux` running multiple windows in the **same project directory**, macOS terminals without `tty` APIs (WezTerm, Kitty, Alacritty, Warp) with same-directory windows outside `tmux`, or when explicitly requested:
  ```bash
  agent-voice-input integrate --dry-run --json
  agent-voice-input integrate --json
  ```

### Step 6: Health Check, Start & First Dictation Verification

```bash
# 1. Verify audio, shim, credentials, hotwords & live endpoint ping
agent-voice-input doctor --ping --json

# 2. Start daemon (ONLY in a local GUI terminal on macOS; never over macOS SSH)
agent-voice-input start
```

Wait through first-run native shim compilation (~5–15s on macOS; ~15–60s+ on WSL2 with Defender). Fix any `FAIL` items in `doctor --ping --json` first, then separate verification into two clear signals:
1. **Isolated mic + pipeline check**: Run `agent-voice-input once --duration 4 --dry-run` (in a local GUI terminal on macOS) and ask the user to speak a short phrase to verify real microphone capture, STT, and refinement without pasting.
2. **Live hotkey + paste check**: Once `start` is running, ask the user to focus a scratch input box/prompt, tap the hotkey (`RCommand` on MacBook, `RControl` on desktop Mac/WSL2, `<= 0.5s`) to dictate one sentence, and tap again to confirm refined paste (never auto-presses Enter).

### Daily Operations Reference

- **Idempotent terminal auto-start** (1 line in `~/.zshrc` / `~/.bashrc`; never macOS `launchd`):
  ```bash
  [ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1
  ```
- **Upgrade**: `uv tool upgrade agent-voice-input && agent-voice-input restart`
- **Uninstall**: `agent-voice-input stop`, optionally `agent-voice-input integrate --uninstall`, then `uv tool uninstall agent-voice-input`.

---

## Fast Troubleshooting Cheatsheet

| Symptom | Cause & Fix |
| :--- | :--- |
| `agent-voice-input: command not found` | `~/.local/bin` is missing from `PATH`. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc` and open a new shell. |
| macOS records silence / empty audio | ① Started over SSH (`sshd`) or `launchd`: stop and run `start` in a local GUI terminal. ② Missing Microphone permission: System Settings → Privacy & Security → Microphone → enable the terminal starting the daemon, then restart that terminal. |
| macOS paste fails in GUI apps / non-Ghostty terminals | Grant **Accessibility** to the starting terminal (also needed for window titles/session bindings in third-party terminals & desktop apps) and **Automation** for Ghostty / `Terminal.app` / `iTerm2`. **Input Monitoring** is **not** required for modifier hotkeys. |
| WSL2 cannot auto-paste into Administrator windows | Windows UIPI blocks non-elevated `win_shim` from pasting into elevated windows. The refined text is on the clipboard—press `Ctrl+V` manually. |
| `status` or `doctor` reports `code_drift` | Package upgraded while an older daemon was running. Run `agent-voice-input restart` (in a local GUI terminal on macOS). |
| WSL2 missing keys or proxy in daemon | Put API keys (and optional `HTTPS_PROXY`) in `~/.agent-voice-input/.env` (uncomment `#`), then `agent-voice-input restart`. |
| Slow responses, cache misses, or failed clips | Run `agent-voice-input diagnose --json` for latency p50, per-mode cache hit rate, and redacted logs; run `agent-voice-input retry --list` to recover stashed clips. |
