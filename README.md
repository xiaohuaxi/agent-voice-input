**English** | [简体中文](https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.zh-CN.md)

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)

**Context-aware voice input built specifically for CLI coding agents** (**Codex**, **Claude Code**, and **Antigravity CLI / `agy`**).

Generic OS dictation has no idea what files, symbols, or CLI flags exist in your repository. `agent-voice-input` automatically locates the active coding agent session in your focused terminal window or pane, pairs fast speech recognition with lightweight refinement grounded in your live dialogue + `git status` filenames, and fixes homophone identifiers, mixed-language technical terms, and mid-sentence self-corrections before pasting safely into your terminal:

```text
🎙️ What you say casually to your terminal:
   "Check the default max duration seconds in platform w s l dot py, change it from
    three minutes to five minutes—actually wait, make it ten minutes—and run unit tests
    without piping to head"

❌ Generic OS dictation:
   Check the default max duration seconds in platform WSL dot pie, change it from 3 minutes to 5 minutes actually wait make it 10 minutes and run unit tests without piping to head

✅ agent-voice-input (refined against your active Agent session & repo context, pasted without pressing Enter):
   Check the default of max_duration_sec in platform_wsl.py, change it to 10 minutes, and run the unit tests without piping to head.
```

> Verified on **Windows Terminal + WSL2 (including Remote Desktop and `tmux`)** and **macOS (`Ghostty`, built-in `Terminal.app`, and `tmux`)**; common third-party macOS terminals (`iTerm2`, `WezTerm`, `Alacritty`, `Kitty`, `Warp`, etc.) share baseline window-scoping, child-PTY session discovery, and `Cmd+V` / `tmux` paste support (community verification PRs welcome).

---

## Why It Works Better for Coding Agents

- **Grounded in Your Active Agent Session**: Automatically resolves the foreground agent session inside your active terminal window, Ghostty split, or `tmux` pane (zero `~/.tmux.conf` changes needed), incrementally extracting recent dialogue and `git status` paths as context while strictly ignoring background subagents and sibling splits.
- **Provider-Driven Speech + Context Pipeline**: Pairs fast dedicated speech-to-text with a stateless Keep-Alive `codex` refiner by default, switches cleanly to any built-in cloud/local API provider, or runs single-hop multimodal audio + context refinement (Gemini / OpenRouter)—just pick a `provider` and its built-in default model applies automatically.
- **Built for Long & Back-to-Back Dictation**: Start a new recording immediately while earlier utterances are still transcribing or refining—utterances queue and paste in FIFO order (joining consecutive focus-drifted clipboard fallbacks with a blank line instead of overwriting); local silence gating and loudness normalization block empty triggers; supports up to 10-minute recordings by default (with a 15s HUD countdown), splits recordings over 3 minutes (`180s`) at natural pauses for concurrent transcription, alerts immediately if a mic is missing or unplugged, and stashes failed audio for `agent-voice-input retry` so long thoughts are never lost.
- **Native HiDPI Waveform HUD & Focus-Safe Paste**: Non-activating floating capsule on both Windows (Per-Monitor V2 HiDPI) and macOS (Retina AppKit) displays live timer, waveform bars, and stage progress in English or Chinese matching your desktop locale. Locks onto the origin terminal window/split on record start, never auto-presses Enter, and falls back to clipboard if focus moved elsewhere.
- **Zero Extra Python Dependencies**: Pure Python `>= 3.10` standard library plus host `ffmpeg` (uses built-in `powershell.exe` on WSL2; compiles and caches a native `mac_shim` helper via `/usr/bin/swiftc` on macOS).

---

## Quick Start

### Option 1: Let Your Coding Agent Set It Up (Recommended)

Copy and paste this prompt directly into **Codex**, **Claude Code**, or **AGY**:

