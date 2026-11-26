**English** | [简体中文](https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.zh-CN.md)

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)

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

## Quick Start in 60 Seconds

### Path A: Let Your AI Agent Set It Up (Recommended)

The CLI is **100% non-interactive** (zero `y/n` prompts) with structured `--json` diagnostics. Paste this into **Codex**, **Claude Code**, or **AGY**:

```text
Install and configure agent-voice-input following docs/agent-setup.md (https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md):
1. Respect my local keyboard layout (auto-use ROption on MacBook built-in keyboards; keep RControl on desktop Macs such as Mac mini with external keyboards and on Windows/WSL2).
2. Only run `agent-voice-input integrate` if my setup actually needs hooks (e.g., multiple non-tmux WSL2 windows in the same directory).
3. After `doctor --ping --json` and `start`, guide me through a quick live or `--dry-run` connectivity check.
```

### Path B: Manual Setup in 3 Steps

```bash
# 1. Install prerequisites (macOS: `brew install ffmpeg` + `xcode-select --install` | WSL2: `sudo apt install ffmpeg`)
uv tool install agent-voice-input          # or `pipx install agent-voice-input` / `pip install agent-voice-input`

# 2. Initialize 0600 config.toml & agent-voice-input.env (optional: `--asr`, `--refiner`, `--hotkey`)
agent-voice-input init

# 3. Verify connectivity & start daemon (first start prints a quick test-dictation tip)
agent-voice-input doctor --ping && agent-voice-input start
```

- **Keyboard-aware defaults**: `init` automatically sets `key = "ROption"` on MacBook built-in keyboards, and keeps `key = "RControl"` on desktop Macs (e.g., Mac mini with an external full-size keyboard) and Windows/WSL2. Tap hotkey `<= 0.5s` to start/stop; hold `0.8s` to cancel.
- **Simplest 1-key setup**: `init` auto-detects existing environment keys and local `codex` CLI logins and tells you immediately if you are ready out of the box. If you have no Stage 1 key yet, add just **1 key** to `~/.config/agent-voice-input/agent-voice-input.env`:
  - **Mainland China**: `DASHSCOPE_API_KEY` or `DEEPSEEK_API_KEY`
  - **Global / Overseas**: `OPENAI_API_KEY` or `GEMINI_API_KEY`
- Templates & full agent guide: [`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml), [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env), and [`docs/agent-setup.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md).

