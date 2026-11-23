**English** | [简体中文](README.zh-CN.md)

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**Context-aware voice input for CLI coding agents—grounded in your live session, repo, and SSH hosts.**  
Built for **Codex**, **Claude Code**, and **Antigravity CLI (`agy`)**, while working out of the box across desktop IDEs and everyday apps.

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

---

## Core Technical Highlights

- **Two-Stage & Single-Hop Modes + 50K Block Prefix Caching → High Accuracy, Low Latency & Lower Cost**: Pair dedicated Stage 1 STT with lightweight Stage 2 refinement, or run single-hop multimodal audio; session history is segmented into immutable 50K read-only prefix blocks for high prompt-cache hits, while back-to-back utterances refine concurrently and paste in strict FIFO order.
- **Focus-Aware Terminal, IDE & SSH Routing → Zero Context Crosstalk**: Automatically binds to the active coding session and `git status` filenames in your focused terminal window, Ghostty split, or `tmux` pane; desktop IDEs mount only the local workspace without leaking terminal sessions; everyday/chat apps switch to clean general dictation; SSH terminals block local session leaks by default and support zero-install remote context over `ssh -- <host> python3 -` with a 1-line whitelist.
- **Zero Third-Party Python Dependencies + Acoustic Silence Gating → Lightweight & Waste-Free**: Pure Python `>= 3.10` standard library plus host `ffmpeg` and minimal native shims (no Electron); local silence gating and loudness normalization block empty triggers before calling APIs, recordings run up to 10 minutes (auto-splitting `> 3 min` clips at natural pauses), and failed or in-flight-cancelled audio is stashed (`0600`) for `retry`.
- **Zero-Clipboard Injection in Ghostty/`tmux` + Never Auto-Presses Enter → Terminal-Safe Delivery**: Locks onto the origin window on record start and falls back to clipboard if focus drifts; injects text directly in macOS Ghostty and `tmux` without touching your clipboard (other windows snapshot and restore clipboard payloads); paste-only by default so a misheard command never executes by accident.
- **100% Non-Interactive CLI → Built for Your AI Agent to Install & Troubleshoot**: Every command runs without `y/n` prompts and supports structured `--json` diagnostics (`doctor --ping --json`, `diagnose --json`), so you can hand setup and troubleshooting straight to Codex, Claude Code, or AGY.

---

## Platform & Terminal Support

| Environment | Status | Supported Terminals, Editors & Notes |
| :--- | :--- | :--- |
| **macOS** *(Apple Silicon & Intel)* | **Supported** | Verified on **Ghostty**, built-in **Terminal.app**, and **`tmux`**; built-in window-scoping, session discovery & paste support for **iTerm2**, **WezTerm**, **Kitty**, **Alacritty**, **Warp**, desktop IDEs, and everyday GUI apps |
| **Windows + WSL2** | **Supported** | Verified on **Windows Terminal** (incl. Remote Desktop & **`tmux`**); built-in per-process paste routing for **WezTerm**, **Alacritty**, **Tabby**, **Hyper**, **mintty** / **PuTTY**, desktop IDEs, and everyday GUI apps |
| **SSH Remote Linux** | **Supported (Zero-Install)** | Headless Linux servers need no `agent-voice-input` install, daemon, or API keys—only built-in Python `>= 3.10`; blocks local session crosstalk out of the box, and whitelisting `[remote] hosts` prefetches remote CLI dialogue & repo filenames |
| **Desktop IDEs & Everyday Apps** | **Supported** | **VS Code**, **Cursor**, **Windsurf**, **Zed**, **JetBrains**, **Xcode**, **Antigravity IDE** (workspace-grounded, isolated from terminal sessions), plus IM, browsers, and general desktop apps (`hotkey.scope = "all"` by default) |
| **Native Linux Desktop & Standalone Windows (No WSL2)** | **Not Yet Native** | No native global hotkey / floating HUD yet on Wayland/X11 or Windows without WSL2 (CLI `once` / `toggle` and headless SSH remote targets work on Linux today) |

