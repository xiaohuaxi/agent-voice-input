**English** | [简体中文](https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.zh-CN.md)

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)

**Context-aware voice input for CLI coding agents—grounded in your live session, repo, and SSH hosts.**  
Supports **macOS** and **Windows + WSL2**. Built for **Codex**, **Claude Code**, and **Antigravity CLI (`agy`)**, while working out of the box across desktop IDEs and everyday apps.

```text
🎙️ What you say casually to your terminal:
   "Uh check the default retry timeout in session router dot py, change it from
    five seconds to ten seconds—actually wait, make it fifteen seconds—and run
    the unit tests without piping to head"

❌ Generic OS dictation:
   Uh check the default retry timeout in session router dot pie change it from 5 seconds to 10 seconds actually wait make it 15 seconds and run the unit tests without piping to head

✅ agent-voice-input (grounded in active Agent session & repo, pasted without Enter):
   Check the default of retry_timeout_sec in session_router.py, change it to 15 seconds, and run the unit tests without piping to head.
```

- **Grounded in Your Active Context**: Automatically reads the focused terminal, Ghostty split, `tmux` pane, desktop IDE workspace, or whitelisted SSH host—fixing filenames, symbols, and mid-sentence self-corrections with 50K block prefix caching.
- **Anti-Crosstalk & Paste-Only Safety**: Locks onto the origin window on record start, injects directly without touching your clipboard in macOS Ghostty/`tmux` (snapshot & restore elsewhere), falls back to clipboard if focus drifts, and never auto-presses Enter.
- **Lightweight Pure Standard Library**: Zero third-party Python dependencies (pure Python `>= 3.10` + host `ffmpeg` + thin native shims, no Electron), with local silence gating, long-pause compression, and `retry` recovery for failed clips.
- **100% Non-Interactive CLI**: Zero `y/n` prompts and structured `--json` diagnostics, designed for both humans and coding agents.

---

## Quick Start

### Option A: Let Your Coding Agent Install & Configure It (Recommended)

Paste this single sentence into **Claude Code**, **Codex**, **Antigravity (`agy`)**, or **Cursor**:

```text
Follow https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md to install and configure agent-voice-input on this machine (check ffmpeg/uv as needed), then walk me through my first voice dictation test.
```

### Option B: Manual Setup

**1. Install** *(handles Python `>= 3.10` and PEP 668 automatically via `uv`; no `sudo`, no shell rc edits)*:

```bash
(tmp=$(mktemp) && trap 'rm -f "$tmp"' EXIT && curl -fsSL https://raw.githubusercontent.com/xiaohuaxi/agent-voice-input/main/install.sh -o "$tmp" && sh "$tmp")
```

*(Already have `uv`? Run `uv tool install --python '>=3.10' agent-voice-input` directly. Need `ffmpeg`? macOS: `brew install ffmpeg` + `xcode-select --install`; WSL2: `sudo apt update && sudo apt install -y ffmpeg`.)*

<details>
<summary>Zero-dependency install from git clone (Python &gt;= 3.10 already installed)</summary>

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git && mkdir -p ~/.local/bin
ln -s "$PWD/agent-voice-input/bin/agent-voice-input" ~/.local/bin/agent-voice-input
```

</details>

**2. Initialize, add 1 API key & start** *(remember to remove the leading `#` when editing `.env`)*:

```bash
# 1) Create ~/.agent-voice-input/ (config.toml, .env, starter hotwords/personal.txt)
agent-voice-input init

# 2) Uncomment and add 1 ASR key in ~/.agent-voice-input/.env
#    Mainland China: DASHSCOPE_API_KEY or DEEPSEEK_API_KEY
#    Global:         OPENROUTER_API_KEY, OPENAI_API_KEY, or GEMINI_API_KEY
#    (Existing local `codex` ChatGPT subscription login is auto-reused first by `auto`,
#     never Fast; to pin Codex including API-key mode, set `refiner.provider = "codex"`.)

# 3) Verify connectivity & start daemon (first run compiles native shim)
agent-voice-input doctor --ping && agent-voice-input start
```

- **Default hotkeys**: MacBook built-in keyboards auto-default to **Right Command (`RCommand`)**; desktop Macs (e.g., Mac mini) and Windows/WSL2 default to **Right Ctrl (`RControl`)**. Tap `<= 0.5s` to start/stop; hold `0.8s` to cancel.
- **macOS note**: Always run `agent-voice-input start` from your **local GUI terminal** (not over SSH or `launchd`), and grant Microphone (+ Automation/Accessibility as prompted) to that terminal.

---

## Supported Setups & 1-Key Recipes

- **Zero hooks needed for ~80% of workflows**: macOS Ghostty (`✅`), `Terminal.app` (`🧪`), `iTerm2` (`🧪`), local/remote `tmux` (`✅`), Antigravity 2.0 / ChatGPT Codex / Claude Desktop (`✅` macOS), desktop IDEs (`🧪`), WSL2 single/distinct-dir windows (`✅`), and any everyday app (`⚪`) work out of the box. See the full [Compatibility Matrix & User Guide](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/guide.md).

