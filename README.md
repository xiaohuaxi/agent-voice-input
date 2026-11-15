[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

Context-aware voice input for CLI coding agents (such as **Codex**, **Claude Code**, and **Antigravity CLI / `agy`**).

Instead of treating terminal dictation like generic chat input, `agent-voice-input` pairs fast speech recognition with lightweight refinement grounded in your active coding session—fixing homophone identifiers, mixed Chinese–English technical terms, and spoken self-corrections before pasting safely into the terminal.

> **Early Release (`v0.2.1`)**: Verified on **Windows Terminal + WSL2 (including `tmux` split panes and Remote Desktop)** and **macOS + Ghostty (including `tmux`)**. Broader terminal coverage is actively in progress.

---

## How It Works

1. **Active Session Context**: Locates the foreground agent session—including the active pane inside `tmux` without editing `~/.tmux.conf` (via optional terminal/pane title tags, runtime session registry, Ghostty split `tty`/`cwd` matching on macOS, or `/proc` / `ps` root-session scanning while ignoring background subagents and sibling panes)—and incrementally extracts recent dialogue plus `git status` filenames as context.
2. **Stage 1 — Speech Recognition**: Applies a local silence gate (`-50 dBFS`) and RMS loudness normalization (`-20 dBFS`), automatically splits recordings longer than 3 minutes (`180s`) at natural silent pauses for concurrent transcription, stashes failed recordings under `failed/` for `retry` recovery, and transcribes audio via built-in cloud or local-compatible providers (`dashscope`, `doubao`, `openrouter` dual-protocol STT & multimodal audio, `gemini`, `openai`, `groq`, `siliconflow`, or `openai-compatible`).
3. **Stage 2 — Lightweight Context Refinement**: Cleans up technical terms, identifier casing, and explicit spoken self-corrections against the extracted session context. Defaults to local Codex CLI (`codex` with `gpt-6-luna`), and also supports `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, or `off` (automatically skipped when Stage 1 already performed single-hop multimodal refinement).
4. **Focus-Safe Paste**: Shows recording/recognition/refinement stages in a non-activating bottom HUD pill (configurable to `"top"`; Windows & macOS, automatically matching Chinese or English desktop UI language), starts hotkey recordings only in terminal windows by default, locks onto the origin terminal window/split when recording begins, never auto-presses Enter, and falls back to clipboard-only if focus drifted elsewhere.
5. **Zero Extra Python Dependencies**: Uses only the Python 3.10+ standard library plus host `ffmpeg` (built-in `powershell.exe` on WSL2; cached native `mac_shim.swift` helper via `/usr/bin/swiftc` on macOS).

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

### 3. Configure Credentials

Create `~/.config/agent-voice-input/agent-voice-input.env` (or export variables in your shell) with at least one provider key—for example, Alibaba Bailian (`DASHSCOPE_API_KEY`) or a single `OPENROUTER_API_KEY` covering both stages:

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
DASHSCOPE_API_KEY="<your-dashscope-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env
```

To customize hotkeys (`trigger_mode = "tap_toggle"` or `"ptt"`, `only_in_terminals = true | false`), HUD language & position (`[ui] language = "auto" | "zh" | "en"` and `position = "bottom" | "top"`, or `AGENT_VOICE_INPUT_UI_LANGUAGE` / `AGENT_VOICE_INPUT_UI_POSITION`), macOS audio input (`[audio] macos_device = "default"` or `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`), providers, models, or Stage 2 refinement, copy [`config.example.toml`](config.example.toml) to `~/.config/agent-voice-input/config.toml` (see [`config.example.env`](config.example.env) for all credential variables).

### 4. Verify & Run

```bash
# Check audio bridge, mac_shim/win_shim status, resolved providers/models, credentials, and session detection
agent-voice-input doctor

# Start background daemon (defaults to isolated Right Ctrl tap on WSL2 & macOS)
agent-voice-input start

# Other common commands
agent-voice-input status
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input retry --list
agent-voice-input stop
```

---

## macOS + Ghostty Experience & Permissions

On macOS with **Ghostty**, `agent-voice-input` works out of the box without terminal hooks or invasive input permissions:

- **Terminal-Scoped Hotkey & Bottom Capsule HUD**: Tap Right Ctrl (`RControl`) to toggle recording—or set `trigger_mode = "ptt"` to hold-to-speak (enabled only in terminal windows by default via `[hotkey] only_in_terminals = true`, while stopping/canceling an active recording works from any app)—with a non-activating bottom HUD pill (or `[ui] position = "top"`) showing recording, recognition, and refinement progress in Chinese or English matching your desktop locale.
- **Native Split Targeting & Anti-Drift Guard**: Locks onto the exact focused Ghostty split surface when recording starts and injects bracketed-paste text directly into that split without Accessibility synthetic keystrokes; if you switch splits or apps mid-transcription, it safely copies to the clipboard instead of pasting into the wrong target.
- **Zero-Hook Split & `tmux` Session Binding**: Uses the focused Ghostty split's `tty` and `cwd` (and the attached client's active pane when inside `tmux`) to match the active `codex`, `claude`, or `agy` session in that pane, keeping multiple side-by-side splits isolated even without title hooks.
- **Audio Input Selection**: Defaults to `[audio] macos_device = "default"` (system default microphone); override via `macos_device` in `config.toml` or `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE` (device index or name, inspectable via `agent-voice-input doctor`).
- **macOS Permissions (TCC)**:
  - **Microphone**: Required for your terminal / background Python environment so `ffmpeg` (`avfoundation`) can record audio.
  - **Automation (Ghostty)**: Prompted once on first split query / text injection into Ghostty.
  - **Accessibility & Input Monitoring**: **Not required** for the default `RControl` + Ghostty setup. Only needed when pasting into non-Ghostty apps via simulated `Cmd+V` (Accessibility) or when binding non-modifier hotkeys / running `agent-voice-input key-test` (Input Monitoring / Accessibility).

