**English** | [简体中文](guide.zh-CN.md)

# User Guide, Compatibility Matrix, Hotwords & Troubleshooting

## Support & Verification Matrix

**Status Legend**:
- `✅` **Verified on hardware**: Used daily or verified end-to-end on real devices.
- `🧪` **CI & unit-tested (seeking field reports)**: Dedicated adaptation covered by offline unit tests or automated CI checks (real `tmux`, real `sshd`, `mac_shim`/`win_shim` self-checks)—feedback welcome to light up `✅`.
- `⚪` **Generic fallback**: Works out of the box under default `hotkey.scope = "all"` with refined paste and window-locked anti-crosstalk, without app-specific context.

| Category | Target | Status | Scope, Delivery & Boundaries |
| :--- | :--- | :---: | :--- |
| **Host OS** | **macOS** *(Apple Silicon / Intel)* | `✅` | Daily driver; native `mac_shim` HUD capsule + system microphone |
| **Host OS** | **Windows + WSL2** | `✅` | Daily driver; `win_shim` native WASAPI capture + Per-Monitor V2 HiDPI capsule |
| **Host OS** | **SSH Remote Linux Host** | `✅` | Zero-install remote probe (Python `>= 3.10` only) + local crosstalk blocking; Ubuntu CI real `sshd` E2E |
| **Host OS** | **Native Linux Desktop / Standalone Windows** | `—` | Global hotkey daemon & `toggle` not yet supported natively; native Linux supports one-shot file transcription without local hotkeys/mic (`once --file <wav> --dry-run` — still calls your configured cloud ASR/refiner provider; `--dry-run` only skips pasting) and headless SSH remote targets |
| **Terminal** | **macOS Ghostty** | `✅` | AppleScript zero-clipboard direct injection + same-split `tmux` pane anti-crosstalk + zero-hook full context *(Codex / Claude Code / AGY CLI hardware-verified)* |
| **Terminal** | **Windows Terminal (WSL2)** | `✅` | `win_shim` transient clipboard + shortcut paste (preserves clipboard) + same-window tab anti-crosstalk + hook/`tmux` precision context *(Codex / Claude Code / AGY CLI hardware-verified)* |
| **Terminal** | **Local & Remote `tmux`** | `✅` | **macOS**: zero-clipboard direct injection / **WSL2**: `win_shim` transient clipboard + paste; process tree isolated by focused pane; Linux & macOS CI real `tmux` E2E |
| **Terminal** | **Other Built-in Terminals & Dedicated SSH Clients** | `🧪` | Built-in paste-chord routing, anti-crosstalk & client rules (see list below); other CLI agents (OpenCode, Aider, etc.) fall back to workspace `git`/filenames |
| **Desktop App** | **Antigravity / AGY Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS for **Antigravity 2.0 standalone desktop** (read-only `conversation_summaries.db` + title matching, workspace `git`/filenames + `brain/` incremental cache, thread switching); legacy VS Code Fork also supported *(WSL2 scans WSL paths only)* |
| **Desktop App** | **ChatGPT / Codex Unified Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS (`com.openai.codex`): reads `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl` read-only to promote local repo tasks to `agent` mode, keeps cloud ChatGPT chats in `general` mode, and re-verifies same-window thread switching; Windows (`ChatGPT.exe` / `Codex.exe`) provides refined paste & anti-crosstalk |
| **Desktop App** | **Claude Desktop** | `✅` *(macOS)* / `🧪` *(Windows)* | Hardware-verified on macOS (`com.anthropic.claudefordesktop`): **Code & Cowork** local session binding (bounded read-only `AXWebArea` probe, `Claude`/`Claude-3p` metadata scan, `50K` cache, pre-paste thread check), while cloud Chat stays in `general` mode; Windows `Claude.exe` provides refined paste & anti-crosstalk |
| **Desktop App** | **Other Desktop IDEs** | `🧪` | IDEs (**VS Code**, **Cursor**, **Windsurf**, **JetBrains**, **Zed**, etc.) run as `coding_gui` with workspace `git`/filenames; running CLI agents inside an IDE's integrated terminal is also treated as `coding_gui` (skips CLI `sync_session` and integrated terminal splits) |
| **Desktop App** | **Any Other Desktop App** *(browsers, IM, notes, email)* | `⚪` | Works out of the box under default `hotkey.scope = "all"` in `general` mode (personal hotwords only, refined paste + window-locked anti-crosstalk) |

