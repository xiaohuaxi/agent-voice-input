# Changelog

All notable changes to `agent-voice-input` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.0] - 2026-11-25

### Added
- **One-click CLI agent integration (`agent-voice-input integrate`)**: Added `agent-voice-input integrate` (`--status`, `--dry-run`, `--cli`, `--uninstall`, `--json`) for Claude Code, Codex, and Antigravity (`agy`), featuring dotfiles symlink target resolution, isolated `~/.cache` backups, comment-preserving Codex TOML patching, and uninstall-safe inline hook commands.
- **Dedicated agent integration guide & redesigned 60-second quickstart**: Added `docs/agent-setup.md`, synced bilingual `integrations/` templates, and moved the 60-second quickstart path above the fold in `README.md` and `README.zh-CN.md`.

### Changed
- **Environment-aware `init`, aligned CLI UX & updated provider defaults**: Upgraded non-interactive `agent-voice-input init` to pick `ROption` on MacBooks while keeping `RControl` on desktop Macs (e.g., Mac mini), support `--asr`/`--refiner`/`--hotkey`, and summarize detected API keys and Codex login; realigned `start`/`restart`/`stop`/`init` output into 10-column label blocks with a 2-line aligned `integrate` fallback hint for missing hooks or ambiguous environments; deprioritized `openrouter` in `ASR_AUTO_PRIORITY` and upgraded OpenRouter's default refiner model to `google/gemini-3.8-flash`.
- **`AgyRefiner` prewarming, `60m` idle TTL, tail-growing session reuse & thought-chunk telemetry**: Extended the `agy acp` resident worker idle TTL to `60m`, added side-effect-free asynchronous prewarming via read-only `session/list` (triggered at both recording start and ASR start), enabled reusing the same `conversation_id` across active tail-block growth and `50K` sealed-block continuations, and added zero-text count-only telemetry for `agent_thought_chunk`.

### Fixed
- **`doctor` Codex hook checks & JSON output completeness**: Fixed `doctor` omitting Codex `[tui].terminal_title` checks and `CODEX_HOME` consistency validation, and added `hooks`, `[keys]`, and `Next:` guidance to `doctor` / `--json` output.
- **Stage 3A platform recording lifecycle review & WSL recording guards**: Closed the Stage 3A platform recording lifecycle review with cross-platform state-invariant and lock-order documentation, `_expected_gen` stale-generation protection in `WslPlatform.stop_recording()`, and writer-ownership verification before forced kills in `cancel_recording()`.

## [0.15.0] - 2026-11-25

### Added
- **Claude Desktop (Code & Cowork) local session binding on macOS**: Added bounded read-only `AXWebArea` route and title probing for `com.anthropic.claudefordesktop` with dual official (`Claude`) and 3P (`Claude-3p`) metadata discovery, `50K` incremental session caching, local Cowork folder resolution, and pre-paste same-window thread verification, while keeping cloud Chat isolated in `general` + `ai_chat` mode.

### Changed
- **Extended clipboard-fallback HUD toast duration to 5s**: Extended the clipboard-copied HUD notification duration from `2s` to `5s` across Python, macOS, and Windows so fallback notices when focus shifts or modifier keys are held remain easier to notice.
- **Single-owner voice-priority clipboard pipeline**: Simplified clipboard delivery across macOS and WSL2 so the native bridge solely owns clipboard writes during paste, prioritizing voice output over restoring the previous clipboard on transient conflicts or timeouts without complex recapture/rollback paths.
- **Modularized daemon & zero-dependency multi-file native shims**: Split daemon internals into focused modules (`control_ipc.py` and `daemon_pipeline.py`) and decomposed the monolithic native shims into multi-file sources (`mac_shim/*.swift` and `win_shim/*.cs`) with synced packaging metadata and zero added runtime dependencies.