```text
Please install and configure agent-voice-input (https://github.com/xiaohuaxi/agent-voice-input) for me:
1. Check python3 (>=3.10) and ffmpeg (plus Xcode Command Line Tools `/usr/bin/swiftc` on macOS), then install via `uv tool install agent-voice-input` (or `pipx` if `uv` is unavailable).
2. Check if an API key is already set in my environment (e.g. DASHSCOPE_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY); if not, ask which provider I want to use and write it to `~/.config/agent-voice-input/agent-voice-input.env` with chmod 600.
3. If the `codex` CLI is not installed or logged in on this machine, set `[refiner] provider` in `~/.config/agent-voice-input/config.toml` to match my configured API provider (leave `model` unset so built-in defaults apply).
4. Run `agent-voice-input doctor` to verify all checks pass, then start the background daemon with `agent-voice-input start`.
```

### Option 2: Manual Setup in 3 Steps

**1. Prerequisites**

- **Python** `>= 3.10`
- **ffmpeg**: `sudo apt install ffmpeg` (Ubuntu / WSL2) or `brew install ffmpeg` (macOS)
- **Xcode Command Line Tools** (recommended on macOS): `xcode-select --install` for `/usr/bin/swiftc` to compile and cache the native hotkey & HUD helper on first start (falls back to CLI `toggle` / `once` if absent).

**2. Install**

```bash
uv tool install agent-voice-input
# Or with pipx: pipx install agent-voice-input
```

**3. Configure One Credential & Start**