<details>
<summary><b>Built-in Terminals & Dedicated SSH Clients (<code>🧪</code>)</b></summary>

- **macOS Terminals**: `Terminal.app`, `iTerm2`, `WezTerm`, `Kitty`, `Alacritty`, `Warp`, `Hyper`, `Rio`, `Tabby`
- **Windows Terminals**: `WezTerm`, `Alacritty`, `Ghostty`, `Tabby`, `Hyper`, `Rio`, `Warp`, `Mintty`, `PuTTY`, `KiTTY`
- **Dedicated SSH Clients**: `MobaXterm`, `Xshell`, `WindTerm`, `SecureCRT`, `Termius`, `Electerm`, `FinalShell`, `RoyalTS`

</details>

---

## Platform Details (macOS TCC & WSL2 WASAPI)

### macOS (`Ghostty`, `tmux` & Other Terminals)

- **Native Retina HUD & Delivery**: Compiles and caches native `mac_shim` via `/usr/bin/swiftc` (first run ~5–15s) for `RCommand` / `RControl` / `ROption` / `Fn` tap or `ptt` hold, remote-desktop hotkey yielding (`Windows App`, `Screen Sharing`, Parsec, VMware/Parallels), and Retina capsule rendering. Injects directly in **macOS Ghostty** and **macOS `tmux`** without touching your clipboard; other apps lock onto the origin window ID and paste via native `CGEvent` `Cmd+V` with clipboard snapshot/restore.
- **Zero-Hook Tab/Split & `tmux` Binding**: Reads Ghostty split `tty`/`cwd`, `Terminal.app` (`🧪`) selected tab `tty`, `iTerm2` (`🧪`, Automation preflight with PTY fallback), or child PTY trees (plus active `tmux` pane) to bind `codex`, `claude`, or `agy` sessions. Audio input defaults to system mic (`[audio] macos_device = "default"`).
- **Minimal TCC Permissions**: All TCC permissions belong to the **GUI terminal app that runs `agent-voice-input start`** (restarting from another terminal requires re-granting).
  - **Microphone** (required, for `ffmpeg`).
  - **Automation → Ghostty** (or **Terminal** / **iTerm2** for exact `tty`).
  - **Accessibility** (required for `Cmd+V` paste into desktop GUI apps / non-Ghostty terminals outside `tmux` and reading window titles/session bindings in third-party terminals & desktop apps; not needed inside Ghostty or `tmux`).
  - **Input Monitoring** is **not required** for modifier hotkeys (`RControl` / `RCommand` / `ROption` / `Fn`).

### Windows Terminal + WSL2 (Including Remote Desktop & `tmux`)

- **Global Scope, Remote-Desktop Yielding & HiDPI Capsule**: Isolated Right Ctrl tap toggles recording across terminals, IDEs, and desktop apps (`hotkey.scope = "all"`, or `"coding"` / `"terminals"`; chords like `RControl+C` pass through untouched) with a Per-Monitor V2 HiDPI capsule. Remote desktop / VM clients (`mstsc`, `vmconnect`, Parsec, RustDesk, etc.) automatically yield triggers (`yield_remote_desktop = true`; bare `msrdc` stays unblocked for Win11 WSLg—non-WSLg AVD / Windows 365 users can add `ignore_apps = ["msrdc"]`). Even inside `tmux`, WSL2 delivers via `win_shim` transient clipboard snapshots + per-process paste shortcuts (`Ctrl+V`, `Ctrl+Shift+V` for WezTerm/Alacritty/Ghostty/Tabby/Hyper/Rio/Warp/WindTerm/SecureCRT/Termius/Electerm, `Shift+Insert` for mintty/PuTTY/KiTTY/MobaXterm/Xshell) without clobbering existing clipboard contents.
- **Native WASAPI Capture**: Resident `win_shim` captures 16 kHz audio via Windows WASAPI, avoiding WSLg `RDPSource` zero-packet deadlocks; start/stop beeps default to off to avoid RDP audio wake-up lag. First compilation + Defender scan takes ~15–60s+.
- **Zero-Config `tmux` & Optional Hooks**: Tracks active `tmux` panes without editing `~/.tmux.conf`. Only if you run multiple standalone non-`tmux` windows in the **same directory** do you need `agent-voice-input integrate` (see [`integrations/README.md`](../integrations/README.md)).

---

## Hotwords & Agent-Assisted Vocabulary