> [!TIP]
> **Zero hooks, zero `tmux` config, desktop-wide out of the box**: Single-window terminals, macOS Ghostty splits, `tmux` panes (no `~/.tmux.conf` edits), desktop IDEs, and everyday apps all work immediately. Optional title hooks or a 1-line `[remote] hosts` whitelist are only needed for multi-window same-directory WSL2 tabs or remote SSH context prefetch.

---

## 1-Minute Quick Start

### Path A: Let Your AI Agent Install & Configure It (Recommended)

Because the CLI is **100% non-interactive** (zero `y/n` prompts), paste this directly into **Codex**, **Claude Code**, or **AGY**:

```text
Please install and configure agent-voice-input (https://github.com/xiaohuaxi/agent-voice-input) for me:
1. Check python3 (>=3.10) and ffmpeg (plus Xcode Command Line Tools `/usr/bin/swiftc` on macOS), then install via `uv tool install agent-voice-input` (fall back to `pipx` if `uv` is unavailable).
2. Run `agent-voice-input init` to create 0600 `~/.config/agent-voice-input/config.toml` and `agent-voice-input.env`.
3. Check if a Stage 1 speech-capable API key is already configured (DASHSCOPE_API_KEY, DOUBAO_SPEECH_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, GROQ_API_KEY, or SILICONFLOW_API_KEY; optional Stage 2 keys like DEEPSEEK_API_KEY / ARK_API_KEY or local codex/agy CLI login can pair with it); if none exists, ask which provider I prefer and add it to `agent-voice-input.env`.
4. Run `agent-voice-input doctor --ping`, then start the background daemon with `agent-voice-input start`.
```

### Path B: Manual Setup in 3 Steps (`v0.13.0`)

```bash
# 1. Install ffmpeg + CLI (WSL2: `sudo apt install ffmpeg` | macOS: `brew install ffmpeg` + `xcode-select --install`)
uv tool install agent-voice-input          # or `pipx install agent-voice-input`

# 2. Create 0600 config.toml & agent-voice-input.env, then uncomment at least one Stage 1 key in .env
agent-voice-input init

# 3. Verify connectivity & start daemon (tap Right Ctrl <= 0.5s to start/stop; hold 0.8s to cancel)
agent-voice-input doctor --ping && agent-voice-input start
```

> MacBook keyboards without a Right Ctrl key can set `key = "ROption"` or `"Fn"` under `[hotkey]` in `~/.config/agent-voice-input/config.toml` and run `agent-voice-input restart`. Full templates: [`v0.13.0/config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.13.0/config.example.toml) ([`main`](config.example.toml)) and [`v0.13.0/config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.13.0/config.example.env) ([`main`](config.example.env)).

### Recommended Provider Combinations by Scenario