### Fixed
- **Killable single-slot macOS pasteboard worker & slow Accessibility decoupling**: Isolated macOS clipboard snapshot capture in a killable single-slot worker and decoupled slow Accessibility probes from hot paths, preventing 60s Windows App / RDP clipboard promise hangs from stalling HUD updates, hotkeys, or dictation delivery.
- **Single-writer bridge stdin, non-blocking HUD dispatch & unified recording ownership**: Consolidated native bridge stdin onto a single-writer channel with lock-free non-blocking HUD dispatch and unified platform recording ownership across macOS and WSL2—fixing WSL stop/cancel write-timeout state recovery and premature slot release, control-socket ordering, and pipe close races.

## [0.14.0] - 2026-11-24

### Added
- **First-class DeepSeek official ASR provider (`asr.provider = "deepseek"`)**: Added built-in DeepSeek Stage 1 speech recognition (`POST https://api.deepseek.com/v1/tools/asr`, default display model `deepseek-asr`, `asr.api = "auto"` only, automatic detection across 21 languages without hotword/language parameters), enabling zero-config two-stage dictation (`deepseek-asr` + `deepseek-flash`) with a single `DEEPSEEK_API_KEY`, and included `deepseek` in `asr.provider = "auto"` priority.
- **Unified ChatGPT / Codex desktop app detection**: Added dedicated `ai_chat` recognition for the unified ChatGPT/Codex desktop app (`com.openai.codex` / legacy `com.openai.chat` on macOS; `ChatGPT.exe` / `Codex.exe` on Windows) with streaming-title drift handling and window-locked anti-crosstalk.
- **Antigravity 2.0 standalone desktop session & workspace binding**: Added read-only `conversation_summaries.db` lookup with exact/truncated-prefix window title matching, structured workspace URI resolution (including WSL `vscode-remote://wsl+...`), foreground view ranking (`annotations/*.pbtxt`), and `brain/` incremental `50K` context sync while preserving legacy Antigravity VS Code Fork compatibility.
- **ChatGPT / Codex unified desktop local-repository binding & dynamic mode routing**: Added read-only `~/.codex/sqlite/codex.db` (and legacy `state_5.sqlite`) plus `~/.codex/sessions/*.jsonl` resolution to automatically promote local repository Codex tasks to `agent` mode with project `git`/file hotwords and `50K` incremental session caching, while keeping cloud ChatGPT chats strictly isolated in `general` + `ai_chat` mode (with cloud/cross-repo ambiguity veto) and adding pre-delivery same-window thread verification across macOS and WSL2 to prevent in-window session crosstalk.
- **Bilingual compatibility & verification status matrices (`✅` / `🧪` / `⚪`)**: Added explicit hardware-verified (`✅`), CI/unit-tested (`🧪`), and generic fallback (`⚪`) matrices across host platforms, terminals, SSH hosts, desktop apps, and model providers in `README.md` and `README.zh-CN.md`.
- **Non-blocking `control.sock` `probe_route`, hardened `selftest_wav` & dual-platform regression matrix (`regress_matrix.py`)**: Dispatched `probe_route` and `selftest_wav` asynchronously off the daemon control loop with redacted routing summaries, `no_telemetry` isolation, `use_live_window`, and opt-in `deliver` mode; added the symmetric macOS/WSL2 `.agents/skills/voice-eval/scripts/regress_matrix.py` suite (offline multi-CLI & desktop WAL routing matrix, live daemon health/delivery probe, and read-only local CLI/DB compatibility check) plus a desktop GUI end-to-end acceptance checklist in `voice-eval`.

### Changed
- **Fast byte pre-filtering for Claude Code `2.1.330` giant non-dialogue JSONL records**: Shared a unified byte-level pre-filter (`_should_prefilter_skip_jsonl_line`) across both local incremental `SessionTracker` reads and SSH `_remote_probe.py` scans to skip >1 KB non-dialogue records (`attachment`, `api-request-shape`, `api-request-blob`, `api-request`, `progress`, `file-history-snapshot`, `queue-operation`) before `json.loads` while preserving `user`, `assistant`, and `compact_boundary` lines.
- **Zero-deliberation Stage 2 refiner prompt tuning**: Added explicit zero-deliberation instructions across Stage 2 refiner system prompts (`REFINER_OUTPUT_LINE`), `<task_reminder>`, and `agy` isolated agent rules (`_AGY_AGENT_MD_CONTENT`) to suppress internal rule auditing or answer planning and emit cleaned text immediately.