Keep your personal names, project brands, and recurring ASR mishearings in `~/.agent-voice-input/hotwords/personal.txt` (locate via `agent-voice-input hotwords path`; hot-reloads automatically; directory-fd `flock` leaves zero `.lock` files):

- **Starter vocabulary & dotfiles symlink**: First `agent-voice-input init` seeds ~18 common AI/developer starter terms (editable and removable; never overwritten even by `init --force`). To sync across machines via dotfiles, **symlink the `hotwords/` directory** rather than `personal.txt` itself (single-file symlinks are rejected):
  - *Before first `init`*: `mkdir -p ~/.agent-voice-input && ln -s ~/.dotfiles/avi-hotwords ~/.agent-voice-input/hotwords`
  - *After `init` already created `hotwords/`*: back up/merge `personal.txt` into `~/.dotfiles/avi-hotwords/`, remove local `~/.agent-voice-input/hotwords`, then run `ln -s ~/.dotfiles/avi-hotwords ~/.agent-voice-input/hotwords` (running `ln -sfn` while `hotwords/` is still a real directory creates a nested link inside it).
- **Pinning (`!`) & mishearing aliases (`<-`)**: Prefix critical terms with `!` (`!Antigravity` or `agent-voice-input hotwords add Antigravity --pin`). Add mishearing aliases with `CanonicalTerm <- mishearing1, mishearing2` or `agent-voice-input hotwords add Harness --alias 哈尼斯,哈尼思` (max 5 aliases per entry; single-char aliases rejected; 2-char aliases warn; ambiguous aliases shared by multiple terms are hidden automatically while keeping both canonical terms). Stage 1 receives canonical terms only; Stage 2 / single-hop uses aliases to fix stubborn mishearings.
- **Decoupled Stage 1 & Stage 2 budgets**:
  - **Stage 1 ASR**: Orders entries by tier (`!pinned` → `aliased` → `plain`, preserving file line order within each tier). In `general` mode, 100% of quota goes to personal terms; in `agent` mode, at least 30% of slots are reserved for dynamic repo/session terms with overflow refill (DashScope **150 terms / 1,800 chars**, Doubao **50 / 1,000**, OpenAI `keywords[]` **50**, OpenRouter `keyterms` **50**, Whisper **35 / 353**; `deepseek-asr` relies on Stage 2).
  - **Stage 2 / Single-Hop Block 0 (`<vocabulary_hints>`)**: Selects up to **200 items / 3,000 chars** in the same priority-tier order (`!pinned` → effective `<-` aliased → plain, preserving top-to-bottom file order) across both `agent` and `general` modes. Inspect live budgets anytime via `agent-voice-input hotwords list [--json]`.
- **Two rules for your Agent**: **always show the diff and wait for confirmation before editing `personal.txt`**, and **never guess mishearing aliases up front—only add `<-` after observing a real ASR mistake**.

```text
# 1. Bootstrap from scratch (canonical terms only)
Read my recent projects, READMEs, and specs to draft a clean flat list of proper nouns I actually speak aloud for ~/.agent-voice-input/hotwords/personal.txt (use `!` only for top-priority terms). Do not guess mishearing aliases; show me the diff first and write only after I confirm.

# 2. Fix a real mishearing on the spot
Voice input just misheard "Harness" as "哈尼斯". Add a mishearing alias with `agent-voice-input hotwords add Harness --alias 哈尼斯` (show me the change first).

# 3. Periodic cleanup & prioritization
Run `agent-voice-input hotwords list --json`, prune low-frequency or stale entries if we are near Stage 1/2 budgets, pin my highest-frequency cross-project terms with `!`, and show me the diff before applying.
```

---

## Advanced Configuration, Archiving & CLI Flags

