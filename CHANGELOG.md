# Changelog

All notable changes to `agent-voice-input` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Windows `warp` terminal & `Ctrl+Shift+V` paste routing**: Added `warp` to `WINDOWS_TERMINALS` and `WINDOWS_CTRL_SHIFT_V_PROCS` in `app_scope.py` (forwarded to `win_shim.ps1` via `-AllowedTerminals` with standalone default fallback), bringing Windows terminal detection and paste-chord routing to parity with macOS `Warp` support.
- **Architecture contract, microsecond GIL-preemption concurrency & tiered coverage gates**: Added `tests/test_architecture_contracts.py` (enforcing strict 21-module intra-package DAG, stdlib-only runtime imports, live `unittest.mock.patch` bytecode target verification, 3-end HUD state/layer/duration parity, provider factory completeness, and cross-platform terminal parity), `tests/test_concurrency_races.py` (`1µs` `sys.setswitchinterval` + stack-dumping deadlock watchdog torture tests across `VoiceInputDaemon` out-of-order Stage 2 / cancellation / mic-drop storms, `_KeepAlivePool` + `CancelToken` socket shutdowns, `SessionTracker` + `RemoteSshManager` + cross-process `flock` session registry, and `JsonlShimMixin` crash self-healing), `tests/test_core_scenarios_coverage.py` (real-input TOML/config/window-title/LLM/loopback-redirect/in-process `_remote_probe` scenario tests with zero fake syscall mocks), and `scripts/check_coverage.py` (wired into CI to enforce independent Statement % and Pure Branch % thresholds alongside a zero-dependency `--stdlib-trace` local fallback).

### Changed
- **100 MiB clipboard snapshot limit**: Raised the transient clipboard backup/restore cap from `20 MiB` to `100 MiB` across Windows (`win_shim.ps1` `MaxClipSnapshotBytes`) and macOS (`mac_shim.swift` `PasteboardGuard.defaultMaxSnapshotBytes`) so uncompressed 4K/5K screenshots and multi-format clipboard payloads restore cleanly after the default `250ms` paste delay, clearing snapshot references immediately once restoration completes, is superseded, or is abandoned, and without double-counting synthesized `CF_DIB` alongside `CF_DIBV5` (or `.tiff` alongside `.png`).
- **Clearer WASAPI zero-frame startup toast (`mic_no_stream`)**: Distinguished `wasapi_startup_no_frames` (`IAudioClient.Start()` succeeded on an active endpoint but produced zero audio packets within `850ms`, such as a Bluetooth headset resting inside its charging case) from exclusive-mode device lock (`mic_unavailable`), showing `"! 麦克风无音频流，请检查耳机/设备"` / `"! No mic audio stream, check headset"` instead of `"! 麦克风暂忙，请再试一次"`.
- **Modularized core package boundaries & faster `session-hook` cold start**: Extracted shared pure-stdlib HTTP transport (`http_transport.py`), cross-stage LLM helpers (`llm_common.py`), cross-platform recorder/shim primitives (`platform_common.py`), and workspace/IDE/hotword discovery (`workspace_ctx.py`), eliminating `refiner` -> `asr_providers` and `platform_macos` -> `platform_wsl` coupling; deduplicated shared transcript/CLI helpers onto standalone `_remote_probe.py` while unifying 5 local-vs-remote edge cases (`file://` + NUL CWD guards, symlinked broad-directory ranking, short-read-safe `0700` registry reads, and strict `claude`/`codex`/`agy` CLI matching); centralized terminal lists and default provider URLs into `app_scope.py` and `config.py`; and added lazy-import fast paths for `agent-voice-input session-hook` and `remote-ctx` that skip loading heavy ASR, refiner, daemon, HTTP transport, and platform modules on every agent turn.