### Fixed
- **macOS `mac_shim` main-thread offloading, bounded paste wait & AGY hook diagnostics**: Offloaded CoreAudio and Accessibility queries in `mac_shim.swift` to a dedicated background serial queue, skipped AX IPC for single-display HUD placement, added a clipboard snapshot read time budget and pre-post total deadline guard (`pinnedWindowId` validation included), tightened `platform_macos.py` modifier-release waits with PTT-safe dynamic idle timeouts, immediate pre-commit `key_state` rechecks, event-queue flush barriers, and pre-commit deadline checks, and updated `doctor` & hook integration checks to support `~/.gemini/config/hooks.json` and dotfiles symlinks.
- **Bilingual README accuracy & boundary clarifications**: Corrected native Linux scope to offline file transcription (`once --file <wav> --dry-run`) and headless SSH remote targets only (no native daemon, hotkey, or `toggle` yet), clarified that zero-clipboard `tmux` direct injection applies only on macOS (WSL2 uses `win_shim` transient clipboard snapshots + shortcut paste without clobbering clipboard contents), documented official OpenAI single-hop audio models (`gpt-audio-1.5` / `gpt-audio-mini`), and explicitly stated IDE integrated terminal boundaries (`coding_gui` workspace context without CLI `sync_session` or split-pane distinction).
- **Pre-release cross-module context, delivery & control-socket hardening**: Added Antigravity 2.0 pre-delivery same-window active-conversation verification, truncated-title/Codex-workspace disambiguation, and SQLite fail-closed handling; aligned WSL2 and macOS pre-paste focus/modifier guards (`platform_wsl.py` / `platform_macos.py`) and isolated `control.sock` `selftest_wav` execution with client-disconnect blocking so probes cannot mutate live daemon state or deliver after disconnect.
- **DeepSeek URL/telemetry consistency & regression matrix integrity**: Aligned DeepSeek ASR/refiner custom base-URL resolution, `diagnose` / telemetry schema consistency, and WAV/remote-title-hook edge cases; hardened `.agents/skills/voice-eval/scripts/regress_matrix.py` to eliminate false-green assertions, isolate host state, and sync routing documentation.
- **macOS async `PasteboardGuard` capture & stale `NSEvent` filtering**: Offloaded `NSPasteboard` snapshot capture in `mac_shim.swift` onto a bounded background read-only queue with a `200ms` hard timeout, bad `changeCount` negative caching, and concurrency throttling to eliminate 60s main-thread freezes of the HUD, waveform, and hotkeys caused by unfulfilled Windows App / RDP clipboard promises, alongside stale `NSEvent` key-event filtering with safe PTT-active cancellation.

## [0.13.0] - 2026-11-23

### Added
- **Cross-platform glassmorphic queue badge & HUD visual polish**: Refined the pipelined recording HUD across Windows (`win_shim.ps1`) and macOS (`mac_shim.swift`) with a unified glassmorphic `+1`/`+2` in-flight queue badge and consistent visual styling.
- **76 curated built-in technical hotwords**: Expanded the low-priority built-in vocabulary with 76 high-value developer, CLI agent, and model terms to improve out-of-the-box Stage 1 recognition accuracy.
- **User-first bilingual README & community documentation**: Redesigned the top-of-page onboarding experience in `README.md` / `README.zh-CN.md` with clear zero-relay / no-author-server privacy guarantees, and added `SECURITY.md` plus `.github/ISSUE_TEMPLATE/` templates.
- **Three offline cross-component E2E test suites**: Added end-to-end offline tests covering real CLI subprocess lifecycle management (`start`/`status`/`stop`/`restart`), real `ffmpeg` + `SessionTracker` + hotwords + two-stage loopback HTTP + `retry` recovery, and `VoiceInputDaemon` with real `sshd ControlMaster` context prefetching.