- **App home (`~/.agent-voice-input/`)**: `config.toml`, `.env`, `hotwords/personal.txt`, and opt-in `audio/` & `transcripts/` all live under `~/.agent-voice-input/` (override via `AGENT_VOICE_INPUT_HOME`). `init` writes a default-ignore `.gitignore` tracking only `hotwords/`; `init --force` overwrites only `config.toml` while preserving `.env`, `hotwords/personal.txt`, and `.gitignore`.
- **Provider-first defaults & HUD locale**: Leave `model = ""` to use each provider's default; overriding `provider` via CLI without `--*-model` resets to the new provider's default. `ui.language` accepts `"auto"`, `"zh"`, `"en"`, and BCP 47 tags (`"zh-CN"`, `"en-US"`).
- **Two-stage vs. single-hop (`asr.api`, `asr.doubao_resource_id`)**: `"auto"` routes dedicated STT models (`qwen3-asr-flash`, `doubao`, `deepseek-asr`, `whisper-*`, `*-transcribe`, `SenseVoiceSmall`) to Stage 1 + Stage 2, and routes native `gemini`, DashScope `qwen*-omni*`, official OpenAI `gpt-audio-1.5` / `gpt-audio-mini`, and explicit `asr.api = "chat-completions"` to single-hop (50K `<session_reference_log>` blocks, in-memory `64 kbps` MP3 for `>= 3s` clips, `610s` cap; skips Stage 2 unless `refiner.force_on_single_hop = true`). DeepSeek official ASR supports `asr.api = "auto"` only.
- **Stage 2 protocol & reasoning budget (`refiner.api`, `effort`, `session_max_turns`)**: `refiner.api = "auto"` uses stateless HTTP `/responses` (`store=false`, `stream=true`) for `codex` and official `openai` (`api.openai.com`), `agy acp` stdio JSON-RPC for `agy`, `:generateContent` for `gemini`, and `/chat/completions` elsewhere. `asr.effort` and `refiner.effort` default to `""` (`"low"` for `codex`/`agy`/`openai`/`openrouter` refinement, `"none"` for Stage 1 single-hop and other Stage 2 providers) and accept `"default"` or `"none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`. For `agy`, `refiner.session_max_turns = 0` rotates naturally on 200K eviction or vocabulary changes.
- **Extra instructions, prefix caching & cold-start fallback**: Append custom preferences via `refiner.extra_instructions` (max 20 items / 2,000 chars; both two-stage and single-hop). Dialogue up to `context_max_chars = 200000` is evicted in `50K` sealed prefix blocks ahead of `<workspace_git_status>` and `<asr_draft_to_clean>`. When a coding session starts empty/short, `cold_start_fallback = "auto"` (or `"hotwords"` / `"off"`) supplements Stage 1 hotwords and prepends one frozen `<= 25K` `<prior_session_reference>` block from recent same-directory sessions.
- **Retries, silence trimming & opt-in local archive (`[audio]`, `max_retries`, `[archive]`)**:
  - `[audio].silence_trim = "balanced" | "conservative" | "off"` compresses long thinking pauses before upload; recordings up to 10 minutes auto-split `> 3 min` clips at pauses; failed/cancelled-in-flight clips stash (`0600`, 24h TTL, max 10) in `failed/` for `retry`.
  - Both stages default to `max_retries = 2` (`0..10`) under stage `timeout_sec` with a 9s connect/TLS cap.
  - Under `[archive]` (both off by default, `0700` dirs / `0600` files, excluded by `.gitignore`, surfaced as `Archive:` in `status`/`doctor`/`start`): `save_transcripts = true` appends **14-field** JSONL records (ASR draft, final text, and flat metadata including `agent` and `agent_session_id`, defaulting to `null` in `general` mode or legacy 12-field entries—never context/hotwords/secrets) to `~/.agent-voice-input/transcripts/YYYY-MM.jsonl`; `save_audio = true` archives voiced clips as `16 kHz` `64 kbps` MP3s to `~/.agent-voice-input/audio/<id>.mp3` (`audio_max_count = 1000`, `0` = unlimited). Transcripts, audio, and `diag.jsonl` share the same short utterance ID (`YYYYMMDD-HHMMSS-<4hex>`).

### Runtime CLI Override Flags