Add at least one provider key to `~/.config/agent-voice-input/agent-voice-input.env` (for example `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `DASHSCOPE_API_KEY`):

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
OPENROUTER_API_KEY="<your-openrouter-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env

# Verify audio bridge, native shim, resolved providers/models, and session binding, then start daemon
agent-voice-input doctor
agent-voice-input start
```

> Tap **Right Ctrl** (`RControl`) inside your terminal to start and finish recording. To customize hotkeys (`trigger_mode = "ptt"`), HUD position (`bottom` / `top`), duration limits, or providers, copy [`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml) to `~/.config/agent-voice-input/config.toml` (see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env) for all credential variables).

---

## Platform Experience

### Windows Terminal + WSL2 (Including Remote Desktop & `tmux`)

- **Terminal-Scoped Hotkey & HiDPI Capsule**: Isolated Right Ctrl tap toggles recording (chords like `RControl+C` pass through untouched; starts only when a terminal is focused, while stopping/canceling works from any app) with an anti-aliased Per-Monitor V2 HiDPI capsule above the taskbar.
- **Native WASAPI Capture (Zero WSLg / RDP Glitches)**: Captures 16 kHz audio directly via Windows WASAPI in a resident `win_shim`, bypassing WSLg `RDPSource` zero-packet deadlocks; start/stop audio cues default to off to avoid 1–2s Remote Desktop audio wake-up lag.
- **Zero-Config `tmux` Pane Resolution**: Automatically tracks the active pane of the attached `tmux` client without modifying `~/.tmux.conf`.

### macOS (`Ghostty`, Built-in `Terminal.app` & `tmux`)

- **Native Retina HUD & Terminal-Aware Delivery**: Uses a cached native `mac_shim` for Right Ctrl tap/PTT and Retina capsule rendering. In **Ghostty**, locks onto the exact focused split surface and injects bracketed-paste text directly; in **built-in `Terminal.app`** (and other macOS terminals), locks onto the window ID / active tab `tty` and delivers via `tmux` buffer paste (when attached to `tmux`) or `Cmd+V` (falling back to clipboard if focus or tab moved).
- **Zero-Hook Tab/Split & `tmux` Session Binding**: Reads Ghostty's split `tty`/`cwd`, `Terminal.app`'s selected tab `tty`, or the terminal window's child PTY tree (plus active pane inside `tmux`) to bind directly to the `codex`, `claude`, or `agy` session running in that tab while isolating sibling terminals; audio input defaults to system mic (`[audio] macos_device = "default"`).
- **Minimal macOS Permissions (TCC)**: Requires **Microphone** (for `ffmpeg`) plus **Automation → Ghostty** (for Ghostty) or **Automation → Terminal** (for multi-tab `Terminal.app` active-tab lookup). **Input Monitoring** is **not required** for default modifier hotkeys (`RControl`), and **Accessibility** is only needed for `Cmd+V` paste into non-Ghostty terminals when not running inside `tmux`.

---

## Configuration & Providers Reference

Configuration uses two clean files (`CLI flags > config.toml > built-in defaults`):

- **`~/.config/agent-voice-input/config.toml`**: Structured `[hotkey]`, `[ui]`, `[audio]`, `[asr]`, and `[refiner]` settings.
- **`~/.config/agent-voice-input/agent-voice-input.env`**: API credentials (prefix-style aliases also supported; see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)).

### How `provider`, `model`, `asr.api`, and `refiner.api` Work

- **Provider-first defaults (`model` is optional)**: Leave `model = ""` (or omit it) in `[asr]` and `[refiner]` to automatically use the selected `provider`'s built-in default model. Overriding `provider` via CLI without specifying `model` cleanly resets any config-file `model` to the new provider's default. When explicitly set, `model` passes the upstream model ID verbatim.
- **Two-stage STT vs. single-hop multimodal (`asr.api`)**:
  - `asr.api = "auto"` (default) routes `dashscope`, `doubao`, and dedicated STT models (`qwen/qwen3-asr-1.7b`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`, etc.) through Stage 1 transcription + Stage 2 context refinement;
  - Native `gemini` and multimodal chat-audio models on OpenRouter / OpenAI-compatible channels (`gemini-2.5-flash`, `google/gemini-2.5-flash`, `gpt-audio*`, etc.) perform transcription + context refinement in a single hop and skip Stage 2 automatically (unless `refiner.force_on_single_hop = true`). For newly released uncataloged models on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **Stage 2 wire protocol (`refiner.api`)**: Accepts `"auto" | "chat-completions" | "responses"`. `"auto"` (default) routes `codex` to stateless HTTP `/responses` (`store=false`, `stream=true`), `gemini` to native `:generateContent`, and all other providers to `/chat/completions`; set `refiner.api = "responses"` on `openai`, `openrouter`, or `openai-compatible` to opt into the stateless `/responses` engine.
- **Reasoning budget (`effort`)**: Accepts `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`. `asr.effort` defaults to `"auto"` (suppressing thinking latency on supported multimodal models); `refiner.effort` defaults to `"low"`.
- **Stage 2 prompt & context caching (`refiner.prompt`, `include_git_status`, `context_max_chars`)**: Leave `prompt = ""` (default) to use the built-in English voice-dictation editor prompt (repairs ASR typos/identifiers from read-only `<session_reference_log>` blocks and `<workspace_git_status>`, smooths spoken disfluencies while preserving original sentence structure and language, and strictly forbids answering dictated questions), or customize it with TOML multiline strings `prompt = '''...'''`. Recent dialogue (up to `context_max_chars = 200000`, evicted in ~1/4-window `50K` blocks) is segmented into immutable `<session_reference_log>` prefix blocks (`developer` messages on `/responses`, or leading blocks in a single `user` message on `/chat/completions`) ahead of the current draft and trailing `git status` paths (`include_git_status = true` by default) for high-hit prefix caching (`codex` pairs deterministic per-session `prompt_cache_key` with Keep-Alive edge-affinity cookies).
- **Same-provider retries & handshake timeout (`max_retries`)**: `[asr] max_retries` and `[refiner] max_retries` default to `2` (`0..10`, extra retries beyond the initial request; `0` disables retries), separating rate-limit throttling (`HTTP 429`, `Retry-After`, vendor QPS codes) from transient network/5xx errors under the stage `timeout_sec` budget with a 9s connect/TLS handshake cap.
- **Optional local MP3 recording archive (`audio.save_recordings`)**: Disabled by default (`false`). When enabled, valid non-silent recordings are compressed asynchronously via `ffmpeg` (`16 kHz` mono `32 kbps` MP3) into `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/` with `0600` permissions and ring-pruned to the newest `save_recordings_max_count` (`50` by default, `1..5000`) files without adding latency to live dictation.

### Built-in Providers

| `provider` | Stages | Built-in Default `asr.model` | Built-in Default `refiner.model` | Credential Source / Env |
| :--- | :--- | :--- | :--- | :--- |
| **`codex`** | `refiner` | — | `gpt-6-luna` | Local `codex` CLI login *(stateless HTTP `/responses` + 30m safe refresh; never enables Fast mode)* |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` | `qwen-turbo-latest` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` *(auto-probes granted slot)* |
| **`volcengine`** | `refiner` | — | `doubao-seed-1-6-flash` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-2.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-2.5-flash` *(single-hop)* | `gemini-2.5-flash-lite` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs explicit `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |

- `asr.provider = "auto"` (default): selects the first configured credential in order `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow`.
- `refiner.provider = "off"`: disables Stage 2 refinement.

### Common `config.toml` Recipes

```toml
# Recipe 1: OpenRouter single key — two-stage dedicated STT + fast refiner (uses built-in defaults)
[asr]
provider = "openrouter"

[refiner]
provider = "openrouter"
```

```toml
# Recipe 2: Single-hop multimodal audio + context refinement (OpenRouter or native Gemini; skips Stage 2)
[asr]
provider = "openrouter"          # Or set provider = "gemini" (with GEMINI_API_KEY, no model needed)
model = "google/gemini-2.5-flash"
effort = "auto"
```

```toml
# Recipe 3: Low-latency Mainland China setup (DashScope STT + stateless local Codex or API refiner)
[asr]
provider = "dashscope"

[refiner]
provider = "codex"               # Or "dashscope" / "volcengine" if Codex CLI is not installed
```

---

## CLI Commands & Overrides

### Common Subcommands

```bash
agent-voice-input start                  # Start background daemon
agent-voice-input restart                # Restart daemon after config changes
agent-voice-input status                 # Inspect daemon state, last latency, and redacted errors
agent-voice-input doctor                 # Check audio bridge, native shim, credentials, and session binding
agent-voice-input retry --list           # List stashed failed recordings (`agent-voice-input retry` re-runs latest)
agent-voice-input once --duration 4 --dry-run  # Record once and print result without pasting
agent-voice-input toggle                 # Toggle recording start/stop from CLI
agent-voice-input stop                   # Stop background daemon
agent-voice-input key-test               # Inspect raw key scancodes / modifier events
```

### Runtime CLI Flags

| CLI Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `config.toml` or `config.json` (also supports `AGENT_VOICE_INPUT_CONFIG`) |
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`) |
| `--asr-model` | Stage 1 upstream model ID |
| `--asr-api` | Stage 1 protocol (`auto`, `audio-transcriptions`, `chat-completions`) |
| `--asr-effort` | Stage 1 thinking budget |
| `--refiner`, `--refiner-provider` | Stage 2 provider (`codex`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`) |
| `--refiner-model` | Stage 2 upstream model ID |
| `--refiner-api` | Stage 2 protocol (`auto`, `chat-completions`, `responses`) |
| `--refiner-effort`, `--effort` | Stage 2 thinking budget |

*(Environment variables are reserved for API credentials, bootstrap paths `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR`, and low-level runtime/diagnostic hooks; ordinary options live in `config.toml`.)*

### Optional: Multi-Window Session Title Hooks

Single-window setups, macOS + Ghostty splits, and `tmux` panes work out of the box with zero hooks. If you run multiple standalone terminal windows side by side in the **same project directory** on WSL2 or in terminals without per-split `tty`/`cwd` introspection, see [`integrations/`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md) for optional lightweight session title hooks (`agent-voice-input session-hook`) for Codex, Claude Code, and Antigravity CLI.

---

## Development

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git ~/repos/agent-voice-input
ln -sf ~/repos/agent-voice-input/bin/agent-voice-input ~/.local/bin/agent-voice-input
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Acknowledgments

Early exploration for this project was inspired by [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr), particularly around pairing `Qwen3-ASR` with technical terminology context for mixed Chinese–English speech, as well as pre-transcription silence gating and loudness normalization.

## License

[Apache-2.0 License](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)