### Changed
- **Upgraded OpenAI & Gemini public API default models**: Upgraded OpenAI defaults to `gpt-transcribe` (Stage 1 ASR) and `gpt-6-luna` (Stage 2 Refiner), and upgraded the Gemini public API default to `gemini-3.7-flash` (while keeping AGY subscription refinement on `gemini-3.8-flash`).
- **Extended `agy` resident worker idle TTL**: Increased the idle reap timeout of the resident `agy acp` Stage 2 worker from `3` minutes to `15` minutes to preserve warm sessions across regular dictation pauses.
- **Unified refiner proxy engine**: Migrated Codex and OpenAI Responses connection pooling onto the shared `http_transport.py` proxy parser, adding `*.domain` wildcard support in `NO_PROXY` and actionable guidance when an unsupported proxy URL scheme is configured.

### Fixed
- **Cross-window & tmux focus isolation**: Prevented Ghostty same-surface `tmux` pane switching from cross-talking or injecting wrong session context (with `0ms` mic-start concurrent origin sampling and empty-window safe fallback), blocked context leakage when switching from tagged Windows Terminal tabs to plain shells, and aligned 13 title-marker pairs across macOS and Windows.
- **Recording, cancellation & SSH prefetch concurrency safety**: Sent a `rec_gen`-guarded discard-stop on WSL `rec_start` timeouts with stale-generation guards in native `StopRecording`; ensured macOS PTT crash EOF stops the microphone before refreshing the HUD without leaking event threads; guaranteed exactly-once `failed/` stashing when cancellation races with `stop`/`restart`; and enforced monotonic generations on SSH remote prefetch callbacks and shared-directory file caches to prevent stale rollbacks.
- **Hotkey validation & cross-platform first-run diagnostics**: Added whitelist normalization for hotkey names and eliminated the WSL2 invalid-hotkey false-ready silent failure; relaxed WSL C# cold-compile/startup timeouts (`15s` / `18s`), surfaced `ps_error`, and fixed WSLg microphone-less false-green checks; distinguished `accessibility_denied` vs. `send_input_failed` clipboard-fallback HUD hints; and fixed `doctor` AGY `~/.gemini/hooks.json` & `voice-title.sh` checks, fully commented `config.example.env` (preventing placeholder `401` errors), `remote.hosts.timeout_ms` parsing whitelist, missing `doubao` API key hints, single-hop `content_filter`/`safety` truncation blocking, `thinkingLevel="low"` for `gemini-flash-latest`, and `start` missing-key/`ffmpeg` warnings with hotkey tips.

## [0.12.0] - 2026-11-23

### Added
- **Explicit Antigravity CLI subscription refiner (`refiner.provider = "agy"`)**: Added opt-in `agy` Stage 2 refiner backed by a resident, idle-reaped single `agy acp` worker reusing isolated tool-free plain-text sessions across turns with incremental prefix continuation (not auto-selected by `refiner.provider = "auto"`), along with `SessionTracker` self-contamination exclusion so refiner ACP sessions never pollute active dictation context.
- **HTTP/HTTPS proxy Keep-Alive connection pooling**: Upgraded `_KeepAlivePool` in `http_transport.py` to pool persistent HTTP/1.1 `CONNECT` tunnels over `http://` proxies and single-layer TLS proxy connections, stripping/redacting caller `Proxy-Authorization` headers and failing closed on unsafe or malformed proxy URLs—significantly cutting cross-region and proxied API round-trip latency.
- **Expanded multi-OS CI matrix & real loopback E2E**: Extended GitHub Actions CI across Python `3.10–3.14` with clean wheel installation smoke tests on Linux/macOS/Windows, real `sshd` loopback remote-probing tests, multi-process daemon lifecycle E2E, and Win32 clipboard sequence verification.

### Changed
- **Standardized single-header `DOUBAO_SPEECH_API_KEY`, flash-first fallback & polling tuning**: Standardized `DOUBAO_SPEECH_API_KEY` for single-header (`X-Api-Key`) Doubao Speech auth without requiring `APP_ID` (retaining legacy `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` compatibility), switched Doubao ASR internal routing to prefer the low-latency flash slot (`volc.bigasr.auc_turbo`) with cached fallback to Doubao 2.0 standard (`volc.seedasr.auc`), and tuned Doubao 2.0 asynchronous `/query` polling backoff.
- **Refiner prompt & cross-platform context refinements**: Tightened anti-over-edit prompt guidance to avoid superfluous editorial comments or rewrites; anchored the macOS HUD capsule to the active focused window on multi-display setups; filtered built-in CJK hotwords for non-Chinese locales; added Eternal Terminal (`et`) remote SSH command recognition; and hardened POSIX `LC_ALL=C` strict ASCII locale compatibility.