### Fixed
- **Cross-thread shim pipe reaping, pooled connection abort generation, and registry `.tmp` lock races**: Enforced owner-exclusive `stdout` closure and lock-coordinated/deferred `stdin` closure during `JsonlShimMixin` subprocess reaping to prevent cross-thread `io.TextIOWrapper` races or hangs on abrupt shim crashes; added per-borrow generation guards (`_borrow_gen` / `make_abort_fn`) and atomic `_aborted` checks in `_KeepAlivePool.acquire`/`release` so stale `CancelToken` callbacks cannot abort recycled keep-alive connections; and extended `write_session_registry_entry`'s shared `.registry.lock` (`LOCK_SH`) across `.tmp` creation so `prune_stale_session_registry` (`LOCK_EX`) cannot unlink in-flight `.tmp` files across system clock jumps.

## [0.9.0] - 2026-11-19

### Added
- **Zero-install SSH remote Linux session context routing (`[remote]`)**: Added stateless `ssh ... -- <target> python3 -` remote context probing (`_remote_probe.py` & `remote_ssh.py`) requiring zero remote daemon or package installation across Codex CLI, Claude Code, and Antigravity CLI. Supports Tier 1 `[@host]` + `[#id8]`/`[§id6+]`/UUID lookup, Tier 2 `cwd`-scoped matching (preferring exact over bounded non-broad child/parent directories, matching title CLI hints, and selecting the latest `mtime` session within the same rank), remote `tmux` active-pane discovery, non-Git shallow workspace filename fallback, broad privacy root refusal (`~`, `/`, `/tmp`, `/home`), ETag `not_modified` JSONL short-circuiting, forward-anchored `50K` whole-chunk eviction (`trim_turns_chunk_aligned`) for byte-identical LLM prefix caching, `start_recording` async prefetching with configurable `timeout_ms` (`400ms` default) post-stop budget, per-host circuit breakers, WSL2 `ssh.exe` path resolution, `remote-ctx` CLI helper, `integrations/remote/remote-title-hook.sh`, and `docs/context-routing.md`.
- **Desktop IDE workspace & Antigravity IDE conversation binding**: Added bounded non-recursive workspace resolution across VS Code, Cursor, Windsurf, Antigravity IDE, Zed, JetBrains IDEs, Xcode, and Sublime Text (via window title parsing and macOS `AXDocument` `file://` URLs, with zero-slow-mount-I/O WSL2 `/mnt/` guards), injecting workspace `git status` and core filenames without leaking unrelated terminal CLI sessions, plus 3-tier `brain/<cid>` evidence matching (selecting the latest matching conversation for that workspace) and incremental `50K` cached context sync for desktop Antigravity IDE.
- **Non-Git folder filename & hotword fallback**: Added a bounded shallow `os.scandir` fallback (up to `200` entries across root + 1 subdir level, cached for `300s`, with strict `~` / `/` / `/tmp` / `/Users` / `/home/<user>` privacy guards) across local CLI sessions, desktop IDE workspaces, and remote SSH folders so ordinary document/project directories without `.git` still supply local filenames and stems as spelling references and Stage 1 hotwords.
- **Desktop AI chat & instant-messaging scene hints**: Added zero-regex foreground app detection and `<target_app_hint>` tail injection (with linear-time echo stripping and zero prefix-cache impact) for desktop AI chat apps (`ChatGPT`, `Claude`, `Perplexity`, `Cherry Studio`, `Chatbox`) and instant-messaging clients (`WeChat`, `Feishu`/`Lark`, `DingTalk`, `Slack`, `Discord`, `Telegram`, `WhatsApp`, `Teams`, `Messages`, `QQ`).
- **In-flight cancellation (`cancel` & hotkey hold-cancel) & bounded concurrent Stage 2**: Added `agent-voice-input cancel [--seq N]` and idle hotkey hold-to-cancel (`1.2s–8s` in `tap_toggle` mode while transcribing or refining) with per-utterance `CancelToken`, immediate `shutdown(SHUT_RDWR)` socket wakeup, and `0600` `failed/` audio stashing; overlapped stateless Stage 2 refinement requests concurrently while keeping Stage 1 ASR serialized and committing pastes in strict `seq_id` FIFO order (`queue_wait_ms` telemetry).
- **Built-in `deepseek` provider, `init [--force]` & upgraded `doctor [--ping] [--remote] [--json]`**: Added first-class `deepseek` Stage 2 refiner provider (`DEEPSEEK_API_KEY`, default `deepseek-flash` on `https://api.deepseek.com/v1` with thinking disabled); added `init` to atomically create `0700` config directories with `0600` `config.toml` and commented `agent-voice-input.env` templates; and upgraded `doctor` with macOS/WSL2 readiness checks (read-only TCC checks, non-activating WASAPI capture endpoint probe, credential mode warnings, and actionable 1-line `Fix:` commands), 5s zero-token `--ping` endpoint checks, `--remote` SSH host checks, and redacted `--json` output.
- **Single-instance `daemon.lock`, session registry hygiene, Windows CI gate & automated GitHub Releases**: Added non-inheritable (`O_CLOEXEC`) `fcntl.flock` single-instance locking on persistent `0600` `daemon.lock` (with live legacy `control.sock` upgrade detection and lock-coordinated `stop`/`restart`), lock-protected lazy pruning of expired (`>7d`) dead-PID `sessions/*.json` entries throttled via `.prune.stamp`, compiled `win_shim.ps1` C# DLL disk caching under `%LOCALAPPDATA%\agent-voice-input\` keyed by SHA-256 with automatic respawn self-healing, headless `win_shim.ps1 -Mode self-check` wired into GitHub Actions `windows-latest` CI, and automatic GitHub Release notes extraction from `CHANGELOG.md` on `v*` tags in `publish.yml`.

