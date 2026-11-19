# Changelog

All notable changes to `agent-voice-input` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Zero-friction Stage 2 `auto` routing, built-in `deepseek` provider & Codex public model fallback**: Changed default `refiner.provider` to `"auto"` (zero-subprocess check preferring local `~/.codex/auth.json` login, falling back through configured API credentials `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, or cleanly bypassing Stage 2 when none are set); added first-class `deepseek` refiner provider (`DEEPSEEK_API_KEY`, default `deepseek-flash` on `https://api.deepseek.com/v1` with thinking disabled); and added static `models_cache.json` + one-shot runtime fallback to public Codex mini models (`gpt-5.4-mini` / `gpt-5-codex-mini` / `codex-mini-latest`) when default `gpt-6-luna` is unavailable on standard ChatGPT accounts.
- **`agent-voice-input init [--force]` & upgraded `doctor [--ping] [--json]`**: Added `init` to atomically create `0700` config directories (`AGENT_VOICE_INPUT_CONFIG` / `XDG_CONFIG_HOME` / `~/.config/agent-voice-input`) with `0600` `config.toml` and fully commented `agent-voice-input.env` templates; upgraded `doctor` with strict macOS/WSL2 readiness checks (read-only `AXIsProcessTrusted` / `CGPreflightListenEventAccess` on macOS, non-activating WASAPI default capture endpoint probe on WSL2, credential file permission warnings, daemon/hook status, and actionable 1-line `Fix:` commands) plus optional 5s zero-token `--ping` endpoint verification and `--json` redacted output.
- **Single-instance `daemon.lock` guard & stale session registry hygiene**: Added non-inheritable (`O_CLOEXEC`) `fcntl.flock` single-instance locking on persistent `0600` `daemon.lock` (with live legacy `control.sock` upgrade detection and lock-coordinated `stop`/`restart`), plus lock-protected lazy pruning of expired (`>7d`) dead-PID `sessions/*.json` entries and stale `.tmp` files throttled across short-lived hook invocations via `.prune.stamp`.
- **Headless `win_shim.ps1 -Mode self-check` & `windows-latest` CI gate**: Extracted pure `BuildPasteChordInputs` and added zero-hook, zero-clipboard, offscreen-HUD `win_shim.ps1 -Mode self-check` (verifying PowerShell AST, cold + cached C# assembly loading, paste chords, title tag parsing, WAV/RMS helpers, and clipboard guard state machine) wired into GitHub Actions `windows-latest` CI alongside `mac_shim.swift` packaging assertions.

### Fixed
- **Long-silence sparse sigh / ambient-click hallucination filter & `retry --print` deduplication**: Added a `1.5s` sliding-window voiced-concentration and clear-peak gate in `audio_proc.process_wav` for clips `>= 8s` with `< 5%` voiced frames to block scattered breaths/clicks from triggering single-hop multimodal hallucinations while preserving short utterances and whisper gain; suppressed duplicate `stdout` text printing in `agent-voice-input retry` when text is automatically pasted into the active terminal (with `--print` to opt in).

## [0.9.0] - 2026-11-19

### Added
- **Desktop IDE workspace & Antigravity IDE conversation binding**: Added bounded non-recursive workspace resolution across VS Code, Cursor, Windsurf, Antigravity IDE, Zed, JetBrains IDEs, Xcode, and Sublime Text (via window title parsing and macOS `AXDocument` `file://` URLs, with zero-slow-mount-I/O WSL2 `/mnt/` guards), injecting workspace `git status` and core filenames without leaking unrelated terminal CLI sessions, plus 3-tier `brain/<cid>` root-session matching (selecting the latest matching conversation for that workspace) and incremental `50K` cached context sync for desktop Antigravity IDE.
- **Non-Git folder filename & hotword fallback**: Added a bounded shallow `os.scandir` fallback (up to `200` entries across root + 1 subdir level, cached for `300s`, with strict `~` / `/` / `/tmp` / `/Users` / `/home/<user>` privacy guards) across both CLI sessions and desktop IDE workspaces so ordinary document/project folders without `.git` still supply local filenames and stems as spelling references and Stage 1 hotwords.
- **Desktop AI chat & instant-messaging scene hints**: Added zero-regex foreground app detection and `<target_app_hint>` tail injection (with linear-time echo stripping and zero prefix-cache impact) for desktop AI chat apps (`ChatGPT`, `Claude`, `Perplexity`, `Cherry Studio`, `Chatbox`) and instant-messaging clients (`WeChat`, `Feishu`/`Lark`, `DingTalk`, `Slack`, `Discord`, `Telegram`, `WhatsApp`, `Teams`, `Messages`, `QQ`).
- **In-flight cancellation (`cancel` & hotkey hold-cancel)**: Added `agent-voice-input cancel [--seq N]` and idle hotkey hold-to-cancel (`1.2s–8s` in `tap_toggle` mode while transcribing or refining). Per-utterance `CancelToken` propagates across chunked ASR, retry backoff, polling, and Stage 2 refinement, immediately waking blocked HTTP/TLS sockets via `shutdown(SHUT_RDWR)` and stashing post-gate audio (`0600`) in `failed/` for `agent-voice-input retry`.
- **Bounded concurrent Stage 2 refinement with strict FIFO paste**: Stage 1 ASR remains serialized to respect upstream rate limits while stateless Stage 2 refinement requests overlap concurrently and commit to the target window in strict `seq_id` order, recording `queue_wait_ms` in telemetry.
- **Compiled C# shim disk cache & self-healing on WSL2**: Cached the compiled `win_shim.ps1` C# assembly under `%LOCALAPPDATA%\agent-voice-input\` keyed by script SHA-256 to eliminate cold-start compilation jitter, and added automatic detection and respawn recovery when a helper exits or stalls before signaling `ready`.
- **Race-free multi-format clipboard backup & restoration**: Added single-slot clipboard snapshot and delayed restoration across Windows (native Win32 `HGLOBAL` via `HWND_MESSAGE` with `Win+V` cloud/history suppression) and macOS (`mac_shim` `paste_clip` preserving multi-UTI text and image items), preventing back-to-back utterances from overwriting pre-existing clipboard contents while clearing sensitive password-manager items (`ConcealedType` / `ClipboardViewerIgnore`) instead of re-writing them.

## [0.8.0] - 2026-11-18

### Added
- **Global desktop dictation scope & remote desktop auto-yield**: Added `hotkey.scope = "all" | "coding" | "terminals"` (default `"all"`, backward-compatible with `only_in_terminals`), `hotkey.yield_remote_desktop = true`, and `hotkey.ignore_apps`. Hotkeys automatically pass through to focused remote desktop, streaming, and VM clients (while exempting Windows 11 WSLg `msrdc.exe`), and isolate background terminal sessions from non-terminal windows via three-tier routing (`terminal` / `coding_gui` / `general_gui`).
- **Expanded deterministic Stage 1 hotword extraction**: Extended identifier matching to `PascalCase`, `kebab-case`, and `.jsonl` tokens, added cached `git` core filename fallback on clean worktrees, and introduced low-priority built-in AI/CLI vocabulary within per-provider hotword caps.
- **Point-in-time recording replay evaluation**: Extended `voice-eval` to align archived local recordings against historical session transcripts at utterance timestamps in out-of-repo cache directories.

### Changed
- **Refiner prompt & payload overhaul**: Resolved clause-retention vs. self-correction rule conflicts to clean cross-clause self-repairs, repetitive phrasing, and empty trailing fillers; enabled phonetic repair of general AI/programming terms beyond transcript history; moved `<workspace_git_status>` ahead of `<asr_draft_to_clean>`; and introduced adaptive `agent` / `general` refinement sub-rules.
- **Single-hop multimodal 50K sealed prefix cache & 2026 model defaults**: Unified single-hop audio providers (`GeminiAudioProvider` and `ChatAudioAsrProvider`) onto shared `prompts.py` rules and `50K` `<session_reference_log>` blocks with sealed-block `cache_control`, `610s` unsplit audio threshold, in-memory `64 kbps MP3` compression for clips `>= 3s`, and `audio_tokens` / `cached_input_tokens` telemetry. Upgraded built-in default models (`gemini-3.5-flash-lite`, `qwen3.8-flash`, `qwen3.8-omni-flash`, `doubao-seed-2-0-mini-260428`) with per-provider default `effort` and explicit thinking suppression.
- **Cross-platform paste routing & zero-clipboard direct injection**: Routed Windows paste chords per process (`Ctrl+V`, `Ctrl+Shift+V`, and extended-key `Shift+Insert`) with `[#id8]` session-tag drift verification; switched macOS `Cmd+V` injection to zero-`Carbon` native `CGEvent` in `mac_shim`, relaxed dynamic window-title drift checks for same-window GUI apps, gated `tmux` paste on foreground terminals, and removed redundant clipboard writes on macOS Ghostty / `tmux` direct injection paths.

### Fixed
- **WSL2 Bluetooth charging-case ghost-endpoint race**: Added an `850ms` `PeekMessage`-pumped first-frame WASAPI startup gate (`mic_busy:wasapi_startup_no_frames`) and frame-verified stream reopen in `win_shim.ps1`, preventing dual HUD capsules when Bluetooth headsets report active while inside a charging case.

## [0.7.1] - 2026-11-18

### Fixed
- **CI test portability without `ffmpeg`**: Fixed recording archive unit tests to skip or isolate cleanly on runners where `ffmpeg` is not installed.

## [0.7.0] - 2026-11-18

### Added
- **Stateless HTTP `/responses` refiner & structured telemetry**: Migrated `CodexRefiner` and OpenAI Responses refinement to stateless Keep-Alive HTTP requests, added `telemetry.jsonl` latency/token metrics, introduced optional `0600` ring-buffer MP3 recording archiving (`audio.save_recordings`, up to `300` clips at `64 kbps`), and added the local `voice-eval` benchmark and live diagnostics skill.

## [0.6.1] - 2026-11-17

### Changed
- **Compaction-aware session context & faster Codex prewarm**: Truncated pre-compaction history in long agent transcripts and parallelized Codex thread cleanup/prewarm operations.

## [0.6.0] - 2026-11-17

### Changed
- **Prefix-cache-friendly session context chunking**: Restructured session dialogue history into deterministic `50K` chunks so multi-turn dictation achieves high upstream prompt-cache hit rates.

## [0.5.0] - 2026-11-16

### Added
- **Serial pipelined back-to-back dictation**: Enabled starting a new recording while previous utterances are still transcribing or refining, with modifier key-hold paste deferral and blank-line joining on focus-drifted clipboard fallbacks.

## [0.4.0] - 2026-11-16

### Added
- **macOS `Terminal.app` & `tmux` daemon integration**: Added active-tab TTY discovery for built-in `Terminal.app`, daemon-side `tmux` buffer paste, GUI child-PTY tree scoping, microphone TCC preflight checks, upper HUD alert toasts, and same-provider retries with a `9s` handshake timeout.

### Changed
- **Configuration & credential boundary cleanup**: Consolidated ordinary runtime settings into `config.toml` while reserving environment variables for provider API credentials and bootstrap paths, accompanied by revamped bilingual documentation.

## [0.3.0] - 2026-11-15

### Added
- **Long-audio pause splitting & failed-audio `retry`**: Added `180s` pause-aware splitting with concurrent chunk transcription (up to 10-minute recordings with a `15s` HUD countdown), `0600` failed-audio stashing in `failed/` with `agent-voice-input retry`, immediate missing/unplugged microphone alerts, stdlib HTTP Keep-Alive connection pooling, a HiDPI gemstone waveform HUD capsule, and non-interactive macOS Command Line Tools guards.

## [0.2.1] - 2026-11-14

### Added
- **CLI `--version` & terminal hotkey gate**: Added `agent-voice-input --version` (and daemon `status` version reporting), terminal-only hotkey gating, bottom anti-aliased HUD pill styling, and `tmux` session discovery fixes across macOS and WSL2.

## [0.2.0] - 2026-11-14

### Added
- **macOS support, `tmux` focus resolution & multi-provider config**: Added native macOS hotkey/HUD Swift helper (`mac_shim.swift`) and Ghostty direct injection, `tmux` multi-pane focus resolution with `session-hook` integration, automatic HUD language adaptation to desktop UI locale, and structured `provider`/`model`/`api`/`effort` configuration with OpenRouter dual-protocol support.

## [0.1.0] - 2026-11-14

### Added
- **Initial WSL2 release**: Two-stage context-aware voice input daemon and CLI for coding agents (**Codex**, **Claude Code**, **AGY**) on Windows + WSL2, featuring native `win_shim.ps1` WASAPI capture, global hotkey & HUD bridge, active terminal session discovery, `ffmpeg` loudness normalization with local silence gating, and safe paste without auto-pressing `Enter`.

[Unreleased]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/xiaohuaxi/agent-voice-input/releases/tag/v0.1.0