### Fixed
- **Dictation, delivery & storage reliability hardening**: Guarded multi-chunk ASR against silent dropped-chunk data loss, fixed a Windows `rec_stop` race that could block recording completion, enabled proactive `24h` TTL cleanup for `failed/` audio snapshots, added graceful fallback when `hotwords.txt` contains invalid text encodings, unified the 4-state HUD / telemetry / `diagnose` delivery contract, and resolved macOS `/var` symlink path comparisons in tests.

## [0.11.0] - 2026-11-21

### Added
- **Agent session cold-start context fallback (`[refiner].cold_start_fallback`)**: Added three-layer cold-start fallback (`"auto"` default, `"hotwords"`, or `"off"`) when a newly opened terminal or coding agent session has no active transcript yet—combining Layer 0 safe `win.cwd` workspace/git fallback gated by the Remote Veto Gate, Layer 1 cross-CLI exact-`cwd` prior-session hotword extraction, and Layer 2 natural-eviction `<=25K` `<prior_session_reference>` frozen prefix blocks that drop cleanly once live turns grow; aligned single-RTT SSH `_remote_probe.py` cold-start probing and exposed a safe `ctx_fallback` field in `diagnose`.
- **First-class scoped hotwords management (`agent-voice-input hotwords`)**: Added `src/agent_voice_input/hotwords.py` and `agent-voice-input hotwords list|add|remove [--scope global|chat|coding]` backed by scoped `[global]`/`[chat]`/`[coding]` sections in `hotwords.txt` (preserving UTF-8 BOM, CRLF line endings, and comments with `flock` + `0600` atomic writes), live file-stat reloading without restarting the daemon, 4-tier priority merging, and unified `budget_hotwords_for_provider` quota enforcement across Doubao, Whisper, DashScope, Gemini, and OpenRouter.
- **Multi-week sleep/wake recovery & long-run self-healing**: Hardened long-lived daemon sessions across laptop sleep/wake and network switches with dual-clock `_KeepAlivePool` sleep/drift eviction, `0ms` stale-socket probing, zero-DNS proxy bypass, and cancellable `_BoundedDnsResolver` lookups; added Windows `PowerBroadcastWindow` (`WM_POWERBROADCAST`) atomic hook reinstallation and macOS `NSWorkspace.didWakeNotification` wake recovery with PTT safety stop; added O(1) 5-tuple `verified_stat` session short-circuiting and bounded 64-entry LRU caches with ETag pinning; and added `2 MiB` atomic `daemon.log` rotation to `daemon.log.1` with `diagnose` tail fallback.

### Fixed
- **macOS symlinked Claude project directory cold-start discovery**: Resolved symlinked project paths (such as `/var` -> `/private/var`) during local and remote cold-start prior-session discovery so Claude Code project directories match reliably on macOS.
- **Test & CI stability**: Replaced fixed startup sleeps in daemon control-socket tests with `_wait_for_control_socket` readiness polling and added non-interactive `apt-get` HTTPS timeout flags in CI and publish workflows.

## [0.10.0] - 2026-11-20