> **Tip:** **Zero hooks required for ~80% of daily workflows**: macOS Ghostty / `Terminal.app` / `iTerm2`, local or remote `tmux` on any platform (no `~/.tmux.conf` edits), all desktop apps, and WSL2 single-window or distinct-directory windows route accurately out of the box. Only if you run multiple standalone non-`tmux` windows in the **same directory** (e.g., Windows Terminal on WSL2) do you ever need one optional command: `agent-voice-input integrate` (see [`integrations/README.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md)).

---

## Core Technical Highlights

- **Two-Stage & Single-Hop + 50K Block Prefix Caching**: Pair fast Stage 1 STT with lightweight Stage 2 refinement, or run single-hop multimodal audio; session history is sealed into immutable 50K prefix blocks for high cache hits while back-to-back utterances refine concurrently and paste in strict FIFO order.
- **Focus-Aware Terminal, IDE & SSH Routing**: Automatically binds to the active session and `git status` filenames in your focused terminal, Ghostty split, or `tmux` pane; desktop IDEs mount only workspace context without leaking terminal sessions; chat/everyday apps switch to clean general dictation; SSH terminals block local leaks by default and support zero-install remote probing via a 1-line `[remote] hosts` whitelist.
- **Zero Third-Party Python Dependencies + Acoustic Silence Gating & Trimming**: Pure Python `>= 3.10` standard library plus host `ffmpeg` and minimal native shims (no Electron); local silence gating, adaptive long-pause compression (`[audio].silence_trim = "balanced" | "conservative" | "off"`), and loudness normalization filter empty triggers and shorten long thinking pauses before API calls, recordings support up to 10 minutes (auto-splitting `> 3 min` clips at pauses), and failed or in-flight-cancelled clips stash (`0600`) for `retry`.
- **Zero-Clipboard Injection in macOS Ghostty/`tmux` + Never Auto-Presses Enter**: Locks onto the origin window on record start and falls back to clipboard if focus drifts; injects directly in **macOS** Ghostty and **macOS** `tmux` without touching your clipboard (WSL2—including inside `tmux`—and other windows snapshot and restore clipboard contents); paste-only by default so misheard commands never auto-execute.
- **100% Non-Interactive CLI for Human & Agent Workflows**: Every command runs without `y/n` prompts and supports `--json` output (`doctor --ping --json`, `diagnose --json`, `integrate --status --json`), so both humans and AI agents can install, verify, and troubleshoot cleanly.

---

## Support & Verification Matrix

**Status Legend**:
- `✅` **Verified on hardware**: Used daily or verified end-to-end by the author on real devices.
- `🧪` **CI & unit-tested (seeking field reports)**: Dedicated adaptation implemented and covered by offline unit tests or automated CI checks (e.g., real `tmux`, real `sshd`, `mac_shim`/`win_shim` self-checks), not yet verified on that specific client or account on real hardware—feedback welcome to light up `✅`.
- `⚪` **Generic fallback**: Works out of the box under default `hotkey.scope = "all"` with refined paste and window-locked anti-crosstalk, without app-specific context or tone adaptation.

### Platforms, Terminals, SSH & Key Desktop Apps

| Category | Target | Status | Scope, Delivery & Boundaries |
| :--- | :--- | :---: | :--- |
| **Host OS** | **macOS** *(Apple Silicon / Intel)* | `✅` | Daily driver; native `mac_shim` HUD capsule + system microphone |
| **Host OS** | **Windows + WSL2** | `✅` | Daily driver; `win_shim` native WASAPI capture + Per-Monitor V2 HiDPI capsule |
| **Host OS** | **SSH Remote Linux Host** | `✅` | Zero-install remote probe (Python `>= 3.10` only) + local crosstalk blocking; verified via Ubuntu CI real `sshd` E2E |
| **Host OS** | **Native Linux Desktop / Standalone Windows (No WSL2)** | `—` | Daemon, global hotkey & `toggle` not yet supported natively; native Linux currently supports offline file transcription (`once --file <wav> --dry-run`) and headless SSH remote targets |
| **Terminal** | **macOS Ghostty** | `✅` | AppleScript zero-clipboard direct injection + same-split `tmux` pane anti-crosstalk + zero-hook full context *(Codex / Claude Code / AGY CLI hardware-verified)* |
| **Terminal** | **Windows Terminal (WSL2)** | `✅` | `win_shim` transient clipboard + shortcut paste (preserves clipboard) + same-window tab anti-crosstalk + hook/`tmux` precision context *(Codex / Claude Code / AGY CLI hardware-verified)* |
| **Terminal** | **Local & Remote `tmux`** | `✅` | **macOS**: zero-clipboard direct injection / **WSL2**: `win_shim` transient clipboard + paste (preserves clipboard); process tree isolated by focused pane; Linux & macOS CI real `tmux` E2E |
| **Terminal** | **Other Built-in Terminals & Dedicated SSH Clients** | `🧪` | Built-in paste-chord routing, anti-crosstalk & client rules (see full list below); seeking real-hardware feedback |
| **Desktop App** | **Antigravity / AGY Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS for **Antigravity 2.0 standalone desktop** (read-only `conversation_summaries.db` + exact/truncated-prefix title matching, workspace `git`/filenames + `brain/` incremental cache, and thread switching); legacy VS Code Fork also supported *(Windows desktop pending field reports; WSL2 scans WSL paths only)* |
| **Desktop App** | **ChatGPT / Codex Unified Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS: the unified desktop app (`com.openai.codex`) reads `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl` read-only to auto-promote local repo tasks to `agent` mode with project hotwords & incremental cache, keeps cloud ChatGPT chats in `general` + `ai_chat` with zero crosstalk, and re-verifies same-window thread switching before paste; the legacy standalone ChatGPT app (`com.openai.chat`) stays in `general` + `ai_chat` with refined paste & window-locked anti-crosstalk, and Windows (`ChatGPT.exe` / `Codex.exe`) currently provides refined paste & window-locked anti-crosstalk *(Windows/WSL2 cross-OS context binding is on the roadmap)* |
| **Desktop App** | **Claude Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS (`com.anthropic.claudefordesktop`): **Code & Cowork** local session binding (bounded read-only `AXWebArea` route/title probe, joint `Claude`/`Claude-3p` metadata scan, `50K` incremental cache, and pre-paste same-window thread verification) plus refined paste with clipboard restore, while cloud Chat stays in `general` + `ai_chat` with zero crosstalk *(Windows `Claude.exe` pending field reports)* |
| **Desktop App** | **Other Desktop IDEs & Mainstream IM** | `🧪` | IDEs (**VS Code**, **Cursor**, **Windsurf**, **JetBrains**, **Zed**, etc.) run as `coding_gui` with workspace `git`/filenames; **running CLI agents inside an IDE's integrated terminal is also treated as `coding_gui`** (skips CLI `sync_session`, does not read `codex`/`claude`/`agy` history inside the integrated terminal, and does not distinguish integrated terminal splits). IM apps (**WeChat**, **Feishu/Lark**, **Slack**, **Discord**, etc.) run as `im_chat` |
| **Desktop App** | **Any Other Desktop App** *(browsers, notes, email, etc.)* | `⚪` | Works out of the box under default `hotkey.scope = "all"` with refined paste + window-locked anti-crosstalk |

<details>
<summary><b>Full List of Built-in Terminals & Dedicated SSH Clients (<code>🧪</code>)</b></summary>

- **macOS Terminals (`🧪`)**: `Terminal.app`, `iTerm2`, `WezTerm`, `Kitty`, `Alacritty`, `Warp`, `Hyper`, `Rio`, `Tabby`
- **Windows Terminals (`🧪`)**: `WezTerm`, `Alacritty`, `Ghostty`, `Tabby`, `Hyper`, `Rio`, `Warp`, `Mintty`, `PuTTY`, `KiTTY`
- **Dedicated SSH Clients (`🧪`)**: `MobaXterm`, `Xshell`, `WindTerm`, `SecureCRT`, `Termius`, `Electerm`, `FinalShell`, `RoyalTS`

</details>

> Using a `🧪` or `⚪` environment? If it works smoothly—or if you run into an edge case—please open an Issue with your **OS + terminal/app + provider** to help us light up more `✅`.

---

## Recommended Provider Combinations by Scenario

| Scenario | Recommended Stage 1 (ASR) | Recommended Stage 2 (Refiner) | Keys / Setup Needed |
| :--- | :--- | :--- | :--- |
| **Reuse Existing Codex / AGY Subscription** *(zero extra Stage 2 API billing)* | `dashscope` `✅`, `doubao` `✅`, `openai` `✅`, `deepseek` `✅`, or `groq` `🧪` | `codex` `✅` *(auto-detected by `auto`; never enables Fast mode)* or `agy` `✅` *(explicit opt-in)* | 1 Stage 1 speech key + local `codex` or `agy` CLI login |
| **Chinese / Mixed Chinese–English Daily Driver** | `dashscope` (`qwen3-asr-flash` `✅`), `doubao` (`bigmodel` `✅`), or `deepseek` (`deepseek-asr` `✅`) | `codex` `✅` / `agy` `✅` / `deepseek` `✅` / `dashscope` `✅` / `volcengine` `✅` | `DASHSCOPE_API_KEY`, `DOUBAO_SPEECH_API_KEY` *(single-header `X-Api-Key`, no `APP_ID` needed)*, or `DEEPSEEK_API_KEY` + optional Stage 2 key |
| **English / Global Daily Driver** | `openai` (`gpt-transcribe` `✅`) or `groq` (`whisper-large-v3-turbo` `🧪`) | `codex` `✅` / `agy` `✅` / `openai` `✅` / `gemini` `✅` / `openrouter` `✅` | `OPENAI_API_KEY` or `GROQ_API_KEY` + optional Stage 2 key |
| **Single-Key Minimal Setup** | `deepseek` `✅` *(cost-effective two-stage: `deepseek-asr` + `deepseek-flash`)*, `dashscope` `✅` *(two-stage or `qwen3.8-omni-flash` single-hop)*, `gemini` `✅` *(native single-hop)*, `openrouter` `✅` *(two-stage or single-hop)*, or `openai` `✅` *(two-stage or `gpt-audio-1.5` / `gpt-audio-mini` single-hop)* | Built-in default for two-stage, or skipped in single-hop | Just 1 key: `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, or `OPENAI_API_KEY` |

> **On local / self-hosted models**: This release focuses on thoroughly tested mainstream cloud APIs and existing CLI logins (`codex` / `agy`). While a generic `openai-compatible` custom `base_url` hook already exists under the hood (`🧪`), fully verified **first-class support for local / private self-hosted models** (compact local ASR + refiner) is planned for **`v1.1+`**.

---

## Privacy & Data-Flow Commitments

1. **No Author Servers, Zero Middleman — 100% User-Controlled**: `agent-voice-input` has **no author-hosted servers, relays, or hidden telemetry**. All audio and context go directly from your machine to the model provider you explicitly configure; every model and credential stays strictly under your own control (Apache-2.0 open source).
2. **No Audio or Transcripts Stored by Default**: Status snapshots, logs, and telemetry redact secrets and never write user speech transcripts. Audio is only stashed (`0600`, 24h TTL, max 10 clips) in `failed/` when recognition/delivery fails or an in-flight job is cancelled so `retry` can recover it (recording-time cancels drop immediately; optional MP3 archiving is off by default).
3. **`0700`/`0600` Isolation & No Invasive Scraping**: Runtime/config files enforce private permissions. Context lookup only inspects the focused window title, macOS `AXDocument`, plain-text CLI agent logs, and read-only AGY/Codex session indexes—never VS Code/Cursor internal state DBs (`state.vscdb`) and never Accessibility/UIAutomation DOM trees in chat apps or browsers. Non-terminal windows hard-skip terminal CLI sessions, and SSH remote probing only queries explicitly whitelisted hosts with zero remote keys.
4. **Scoped Context Only (Never Full Repo) & Transparent `auto` Detection**: Coding mode sends only recent dialogue from the active window plus `git status`/shallow filenames—never your full repository source. Note that default `provider = "auto"` checks your environment/`.env` keys and existing local `codex` CLI login (`agy` is explicit opt-in only and never auto-selected); set explicit `provider` values or `refiner.provider = "off"` if you want fixed routing.

---

## FAQ & Common Gotchas

- **My MacBook doesn't have a Right Ctrl key—what should I use?**  
  `agent-voice-input init` automatically detects MacBook built-in keyboards and sets `key = "ROption"` (Right Option), while desktop Macs (e.g., Mac mini) and external full-size keyboards keep `key = "RControl"`. You can also switch anytime under `[hotkey]` in `~/.config/agent-voice-input/config.toml` (`"ROption"`, `"Fn"`, or `"RControl"`), run `agent-voice-input restart`, or inspect raw keycodes with `agent-voice-input key-test`.
- **Why does macOS record pure silence when I start the daemon over SSH?**  
  macOS binds Microphone TCC permission to the responsible GUI process; daemons launched under `sshd` receive silent all-zero audio frames. Always run `agent-voice-input start` / `restart` inside an authorized local GUI terminal (such as Ghostty or `Terminal.app`).
- **How do I route the background daemon through a proxy?**  
  Export `HTTPS_PROXY` (and `NO_PROXY=127.0.0.1,localhost` for loopback endpoints) in your shell or set it in `~/.config/agent-voice-input/agent-voice-input.env` (shell environment variables take precedence), then run `agent-voice-input restart`. Because the pure-stdlib HTTP client only speaks `http://` proxies, if your proxy client only sets `ALL_PROXY=socks5://...`, also set its HTTP/HTTPS mixed port (`HTTPS_PROXY=http://127.0.0.1:<port>`) and verify with `agent-voice-input doctor --ping`.
- **Why does it only paste and never auto-press Enter?**  
  In terminals and SSH sessions, auto-executing a misheard command is dangerous. Paste-only lets you glance before hitting Enter, and if you switch windows mid-processing, output safely falls back to your clipboard instead of pasting into the wrong window.
- **Should I choose two-stage (STT + Refiner) or single-hop multimodal?**  
  **Two-stage** gives the widest provider choice (including single-key `deepseek` two-stage), lets you reuse existing Codex/AGY subscriptions with zero extra Stage 2 fee, and handles long technical dictation most reliably. **Single-hop** (`gemini`, DashScope `qwen3.8-omni-flash`, OpenAI `gpt-audio-1.5` / `gpt-audio-mini`, or OpenRouter multimodal chat-audio) is simpler with one request and one key, and sends audio plus session context directly to that single multimodal provider.

---

## CLI Commands & Optional Integrations

```bash
agent-voice-input init [--asr P] [--refiner P] [--hotkey K] [--force]  # Create 0600 config.toml & agent-voice-input.env with smart defaults
agent-voice-input start | restart | stop                # Manage background daemon (start is idempotent and warns on code/version drift)
agent-voice-input status                                # Inspect daemon health, active pipeline, and drift (--history N)
agent-voice-input doctor [--ping] [--remote] [--json]   # Check audio, native shim, credentials, hooks, SSH readiness, and endpoint ping
agent-voice-input integrate [--status] [--dry-run] [--cli claude|codex|agy] [--uninstall] [--json]  # Safe optional CLI hook setup
agent-voice-input diagnose [--last N] [--json]          # Read-only diagnostics: latency p50, cache hits, failed/ count, redacted log tail
agent-voice-input retry [--list] [--print]              # List or re-run stashed failed recordings
agent-voice-input hotwords list|add|remove              # Manage [global]/[chat]/[coding] hotwords & aliases (--alias, --scope, --json)
agent-voice-input once --duration 4 --dry-run           # Record once and print without pasting (or `--file <wav> --dry-run` on Linux)
agent-voice-input toggle | cancel                       # Trigger start/stop or cancel active/in-flight jobs (--seq N)
agent-voice-input key-test                              # Inspect raw key scancodes / modifier events
```

- **AI Agent setup & troubleshooting contract**: [`docs/agent-setup.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md)
- **Optional multi-window hooks & SSH remote Linux (`[remote]`)**: Single-window terminals, Ghostty splits, and `tmux` panes work with zero hooks. For multiple standalone WSL2 windows in the **same project directory** (use `agent-voice-input integrate`), or to enable zero-install SSH remote context (`[remote] hosts = ["devbox"]`, verified with `agent-voice-input doctor --remote`), see [`integrations/README.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md) (SSH section: [SSH Remote Linux Hosts](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.md#5-ssh-remote-linux-hosts-zero-install--optional-title-hook)) and [`docs/context-routing.md`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/context-routing.md).

---

## Hotwords & Agent-Assisted Vocabulary

Keep your personal names, project brands, and recurring ASR mishearings in a single plain-text file—and let **Claude Code**, **Codex**, or **AGY** bootstrap or tidy it for you:

- **File & 3 scopes**: `~/.config/agent-voice-input/hotwords.txt` (hot-reloads automatically; flat files map to `[global]`; line order = priority). Optional `[global]`, `[chat]`, and `[coding]` sections mount by context—Stage 2 / single-hop uses `[coding] + [global]` in `agent` mode and automatically mounts **`[global] + [chat]`** in `general` mode so WeChat, browsers, GrokBot, and everyday apps all benefit from your personal terms.
- **Optional mishearing aliases (`<-`)**: Write `CanonicalTerm <- mishearing1, mishearing2` or run `agent-voice-input hotwords add Harness --alias 哈尼斯,哈尼思`. Guardrails: max 5 aliases per entry, single-character aliases rejected (2-character aliases warn), aliases cannot collide with canonical terms. Stage 1 ASR receives canonical terms only; Stage 2 / single-hop uses aliases to fix stubborn mishearings.
- **Stage 1 & Stage 2 budgets**: Stage 1 respects tighter per-provider quotas (e.g., Doubao 50 terms / 500 chars, DashScope 80 / 900, OpenAI `keywords[]` 50, Whisper 35 / 353; `deepseek-asr` relies on Stage 2). Stage 2 / single-hop carries a wide **100 items / 1,500 chars** vocabulary budget—inspect both anytime via `agent-voice-input hotwords list [--json]`.
- **Two rules for your Agent**: **always show the diff and wait for your confirmation before editing `hotwords.txt`**, and **never guess mishearing aliases up front—only add `<-` aliases after observing a real ASR mistake**.

Copy-paste prompts for your coding agent:

```text
# 1. Bootstrap from scratch (canonical terms only)
Read my recent projects, READMEs, and specs to draft a clean list of proper nouns I actually speak aloud for ~/.config/agent-voice-input/hotwords.txt ([global]/[chat]/[coding]). Do not guess mishearing aliases; show me the diff first and write only after I confirm.

# 2. Fix a real mishearing on the spot
Voice input just misheard "Harness" as "哈尼斯". Add a mishearing alias with `agent-voice-input hotwords add Harness --alias 哈尼斯` (show me the change first).

# 3. Periodic cleanup & prioritization
Run `agent-voice-input hotwords list --json`, prune low-frequency or stale entries if we are near Stage 1/2 budgets, move my cross-project favorites to the top of [global], and show me the diff before applying.
```

---

## Detailed Reference

<details open>
<summary><b>Built-in Providers, Verification Matrix & Common <code>config.toml</code> Recipes</b></summary>

Configuration priority: `CLI flags > ~/.config/agent-voice-input/config.toml > built-in defaults`. Credentials live in `~/.config/agent-voice-input/agent-voice-input.env` (see [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)).

| `provider` | Stages | Verification | Built-in Default `asr.model` | Built-in Default `refiner.model` | Credential Source / Env |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **`codex`** | `refiner` | `✅` | — | `gpt-6-luna` *(auto-falls back to public `gpt-5.4-mini` / `gpt-5-codex-mini`)* | Local `codex` CLI login *(stateless HTTP `/responses` + 30m safe refresh; never enables Fast mode)* |
| **`agy`** | `refiner` | `✅` | — | `gemini-3.8-flash` *(default `effort = "low"`; explicit opt-in only, never chosen by `auto`)* | Local `agy` CLI login *(zero-tool `agy acp` stdio JSON-RPC with incremental session resume)* |
| **`deepseek`** | `asr`, `refiner` | `✅` *(both stages)* | `deepseek-asr` *(official `POST /v1/tools/asr`; `asr.api = "auto"` only; auto-detects 21 languages, no hotwords/language params)* | `deepseek-flash` | `DEEPSEEK_API_KEY` *(compat: `API_KEY_DEEPSEEK`; 1 key runs full two-stage)* |
| **`dashscope`** | `asr`, `refiner` | `✅` *(STT, single-hop & refiner)* | `qwen3-asr-flash` *(or single-hop `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `✅` *(1.0 & 2.0)* | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *(single-header `X-Api-Key`, no `APP_ID` needed; compat: `DOUBAO_ASR_API_KEY` or legacy `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`; `auto` prefers sync `volc.bigasr.auc_turbo` with cached fallback to `volc.seedasr.auc` / `volc.bigasr.auc`)* |
| **`volcengine`** | `refiner` | `✅` *(`doctor --ping` & refiner)* | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `✅` *(STT, single-hop & refiner)* | `qwen/qwen3-asr-1.7b` | `google/gemini-3.8-flash` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `✅` *(single-hop & refiner)* | `gemini-3.7-flash` *(single-hop)* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `✅` *(STT, single-hop & refiner)* | `gpt-transcribe` *(or single-hop `gpt-audio-1.5` / `gpt-audio-mini`)* | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `🧪` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `🧪` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `🧪` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs explicit `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |
| **`off`** | `refiner` | `✅` | — | *(Stage 2 disabled; raw Stage 1 output)* | None |

- `asr.provider = "auto"` (default): picks the first configured credential in order `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow`.
- `refiner.provider = "auto"` (default): prefers local `codex` CLI login when present, else picks `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, or degrades cleanly to `"off"`. (`agy` is explicit opt-in only.)

```toml
# Recipe 1: Reuse local Codex or AGY CLI login for Stage 2 + fast Stage 1 STT
[asr]
provider = "dashscope"           # Or "doubao" / "openai" / "deepseek" / "groq"

[refiner]
provider = "codex"               # Or "agy" for Antigravity CLI login
```

```toml
# Recipe 2: Single-key two-stage via DeepSeek or OpenRouter (uses built-in defaults)
[asr]
provider = "deepseek"            # Or "openrouter" (with just DEEPSEEK_API_KEY, `auto` already picks deepseek for both stages)

[refiner]
provider = "deepseek"            # Or "openrouter"
```

```toml
# Recipe 3: Single-hop multimodal audio + 50K cached session context (skips Stage 2)
[asr]
provider = "gemini"              # Or "dashscope" + model = "qwen3.8-omni-flash", "openai" + model = "gpt-audio-1.5" / "gpt-audio-mini", or "openrouter" + model = "google/gemini-3.8-flash"
effort = "auto"
```

</details>

<details>
<summary><b>Platform Details (WSL2 WASAPI / macOS TCC Permissions & Delivery)</b></summary>

### Windows Terminal + WSL2 (Including Remote Desktop & `tmux`)
- **Global Scope, Remote-Desktop Yielding & HiDPI Capsule**: Isolated Right Ctrl tap toggles recording across terminals, IDEs, and desktop apps by default (`hotkey.scope = "all"`, or `"coding"` / `"terminals"`; chords like `RControl+C` pass through untouched) with an anti-aliased Per-Monitor V2 HiDPI capsule. Remote desktop / VM clients (`mstsc`, `vmconnect`, Parsec, RustDesk, etc.) automatically yield new triggers (`yield_remote_desktop = true`; bare `msrdc` stays unblocked for Win11 WSLg—non-WSLg AVD / Windows 365 users can add `ignore_apps = ["msrdc"]`). Even inside `tmux`, WSL2 delivers via `win_shim` transient clipboard snapshots + per-process paste shortcuts (`Ctrl+V`, `Ctrl+Shift+V` for `WezTerm`/`Alacritty`/`Ghostty`/`Tabby`/`Hyper`/`Rio`/`Warp`/`WindTerm`/`SecureCRT`/`Termius`/`Electerm`, and `Shift+Insert` for `mintty`/`PuTTY`/`KiTTY`/`MobaXterm`/`Xshell`) without clobbering existing clipboard contents.
- **Native WASAPI Capture**: Captures 16 kHz audio via Windows WASAPI in a resident `win_shim`, avoiding WSLg `RDPSource` zero-packet deadlocks; start/stop beeps default to off to avoid RDP audio wake-up lag.
- **Zero-Config `tmux` Pane Resolution**: Tracks the active pane of the attached `tmux` client without editing `~/.tmux.conf`.

### macOS (`Ghostty`, `tmux` & Other Terminals)
- **Native Retina HUD & Context-Safe Delivery**: Uses a cached native `mac_shim` compiled via `/usr/bin/swiftc` for Right Ctrl / `ROption` / `Fn` tap or `ptt` hold, remote-desktop hotkey yielding (`Windows App`, `Screen Sharing`, Parsec, VMware/Parallels, etc.), and Retina capsule rendering. Injects directly in **macOS Ghostty** and **macOS `tmux`** without touching your system clipboard; other apps lock onto the origin window ID and paste via native `CGEvent` `Cmd+V` with clipboard snapshot/restore.
- **Zero-Hook Tab/Split & `tmux` Binding**: Reads Ghostty split `tty`/`cwd`, `Terminal.app` selected tab `tty`, or child PTY trees (plus active `tmux` pane) to bind `codex`, `claude`, or `agy` sessions while skipping session lookup in non-terminal GUI apps (including IDE integrated terminals). Audio input defaults to system mic (`[audio] macos_device = "default"`).
- **Minimal macOS Permissions (TCC)**: Requires **Microphone** (for `ffmpeg`) plus **Automation → Ghostty** (or **Terminal** for multi-tab `Terminal.app`). **Input Monitoring** is **not required** for default modifier hotkeys (`RControl` / `ROption` / `Fn`). **Accessibility** is only needed for native `Cmd+V` paste into desktop GUI apps and non-Ghostty terminals outside `tmux` (not needed inside Ghostty or `tmux`).

</details>

<details>
<summary><b>Advanced Configuration Mechanics (Protocols, Caching, Hotwords, Retries & Archiving)</b></summary>

- **Provider-first defaults (`model`)**: Leave `model = ""` to use each provider's built-in default; overriding `provider` via CLI without `--*-model` resets to the new provider's default. Explicit `model` values are passed upstream verbatim.
- **Two-stage vs. single-hop (`asr.api`)**: `"auto"` routes dedicated STT models (`qwen3-asr-flash`, `doubao`, `deepseek-asr`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`) to Stage 1 + Stage 2, and routes native `gemini`, DashScope `qwen*-omni*`, official OpenAI `gpt-audio-1.5` / `gpt-audio-mini` (or explicit `asr.api = "chat-completions"`), and multimodal chat-audio models on OpenRouter/OpenAI-compatible to single-hop (sharing the 50K sealed `<session_reference_log>` prefix blocks, in-memory `64 kbps` MP3 compression for `>= 3s` clips, and `610s` single-chunk cap; skips Stage 2 unless `refiner.force_on_single_hop = true`). DeepSeek official ASR (`POST https://api.deepseek.com/v1/tools/asr`) supports `asr.api = "auto"` only (21-language auto-detection, no hotword/language params). For uncataloged models on `openrouter`, set `asr.api = "audio-transcriptions"` or `"chat-completions"` explicitly.
- **Stage 2 protocol & reasoning budget (`refiner.api`, `effort`)**: `refiner.api = "auto"` uses stateless HTTP `/responses` (`store=false`, `stream=true`) for `codex`, local `agy acp` stdio JSON-RPC for `agy`, native `:generateContent` for `gemini`, and `/chat/completions` elsewhere (`openai`/`openrouter`/`openai-compatible` can opt into `"responses"`). `effort` accepts `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"` (`refiner.effort` defaults to `"low"` on `codex`/`agy`/`openai`, and `"auto"` zero-thinking fast mode elsewhere).
- **Prompt caching & cold-start fallback (`refiner.prompt`, `include_git_status`, `context_max_chars`, `cold_start_fallback`)**: Leave `prompt = ""` to use built-in adaptive `agent` / `general` dictation-editor prompts (non-empty `prompt` overrides both). Dialogue up to `context_max_chars = 200000` is evicted in `50K` sealed prefix blocks ahead of `<workspace_git_status>` (falls back to core tracked filenames when clean) and `<asr_draft_to_clean>`. When a new coding session starts empty/short, `cold_start_fallback = "auto"` (or `"hotwords"` / `"off"`) supplements Stage 1 hotwords and prepends one frozen `<= 25K` `<prior_session_reference>` block from recent exact-same-directory sessions.
- **Layered hotwords (`asr.hotwords_file` & `hotwords` CLI)**: Edit `~/.config/agent-voice-input/hotwords.txt` or use `agent-voice-input hotwords list|add [--alias]|remove`—hot-reloads automatically on the next dictation. Flat files map to `[global]`; `[chat]` and `[coding]` auto-mount ahead of dynamic git/session terms and built-in hotwords, optional `Term <- alias1, alias2` rules feed Stage 2 / single-hop (`100 items / 1,500 chars`), and `hotwords list` inspects both Stage 1 provider quotas (`doubao`, `dashscope`, `openai`, `whisper`, `gemini`) and Stage 2 budgets.
- **Same-provider retries & optional MP3 archive (`max_retries`, `audio.save_recordings`)**: Both stages default to `max_retries = 2` (`0..10`) under stage `timeout_sec` with a 9s connect/TLS handshake cap, separating `429`/QPS throttling from transient network/5xx errors. Setting `audio.save_recordings = true` asynchronously archives voiced clips as `0600` `16 kHz` `64 kbps` MP3s in `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/` (ring-pruned to `save_recordings_max_count = 300`).

</details>

<details>
<summary><b>Runtime CLI Override Flags</b></summary>

| CLI Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `config.toml` or `config.json` (also supports `AGENT_VOICE_INPUT_CONFIG` and `XDG_CONFIG_HOME`) |
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `deepseek`, `openai`, `gemini`, `groq`, `openrouter`, `siliconflow`, `openai-compatible`; aliases `volcengine`/`volc` → `doubao`, `bailian` → `dashscope`) |
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

[Apache-2.0 License](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)
