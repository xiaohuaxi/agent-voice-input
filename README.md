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

- **Grounded in Your Active Agent Session — Yet Ready Anywhere**: In terminals, automatically resolves the foreground agent session inside your active window, Ghostty split, or `tmux` pane (zero `~/.tmux.conf` changes needed) to extract recent dialogue and `git status` paths (plus shallow local filenames in non-Git folders) while isolating background subagents and sibling splits. Works across desktop apps out of the box (`hotkey.scope = "all"` by default; configurable to `"coding"` or `"terminals"`): desktop IDEs (`VS Code`, `Cursor`, `Windsurf`, `Zed`, `JetBrains`, `Xcode`, etc.) resolve the open local workspace (Git repo or ordinary folder) for filename/status hotwords without leaking terminal CLI sessions—and desktop **Antigravity IDE** additionally binds its latest matching `brain/` conversation for that workspace—while AI chat / IM apps and everyday windows use clean general dictation, and remote desktop / VM clients automatically yield the hotkey (`yield_remote_desktop = true`).
- **Provider-Driven Speech + Context Pipeline**: `asr.provider = "auto"` and `refiner.provider = "auto"` work out of the box with a single API key (or local `codex` CLI login, with automatic public mini-model fallback on standard ChatGPT accounts), switch cleanly to any built-in cloud/local API provider (`deepseek`, `dashscope`, `openrouter`, `gemini`, `volcengine`, `openai`, `groq`, `siliconflow`), or run single-hop multimodal audio + 50K cached session context refinement (DashScope Qwen-Omni / Gemini / OpenRouter)—just pick a `provider` and its built-in default model applies automatically.
- **Built for Long & Back-to-Back Dictation**: Start a new recording immediately while earlier utterances are still transcribing or refining—Stage 2 refinements overlap concurrently while utterances always paste in strict `seq_id` FIFO order (joining consecutive focus-drifted clipboard fallbacks with a blank line instead of overwriting); cancel an active recording or abort in-flight recognition/refinement instantly via `agent-voice-input cancel` (or by holding & releasing the `tap_toggle` hotkey for `1.2s–8s`, i.e. above `hotkey.tap_max_ms`, while idle from recording); local silence gating and loudness normalization block empty triggers; supports up to 10-minute recordings by default (with a 15s HUD countdown), splits recordings over 3 minutes (`180s`) at natural pauses for concurrent transcription, alerts immediately if a mic is missing or unplugged, and stashes failed or actively cancelled audio (`0600`) for `agent-voice-input retry` so long thoughts are never lost.
- **Native HiDPI Waveform HUD & Focus-Safe Paste**: Non-activating floating capsule on both Windows (Per-Monitor V2 HiDPI) and macOS (Retina AppKit) displays live timer, waveform bars, and stage progress in English or Chinese matching your desktop locale. Locks onto the origin window/split on record start, routes per-terminal paste chords (`Ctrl+V` / `Ctrl+Shift+V` / `Shift+Insert` on Windows; zero-clipboard direct injection in Ghostty and `tmux` on macOS, or native `CGEvent` `Cmd+V`), never auto-presses Enter, and falls back to clipboard if focus moved elsewhere.
- **Zero Extra Python Dependencies**: Pure Python `>= 3.10` standard library plus host `ffmpeg` (uses built-in `powershell.exe` on WSL2; compiles and caches a native `mac_shim` helper via `/usr/bin/swiftc` on macOS).

---

## Quick Start

### Option 1: Let Your Coding Agent Set It Up (Recommended)

Copy and paste this prompt directly into **Codex**, **Claude Code**, or **AGY**:

```text
Please install and configure agent-voice-input (https://github.com/xiaohuaxi/agent-voice-input) for me:
1. Check python3 (>=3.10) and ffmpeg (plus Xcode Command Line Tools `/usr/bin/swiftc` on macOS), then install via `uv tool install agent-voice-input` (or `pipx` if `uv` is unavailable).
2. Run `agent-voice-input init` to create `~/.config/agent-voice-input/config.toml` and `agent-voice-input.env` (chmod 600).
3. Check if an API key is already set in my environment (e.g. DASHSCOPE_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY); if not, ask which provider I want to use and add it to `~/.config/agent-voice-input/agent-voice-input.env`.
4. Run `agent-voice-input doctor --ping` to verify all checks pass, then start the background daemon with `agent-voice-input start`.
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

**3. Initialize Config, Add One Credential & Start**

Initialize `0600` config files via `agent-voice-input init` and uncomment at least one provider key in `~/.config/agent-voice-input/agent-voice-input.env` (for example `DASHSCOPE_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY`, plus optional `DEEPSEEK_API_KEY` or local `codex login` for Stage 2):

```bash
agent-voice-input init
# Edit ~/.config/agent-voice-input/agent-voice-input.env and set e.g. OPENROUTER_API_KEY="sk-or-..."

# Verify audio bridge, native shim, resolved providers/models, and endpoint connectivity, then start daemon
agent-voice-input doctor --ping
agent-voice-input start
```

> Tap **Right Ctrl** (`RControl`) inside your terminal to start and finish recording (while an utterance is transcribing or refining in `tap_toggle` mode, holding & releasing `RControl` alone for `1.2s–8s` cancels in-flight processing). To customize hotkeys (`trigger_mode = "ptt"`), HUD position (`bottom` / `top`), duration limits, or providers, copy [`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml) to `~/.config/agent-voice-input/config.toml` (see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env) for all credential variables).

---

## Platform Experience

### Windows Terminal + WSL2 (Including Remote Desktop & `tmux`)

- **Global Scope, Remote-Desktop Yielding & HiDPI Capsule**: Isolated Right Ctrl tap toggles recording across terminals, IDEs, and desktop apps by default (`hotkey.scope = "all"`, or `"coding"` / `"terminals"`; chords like `RControl+C` pass through untouched, and stopping/canceling works from any app) with an anti-aliased Per-Monitor V2 HiDPI capsule above the taskbar. Remote desktop / VM clients (`mstsc`, `vmconnect`, Parsec, RustDesk, etc.) automatically yield new hotkey triggers (`yield_remote_desktop = true`; bare `msrdc` is left unblocked for Win11 WSLg—non-WSLg Azure Virtual Desktop / Windows 365 users can add `ignore_apps = ["msrdc"]`), and per-process paste routing handles `Ctrl+V`, `Ctrl+Shift+V` (`WezTerm`, `Alacritty`, `Tabby`, `Hyper`, etc.), and `Shift+Insert` (`mintty`, `PuTTY`, `KiTTY`).
- **Native WASAPI Capture (Zero WSLg / RDP Glitches)**: Captures 16 kHz audio directly via Windows WASAPI in a resident `win_shim`, bypassing WSLg `RDPSource` zero-packet deadlocks; start/stop audio cues default to off to avoid 1–2s Remote Desktop audio wake-up lag.
- **Zero-Config `tmux` Pane Resolution**: Automatically tracks the active pane of the attached `tmux` client without modifying `~/.tmux.conf`.

### macOS (`Ghostty`, Built-in `Terminal.app` & `tmux`)

- **Native Retina HUD & Context-Safe Delivery**: Uses a cached native `mac_shim` for Right Ctrl tap/PTT, remote-desktop hotkey yielding (`Windows App`, `Screen Sharing`, Parsec, VMware/Parallels, etc.), and Retina capsule rendering. In **Ghostty** and **`tmux`**, injects text directly without touching your system clipboard; in other terminals, IDEs, and desktop GUI apps, locks onto the origin window ID and delivers via native `CGEvent` `Cmd+V` (falling back to clipboard if focus or tab moved).
- **Zero-Hook Tab/Split & `tmux` Session Binding**: Reads Ghostty's split `tty`/`cwd`, `Terminal.app`'s selected tab `tty`, or the terminal window's child PTY tree (plus active pane inside `tmux`) to bind directly to the `codex`, `claude`, or `agy` session running in that tab while strictly skipping session lookup in non-terminal GUI apps; audio input defaults to system mic (`[audio] macos_device = "default"`).
- **Minimal macOS Permissions (TCC)**: Requires **Microphone** (for `ffmpeg`) plus **Automation → Ghostty** (for Ghostty) or **Automation → Terminal** (for multi-tab `Terminal.app` active-tab lookup). **Input Monitoring** is **not required** for default modifier hotkeys (`RControl`), and **Accessibility** is granted to your terminal app for native `Cmd+V` paste into desktop GUI apps and non-Ghostty terminals outside `tmux` (not needed inside Ghostty or `tmux`).