### Added
- **Official `diagnose` CLI subcommand, Doubao 1.0 `--ping` & full `--help` coverage**: Added `agent-voice-input diagnose [--last N] [--json]` for read-only live diagnostics (daemon health, latency p50, prompt-cache hits, `failed/` count, and redacted log tail), added Doubao ASR 1.0 endpoint `--ping` probing in `doctor`, surfaced resolved active ASR/Refiner pipelines across `status` and `doctor`, and completed `--help` coverage across all subcommands.
- **3-zone live hold-to-cancel hotkey FSM & in-place hold preview**: Replaced release-only cancel handling in `tap_toggle` mode with a responsive 3-zone state machine (`<= 500ms` isolated tap toggles recording, `500–800ms` silent hesitation deadzone, and `>= 800ms` configurable `hold_cancel_ms` firing `hold_cancel` immediately while still held and swallowing the subsequent release), displaying a live in-place preview hint (`"继续按住取消…"` / `"keep holding to cancel…"`) after `250ms` and enabling layered cancellation across both active recording and in-flight processing (lifting the `only_inflight=True` restriction).
- **Back-to-back in-flight HUD badge (`+1`/`+2`) & bilingual offscreen layout self-checks**: Added background in-flight count badges (`+1`, `+2`, …) to the HUD pill during pipelined dictation, and added automated bilingual offscreen pixel-layout self-checks across Windows (`win_shim.ps1`, 216 cases) and macOS Retina (`mac_shim.swift`), wired into `e2e_system.py` (`C7`).

### Changed
- **Idempotent `start`, unconditional `restart` & explicit `status` states**: Made `agent-voice-input start` cleanly idempotent while warning when the running daemon's code or version has drifted from disk, made `restart` unconditionally stop and relaunch, and distinguished `stopped`, `unresponsive`, and `error` states in `status` (with `--history N`).
- **Zero-surprise `auto` pipeline routing & cross-stage provider aliases**: Unified Codex subscription vs. API-key authentication mode checks and base-URL sanitization so `refiner.provider = "auto"` only reuses confirmed local ChatGPT subscription logins without unexpected API billing; added cross-stage aliases (`volcengine`/`volc` → `doubao` for Stage 1, `doubao`/`volc`/`ark` → `volcengine` for Stage 2, and `bailian` → `dashscope` on both stages).
- **Refined 3-end recording copy & smooth `tap_max_ms` config migration**: Tightened recording HUD prompts across Python, Windows, and macOS (`"按右 Ctrl 结束"` / `"松开右 Ctrl 结束"` and matching English copy) and preserved smooth backward compatibility for legacy `hotkey.tap_max_ms` configurations alongside the new `500ms` default and `hold_cancel_ms = 800`.

### Fixed
- **Cross-platform hotkey chord state self-healing**: Added Windows stale `_otherDown` key state self-healing in `win_shim.ps1` and periodic macOS modifier-chord re-checks in `mac_shim.swift` so interrupted modifier chords do not leave hotkeys stuck.
- **CI runner tool completeness & `voice-eval` cache isolation**: Installed `ffmpeg` and `tmux` across all GitHub Actions runners and forbade silent test skips in CI (`ci.yml` / `publish.yml`), and isolated per-slot ASR cache keys while fixing `--manifest-from` denominator counts in `eval_recordings.py`.

## [0.9.1] - 2026-11-20

### Added
- **Windows `warp` terminal & `Ctrl+Shift+V` paste routing**: Added `warp` to `WINDOWS_TERMINALS` and `WINDOWS_CTRL_SHIFT_V_PROCS` in `app_scope.py` (forwarded to `win_shim.ps1` via `-AllowedTerminals` with standalone default fallback), bringing Windows terminal detection and paste-chord routing to parity with macOS `Warp` support.
- **Architecture contract, microsecond GIL-preemption concurrency & tiered coverage gates**: Added `tests/test_architecture_contracts.py` (enforcing strict 21-module intra-package DAG, stdlib-only runtime imports, live `unittest.mock.patch` bytecode target verification, 3-end HUD state/layer/duration parity, provider factory completeness, and cross-platform terminal parity), `tests/test_concurrency_races.py` (`1µs` `sys.setswitchinterval` + stack-dumping deadlock watchdog torture tests across `VoiceInputDaemon` out-of-order Stage 2 / cancellation / mic-drop storms, `_KeepAlivePool` + `CancelToken` socket shutdowns, `SessionTracker` + `RemoteSshManager` + cross-process `flock` session registry, and `JsonlShimMixin` crash self-healing), `tests/test_core_scenarios_coverage.py` (real-input TOML/config/window-title/LLM/loopback-redirect/in-process `_remote_probe` scenario tests with zero fake syscall mocks), and `scripts/check_coverage.py` (wired into CI to enforce independent Statement % and Pure Branch % thresholds alongside a zero-dependency `--stdlib-trace` local fallback).

