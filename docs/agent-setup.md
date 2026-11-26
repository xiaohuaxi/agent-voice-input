# AI Agent Setup & Troubleshooting Guide (`agent-voice-input`)

> **Audience**: Coding agents (Claude Code, Codex, Antigravity CLI `agy`, etc.) installing, configuring, or troubleshooting `agent-voice-input` for a user.  
> **Design Contract**: Every `agent-voice-input` subcommand is **100% non-interactive** (zero `y/n` prompts) and supports `--json` on diagnostic/integration commands.

## Hard Safety Rules (Read First)

1. **Never leak secrets**: Never print, echo, log, or commit raw API keys in chat output or git repositories. Write credentials only to `0600` `~/.agent-voice-input/.env` (or `$AGENT_VOICE_INPUT_HOME/.env`).
2. **Never enable Codex Fast mode**: Do not set `service_tier="fast"` or enable Fast mode via any config, flag, or environment variable.
3. **Never hand-edit third-party CLI hook files**: Do **not** manually patch `~/.claude/settings.json`, `~/.gemini/config/hooks.json`, or `~/.codex/config.toml`. Always use `agent-voice-input integrate`, which preserves dotfiles symlinks, writes out-of-tree backups, and validates schemas.
4. **macOS SSH daemon rule**: Never run `agent-voice-input start` or `restart` over SSH (`$SSH_CONNECTION` / `$SSH_TTY` set) on macOS. Daemons started under `sshd` lack Microphone TCC attribution and record all-zero silence; instruct the user to run `start` in their local GUI terminal (Ghostty / `Terminal.app`).

---

## Step 1: Check Platform & Prerequisites

Inspect the host first (`uname -s`, `/proc/version`, `python3 --version`, `command -v ffmpeg`):

- **macOS (Apple Silicon / Intel)**: Full daemon + Retina HUD + system mic.
  - Requires `python3 >= 3.10`, `ffmpeg` (`brew install ffmpeg`), and Xcode Command Line Tools `/usr/bin/swiftc` (`xcode-select --install`).
  - If running inside an SSH session on macOS, you may install, `init`, and run `doctor --ping --json`, but stop before `start` and ask the user to run `agent-voice-input start` in a local GUI terminal.
- **Windows + WSL2**: Full daemon + WASAPI capture + Per-Monitor V2 HUD.
  - Requires `python3 >= 3.10` and `ffmpeg` (`sudo apt install ffmpeg`). Windows `powershell.exe` is resolved automatically even when `appendWindowsPath` is off.
- **Native Linux Desktop / Standalone Windows (no WSL2)**:
  - The global hotkey daemon and `toggle` are **not supported natively** yet. Native Linux only supports offline transcription (`agent-voice-input once --file <wav> --dry-run`) or serving as a headless SSH remote target (needs `python3 >= 3.10` only—do not install the package on remote SSH hosts unless explicitly requested). Inform the user and stop if they expect a local Linux desktop hotkey daemon.