---

## Configuration & Providers Reference

Configuration uses two clean files (`CLI flags > config.toml > built-in defaults`):

- **`~/.config/agent-voice-input/config.toml`**: Structured `[hotkey]`, `[ui]`, `[audio]`, `[asr]`, and `[refiner]` settings.
- **`~/.config/agent-voice-input/agent-voice-input.env`**: API credentials (prefix-style aliases also supported; see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)).

### How `provider`, `model`, `asr.api`, and `refiner.api` Work

- **Provider-first defaults (`model` is optional)**: Leave `model = ""` (or omit it) in `[asr]` and `[refiner]` to automatically use the selected `provider`'s built-in default model. Overriding `provider` via CLI without specifying `model` cleanly resets any config-file `model` to the new provider's default. When explicitly set, `model` passes the upstream model ID verbatim.
- **Two-stage STT vs. single-hop multimodal (`asr.api`)**:
  - `asr.api = "auto"` (default) routes `dashscope` (`qwen3-asr-flash`), `doubao`, and dedicated STT models (`qwen/qwen3-asr-1.7b`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`, etc.) through Stage 1 transcription + Stage 2 context refinement;
  - Native `gemini`, DashScope `qwen*-omni*` (`qwen3.8-omni-flash`), and multimodal chat-audio models on OpenRouter / OpenAI-compatible channels (`gemini-3.5-flash-lite`, `google/gemini-3.5-flash-lite`, `qwen/qwen3.8-omni-flash`, `gpt-audio*`, etc.) perform transcription + context refinement in a single hop—sharing the same 50K sealed `<session_reference_log>` prefix blocks (sending up to `refiner.context_max_chars` of active session dialogue to the selected ASR provider in `agent` mode), compressing `>= 3s` audio in-memory to `64 kbps` MP3, using a `610s` single-chunk ceiling, and skipping Stage 2 automatically (unless `refiner.force_on_single_hop = true`). For newly released uncataloged models on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **Stage 2 wire protocol (`refiner.api`)**: Accepts `"auto" | "chat-completions" | "responses"`. `"auto"` (default) routes `codex` to stateless HTTP `/responses` (`store=false`, `stream=true`), `gemini` to native `:generateContent`, and all other providers to `/chat/completions`; set `refiner.api = "responses"` on `openai`, `openrouter`, or `openai-compatible` to opt into the stateless `/responses` engine.
- **Reasoning budget (`effort`)**: Accepts `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`. `asr.effort` defaults to `"auto"`; `refiner.effort` defaults per provider (`"low"` for `codex` / `openai`, and `"auto"` zero-thinking fast mode—e.g. `minimal` on Gemini 3+, `enable_thinking=false` on DashScope Qwen 3+, `thinking.type="disabled"` on Volcengine Seed / official DeepSeek—for other providers).
- **Stage 2 prompt & context caching (`refiner.prompt`, `include_git_status`, `context_max_chars`)**: Leave `prompt = ""` (default) to use the built-in adaptive English voice-dictation editor prompts (`agent` coding mode repairs ASR typos/identifiers from read-only `<session_reference_log>` blocks, `<workspace_git_status>`, and common software/AI terms while resolving spoken self-corrections; `general` mode polishes everyday dictation without code-session bias; setting a non-empty `prompt = '''...'''` overrides both modes). Recent dialogue (up to `context_max_chars = 200000`, evicted in ~1/4-window `50K` blocks) is segmented into immutable `<session_reference_log>` prefix blocks (`developer` messages on `/responses`, or leading blocks in a single `user` message on `/chat/completions`) ahead of the final `user` block containing `<workspace_git_status>` (`include_git_status = true` by default, falling back to core tracked filenames when the worktree is clean), `<asr_draft_to_clean>`, and `<task_reminder>` for high-hit prefix caching (`codex` pairs deterministic per-session `prompt_cache_key` with Keep-Alive edge-affinity cookies; Stage 1 also supplements user `hotwords.txt` and live git/dialogue identifiers with low-priority AI/CLI terms in `agent` mode).
- **Same-provider retries & handshake timeout (`max_retries`)**: `[asr] max_retries` and `[refiner] max_retries` default to `2` (`0..10`, extra retries beyond the initial request; `0` disables retries), separating rate-limit throttling (`HTTP 429`, `Retry-After`, vendor QPS codes) from transient network/5xx errors under the stage `timeout_sec` budget with a 9s connect/TLS handshake cap.
- **Optional local MP3 recording archive (`audio.save_recordings`)**: Disabled by default (`false`). When enabled, valid non-silent recordings are compressed asynchronously via `ffmpeg` (`16 kHz` mono `64 kbps` MP3) into `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/` with `0600` permissions and ring-pruned to the newest `save_recordings_max_count` (`300` by default, `1..5000`) files without adding latency to live dictation.

### Built-in Providers

| `provider` | Stages | Built-in Default `asr.model` | Built-in Default `refiner.model` | Credential Source / Env |
| :--- | :--- | :--- | :--- | :--- |
| **`codex`** | `refiner` | — | `gpt-6-luna` *(auto-falls back to public `gpt-5.4-mini` / `gpt-5-codex-mini`)* | Local `codex` CLI login *(stateless HTTP `/responses` + 30m safe refresh; never enables Fast mode)* |
| **`deepseek`** | `refiner` | — | `deepseek-flash` | `DEEPSEEK_API_KEY` *(compat: `API_KEY_DEEPSEEK`)* |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` *(or single-hop `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` *(auto-probes granted slot)* |
| **`volcengine`** | `refiner` | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-3.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-3.5-flash-lite` *(single-hop)* | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs explicit `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |

- `asr.provider = "auto"` (default): selects the first configured credential in order `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow`.
- `refiner.provider = "auto"` (default): prefers local `codex` CLI login when present, otherwise selects the first configured credential in order `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, or degrades cleanly to `"off"` when no Stage 2 credentials exist.
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
# Recipe 2: Single-hop multimodal audio + 50K cached session context (DashScope Qwen-Omni, OpenRouter, or native Gemini; skips Stage 2)
[asr]
provider = "openrouter"          # Or provider = "dashscope" with model = "qwen3.8-omni-flash", or provider = "gemini" (no model needed)
model = "google/gemini-3.5-flash-lite"
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
agent-voice-input init                   # Create 0600 config.toml and commented agent-voice-input.env (--force overwrites)
agent-voice-input start                  # Start background daemon
agent-voice-input restart                # Restart daemon after config changes
agent-voice-input status                 # Inspect daemon state, last latency, and redacted errors
agent-voice-input doctor [--ping]        # Check audio bridge, native shim, credentials, session binding, and optional endpoint ping (--json supported)
agent-voice-input retry --list           # List stashed failed recordings (`agent-voice-input retry` re-runs latest)
agent-voice-input once --duration 4 --dry-run  # Record once and print result without pasting
agent-voice-input toggle                 # Toggle recording start/stop from CLI
agent-voice-input cancel                 # Cancel active recording, or abort in-flight tasks (`--seq N` targets one)
agent-voice-input stop                   # Stop background daemon
agent-voice-input key-test               # Inspect raw key scancodes / modifier events
```

### Runtime CLI Flags

| CLI Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `config.toml` or `config.json` (also supports `AGENT_VOICE_INPUT_CONFIG` and `XDG_CONFIG_HOME`) |
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`) |
| `--asr-model` | Stage 1 upstream model ID |
| `--asr-api` | Stage 1 protocol (`auto`, `audio-transcriptions`, `chat-completions`) |
| `--asr-effort` | Stage 1 thinking budget |
| `--refiner`, `--refiner-provider` | Stage 2 provider (`auto`, `codex`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`) |
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