| Scenario | Stage 1 (ASR) | Stage 2 (Refiner) | Key / Login Needed |
| :--- | :--- | :--- | :--- |
| **Reuse Codex / AGY Login** *(Best Quality)* | `dashscope`, `openrouter`, `doubao`, `openai`, or `deepseek` | `codex` *(`auto` prefers ChatGPT subscription login first, never Fast; set `refiner.provider = "codex"` to pin, including API-key mode)* or `agy` *(opt-in)* | 1 Stage 1 key + local `codex` / `agy` login |
| **Mainland China (CN / Mixed CN–EN)** | `dashscope` (`qwen3-asr-flash`, 150 terms / 1,800 chars) or `doubao` (`bigmodel`, 50 / 1,000) | `codex` / `agy` / `openrouter` *(or budget `deepseek` / `dashscope`)* | `DASHSCOPE_API_KEY`, or `DOUBAO_SPEECH_API_KEY` *(Stage 2 refinement needs another LLM key or local `codex`/`agy` login)* |
| **Global 1-Key (High Quality Two-Stage)** | `openrouter` (`microsoft/mai-transcribe-2`, ~$0.10/hr) or `openai` (`gpt-transcribe`) | `openrouter` (`google/gemini-3.8-flash`) or `openai` (`gpt-6-luna`) | `OPENROUTER_API_KEY` or `OPENAI_API_KEY` |
| **Minimal 1-Key Budget / Single-Hop** | `deepseek` *(two-stage)*, `dashscope` *(two-stage or `qwen3.8-omni-flash`)*, or `gemini` *(single-hop)* | Built-in default or skipped in single-hop | `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, or `GEMINI_API_KEY` |

Full 13-provider table, `auto` routing rules, and `config.toml` recipes: [Providers & Models Guide](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/providers.md).

---

## Privacy & Data-Flow Commitments

1. **No Author Servers, Zero Middleman**: No author-hosted servers, relays, or hidden telemetry. Audio and context go directly from your machine to the provider you explicitly configure (Apache-2.0 open source).
2. **No Audio or Transcripts Stored by Default**: Status snapshots, logs, and `diag.jsonl` redact secrets and never write speech transcripts, session context, or hotwords. Failed/cancelled-in-flight clips stash (`0600`, 24h TTL, max 10) in `failed/` for `retry`. Optional `[archive]` (`save_transcripts` 14-field JSONL with `agent`/`agent_session_id`, `save_audio` MP3) is off by default, `0700`/`0600`, and gitignored.
3. **`0700`/`0600` Isolation & No Invasive Scraping**: Reads only focused window metadata, plain-text CLI agent logs, and read-only AGY/Codex/Claude local session indexes—never VS Code/Cursor `state.vscdb` or chat/browser DOM trees. SSH probing queries only explicitly whitelisted hosts with zero remote keys.
4. **Scoped Context Only & Transparent `auto`**: Coding mode sends only recent active dialogue and `git status`/shallow filenames—never your full repo. Default `provider = "auto"` checks env/`.env` keys and prefers local `codex` ChatGPT subscription login first for Stage 2 (`agy` is opt-in only); to pin Codex (including API-key mode) or another provider, set explicit providers (e.g., `refiner.provider = "codex"` or `"off"`).

---

## Daily CLI Cheatsheet

```bash
agent-voice-input init [--asr P] [--refiner P] [--hotkey K]   # Initialize ~/.agent-voice-input/
agent-voice-input start | restart | stop                      # Manage background daemon
agent-voice-input status [--history N] [--json]               # Health, active pipeline, archive & hotwords
agent-voice-input doctor [--ping] [--remote] [--json]         # Audio, shim, credentials, hooks & live ping
agent-voice-input diagnose [--last N] [--json]                # Latency p50, cache hits & redacted log tail
agent-voice-input retry [--list] [--print]                    # Recover stashed failed recordings
agent-voice-input hotwords list|add|remove|path               # Manage personal.txt (!pin & <- aliases)
agent-voice-input once --duration 4 --dry-run                 # Test capture & refinement without pasting
agent-voice-input integrate [--status] [--dry-run] [--json]   # Optional multi-window CLI hooks
agent-voice-input key-test                                    # Inspect raw key scancodes / modifiers
```

- **Personal hotwords**: Edit `~/.agent-voice-input/hotwords/personal.txt` (or run `agent-voice-input hotwords add Term --pin --alias ...`) to pin project names and fix recurring mishearings; hot-reloads on the next utterance.
- **Upgrade / Uninstall**: Upgrade via `uv tool upgrade agent-voice-input && agent-voice-input restart` (or re-run `install.sh`); uninstall via `agent-voice-input stop && uv tool uninstall agent-voice-input`.
- **Terminal auto-start** *(never use macOS `launchd`)*: Add `[ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1` to `~/.zshrc` or `~/.bashrc`.

---

## Documentation

- [AI Agent Setup & Troubleshooting Contract](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md) — deterministic setup guide for Coding Agents
- [Providers, Models & `config.toml` Recipes](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/providers.md) — all 13 providers, two-stage vs. single-hop, auto-routing
- [User Guide, Compatibility Matrix, Hotwords & FAQ](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/guide.md) — macOS TCC, WSL2 WASAPI, hotwords, 14-field archiving, CLI flags, PEP 668 & proxy FAQ
- [Optional Multi-Window Hooks & SSH Remote Setup](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md) · [Context Routing Architecture (zh-CN)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/context-routing.md)
- Templates: [`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml) · [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env) · [`install.sh`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/install.sh)

## Security, Acknowledgments & License

- **Security**: See [SECURITY.md](https://github.com/xiaohuaxi/agent-voice-input/blob/main/SECURITY.md) for vulnerability reporting and security boundaries.
- **Acknowledgments**: Early exploration was inspired by [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) (pairing `Qwen3-ASR` with technical terminology context, silence gating, and loudness normalization).
- **License**: [Apache-2.0 License](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)
- **Offline tests (source checkout)**: `PYTHONPATH=src python3 -m unittest discover -s tests -v`