Check `PATH`: `uv tool install` and `pipx install` place binaries in `~/.local/bin`. Confirm `command -v agent-voice-input` resolves (or prepend `~/.local/bin` to `PATH` and note it for the user's shell rc).

---

## Step 2: Install & Initialize

```bash
# Install (prefer uv; fall back to pipx, then pip)
uv tool install agent-voice-input        # fallback: pipx install agent-voice-input

# Create ~/.agent-voice-input/ (0600 config.toml & .env + default-ignore .gitignore tracking only hotwords/)
agent-voice-input init
```

Optional non-interactive flags on `init`: `--asr <provider>`, `--refiner <provider>`, `--hotkey <key>`, `--force`.

**Keyboard / Hotkey Rules**:
- On **MacBook** built-in keyboards (no Right Ctrl), `init` auto-detects the laptop and sets `key = "ROption"` (Right Option; `"Fn"` is also supported).
- On **desktop Macs** (Mac mini, Mac Studio, iMac) or any Mac using an **external 87/104-key keyboard**, and on **Windows/WSL2**, keep `key = "RControl"`. **Do not** overwrite `RControl` to `ROption` on desktop Macs or when the user already has an external keyboard configured.

Read `init`'s stdout summary carefully: it performs a zero-network local check of existing credentials and local `codex` CLI login and prints whether the pipeline is already ready out of the box.

---

## Step 3: Credentials, Provider Routing & Proxy Decision Tree

### Built-in Auto-Routing Reference
- **`asr.provider = "auto"`** priority: `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow`.
- **`refiner.provider = "auto"`** priority: local `codex` CLI login first (zero extra Stage 2 API cost; never enables Fast mode), else `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, else `"off"`.
- **`agy` (Antigravity CLI)**: explicit opt-in only (`refiner.provider = "agy"` in `config.toml`); never auto-selected.
- **OpenRouter default refiner model**: `google/gemini-3.8-flash`.

### Choosing & Writing Keys
1. **If `init` already reports a valid Stage 1 key** (and optionally local `codex` login for Stage 2): do not ask for another key unless the user requested a specific provider.
2. **If no Stage 1 key is configured**: ask the user once which single provider they prefer (mentioning if local `codex` login was detected to cover Stage 2 for free):
   - **Mainland China (国内首选 1 把 Key)**: `DASHSCOPE_API_KEY` (strongest CN/EN two-stage) or `DEEPSEEK_API_KEY` (full two-stage with 1 key). `DOUBAO_SPEECH_API_KEY` is also supported for Stage 1 (single-header `X-Api-Key`, no `APP_ID` needed).
   - **Global / Overseas (海外首选 1 把 Key)**: `OPENAI_API_KEY` (two-stage `gpt-transcribe` + `gpt-6-luna`) or `GEMINI_API_KEY` (native single-hop).
   - **Aggregator**: `OPENROUTER_API_KEY` (two-stage or single-hop; default refiner `google/gemini-3.8-flash`).
   - *Gotcha*: `ARK_API_KEY` (`volcengine`) is **Stage 2 only** and cannot drive Stage 1 ASR.
3. Write the key into `~/.agent-voice-input/.env` and verify permissions remain `0600`.

### Proxy Rules
- The pure-stdlib HTTP client only supports `http://` proxies via `HTTP_PROXY` / `HTTPS_PROXY` (pair with `NO_PROXY=127.0.0.1,localhost`).
- If the shell only exports `ALL_PROXY=socks5://...`, export the proxy client's HTTP mixed port (e.g., `export HTTPS_PROXY=http://127.0.0.1:<port>`) before `doctor --ping` and `start`/`restart`.

---

## Step 4: Optional CLI Hooks (Do Not Over-Configure)

Always inspect read-only status first:

```bash
agent-voice-input integrate --status --json
```

**When to SKIP `integrate` (~80% of users)**:
- macOS **Ghostty**, **`Terminal.app`**, or **`iTerm2`**
- Any platform running inside **`tmux`** (works without editing `~/.tmux.conf`)
- All **desktop apps** (Antigravity 2.0, ChatGPT/Codex Desktop, Claude Desktop, VS Code, Cursor, browsers, IM)
- **WSL2** single-window or multiple windows in **different directories**

**When to RUN `integrate`**:
- **WSL2 Windows Terminal** without `tmux` when running multiple windows in the **same project directory** (or generic `Ubuntu`/`bash` titles).
- **macOS terminals without `tty` APIs** (WezTerm, Kitty, Alacritty, Warp) with multiple same-directory windows outside `tmux`.
- Or whenever the user explicitly asks to install CLI hooks.

**Exact Workflow (never hand-edit JSON/TOML)**:
```bash
agent-voice-input integrate --dry-run --json        # preview exact changes (or add `--cli claude|codex|agy`)
agent-voice-input integrate --json                  # apply idempotently
```
`integrate` automatically resolves dotfiles symlinks to real targets, saves backups in `~/.cache/agent-voice-input/integrate-backups/` (keeping dotfiles git repos clean), preserves existing user hooks and TOML comments, uses self-healing inline commands (no external `.sh` files), and guarantees AGY hooks always output `{}`. Roll back anytime with `agent-voice-input integrate --uninstall`.

---

## Step 5: Verify, Start & Smoke Test

```bash
# 1. Full health check & live endpoint ping
agent-voice-input doctor --ping --json

# 2. Start daemon (local GUI terminal on macOS; never over macOS SSH)
agent-voice-input start
```

Check `doctor --ping --json` for any `FAIL` items and follow their `Fix:` guidance. Once `start` reports ready, invite the user to verify end-to-end:
- **Live check**: Focus a scratch prompt/window and tap the hotkey (`ROption` / `RControl`, `<= 0.5s`) to dictate one short sentence, then tap again to paste (never auto-presses Enter).
- **Isolated dry-run**: Run `agent-voice-input once --duration 4 --dry-run` to confirm microphone capture, STT, and refinement without pasting.

---

## Step 6: Fast Troubleshooting Cheatsheet

| Symptom | Cause & Fix |
| :--- | :--- |
| `agent-voice-input: command not found` | `~/.local/bin` is missing from `PATH`. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` or `~/.bashrc` and open a new shell. Official `integrate` inline hooks already fall back to `$HOME/.local/bin/agent-voice-input`. |
| macOS records silence / empty audio | ① Started over SSH (`sshd` has no Mic TCC): stop and run `start` in a local GUI terminal. ② Missing Microphone permission (麦克风权限): System Settings → Privacy & Security → Microphone → enable your terminal app, then restart the terminal. |
| macOS paste fails in GUI apps / non-Ghostty terminals | Grant **Accessibility** (辅助功能) to the terminal running the daemon, and **Automation** (自动化) for Ghostty / `Terminal.app`. **Input Monitoring** (输入监控) is **not** required for modifier hotkeys (`RControl` / `ROption` / `Fn`). |
| `status` or `doctor` reports `code_drift` | The package was upgraded while an older daemon was still running. Run `agent-voice-input restart` (in a local GUI terminal on macOS). |
| WSL2 missing keys or proxy in daemon | Put API keys in `~/.agent-voice-input/.env` (loaded directly by the daemon regardless of shell rc). For proxy env vars defined in `~/.bashrc`/`~/.zshrc`, run `agent-voice-input restart` from an interactive login shell. |
| Slow responses, cache misses, or failed clips | Run `agent-voice-input diagnose --json` for latency p50, cache hit rate, and redacted logs; run `agent-voice-input retry --list` to recover stashed failed recordings. |
