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

- **Grounded in Your Active Agent Session — Local, Desktop & SSH Remote**: In local terminals, automatically resolves the foreground agent session inside your active window, Ghostty split, or `tmux` pane (zero `~/.tmux.conf` changes needed) to extract recent dialogue and `git status` paths (plus shallow local filenames in non-Git folders) while isolating background subagents and sibling splits. Over SSH to headless Linux servers, foreground SSH/Remote-IDE windows automatically veto local session crosstalk out of the box, and whitelisting `[remote] hosts = ["devbox"]` prefetches remote CLI context over `ssh -- <target> python3 -` during speech with zero remote installation. Across the desktop (`hotkey.scope = "all"` by default; configurable to `"coding"` or `"terminals"`), IDEs (`VS Code`, `Cursor`, `Windsurf`, `Zed`, `JetBrains`, `Xcode`, etc.) resolve the open local workspace without leaking terminal sessions—and desktop **Antigravity IDE** binds its latest matching `brain/` conversation—while AI chat / IM apps and everyday windows use clean general dictation, and remote desktop / VM clients automatically yield the hotkey (`yield_remote_desktop = true`). See [`docs/context-routing.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/context-routing.md) for the full routing matrix.
- **Provider-Driven Speech + Context Pipeline**: `asr.provider = "auto"` and `refiner.provider = "auto"` work out of the box with a single speech-capable API key (pairing automatically with local `codex` CLI login—with public mini-model fallback on standard ChatGPT accounts—or a dedicated Stage 2 key such as `DEEPSEEK_API_KEY`), switch cleanly to any built-in cloud/local API provider (`deepseek`, `dashscope`, `openrouter`, `gemini`, `volcengine`, `openai`, `groq`, `siliconflow`), or run single-hop multimodal audio + 50K cached session context refinement (DashScope Qwen-Omni / Gemini / OpenRouter)—just pick a `provider` and its built-in default model applies automatically.
- **Built for Long & Back-to-Back Dictation**: Start a new recording immediately while earlier utterances are still transcribing or refining—Stage 2 refinements overlap concurrently while utterances always paste in strict `seq_id` FIFO order (joining consecutive focus-drifted clipboard fallbacks with a blank line instead of overwriting); cancel an active recording or abort in-flight recognition/refinement instantly via `agent-voice-input cancel` or by holding the `tap_toggle` hotkey alone for `0.8s` (`hotkey.hold_cancel_ms`, firing live without waiting for release—cancels the active recording first when recording back-to-back, and holding once more aborts background in-flight tasks; `0.5s–0.8s` acts as a silent hesitation deadzone above `hotkey.tap_max_ms = 500`); local silence gating and loudness normalization block empty triggers; supports up to 10-minute recordings by default (with a 15s HUD countdown), splits recordings over 3 minutes (`180s`) at natural pauses for concurrent transcription, alerts immediately if a mic is missing or unplugged, and stashes failed or actively cancelled in-flight audio (`0600`) for `agent-voice-input retry` so long thoughts are never lost.
- **Native HiDPI Waveform HUD & Focus-Safe Paste**: Non-activating floating capsule on both Windows (Per-Monitor V2 HiDPI) and macOS (Retina AppKit) displays live timer, waveform bars, and stage progress in English or Chinese matching your desktop locale. Locks onto the origin window/split on record start, routes per-terminal paste chords (`Ctrl+V` / `Ctrl+Shift+V` / `Shift+Insert` on Windows; zero-clipboard direct injection in Ghostty and `tmux` on macOS, or native `CGEvent` `Cmd+V`), never auto-presses Enter, and falls back to clipboard if focus moved elsewhere.
- **Zero Extra Python Dependencies**: Pure Python `>= 3.10` standard library plus host `ffmpeg` (uses built-in `powershell.exe` on WSL2; compiles and caches a native `mac_shim` helper via `/usr/bin/swiftc` on macOS).

---

## Quick Start

### Option 1: Let Your Coding Agent Set It Up (Recommended)

Copy and paste this prompt directly into **Codex**, **Claude Code**, or **AGY**:

```text
Please install and configure agent-voice-input (https://github.com/xiaohuaxi/agent-voice-input) for me:
1. Check python3 (>=3.10) and ffmpeg (plus Xcode Command Line Tools `/usr/bin/swiftc` on macOS), then install via `uv tool install agent-voice-input` (fall back to `pipx` if `uv` is unavailable).
2. Run `agent-voice-input init` to create `~/.config/agent-voice-input/config.toml` and `agent-voice-input.env` (chmod 600).
3. Check if a Stage 1 speech-capable API key is already set in my environment (e.g. DASHSCOPE_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, SILICONFLOW_API_KEY, or DOUBAO_ASR_API_KEY; optional Stage 2 keys like DEEPSEEK_API_KEY / ARK_API_KEY can be added alongside it); if not, ask which provider I want to use and add it to `~/.config/agent-voice-input/agent-voice-input.env`.
4. Run `agent-voice-input doctor --ping` to verify all checks pass, then start the background daemon with `agent-voice-input start`.
```

### Option 2: Manual Setup in 3 Steps

**1. Prerequisites**

- **Python** `>= 3.10`
- **ffmpeg**: `sudo apt install ffmpeg` (Ubuntu / WSL2) or `brew install ffmpeg` (macOS)
- **Xcode Command Line Tools** (recommended on macOS): `xcode-select --install` for `/usr/bin/swiftc` to compile and cache the native hotkey & HUD helper on first start (falls back to CLI `toggle` / `once` if absent).

**2. Install (`v0.11.0`)**

```bash
uv tool install agent-voice-input
# Or with pipx: pipx install agent-voice-input
# (To track latest main instead: uv tool install git+https://github.com/xiaohuaxi/agent-voice-input.git)
```

**3. Initialize Config, Set One Credential & Start**

```bash
agent-voice-input init
# Edit ~/.config/agent-voice-input/agent-voice-input.env (chmod 600) and uncomment at least one Stage 1 key, then:
agent-voice-input doctor --ping
agent-voice-input start
```

**Stage 1 vs. Stage 2 credentials**: Set at least one Stage 1 speech-capable key in `~/.config/agent-voice-input/agent-voice-input.env` (`DASHSCOPE_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY`, `SILICONFLOW_API_KEY`, or `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`; optional `DEEPSEEK_API_KEY`, `ARK_API_KEY`, or local `codex login` can pair with it for Stage 2; `pipx` works identically in place of `uv tool`).

> Tap **Right Ctrl** (`RControl`) (`<= 0.5s`) inside your terminal to start and finish recording; in `tap_toggle` mode, holding `RControl` alone for `0.8s` cancels the current recording or in-flight processing immediately without waiting for release (with a live hold preview after `0.25s`). `agent-voice-input init` writes a starter `config.toml`; to customize hotkeys (`trigger_mode = "ptt"`, `tap_max_ms`, `hold_cancel_ms`), HUD position (`bottom` / `top`), `[remote]`, duration limits, or providers, see [`v0.11.0/config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.11.0/config.example.toml) (or [`main/config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml)) and [`v0.11.0/config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.11.0/config.example.env).

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

- **`~/.config/agent-voice-input/config.toml`**: Structured `[hotkey]`, `[ui]`, `[audio]`, `[asr]`, `[refiner]`, and `[remote]` settings.
- **`~/.config/agent-voice-input/agent-voice-input.env`**: API credentials (prefix-style aliases also supported; see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)).

### How `provider`, `model`, `asr.api`, and `refiner.api` Work

- **Provider-first defaults (`model` is optional)**: Leave `model = ""` (or omit it) in `[asr]` and `[refiner]` to automatically use the selected `provider`'s built-in default model. Overriding `provider` via CLI without specifying `model` cleanly resets any config-file `model` to the new provider's default. When explicitly set, `model` passes the upstream model ID verbatim.
- **Two-stage STT vs. single-hop multimodal (`asr.api`)**:
  - `asr.api = "auto"` (default) routes `dashscope` (`qwen3-asr-flash`), `doubao`, and dedicated STT models (`qwen/qwen3-asr-1.7b`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`, etc.) through Stage 1 transcription + Stage 2 context refinement;
  - Native `gemini`, DashScope `qwen*-omni*` (`qwen3.8-omni-flash`), and multimodal chat-audio models on OpenRouter / OpenAI-compatible channels (`gemini-3.5-flash-lite`, `google/gemini-3.5-flash-lite`, `qwen/qwen3.8-omni-flash`, `gpt-audio*`, etc.) perform transcription + context refinement in a single hop—sharing the same 50K sealed `<session_reference_log>` prefix blocks (sending up to `refiner.context_max_chars` of active session dialogue to the selected ASR provider in `agent` mode), compressing `>= 3s` audio in-memory to `64 kbps` MP3, using a `610s` single-chunk ceiling, and skipping Stage 2 automatically (unless `refiner.force_on_single_hop = true`). For newly released uncataloged models on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **Stage 2 wire protocol (`refiner.api`)**: Accepts `"auto" | "chat-completions" | "responses"`. `"auto"` (default) routes `codex` to stateless HTTP `/responses` (`store=false`, `stream=true`), `gemini` to native `:generateContent`, and all other providers to `/chat/completions`; set `refiner.api = "responses"` on `openai`, `openrouter`, or `openai-compatible` to opt into the stateless `/responses` engine.
- **Reasoning budget (`effort`)**: Accepts `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`. `asr.effort` defaults to `"auto"`; `refiner.effort` defaults per provider (`"low"` for `codex` / `openai`, and `"auto"` zero-thinking fast mode—e.g. `minimal` on Gemini 3+, `enable_thinking=false` on DashScope Qwen 3+, `thinking.type="disabled"` on Volcengine Seed / official DeepSeek—for other providers).
- **Stage 2 prompt & context caching (`refiner.prompt`, `include_git_status`, `context_max_chars`, `cold_start_fallback`)**: Leave `prompt = ""` (default) to use the built-in adaptive English voice-dictation editor prompts (`agent` coding mode repairs ASR typos/identifiers from read-only `<session_reference_log>` blocks, `<workspace_git_status>`, and common software/AI terms while resolving spoken self-corrections; `general` mode polishes everyday dictation without code-session bias; setting a non-empty `prompt = '''...'''` overrides both modes). Recent dialogue (up to `context_max_chars = 200000`, evicted in ~1/4-window `50K` blocks) is segmented into immutable `<session_reference_log>` prefix blocks (`developer` messages on `/responses`, or leading blocks in a single `user` message on `/chat/completions`) ahead of the final `user` block containing `<workspace_git_status>` (`include_git_status = true` by default, falling back to core tracked filenames when the worktree is clean), `<asr_draft_to_clean>`, and `<task_reminder>` for high-hit prefix caching (`codex` pairs deterministic per-session `prompt_cache_key` with Keep-Alive edge-affinity cookies; Stage 1 also supplements user `hotwords.txt` and live git/dialogue identifiers with low-priority AI/CLI terms in `agent` mode). When an active coding session starts empty/short (or a workspace has no active session), `cold_start_fallback = "auto"` (default; or `"hotwords"` / `"off"`) automatically supplements Stage 1 hotwords from recent exact-same-directory sessions and prepends a single frozen `<= 25K` `<prior_session_reference>` block for Stage 2 / single-hop (naturally evicted once the new session fills the window; also supported in a single RTT over SSH remote).
- **Layered plain-text hotwords (`asr.hotwords_file` & `hotwords` CLI)**: Edit `~/.config/agent-voice-input/hotwords.txt` directly or manage it via `agent-voice-input hotwords list|add|remove`—changes hot-reload automatically on the next dictation without restarting the daemon. Legacy flat files map to `[global]`; optional `[chat]` (IM/AI chat windows) and `[coding]` (terminals, IDEs, Agent sessions) sections auto-mount alongside `[global]` at highest priority ahead of dynamic session/workspace identifiers and built-in terms, with per-provider quota inspection (`doubao`, `whisper`, `dashscope`, `gemini`).
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
agent-voice-input start                  # Start background daemon (idempotent; warns if running code/version drifted)
agent-voice-input restart                # Unconditionally stop and restart daemon after config/code changes
agent-voice-input status                 # Inspect daemon state (stopped/unresponsive/running), active pipeline, and drift (--history N)
agent-voice-input doctor [--ping] [--remote]  # Check audio bridge, native shim, credentials, active pipeline, session/SSH readiness, and endpoint ping (--json supported)
agent-voice-input diagnose [--last N] [--json]  # Run read-only live diagnostics on daemon health, latency p50, cache hits, failed/ count, and redacted log tail
agent-voice-input retry [--list] [--print]  # List or re-run stashed failed recordings (skips duplicate stdout when auto-pasted; --print forces stdout)
agent-voice-input hotwords list|add|remove  # Manage layered plain-text hotwords ([global]/[chat]/[coding]) and inspect ASR provider quotas (--scope, --json)
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
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`; aliases `volcengine`/`volc` → `doubao`, `bailian` → `dashscope`) |
| `--asr-model` | Stage 1 upstream model ID |
| `--asr-api` | Stage 1 protocol (`auto`, `audio-transcriptions`, `chat-completions`) |
| `--asr-effort` | Stage 1 thinking budget |
| `--refiner`, `--refiner-provider` | Stage 2 provider (`auto`, `codex`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`; aliases `doubao`/`volc`/`ark` → `volcengine`, `bailian` → `dashscope`) |
| `--refiner-model` | Stage 2 upstream model ID |
| `--refiner-api` | Stage 2 protocol (`auto`, `chat-completions`, `responses`) |
| `--refiner-effort`, `--effort` | Stage 2 thinking budget |

*(Environment variables are reserved for API credentials, bootstrap paths `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR`, and low-level runtime/diagnostic hooks; ordinary options live in `config.toml`.)*

### Optional: Multi-Window Title Hooks & SSH Remote Linux Hosts

- **Local multi-window hooks**: Single-window setups, macOS + Ghostty splits, and `tmux` panes work out of the box with zero hooks. If you run multiple standalone terminal windows side by side in the **same project directory** on WSL2 or in terminals without per-split `tty`/`cwd` introspection, see [`integrations/`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md) for optional lightweight session title hooks (`agent-voice-input session-hook`) for Codex, Claude Code, and Antigravity CLI.
- **Zero-install SSH remote Linux context (`[remote]`)**: Foreground SSH clients and Remote-SSH IDEs automatically block local background sessions from leaking out of the box. To pull live remote CLI dialogue and workspace filenames from a Linux server (zero remote daemon, pip package, or API keys—only Python 3.10+ stdlib over `BatchMode` SSH, prefetched while you speak), add `hosts = ["devbox"]` under `[remote]` in `config.toml` and verify with `agent-voice-input doctor --remote` (see [`integrations/`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md#5-ssh-remote-linux-hosts-zero-install--optional-title-hook) and [`docs/context-routing.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/context-routing.md)).

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