| Scenario | Recommended Stage 1 (ASR) | Recommended Stage 2 (Refiner) | Keys / Setup Needed |
| :--- | :--- | :--- | :--- |
| **Reuse Existing Codex / AGY Subscription** *(zero extra Stage 2 API billing)* | `dashscope`, `doubao`, `groq`, or `openai` | `codex` *(auto-detected by `auto`; never enables Fast mode)* or `agy` *(explicit opt-in)* | 1 Stage 1 speech key + local `codex` or `agy` CLI login |
| **Chinese / Mixed Chinese–English Daily Driver** | `dashscope` (`qwen3-asr-flash`) or `doubao` (`bigmodel`) | `codex` / `agy` / `deepseek` / `dashscope` / `volcengine` | `DASHSCOPE_API_KEY` or `DOUBAO_SPEECH_API_KEY` *(single-header `X-Api-Key`, no `APP_ID` needed)* + optional Stage 2 key |
| **English / Global Daily Driver** | `groq` (`whisper-large-v3-turbo`) or `openai` (`gpt-transcribe`) | `codex` / `agy` / `openai` / `gemini` / `openrouter` | `GROQ_API_KEY` or `OPENAI_API_KEY` + optional Stage 2 key |
| **Single-Key Minimal Setup** | `gemini` *(native single-hop)*, `openrouter` *(two-stage or single-hop)*, or `dashscope` *(two-stage or `qwen3.8-omni-flash` single-hop)* | Built-in or skipped in single-hop | Just 1 key: `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or `DASHSCOPE_API_KEY` |

> **On local / self-hosted models**: This release focuses on thoroughly tested mainstream cloud APIs and existing CLI logins (`codex` / `agy`). While a generic `openai-compatible` custom `base_url` hook already exists under the hood, fully verified **first-class support for local / private self-hosted models** (compact local ASR + refiner) is planned for **`v1.1+`**.

---

## Privacy & Data-Flow Commitments

1. **No Author Servers, Zero Middleman — 100% User-Controlled**: `agent-voice-input` has **no author-hosted servers, relays, or hidden telemetry**. All audio and context go directly from your machine to the model provider you explicitly configure; every model and credential stays strictly under your own control (Apache-2.0 open source).
2. **No Audio or Transcripts Stored by Default**: Status snapshots, logs, and telemetry redact secrets and never write user speech transcripts. Audio is only stashed (`0600`, 24h TTL, max 10 clips) in `failed/` when recognition/delivery fails or an in-flight job is cancelled so `retry` can recover it (recording-time cancels drop immediately; optional MP3 archiving is off by default).
3. **`0700`/`0600` Isolation & No Invasive Scraping**: Runtime/config files enforce private permissions. Context lookup only inspects the focused window title, macOS `AXDocument`, and plain-text CLI agent logs—never IDE internal SQLite DBs and never Accessibility/UIAutomation DOM trees in chat apps or browsers. Non-terminal windows hard-skip terminal sessions, and SSH remote probing only queries explicitly whitelisted hosts with zero remote keys.
4. **Scoped Context Only (Never Full Repo) & Transparent `auto` Detection**: Coding mode sends only recent dialogue from the active window plus `git status`/shallow filenames—never your full repository source. Note that default `provider = "auto"` checks your environment/`.env` keys and existing local `codex` CLI login (`agy` is explicit opt-in only and never auto-selected); set explicit `provider` values or `refiner.provider = "off"` if you want fixed routing.

---

## FAQ & Common Gotchas

- **My MacBook doesn't have a Right Ctrl key—what should I use?**  
  Set `key = "ROption"` (Right Option) or `key = "Fn"` under `[hotkey]` in `~/.config/agent-voice-input/config.toml`, then run `agent-voice-input restart`. You can inspect raw keycodes anytime with `agent-voice-input key-test`.
- **Why does macOS record pure silence when I start the daemon over SSH?**  
  macOS binds Microphone TCC permission to the responsible GUI process; daemons launched under `sshd` receive silent all-zero audio frames. Always run `agent-voice-input start` / `restart` inside an authorized local GUI terminal (such as Ghostty or `Terminal.app`).
- **How do I route the background daemon through a proxy?**  
  The daemon inherits environment variables at startup: export `HTTPS_PROXY` (and `NO_PROXY=127.0.0.1,localhost` for loopback endpoints) in your shell, then run `agent-voice-input restart`. Because the pure-stdlib HTTP client only speaks `http://` proxies, if your proxy client only sets `ALL_PROXY=socks5://...`, also export its HTTP/HTTPS mixed port (`HTTPS_PROXY=http://127.0.0.1:<port>`) and verify with `agent-voice-input doctor --ping`.
- **Why does it only paste and never auto-press Enter?**  
  In terminals and SSH sessions, auto-executing a misheard command is dangerous. Paste-only lets you glance before hitting Enter, and if you switch windows mid-processing, output safely falls back to your clipboard instead of pasting into the wrong window.
- **Should I choose two-stage (STT + Refiner) or single-hop multimodal?**  
  **Two-stage** gives the widest provider choice, lets you reuse existing Codex/AGY subscriptions with zero extra Stage 2 fee, and handles long technical dictation most reliably. **Single-hop** (`gemini`, DashScope `qwen3.8-omni-flash`, or OpenRouter multimodal chat-audio) is simpler with one request and one key, and sends audio plus session context directly to that single multimodal provider.