---

## Configuration & Providers

Configuration uses a clean two-file layout (`CLI flags > AGENT_VOICE_INPUT_* env vars > config.toml > defaults`):

- **`~/.config/agent-voice-input/config.toml`**: Structured `[hotkey]`, `[ui]`, `[audio]`, `[asr]`, and `[refiner]` settings (`provider`, `model`, `api`, `effort`, `base_url`, `api_key_env`, etc.).
- **`~/.config/agent-voice-input/agent-voice-input.env`**: API keys (and optional `AGENT_VOICE_INPUT_*` overrides).

### Structured `[asr]` & `[refiner]` Fields

- **`provider` & `model`**: `model` always keeps the upstream model ID verbatim (e.g. `"qwen/qwen3-asr-1.7b"` or `"google/gemini-2.5-flash"`). Leave `model = ""` to use the provider's built-in default. When `provider` is overridden at a higher-priority layer (env or CLI) without specifying `model`, any lower-layer `model` is cleared so the new provider's default model applies cleanly.
- **`asr.api` (`"auto" | "audio-transcriptions" | "chat-completions"`)**:
  - `"auto"` (default) uses native adapters for `dashscope`, `doubao`, and native `gemini`. On `openrouter`, `openai`, `groq`, `siliconflow`, and `openai-compatible`, it classifies known dedicated STT models (`qwen3-asr`, `whisper`, `*-transcribe`, `sensevoice`, `deepgram/*`, `assemblyai/*`, `elevenlabs/*`, etc.) to `"audio-transcriptions"` + Stage 2 refinement, and known multimodal audio models (`gemini-*`, `gpt-audio*`, `gpt-4o*audio*`, `*-omni*`, `mimo-*`, `voxtral-small*`, etc.) to `"chat-completions"` for single-hop transcription + context refinement (skipping Stage 2 by default unless `refiner.force_on_single_hop = true`). Note that `dashscope` `qwen3-asr-flash` uses a chat-compatible endpoint for pure STT and still runs Stage 2.
  - When testing a newly released, uncataloged model on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **`asr.effort` & `refiner.effort`**: Unified reasoning/thinking control (`"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`). `asr.effort` defaults to `"auto"` (suppressing default thinking latency on supported multimodal models such as Gemini 2.5 while omitting reasoning fields on pure STT/non-reasoning audio models); `refiner.effort` defaults to `"low"`; `"default"` omits reasoning parameters completely.

### Built-in Providers

| `provider` | Stages | Default `asr.model` | Default `refiner.model` | Recommended Credential Env |
| :--- | :--- | :--- | :--- | :--- |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` | `qwen-turbo-latest` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` |
| **`volcengine`** | `refiner` | — | `doubao-seed-1-6-flash` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-2.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-2.5-flash` | `gemini-2.5-flash-lite` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`codex`** | `refiner` | — | `gpt-6-luna` | Local `codex` CLI login *(never enables Fast mode)* |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |

- **Special values**:
  - `asr.provider = "auto"` (default): selects the first configured credential in order `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow`.
  - `refiner.provider = "off"`: disables Stage 2 refinement.
- Prefix-style credential aliases (`API_KEY_OPENROUTER`, `API_KEY_GEMINI`, `APP_ID_DOUBAO_ASR` + `API_KEY_DOUBAO_ASR`, etc.) are also supported; see [`config.example.env`](config.example.env).

---

## Common Configuration Recipes (`config.toml`)

### Recipe A — Low-Latency Mainland China Setup (`dashscope` + `codex` / `volcengine` / `dashscope`)

```toml
[asr]
provider = "dashscope"
model = "qwen3-asr-flash"

