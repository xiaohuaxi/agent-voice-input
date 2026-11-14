[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

Context-aware voice input for CLI coding agents (such as **Codex**, **Claude Code**, and **Antigravity CLI / `agy`**).

Instead of treating terminal dictation like generic chat input, `agent-voice-input` pairs fast speech recognition with lightweight refinement grounded in your active coding session—fixing homophone identifiers, mixed Chinese–English technical terms, and spoken self-corrections before pasting safely into the terminal.

> **Early Release (`v0.1.0`)**: Verified on **Windows Terminal + WSL2 (including Remote Desktop)** and **macOS + Ghostty**. Broader terminal support, deeper `tmux` integration, and more flexible Stage 2 model choices are actively in progress.

---

## How It Works

1. **Active Session Context**: Locates the foreground agent session (via optional terminal title tags, Ghostty split `tty`/`cwd` matching on macOS, or `/proc` root-session scanning on Linux while ignoring background subagents) and extracts recent dialogue plus `git status` filenames as context.
2. **Stage 1 — Speech Recognition**: Applies a local silence gate (`-50 dBFS`) and RMS loudness normalization (`-20 dBFS`), then transcribes audio via cloud ASR (defaults to Alibaba Bailian `qwen3-asr-flash`; also supports Volcengine Doubao, Gemini, or OpenAI-compatible endpoints).
3. **Stage 2 — Lightweight Context Refinement**: Cleans up technical terms and explicit spoken self-corrections against the extracted session context. For now, Stage 2 defaults to invoking a lightweight model through local Codex CLI (`codex-luna`), and can also be pointed to an OpenAI-compatible API or turned off (`off`).
4. **Focus-Safe Paste**: Shows recording/recognition/refinement stages in a non-activating top HUD pill (Windows & macOS, automatically matching Chinese or English desktop UI language), locks onto the origin terminal window/split when recording begins, never auto-presses Enter, and falls back to clipboard-only if focus drifted elsewhere.
5. **Lightweight Setup**: Relies only on the Python 3.10+ standard library plus host `ffmpeg` (built-in `powershell.exe` on WSL2; cached native `mac_shim.swift` helper via `/usr/bin/swiftc` on macOS), requiring no extra Python packages.

---

## Quick Start

### 1. Prerequisites

- **Python** `>= 3.10`
- **ffmpeg**:
  - Ubuntu / WSL2: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
- **macOS native hotkey & HUD helper** (recommended on macOS): Xcode Command Line Tools (`xcode-select --install`) providing `/usr/bin/swiftc`. `mac_shim.swift` is compiled idempotently and cached on first start; if `swiftc` is absent, `agent-voice-input` automatically falls back to CLI `toggle` / `once` commands.

### 2. Install

```bash
uv tool install agent-voice-input
# Or with pipx / pip:
# pipx install agent-voice-input
```

For local development from source:

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git ~/repos/agent-voice-input
mkdir -p ~/.local/bin
ln -s ~/repos/agent-voice-input/bin/agent-voice-input ~/.local/bin/agent-voice-input
```

### 3. Configure

Create `~/.config/agent-voice-input/agent-voice-input.env` (or export variables in your shell) with at least one Stage 1 ASR key:

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
ALI_API_KEY="<your-dashscope-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env
```

Other supported credentials include `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` (Doubao), `GEMINI_API_KEY`, and `OPENAI_API_KEY`. To customize hotkeys (`trigger_mode = "tap_toggle"` or `"ptt"`), HUD language (`[ui] language = "auto" | "zh" | "en"`, or `AGENT_VOICE_INPUT_UI_LANGUAGE`), macOS audio input (`[audio] macos_device = "default"` or `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`), ASR models, or Stage 2 refinement (such as `refiner.provider = "openai-compatible"` or `"off"`), copy [`config.example.toml`](config.example.toml) to `~/.config/agent-voice-input/config.toml` (also see [`config.example.env`](config.example.env)).

### 4. Verify & Run

```bash
# Check audio bridge, mac_shim/win_shim status, credentials, and session detection
agent-voice-input doctor

# Start background daemon (defaults to isolated Right Ctrl tap on WSL2 & macOS)
agent-voice-input start

# Other common commands
agent-voice-input status
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input stop
```

---

## macOS + Ghostty Experience & Permissions

On macOS with **Ghostty**, `agent-voice-input` works out of the box without terminal hooks or invasive input permissions:

- **Hotkey & Top Capsule HUD**: Tap Right Ctrl (`RControl`) to toggle recording—or set `trigger_mode = "ptt"` to hold-to-speak—with a non-activating top HUD pill showing recording, recognition, and refinement progress.
- **Native Split Targeting & Anti-Drift Guard**: Locks onto the exact focused Ghostty split surface when recording starts and injects bracketed-paste text directly into that split without Accessibility synthetic keystrokes; if you switch splits or apps mid-transcription, it safely copies to the clipboard instead of pasting into the wrong target.
- **Zero-Hook Split Session Binding**: Uses the focused Ghostty split's `tty` and `cwd` to match the active `codex`, `claude`, or `agy` session in that pane, keeping multiple side-by-side splits isolated even without title hooks.
- **Audio Input Selection**: Defaults to `[audio] macos_device = "default"` (system default microphone); override via `macos_device` in `config.toml` or `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE` (device index or name, inspectable via `agent-voice-input doctor`).
- **macOS Permissions (TCC)**:
  - **Microphone**: Required for your terminal / background Python environment so `ffmpeg` (`avfoundation`) can record audio.
  - **Automation (Ghostty)**: Prompted once on first split query / text injection into Ghostty.
  - **Accessibility & Input Monitoring**: **Not required** for the default `RControl` + Ghostty setup. Only needed when pasting into non-Ghostty apps via simulated `Cmd+V` (Accessibility) or when binding non-modifier hotkeys / running `agent-voice-input key-test` (Input Monitoring / Accessibility).

### Optional: Multi-Window Session Title Hooks

If you run multiple agent windows side by side on WSL2, inside `tmux`, or in terminals that do not expose per-split `tty`/`cwd`, see [`integrations/`](integrations/README.md) for optional terminal title hooks for Codex, Claude Code, and Antigravity CLI.

---

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Acknowledgments

Early exploration for this project was inspired by [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr), particularly around pairing `Qwen3-ASR` with technical terminology context biasing for mixed Chinese–English speech, as well as pre-transcription silence gating and loudness normalization.

## License

[Apache-2.0 License](LICENSE)