---

## CLI Commands & Optional Integrations

```bash
agent-voice-input init                   # Create 0600 config.toml and commented agent-voice-input.env (--force overwrites)
agent-voice-input start | restart | stop # Manage background daemon (start is idempotent and warns on code/version drift)
agent-voice-input status                 # Inspect daemon health, active pipeline, and drift (--history N)
agent-voice-input doctor [--ping] [--remote] [--json]   # Check audio, native shim, credentials, SSH readiness, and endpoint ping
agent-voice-input diagnose [--last N] [--json]          # Read-only diagnostics: latency p50, cache hits, failed/ count, redacted log tail
agent-voice-input retry [--list] [--print]              # List or re-run stashed failed recordings
agent-voice-input hotwords list|add|remove              # Manage [global]/[chat]/[coding] hotwords & check provider quotas (--scope, --json)
agent-voice-input once --duration 4 --dry-run           # Record once and print without pasting
agent-voice-input toggle | cancel        # Trigger start/stop or cancel active/in-flight jobs (--seq N)
agent-voice-input key-test               # Inspect raw key scancodes / modifier events
```

- **Optional multi-window hooks & SSH remote Linux (`[remote]`)**: Single-window terminals, Ghostty splits, and `tmux` panes work with zero hooks. For multiple standalone WSL2 windows in the **same project directory**, or to enable zero-install SSH remote context (`[remote] hosts = ["devbox"]`, verified with `agent-voice-input doctor --remote`), see [`integrations/README.md`](integrations/README.md) (SSH section: [SSH Remote Linux Hosts](integrations/README.md#5-ssh-remote-linux-hosts-zero-install--optional-title-hook)) and [`docs/context-routing.md`](docs/context-routing.md).

---

## Detailed Reference

<details>
<summary><b>Platform Details (WSL2 WASAPI / macOS TCC Permissions & Delivery)</b></summary>

### Windows Terminal + WSL2 (Including Remote Desktop & `tmux`)
- **Global Scope, Remote-Desktop Yielding & HiDPI Capsule**: Isolated Right Ctrl tap toggles recording across terminals, IDEs, and desktop apps by default (`hotkey.scope = "all"`, or `"coding"` / `"terminals"`; chords like `RControl+C` pass through untouched) with an anti-aliased Per-Monitor V2 HiDPI capsule. Remote desktop / VM clients (`mstsc`, `vmconnect`, Parsec, RustDesk, etc.) automatically yield new triggers (`yield_remote_desktop = true`; bare `msrdc` stays unblocked for Win11 WSLg—non-WSLg AVD / Windows 365 users can add `ignore_apps = ["msrdc"]`), and per-process paste routing handles `Ctrl+V`, `Ctrl+Shift+V` (`WezTerm`, `Alacritty`, `Tabby`, `Hyper`), and `Shift+Insert` (`mintty`, `PuTTY`, `KiTTY`).
- **Native WASAPI Capture**: Captures 16 kHz audio via Windows WASAPI in a resident `win_shim`, avoiding WSLg `RDPSource` zero-packet deadlocks; start/stop beeps default to off to avoid RDP audio wake-up lag.
- **Zero-Config `tmux` Pane Resolution**: Tracks the active pane of the attached `tmux` client without editing `~/.tmux.conf`.

### macOS (`Ghostty`, `Terminal.app` & `tmux`)
- **Native Retina HUD & Context-Safe Delivery**: Uses a cached native `mac_shim` compiled via `/usr/bin/swiftc` for Right Ctrl / `ROption` / `Fn` tap or `ptt` hold, remote-desktop hotkey yielding (`Windows App`, `Screen Sharing`, Parsec, VMware/Parallels, etc.), and Retina capsule rendering. Injects directly in **Ghostty** and **`tmux`** without touching your system clipboard; other apps lock onto the origin window ID and paste via native `CGEvent` `Cmd+V`.
- **Zero-Hook Tab/Split & `tmux` Binding**: Reads Ghostty split `tty`/`cwd`, `Terminal.app` selected tab `tty`, or child PTY trees (plus active `tmux` pane) to bind `codex`, `claude`, or `agy` sessions while skipping session lookup in non-terminal GUI apps. Audio input defaults to system mic (`[audio] macos_device = "default"`).
- **Minimal macOS Permissions (TCC)**: Requires **Microphone** (for `ffmpeg`) plus **Automation → Ghostty** (or **Terminal** for multi-tab `Terminal.app`). **Input Monitoring** is **not required** for default modifier hotkeys (`RControl` / `ROption` / `Fn`). **Accessibility** is only needed for native `Cmd+V` paste into desktop GUI apps and non-Ghostty terminals outside `tmux` (not needed inside Ghostty or `tmux`).

</details>

<details>
<summary><b>Built-in Providers & Common <code>config.toml</code> Recipes</b></summary>

Configuration priority: `CLI flags > ~/.config/agent-voice-input/config.toml > built-in defaults`. Credentials live in `~/.config/agent-voice-input/agent-voice-input.env` (see [`config.example.env`](config.example.env)).

| `provider` | Stages | Built-in Default `asr.model` | Built-in Default `refiner.model` | Credential Source / Env |
| :--- | :--- | :--- | :--- | :--- |
| **`codex`** | `refiner` | — | `gpt-6-luna` *(auto-falls back to public `gpt-5.4-mini` / `gpt-5-codex-mini`)* | Local `codex` CLI login *(stateless HTTP `/responses` + 30m safe refresh; never enables Fast mode)* |
| **`agy`** | `refiner` | — | `gemini-3.8-flash` *(default `effort = "low"`; explicit opt-in only, never chosen by `auto`)* | Local `agy` CLI login *(zero-tool `agy acp` stdio JSON-RPC with incremental session resume)* |
| **`deepseek`** | `refiner` | — | `deepseek-flash` | `DEEPSEEK_API_KEY` *(compat: `API_KEY_DEEPSEEK`)* |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` *(or single-hop `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *(single-header `X-Api-Key`, no `APP_ID` needed; compat: `DOUBAO_ASR_API_KEY` or legacy `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`; `auto` prefers sync `volc.bigasr.auc_turbo` with cached fallback to `volc.seedasr.auc` / `volc.bigasr.auc`)* |
| **`volcengine`** | `refiner` | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-3.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-3.7-flash` *(single-hop)* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-transcribe` | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs explicit `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |

- `asr.provider = "auto"` (default): picks the first configured credential in order `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow`.
- `refiner.provider = "auto"` (default): prefers local `codex` CLI login when present, else picks `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, or degrades cleanly to `"off"`. (`agy` is explicit opt-in only.)

```toml
# Recipe 1: Reuse local Codex or AGY CLI login for Stage 2 + fast Stage 1 STT
[asr]
provider = "dashscope"           # Or "groq" / "openai" / "doubao"

[refiner]
provider = "codex"               # Or "agy" for Antigravity CLI login
```

```toml
# Recipe 2: Single-key two-stage via OpenRouter (uses built-in defaults)
[asr]
provider = "openrouter"

[refiner]
provider = "openrouter"
```

```toml
# Recipe 3: Single-hop multimodal audio + 50K cached session context (skips Stage 2)
[asr]
provider = "gemini"              # Or "dashscope" + model = "qwen3.8-omni-flash", or "openrouter" + model = "google/gemini-3.5-flash-lite"
effort = "auto"
```

</details>

<details>
<summary><b>Advanced Configuration Mechanics (Protocols, Caching, Hotwords, Retries & Archiving)</b></summary>

- **Provider-first defaults (`model`)**: Leave `model = ""` to use each provider's built-in default; overriding `provider` via CLI without `--*-model` resets to the new provider's default. Explicit `model` values are passed upstream verbatim.
- **Two-stage vs. single-hop (`asr.api`)**: `"auto"` routes dedicated STT models (`qwen3-asr-flash`, `doubao`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`) to Stage 1 + Stage 2, and routes native `gemini`, DashScope `qwen*-omni*`, and multimodal chat-audio models on OpenRouter/OpenAI-compatible to single-hop (sharing the 50K sealed `<session_reference_log>` prefix blocks, in-memory `64 kbps` MP3 compression for `>= 3s` clips, and `610s` single-chunk cap; skips Stage 2 unless `refiner.force_on_single_hop = true`). For uncataloged models on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **Stage 2 protocol & reasoning budget (`refiner.api`, `effort`)**: `refiner.api = "auto"` uses stateless HTTP `/responses` (`store=false`, `stream=true`) for `codex`, local `agy acp` stdio JSON-RPC for `agy`, native `:generateContent` for `gemini`, and `/chat/completions` elsewhere (`openai`/`openrouter`/`openai-compatible` can opt into `"responses"`). `effort` accepts `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"` (`refiner.effort` defaults to `"low"` on `codex`/`agy`/`openai`, and `"auto"` zero-thinking fast mode elsewhere).
- **Prompt caching & cold-start fallback (`refiner.prompt`, `include_git_status`, `context_max_chars`, `cold_start_fallback`)**: Leave `prompt = ""` to use built-in adaptive `agent` / `general` dictation-editor prompts (non-empty `prompt` overrides both). Dialogue up to `context_max_chars = 200000` is evicted in `50K` sealed prefix blocks ahead of `<workspace_git_status>` (falls back to core tracked filenames when clean) and `<asr_draft_to_clean>`. When a new coding session starts empty/short, `cold_start_fallback = "auto"` (or `"hotwords"` / `"off"`) supplements Stage 1 hotwords and prepends one frozen `<= 25K` `<prior_session_reference>` block from recent exact-same-directory sessions.
- **Layered hotwords (`asr.hotwords_file` & `hotwords` CLI)**: Edit `~/.config/agent-voice-input/hotwords.txt` or use `agent-voice-input hotwords list|add|remove`—hot-reloads automatically on the next dictation. Flat files map to `[global]`; optional `[chat]` and `[coding]` sections auto-mount ahead of dynamic git/session terms and built-in developer hotwords, with per-provider quota inspection (`doubao`, `whisper`, `dashscope`, `gemini`).
- **Same-provider retries & optional MP3 archive (`max_retries`, `audio.save_recordings`)**: Both stages default to `max_retries = 2` (`0..10`) under stage `timeout_sec` with a 9s connect/TLS handshake cap, separating `429`/QPS throttling from transient network/5xx errors. Setting `audio.save_recordings = true` asynchronously archives voiced clips as `0600` `16 kHz` `64 kbps` MP3s in `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/` (ring-pruned to `save_recordings_max_count = 300`).

</details>

<details>
<summary><b>Runtime CLI Override Flags</b></summary>

| CLI Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `config.toml` or `config.json` (also supports `AGENT_VOICE_INPUT_CONFIG` and `XDG_CONFIG_HOME`) |
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`; aliases `volcengine`/`volc` → `doubao`, `bailian` → `dashscope`) |
| `--asr-model` | Stage 1 upstream model ID |
| `--asr-api` | Stage 1 protocol (`auto`, `audio-transcriptions`, `chat-completions`) |
| `--asr-effort` | Stage 1 thinking budget |
| `--refiner`, `--refiner-provider` | Stage 2 provider (`auto`, `codex`, `agy`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`; aliases `doubao`/`volc`/`ark` → `volcengine`, `bailian` → `dashscope`) |
| `--refiner-model` | Stage 2 upstream model ID |
| `--refiner-api` | Stage 2 protocol (`auto`, `chat-completions`, `responses`) |
| `--refiner-effort`, `--effort` | Stage 2 thinking budget |

*(Environment variables are reserved for API credentials, bootstrap paths `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR`, and low-level runtime/diagnostic hooks; ordinary options live in `config.toml`.)*

</details>

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

[Apache-2.0 License](LICENSE)