| CLI Flag | Description |
| :--- | :--- |
| `--config PATH` | Path to `config.toml` or `config.json` (also supports `AGENT_VOICE_INPUT_CONFIG` and `AGENT_VOICE_INPUT_HOME`) |
| `--asr`, `--asr-provider` | Stage 1 provider (`auto`, `dashscope`, `doubao`, `deepseek`, `openai`, `gemini`, `groq`, `openrouter`, `siliconflow`, `openai-compatible`; aliases `volcengine`/`volc` → `doubao`, `bailian` → `dashscope`) |
| `--asr-model`, `--asr-api`, `--asr-effort` | Stage 1 upstream model ID, protocol (`auto`, `audio-transcriptions`, `chat-completions`), and thinking budget |
| `--refiner`, `--refiner-provider` | Stage 2 provider (`auto`, `codex`, `agy`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`; aliases `doubao`/`volc`/`ark` → `volcengine`, `bailian` → `dashscope`) |
| `--refiner-model`, `--refiner-api`, `--refiner-effort`, `--effort` | Stage 2 upstream model ID, protocol (`auto`, `chat-completions`, `responses`), and thinking budget |

*(Environment variables are reserved for API credentials, bootstrap paths `AGENT_VOICE_INPUT_HOME` / `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR`, and low-level runtime/diagnostic hooks; ordinary options live in `config.toml`.)*

---

## Daily Operations & FAQ

### Auto-Start, Upgrade, Uninstall & File Locations

- **Idempotent terminal auto-start** (add 1 line to `~/.zshrc` or `~/.bashrc`):
  ```bash
  [ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1
  ```
  > **Warning (macOS):** Never use `launchd` (`LaunchAgent`) to launch the daemon on macOS. Background `launchd` agents lose the GUI terminal's Microphone & Automation TCC attribution and silently record all-zero audio; always start from your authorized GUI terminal.
- **Upgrade**: `uv tool upgrade agent-voice-input && agent-voice-input restart` (or re-run `install.sh`; git clone users: `git pull && agent-voice-input restart`).
- **Uninstall**:
  ```bash
  agent-voice-input stop
  agent-voice-input integrate --uninstall   # optional: remove managed CLI hooks
  uv tool uninstall agent-voice-input       # or rm ~/.local/bin/agent-voice-input
  ```
- **Where files live**:
  | Path | Purpose |
  | :--- | :--- |
  | `~/.agent-voice-input/` | App home (`config.toml`, `.env`, `hotwords/personal.txt`, opt-in `audio/` & `transcripts/`) |
  | `~/.cache/agent-voice-input/` *(or `$XDG_RUNTIME_DIR/agent-voice-input`)* | Runtime dir (`control.sock`, `daemon.lock`, `daemon.log`, `diag.jsonl`, `failed/`, `sessions/`, `integrate-backups/`) |
  | `~/Library/Caches/agent-voice-input/` *(macOS)* | Cached compiled `mac_shim` binary |

### Common Gotchas & FAQ

- **Why isn't bare `pip install agent-voice-input` recommended?**  
  Homebrew Python on macOS and modern Debian/Ubuntu distros enforce **PEP 668 (`externally-managed-environment`)**, causing bare `pip install` outside a virtualenv to fail. Use `install.sh` or `uv tool install --python '>=3.10' agent-voice-input` for an isolated install, or clone the repo and symlink `bin/agent-voice-input` directly with zero dependencies.
- **My MacBook doesn't have a Right Ctrl key—what should I use?**  
  Both `init` and zero-`init` runtime auto-detect MacBook built-in keyboards and default to `key = "RCommand"` (Right Command), while desktop Macs (e.g., Mac mini) and Windows/WSL2 keep `key = "RControl"`. Avoid common macOS conflicts: `ROption` collides with Doubao IME, `Fn` collides with the macOS 🌐 Globe key (to use `Fn`, set System Settings → Keyboard → "Press 🌐 key to" → **Do Nothing**), and `RShift` collides with third-party Chinese IME single-tap toggling. Edit `[hotkey]` in `config.toml`, run `agent-voice-input restart`, or inspect keycodes with `agent-voice-input key-test`.
- **Why does macOS record pure silence when I start the daemon over SSH?**  
  macOS binds Microphone TCC permission to the responsible GUI process; daemons started under `sshd` (or `launchd`) receive silent all-zero audio frames. Always run `agent-voice-input start` / `restart` inside an authorized local GUI terminal (Ghostty, `Terminal.app`, etc.).
- **Why can't WSL2 auto-paste into Windows Administrator windows?**  
  Windows UIPI blocks non-elevated `win_shim` from sending `Ctrl+V` into elevated Administrator windows. The refined text is safely left on your clipboard—press `Ctrl+V` manually.
- **How do I route the background daemon through a proxy?**  
  Export `HTTPS_PROXY` (and `NO_PROXY=127.0.0.1,localhost`) in your shell or add it to `~/.agent-voice-input/.env` (shell env takes precedence), then run `agent-voice-input restart`. Because the stdlib HTTP client only supports `http://` proxies, if your proxy client only sets `ALL_PROXY=socks5://...`, also set its HTTP mixed port (`HTTPS_PROXY=http://127.0.0.1:<port>`) and verify with `agent-voice-input doctor --ping`.
- **Why does it only paste and never auto-press Enter?**  
  In terminals and SSH sessions, auto-executing a misheard command is dangerous. Paste-only lets you glance before hitting Enter, and if focus moves mid-processing, output safely falls back to your clipboard.