### Changed
- **100 MiB clipboard snapshot limit**: Raised the transient clipboard backup/restore cap from `20 MiB` to `100 MiB` across Windows (`win_shim.ps1` `MaxClipSnapshotBytes`) and macOS (`mac_shim.swift` `PasteboardGuard.defaultMaxSnapshotBytes`) so uncompressed 4K/5K screenshots and multi-format clipboard payloads restore cleanly after the default `250ms` paste delay, clearing snapshot references immediately once restoration completes, is superseded, or is abandoned, and without double-counting synthesized `CF_DIB` alongside `CF_DIBV5` (or `.tiff` alongside `.png`).
- **Clearer WASAPI zero-frame startup toast (`mic_no_stream`)**: Distinguished `wasapi_startup_no_frames` (`IAudioClient.Start()` succeeded on an active endpoint but produced zero audio packets within `850ms`, such as a Bluetooth headset resting inside its charging case) from exclusive-mode device lock (`mic_unavailable`), showing `"! 麦克风无音频流，请检查耳机/设备"` / `"! No mic audio stream, check headset"` instead of `"! 麦克风暂忙，请再试一次"`.
- **Modularized core package boundaries, cleaner CLI/daemon bindings & faster `session-hook` cold start**: Extracted shared pure-stdlib HTTP transport (`http_transport.py`), cross-stage LLM helpers (`llm_common.py`), cross-platform recorder/shim primitives (`platform_common.py`), and workspace/IDE/hotword discovery (`workspace_ctx.py`), eliminating `refiner` -> `asr_providers` and `platform_macos` -> `platform_wsl` coupling; deduplicated shared transcript/CLI helpers onto standalone `_remote_probe.py` while unifying 5 local-vs-remote edge cases (`file://` + NUL CWD guards, symlinked broad-directory ranking, short-read-safe `0700` registry reads, and strict `claude`/`codex`/`agy` CLI matching); centralized terminal lists and default provider URLs into `app_scope.py` and `config.py`; removed dynamic `globals()` lazy-symbol injection in `cli.py` and reflection shims / dead wrappers in `daemon.py` in favor of explicit module bindings; and kept lazy-import fast paths for `agent-voice-input session-hook` and `remote-ctx` that skip loading heavy ASR, refiner, daemon, HTTP transport, and platform modules on every agent turn.

### Fixed
- **Cross-thread shim pipe reaping, pooled connection abort generation, and registry `.tmp` lock races**: Enforced owner-exclusive `stdout` closure and lock-coordinated/deferred `stdin` closure during `JsonlShimMixin` subprocess reaping to prevent cross-thread `io.TextIOWrapper` races or hangs on abrupt shim crashes; added per-borrow generation guards (`_borrow_gen` / `make_abort_fn`) and atomic `_aborted` checks in `_KeepAlivePool.acquire`/`release` so stale `CancelToken` callbacks cannot abort recycled keep-alive connections; and extended `write_session_registry_entry`'s shared `.registry.lock` (`LOCK_SH`) across `.tmp` creation so `prune_stale_session_registry` (`LOCK_EX`) cannot unlink in-flight `.tmp` files across system clock jumps.
- **Diverse-environment tool `PATH`, directory permissions, Windows `ssh.exe` & macOS auth cooldown compatibility**: Added fallback discovery for `node`/`codex` (`nvm`/`fnm`), `ffmpeg`, `tmux`, and macOS system tools under minimal non-interactive `PATH` environments (including Homebrew prefixes); handled missing `XDG_RUNTIME_DIR` cache parent permissions and inherited `setgid`/`sticky` mode bits on runtime directories; fixed quoted/space-containing Windows `ssh.exe` paths and SSH account names across remote probe/title matching; and added cooldown-based recovery after transient macOS XProtect/TCC timeouts and `iTerm2` Automation authorization checks instead of permanently locking out the helper.

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

[Unreleased]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/xiaohuaxi/agent-voice-input/compare/v0.9.0...v0.9.1
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
