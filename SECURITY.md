# Security Policy

## Supported Versions

Security fixes are provided for the latest release on the `main` branch.

| Version | Supported |
| --- | --- |
| Latest released version | Yes |
| Older releases | No — please upgrade to the latest release |

## Privacy & Security Architecture

`agent-voice-input` is designed as a local-first desktop voice input tool with a minimal attack surface:

- **No telemetry or analytics reporting:** The project has no author-hosted backend or relay server and never phones home. Audio and session context are sent directly and only to the Stage 1 ASR and Stage 2 Refiner providers you explicitly configure, or that default `auto` mode selects from credentials already present on your machine (including an existing local Codex CLI login).
- **Zero third-party Python runtime dependencies:** Core Python code runs strictly on the Python 3.10+ standard library alongside host OS tools (`ffmpeg`, macOS `swiftc`/`osascript`, Windows `powershell.exe`), minimizing third-party supply-chain risk.
- **Strict local file permissions:** The application home (`~/.agent-voice-input/`), hotwords directory (`hotwords/`), opt-in archive directories (`audio/` and `transcripts/`), runtime directory, and cache directories are created with `0700` permissions (same-UID directory symlinks are allowed for the app home and `hotwords/` without mutating target permissions, while single-file symlinks on `hotwords/personal.txt` and archive target files are rejected). Credential files (`.env`) and configuration (`config.toml`) are created exclusively with `0600` permissions (`O_CREAT | O_EXCL | O_NOFOLLOW`) on initial `init` (`init --force` atomically replaces only `config.toml` via a same-directory temporary file and preserves any existing `.env`); `init` also creates a default-ignore `.gitignore` (tracking only `hotwords/`) so `.env`, `config.toml`, `audio/`, and `transcripts/` are excluded from git by default. Runtime state snapshots, `hotwords/personal.txt`, and staged failed recordings are atomically written with `0600` permissions, while `daemon.log` and opt-in `transcripts/YYYY-MM.jsonl` are appended with `0600` permissions.
- **Bounded local persistence (off by default):** Normal successful dictations persist neither audio nor transcripts on disk. Audio is written (`0600`) only when (1) a post-recording pipeline failure/cancellation is queued for `retry` in `<runtime_dir>/failed/` (capped at the 10 most recent WAV files within 24 hours; recording-time cancellations drop immediately), or (2) you explicitly enable `archive.save_audio = true` (stored as `0600` MP3 under `~/.agent-voice-input/audio/<id>.mp3`, rolling cap `archive.audio_max_count = 1000`, `0` = unlimited). Spoken transcripts are written (`0600`) only when you explicitly enable `archive.save_transcripts = true` (monthly 12-field JSONL under `~/.agent-voice-input/transcripts/YYYY-MM.jsonl`, recording only ASR draft/final text and flat metadata—never session context, hotword lists, or credentials).
- **Secret redaction & remote allowlisting:** Logs, `doctor`, `diagnose`, `status`, and the local diagnostic log (`diag.jsonl`) redact configured secrets and omit spoken voice transcripts, session context, and hotword lists. Optional SSH remote context probing strictly requires explicit `[remote] hosts` allowlist entries and validates untrusted terminal OSC 2 titles before executing any remote probe.

### Trust Model

`agent-voice-input` targets a single-user desktop environment. Security boundaries focus on untrusted cross-boundary inputs (such as terminal OSC 2 window titles controlled by remote output, HTTP/API responses, and SSH remote probe output) and local multi-user file isolation (`0700`/`0600`). Adversarial attacks from processes already running under the same OS user UID are outside the threat model.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them privately using [GitHub Security Advisories](https://github.com/xiaohuaxi/agent-voice-input/security/advisories/new).

When reporting, please include:
- Affected `agent-voice-input` version and platform (macOS or Windows + WSL2)
- A clear description of the vulnerability and potential impact
- Minimal reproduction steps or proof of concept (without real API keys or private audio)

We aim to acknowledge new reports within 3 business days and coordinate a fix and disclosure timeline with you once confirmed.