[refiner]
# Resident local Codex app-server (or switch provider to "volcengine" / "dashscope" for pure API refinement)
provider = "codex"
model = "gpt-6-luna"
effort = "low"
```

### Recipe B — OpenRouter Single Key (Two-Stage Dedicated STT + Refiner)

Uses `OPENROUTER_API_KEY` for both fast dedicated speech-to-text (`/audio/transcriptions`) and session-aware Stage 2 cleanup:

```toml
[asr]
provider = "openrouter"
model = "qwen/qwen3-asr-1.7b"

[refiner]
provider = "openrouter"
model = "google/gemini-2.5-flash-lite"
effort = "low"
```

### Recipe C — OpenRouter Single Key (Multimodal Single-Hop)

Routes audio + session context in a single `/chat/completions` hop with anti-instruction-hijacking guards and automatically skips Stage 2:

```toml
[asr]
provider = "openrouter"
model = "google/gemini-2.5-flash"
api = "auto"
effort = "auto"
```

### Recipe D — Native Gemini Single-Hop or Ultra-Fast Groq Whisper

```toml
# Option 1: Native Google Gemini (:generateContent single-hop audio + context; skips Stage 2)
[asr]
provider = "gemini"
model = "gemini-2.5-flash"
effort = "auto"
```

```toml
# Option 2: Groq Whisper STT + Groq Stage 2 Refiner
[asr]
provider = "groq"
model = "whisper-large-v3-turbo"

[refiner]
provider = "groq"
model = "llama-3.3-70b-versatile"
```

---

## CLI Flags & Environment Overrides

Common runtime flags accepted across `daemon`, `start`, `doctor`, `once`, `retry`, `status`, `toggle`, `stop`, and `key-test`:

| CLI Flag | Env Override (`AGENT_VOICE_INPUT_*`) | Description |
| :--- | :--- | :--- |
| `--config PATH` | `AGENT_VOICE_INPUT_CONFIG` | Path to `config.toml` or `config.json` |
| `--asr`, `--asr-provider` | `AGENT_VOICE_INPUT_ASR_PROVIDER` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`) |
| `--asr-model` | `AGENT_VOICE_INPUT_ASR_MODEL` | Stage 1 upstream model ID |
| `--asr-api` | `AGENT_VOICE_INPUT_ASR_API` | Stage 1 protocol (`auto`, `audio-transcriptions`, `chat-completions`) |
| `--asr-effort` | `AGENT_VOICE_INPUT_ASR_EFFORT` | Stage 1 thinking budget (`auto`, `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) |
| `--refiner`, `--refiner-provider` | `AGENT_VOICE_INPUT_REFINER_PROVIDER` | Stage 2 provider (`codex`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`) |
| `--refiner-model` | `AGENT_VOICE_INPUT_REFINER_MODEL` | Stage 2 upstream model ID |
| `--refiner-effort`, `--effort` | `AGENT_VOICE_INPUT_REFINER_EFFORT` | Stage 2 thinking budget (`auto`, `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) |

*(Shorter `VOICE_INPUT_*` environment variable aliases are also accepted, alongside `AGENT_VOICE_INPUT_HOTKEY`, `AGENT_VOICE_INPUT_TRIGGER_MODE`, `AGENT_VOICE_INPUT_ONLY_IN_TERMINALS`, `AGENT_VOICE_INPUT_UI_LANGUAGE`, `AGENT_VOICE_INPUT_UI_POSITION`, `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`, `AGENT_VOICE_INPUT_ASR_BASE_URL`, `AGENT_VOICE_INPUT_REFINER_BASE_URL`, and `AGENT_VOICE_INPUT_FORCE_REFINE`.)*

### Optional: Multi-Window & `tmux` Session Title Hooks

`tmux` active-pane detection works out of the box without modifying `~/.tmux.conf`. If you run multiple agent windows or panes side by side in the same directory on WSL2, inside `tmux`, or in terminals that do not expose per-split `tty`/`cwd`, see [`integrations/`](integrations/README.md) for optional terminal/pane title hooks (`agent-voice-input session-hook`) for Codex, Claude Code, and Antigravity CLI.

---

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Acknowledgments

Early exploration for this project was inspired by [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr), particularly around pairing `Qwen3-ASR` with technical terminology context biasing for mixed Chinese–English speech, as well as pre-transcription silence gating and loudness normalization.

## License

[Apache-2.0 License](LICENSE)
