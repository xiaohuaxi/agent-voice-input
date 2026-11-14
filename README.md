[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

Context-aware voice input for CLI coding agents (such as **Codex**, **Claude Code**, and **Antigravity CLI / `agy`**).

Instead of treating terminal dictation like generic chat input, `agent-voice-input` pairs fast speech recognition with lightweight refinement grounded in your active coding session—fixing homophone identifiers, mixed Chinese–English technical terms, and spoken self-corrections before pasting safely into the terminal.

> **Early Release (`v0.1.0`)**: This project currently provides a foundational setup verified primarily on **Windows Terminal + WSL2 (including Remote Desktop)**. Broader terminal support, deeper `tmux` integration, native macOS terminal polish, and more flexible Stage 2 model choices are actively in progress.

---

## How It Works

1. **Active Session Context**: Locates the foreground agent session (via optional terminal title tags or `/proc` root-session scanning while ignoring background subagents) and extracts recent dialogue plus `git status` filenames as context.
2. **Stage 1 — Speech Recognition**: Applies a local silence gate (`-50 dBFS`) and RMS loudness normalization (`-20 dBFS`), then transcribes audio via cloud ASR (defaults to Alibaba Bailian `qwen3-asr-flash`; also supports Volcengine Doubao, Gemini, or OpenAI-compatible endpoints).
3. **Stage 2 — Lightweight Context Refinement**: Cleans up technical terms and explicit spoken self-corrections against the extracted session context. For now, Stage 2 defaults to invoking a lightweight model through local Codex CLI (`codex-luna`), and can also be pointed to an OpenAI-compatible API or turned off (`off`).
4. **Focus-Safe Paste**: Shows recording/recognition/refinement stages in a non-activating HUD pill on Windows, verifies terminal focus right before pasting (never auto-pressing Enter), and keeps text in the clipboard only if you switched to another window.
5. **Lightweight Setup**: Currently relies only on the Python 3.10+ standard library plus host `ffmpeg` (and built-in `powershell.exe` on WSL2), requiring no extra Python packages.

---

## Quick Start

### 1. Prerequisites

- **Python** `>= 3.10`
- **ffmpeg** (`sudo apt install ffmpeg` on Ubuntu/WSL2; `brew install ffmpeg` on macOS)

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

Other supported credentials include `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` (Doubao), `GEMINI_API_KEY`, and `OPENAI_API_KEY`. To customize hotkeys, ASR models, or Stage 2 refinement (such as running without Codex CLI via `refiner.provider = "openai-compatible"` or `"off"`), copy [`config.example.toml`](config.example.toml) to `~/.config/agent-voice-input/config.toml` (also see [`config.example.env`](config.example.env)).

### 4. Verify & Run

```bash
# Check audio bridge, credentials, and session detection
agent-voice-input doctor

# Start background daemon (defaults to isolated Right Ctrl tap on WSL2)
agent-voice-input start

# Other common commands
agent-voice-input status
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input stop
```

### Optional: Multi-Window Session Title Hooks

If you run multiple agent windows side by side in the same directory, see [`integrations/`](integrations/README.md) for optional terminal title hooks for Codex, Claude Code, and Antigravity CLI.

---

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Acknowledgments

Early exploration for this project was inspired by [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr), particularly around pairing `Qwen3-ASR` with technical terminology context biasing for mixed Chinese–English speech, as well as pre-transcription silence gating and loudness normalization.

## License

[Apache-2.0 License](LICENSE)