### Changed
- **Default `refiner.provider = "auto"` routing & Codex public model fallback**: Changed default `refiner.provider` to `"auto"` (zero-subprocess check preferring local `~/.codex/auth.json` login, falling back through configured API keys `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow`, or cleanly bypassing Stage 2 when none are set), and added static `models_cache.json` + one-shot runtime fallback to public Codex mini models (`gpt-5.4-mini` / `gpt-5-codex-mini` / `codex-mini-latest`) when default `gpt-6-luna` is unavailable on standard ChatGPT accounts.
- **Deduplicated `retry` stdout on automatic paste**: Suppressed duplicate `stdout` text printing in `agent-voice-input retry` when recovered text is automatically pasted into the active terminal (use `--print` to opt into `stdout`).

### Fixed
- **Phase 0 foreground SSH & Remote-SSH IDE crosstalk isolation**: Added a pre-resolution Remote Veto Gate blocking local background CLI, `tmux`, and disk sessions from leaking into dedicated SSH clients (`PuTTY`, Windows `KiTTY`, `MobaXterm`, `WindTerm`, `SecureCRT`, `Xshell`, `Termius`), remote shell/SSH window titles (`user@host:path`, `[@host]`, `ssh ...`), foreground `ssh` processes on local TTYs/`tmux` panes, and Remote-SSH IDEs (`[SSH: host]`, `vscode-remote://ssh-remote+...`); tightened unscoped non-generic title affinity and stale registry TTY verification; and added zero-popup TCC preflight (`AEDeterminePermissionToAutomateTarget`) for macOS `iTerm2` session TTY lookup.
- **Race-free multi-format clipboard backup & restoration**: Added single-slot clipboard snapshot and delayed restoration across Windows (native Win32 `HGLOBAL` via `HWND_MESSAGE` with `Win+V` cloud/history suppression) and macOS (`mac_shim` `paste_clip` preserving multi-UTI text and image items), preventing back-to-back utterances from overwriting pre-existing clipboard contents while clearing sensitive password-manager items (`ConcealedType` / `Clipboard Viewer Ignore`) instead of re-writing them.
- **Long-silence sparse sigh / ambient-click hallucination filter & concurrent HUD ordering**: Added a `1.5s` sliding-window voiced-concentration and clear-peak gate in `audio_proc.process_wav` for clips `>= 8s` with `< 5%` voiced frames to block scattered breaths or clicks from triggering single-hop multimodal hallucinations while preserving short utterances and whisper gain, and fixed a late `"refining"` HUD status race when concurrent Stage 2 refinement completes rapidly.

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
