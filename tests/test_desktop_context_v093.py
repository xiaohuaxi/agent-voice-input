"""Offline unit tests for v0.9-3 desktop GUI scene awareness & non-Git workspace support."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input.app_scope import (  # noqa: E402
    IdeTitleInfo,
    classify_target_app_hint,
    classify_window_category,
    is_antigravity_ide_window,
    is_codex_desktop_window,
    is_terminal_window,
    parse_ide_window_title,
)
from agent_voice_input.asr_providers import (  # noqa: E402
    AsrContext,
    AsrResult,
    BaseAsrProvider,
    ChatAudioAsrProvider,
    GeminiAudioProvider,
    HttpRequest,
    HttpResponse,
    _coerce_context,
    _normalize_single_hop_text,
)
from agent_voice_input.audio_proc import pack_pcm16_mono_wav  # noqa: E402
from agent_voice_input.config import Config, resolve_credentials  # noqa: E402
from agent_voice_input.daemon import (  # noqa: E402
    VoiceInputDaemon,
    _window_from_mapping,
    build_pipeline_summary,
    run_pipeline_once,
)
from agent_voice_input.platform_macos import MAC_SHIM_SCRIPT, SHIM_SCHEMA_VERSION, MacOSPlatform, _APPLE_SEP  # noqa: E402
from agent_voice_input.platform_wsl import ForegroundWindow, PasteOutcome, WslPlatform  # noqa: E402
from agent_voice_input.prompts import (  # noqa: E402
    REFINER_GENERAL_SYSTEM_PROMPT,
    SINGLE_HOP_GENERAL_SYSTEM_PROMPT,
    TARGET_APP_HINTS,
    strip_target_hint_echo,
)
from agent_voice_input.refiner import (  # noqa: E402
    GeminiRefiner,
    OpenAICompatibleRefiner,
    OpenAIResponsesRefiner,
    _clean_out,
    build_refiner_request_blocks,
)
from agent_voice_input.session_ctx import (  # noqa: E402
    BUILTIN_AGENT_HOTWORDS,
    SessionMatch,
    SessionTracker,
    discover_antigravity_ide_prior_sessions,
    is_wsl_slow_mount_path,
    read_git_status_summary,
    read_repo_core_files,
    read_workspace_shallow_files,
    resolve_antigravity_ide_session,
    resolve_antigravity_ide_session_by_title,
    resolve_codex_desktop_session_by_title,
    resolve_ide_workspace_cwd,
)


def _voiced_wav(duration_ms: int = 450, amp: int = 6000) -> bytes:
    n_samples = int(16000 * duration_ms / 1000)
    pcm = bytearray()
    for i in range(n_samples):
        val = amp if (i // 32) % 2 == 0 else -amp
        pcm.extend(int(val).to_bytes(2, "little", signed=True))
    return pack_pcm16_mono_wav(bytes(pcm), sample_rate=16000)


def _json_resp(payload: object) -> HttpResponse:
    return HttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))


class _DummyAsr(BaseAsrProvider):
    provider_name = "dashscope"

    def __init__(self, text: str = "测试口述草稿", *, env: dict[str, str] | None = None) -> None:
        self.text = text
        self.env = env
        self.seen_contexts: list[AsrContext] = []

    def transcribe(
        self, wav_bytes: bytes, context: AsrContext | None = None, *, is_cancelled: object = None
    ) -> AsrResult:
        del wav_bytes, is_cancelled
        self.seen_contexts.append(_coerce_context(context, "", env=self.env))
        return AsrResult(text=self.text, provider="dashscope", model="qwen3-asr-flash", latency_ms=10)


class _DummyPlatform:
    def __init__(self, fg: ForegroundWindow | None = None) -> None:
        self.fg = fg or ForegroundWindow(100, "Cursor", "a.py — repo — Cursor")

    def is_allowed_terminal(self, win: ForegroundWindow) -> bool:
        return win.process.lower() in ("windowsterminal.exe", "ghostty")

    def get_foreground_window(self) -> ForegroundWindow:
        return self.fg

    def start_recording(self, runtime_dir: Path, **kw: object) -> None:
        del runtime_dir, kw

    def stop_recording(self) -> tuple[bytes, str]:
        return _voiced_wav(), "pulse"

    def cancel_recording(self) -> None:
        pass

    def beep(self, kind: str) -> None:
        del kind

    def hud(self, state: str, **kw: object) -> None:
        del state, kw

    def deliver_text(self, text: str, seq_id: int, target_hwnd: int = 0, **kw: object) -> PasteOutcome:
        del text, seq_id, target_hwnd, kw
        return PasteOutcome(True, True, "ok", self.fg.process)

    def close(self) -> None:
        pass


def _write_agy_brain_session(
    brain_root: Path,
    cid: str,
    records: list[dict[str, object]],
    *,
    mtime: float,
    workspace_uris: list[str] | None = None,
) -> Path:
    cid_dir = brain_root / cid
    logs_dir = cid_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tp = logs_dir / "transcript_full.jsonl"
    tp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    os.utime(tp, (mtime, mtime))
    if workspace_uris is not None:
        sub_dir = cid_dir / ".system_generated" / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "sub1.json").write_text(json.dumps({"workspaceUris": workspace_uris}), encoding="utf-8")
    return tp


class DesktopGuiContextV093Tests(unittest.TestCase):
    def test_parse_ide_window_title_matrix(self) -> None:
        valid_cases = [
            (ForegroundWindow(1, "Cursor", "● a.py — agent-voice-input — Cursor"), IdeTitleInfo(root_name="agent-voice-input")),
            (ForegroundWindow(2, "Code - Insiders.exe", "a.py - agent-voice-input - Visual Studio Code - Insiders [Administrator]"), IdeTitleInfo(root_name="agent-voice-input")),
            (ForegroundWindow(3, "Windsurf", "foo - copy.py — my-repo — Windsurf"), IdeTitleInfo(root_name="my-repo")),
            (ForegroundWindow(4, "Antigravity", "x.py — repo [WSL: Ubuntu] — Antigravity"), IdeTitleInfo(root_name="repo", remote_kind="wsl")),
            (ForegroundWindow(5, "Code.exe", "x.py — repo [SSH: prod] — Visual Studio Code"), IdeTitleInfo(root_name="repo", remote_kind="remote")),
            (ForegroundWindow(6, "Code", "x.py — repo [Dev Container: Python 3] — Visual Studio Code"), IdeTitleInfo(root_name="repo", remote_kind="container")),
            (ForegroundWindow(7, "Cursor", "bot.py — next.js — Cursor"), IdeTitleInfo(root_name="next.js")),
            (ForegroundWindow(8, "Cursor", "Q4产品规划 — Cursor"), IdeTitleInfo(root_name="Q4产品规划")),
            (ForegroundWindow(17, "Antigravity", "main.py — New Chat — Antigravity", bundle_id="com.google.antigravity"), IdeTitleInfo(root_name="New Chat")),
            (ForegroundWindow(18, "Code", "app.py — New Conversation — Visual Studio Code"), IdeTitleInfo(root_name="New Conversation")),
            (ForegroundWindow(20, "Zed", "my-proj — src/main.rs", bundle_id="dev.zed.zed"), IdeTitleInfo(root_name="my-proj")),
            (ForegroundWindow(30, "pycharm64.exe", "proj – [~/repos/proj] – app.py"), IdeTitleInfo(root_name="proj", explicit_path="~/repos/proj")),
            (ForegroundWindow(31, "IntelliJ IDEA", "proj [~/repos/proj] – Main.kt", bundle_id="com.jetbrains.intellij"), IdeTitleInfo(root_name="proj", explicit_path="~/repos/proj")),
            (ForegroundWindow(32, "goland64.exe", "svc – handler.go – GoLand"), IdeTitleInfo(root_name="svc")),
            (ForegroundWindow(40, "Xcode", "VoiceApp — View.swift", bundle_id="com.apple.dt.xcode"), IdeTitleInfo(root_name="VoiceApp")),
            (ForegroundWindow(42, "sublime_text.exe", "~/repos/proj/src/a.py (proj) - Sublime Text (UNREGISTERED)"), IdeTitleInfo(root_name="proj", explicit_path="~/repos/proj/src/a.py")),
        ]
        for win, expected in valid_cases:
            self.assertEqual(parse_ide_window_title(win), expected, msg=f"mismatch for {win}")

        for bad_win in (
            ForegroundWindow(10, "Code.exe", "main.py - Visual Studio Code"),
            ForegroundWindow(11, "Code.exe", "Welcome - Visual Studio Code"),
            ForegroundWindow(12, "Cursor.exe", "Settings — Cursor"),
            ForegroundWindow(13, "Code.exe", "Untitled-1 - Visual Studio Code"),
            ForegroundWindow(14, "Code.exe", "my-proj (Workspace) - Visual Studio Code"),
            ForegroundWindow(15, "Antigravity", "Antigravity", bundle_id="com.google.antigravity"),
            ForegroundWindow(16, "Antigravity", "Agent Manager — Antigravity"),
            ForegroundWindow(21, "Zed", "empty project", bundle_id="dev.zed.zed"),
            ForegroundWindow(22, "Zed", "main.rs", bundle_id="dev.zed.zed"),
            ForegroundWindow(33, "pycharm64.exe", "Welcome to PyCharm"),
            ForegroundWindow(41, "Xcode", "Welcome to Xcode", bundle_id="com.apple.dt.xcode"),
            ForegroundWindow(43, "sublime_text.exe", "notes.txt - Sublime Text"),
        ):
            self.assertIsNone(parse_ide_window_title(bad_win), msg=f"should reject {bad_win}")

        self.assertTrue(is_antigravity_ide_window(ForegroundWindow(50, "Antigravity", "x", bundle_id="com.google.antigravity")))
        self.assertFalse(is_antigravity_ide_window(ForegroundWindow(51, "Cursor", "x")))

    def test_macos_ax_document_shim_and_osascript_compat(self) -> None:
        self.assertEqual(SHIM_SCHEMA_VERSION, 5)
        shim_src = MAC_SHIM_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("kAXDocumentAttribute", shim_src)
        self.assertNotIn("import Carbon", shim_src)
        self.assertNotIn("requestAccess", shim_src)

        plat = MacOSPlatform(Config(), env={"PATH": ""}, shim_cmd_factory=lambda: None)
        try:
            for osa_out in (
                _APPLE_SEP.join(["Cursor", "com.todesktop.230313mzl4w4u92", "501", "a.py — repo — Cursor"]),
                _APPLE_SEP.join(["Cursor", "com.todesktop.230313mzl4w4u92", "501", "a.py — repo — Cursor", "missing value"]),
                _APPLE_SEP.join(["Cursor", "com.todesktop.230313mzl4w4u92", "501", "a.py — repo — Cursor", "vscode-remote://ssh-remote+prod/app/a.py"]),
            ):
                cp = subprocess.CompletedProcess(["osascript"], 0, stdout=osa_out + "\n", stderr="")
                with mock.patch.object(plat, "_run_osascript", return_value=cp):
                    self.assertEqual(plat.get_foreground_window().document, "")

            valid_doc = "file:///Users/demo/repos/my-repo/src/main.py"
            osa_5 = _APPLE_SEP.join(["Cursor", "com.todesktop.230313mzl4w4u92", "501", "main.py — my-repo — Cursor", valid_doc])
            cp_ok = subprocess.CompletedProcess(["osascript"], 0, stdout=osa_5 + "\n", stderr="")
            with mock.patch.object(plat, "_run_osascript", return_value=cp_ok):
                self.assertEqual(plat.get_foreground_window().document, valid_doc)
        finally:
            plat.close()

        mapped = _window_from_mapping(
            {"hwnd": 12, "process": "Xcode", "title": "App — Foo.swift", "document": "file:///Users/demo/App/Foo.swift"}
        )
        self.assertEqual(mapped.document, "file:///Users/demo/App/Foo.swift")

    def test_resolve_ide_workspace_cwd_git_nongit_and_disambiguation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            repos, work, docs, desktop = home / "repos", home / "work", home / "Documents", home / "Desktop"
            for d in (repos, work, docs, desktop):
                d.mkdir(parents=True)

            # 1. Non-Git ordinary folders in ~/Documents or ~/Desktop accepted when unique
            q4_dir = docs / "Q4产品规划"
            q4_dir.mkdir()
            (q4_dir / "路线图.md").write_text("# Q4\n", encoding="utf-8")
            self.assertEqual(
                resolve_ide_workspace_cwd(ForegroundWindow(1, "Cursor", "路线图.md — Q4产品规划 — Cursor"), home=home, wsl=False),
                os.path.realpath(q4_dir),
            )
            desk_notes = desktop / "reading-notes"
            desk_notes.mkdir()
            self.assertEqual(
                resolve_ide_workspace_cwd(ForegroundWindow(2, "Code", "ch1.md — reading-notes — Visual Studio Code"), home=home, wsl=False),
                os.path.realpath(desk_notes),
            )

            # 2a. Two non-Git directories with same name -> ""
            (docs / "dup-folder").mkdir()
            (desktop / "dup-folder").mkdir()
            self.assertEqual(
                resolve_ide_workspace_cwd(ForegroundWindow(3, "Cursor", "a.md — dup-folder — Cursor"), home=home, wsl=False),
                "",
            )

            # 2b. One Git repo + one plain folder -> prefers the sole Git repo; symlink deduplicates by inode
            git_app, plain_app, code_dir = repos / "app", work / "app", home / "code"
            (git_app / ".git").mkdir(parents=True)
            plain_app.mkdir()
            code_dir.mkdir()
            os.symlink(git_app, code_dir / "app")
            for hwnd in (4, 5):
                self.assertEqual(
                    resolve_ide_workspace_cwd(ForegroundWindow(hwnd, "Cursor", "main.py — app — Cursor"), home=home, wsl=False),
                    os.path.realpath(git_app),
                )

            # 2c. Two distinct Git repos with same name (either on disk or via known_cwds) -> strictly returns ""
            old_known_app = Path(td) / "external_old" / "app"
            (old_known_app / ".git").mkdir(parents=True)
            self.assertEqual(
                resolve_ide_workspace_cwd(
                    ForegroundWindow(6, "Cursor", "main.py — app — Cursor"),
                    home=home,
                    known_cwds=[str(old_known_app)],
                    wsl=False,
                ),
                "",
            )
            (plain_app / ".git").write_text("gitdir: /tmp/fake-wt\n", encoding="utf-8")
            self.assertEqual(
                resolve_ide_workspace_cwd(ForegroundWindow(6, "Cursor", "main.py — app — Cursor"), home=home, wsl=False),
                "",
            )

            # 3. macOS AXDocument: monorepo subpackage match, single-file .git walk, & external file fallback
            mono_root = repos / "monorepo"
            (mono_root / ".git").mkdir(parents=True)
            sub_pkg = mono_root / "packages" / "web-ui"
            (sub_pkg / "src").mkdir(parents=True)
            f_sub = sub_pkg / "src" / "App.tsx"
            f_sub.write_text("export default 1;\n", encoding="utf-8")
            zshrc = home / ".zshrc"
            zshrc.write_text("# zsh\n", encoding="utf-8")

            self.assertEqual(
                resolve_ide_workspace_cwd(
                    ForegroundWindow(7, "Cursor", "App.tsx — web-ui — Cursor", document=f_sub.as_uri()), home=home, wsl=False
                ),
                os.path.realpath(sub_pkg),
            )
            self.assertEqual(
                resolve_ide_workspace_cwd(
                    ForegroundWindow(8, "Code", "App.tsx - Visual Studio Code", document=f_sub.as_uri()), home=home, wsl=False
                ),
                os.path.realpath(mono_root),
            )
            self.assertEqual(
                resolve_ide_workspace_cwd(
                    ForegroundWindow(9, "Cursor", ".zshrc — monorepo — Cursor", document=zshrc.as_uri()), home=home, wsl=False
                ),
                os.path.realpath(mono_root),
            )
            self.assertEqual(
                resolve_ide_workspace_cwd(
                    ForegroundWindow(10, "Code", "a.py — monorepo [SSH: prod] — Visual Studio Code"), home=home, wsl=False
                ),
                "",
            )

    def test_nongit_shallow_files_and_privacy_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            docs_proj = home / "Documents" / "季度产品方案"
            for sub in ("specs", ".hidden", "node_modules/pkg", ".venv/lib"):
                (docs_proj / sub).mkdir(parents=True)
            (docs_proj / "Q4架构设计.md").write_text("# arch\n", encoding="utf-8")
            (docs_proj / "budget_forecast.xlsx").write_bytes(b"xlsx")
            (docs_proj / "~$budget_forecast.xlsx").write_bytes(b"lock")
            (docs_proj / "specs" / "api_schema.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
            (docs_proj / ".hidden" / "secret_note.md").write_text("no\n", encoding="utf-8")
            (docs_proj / "node_modules" / "pkg" / "noise_dep.js").write_text("1\n", encoding="utf-8")
            (docs_proj / ".venv" / "lib" / "venv_mod.py").write_text("1\n", encoding="utf-8")
            (home / "私人身份证扫描.pdf").write_bytes(b"pdf")
            (home / "personal_tax_2026.xlsx").write_bytes(b"xlsx")

            shallow = read_workspace_shallow_files(str(docs_proj), home=home, wsl=False)
            for wanted in ("Q4架构设计.md", "budget_forecast.xlsx", "api_schema.yaml"):
                self.assertIn(wanted, shallow)
            for unwanted in ("~$budget_forecast.xlsx", "secret_note.md", "noise_dep.js", "venv_mod.py"):
                self.assertNotIn(unwanted, shallow)

            # Privacy guardrails: ~, /, /tmp, /Users, /home/<user> (including APFS case variants) -> [] & ZERO os.scandir!
            with mock.patch("agent_voice_input.workspace_ctx.os.scandir") as mock_scandir:
                for forbidden in (
                    str(home), "/", "/tmp", "/PRIVATE/TMP", "/Users", "/users", "/home/alice", "/Users/alice", "/users/demo"
                ):
                    self.assertEqual(read_workspace_shallow_files(forbidden, home=home, wsl=False), [])
                self.assertEqual(mock_scandir.call_count, 0)

            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test"}
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc")
            seen_bodies: list[dict[str, object]] = []

            def _fake_transport(req: HttpRequest) -> HttpResponse:
                seen_bodies.append(json.loads(req.body.decode("utf-8")))
                return _json_resp({"choices": [{"message": {"content": "请查看 Q4架构设计.md 与 api_schema.yaml。"}}]})

            refiner = OpenAICompatibleRefiner(
                cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_fake_transport
            )
            asr = _DummyAsr("请查看 Q4 架构设计点 md 与 api schema 点 yaml", env=env)
            ide_win = ForegroundWindow(210, "Cursor", "Q4架构设计.md — 季度产品方案 — Cursor")

            res1 = run_pipeline_once(
                _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                platform=_DummyPlatform(ide_win), origin_window=ide_win, audio_source="winmm", dry_run=True, env=env,
            )
            self.assertTrue(res1.ok)
            for stem in ("Q4架构设计", "budget_forecast", "api_schema"):
                self.assertIn(stem, asr.seen_contexts[-1].hotwords)
            user_msg = str(seen_bodies[-1]["messages"][1]["content"])  # type: ignore[index]
            self.assertIn("workspace_files (local folder; spelling reference only):", user_msg)
            self.assertIn("Q4架构设计.md", user_msg)
            self.assertNotIn("私人身份证扫描", user_msg)

            with mock.patch("agent_voice_input.workspace_ctx.os.scandir") as mock_scan2:
                res2 = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(ide_win), origin_window=ide_win, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(res2.ok)
                self.assertEqual(mock_scan2.call_count, 0)

            cli_rollout = tmp / "rollout-cli.jsonl"
            cli_rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "cli-doc-1", "source": "cli", "cwd": str(docs_proj)}}) + "\n",
                encoding="utf-8",
            )
            snap_doc = tracker.sync_session(
                SessionMatch(cli="codex", session_id="cli-doc-1", jsonl_path=str(cli_rollout), cwd=str(docs_proj))
            )
            assert snap_doc is not None
            self.assertIn("Q4架构设计", snap_doc.hotwords)
            _, git_ctx_doc = tracker.get_refiner_context(snap_doc.session_key, include_git_status=True)
            self.assertIn("workspace_files (local folder; spelling reference only):", git_ctx_doc)

            cli_home_rollout = tmp / "rollout-home.jsonl"
            cli_home_rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "cli-home-1", "source": "cli", "cwd": str(home)}}) + "\n",
                encoding="utf-8",
            )
            snap_home = tracker.sync_session(
                SessionMatch(cli="codex", session_id="cli-home-1", jsonl_path=str(cli_home_rollout), cwd=str(home))
            )
            assert snap_home is not None
            self.assertNotIn("私人身份证扫描", snap_home.hotwords)
            self.assertNotIn("personal_tax_2026", snap_home.hotwords)
            _, git_ctx_home = tracker.get_refiner_context(snap_home.session_key, include_git_status=True)
            self.assertNotIn("私人身份证扫描", git_ctx_home)

    def test_wsl2_slow_mount_zero_io_and_zero_git_spawns(self) -> None:
        with mock.patch("os.stat", side_effect=AssertionError("os.stat forbidden")), mock.patch(
            "os.path.realpath", side_effect=AssertionError("realpath forbidden")
        ):
            for slow in (
                "/mnt/c/Users/dev/proj", "/mnt/d/work", "/mnt/wslg/distro",
                "//server/share/repo", r"\\wsl$\Ubuntu\home\user", r"C:\Users\dev\repo",
            ):
                self.assertTrue(is_wsl_slow_mount_path(slow, wsl=True))
            self.assertFalse(is_wsl_slow_mount_path("/mnt/c/Users/dev/proj", wsl=False))

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repos = home / "repos"
            repos.mkdir(parents=True)
            win_link = repos / "win-proj"
            os.symlink("/mnt/c/Users/demo/win-proj", win_link)
            linux_repo = repos / "linux-proj"
            (linux_repo / ".git").mkdir(parents=True)
            wsl_env = {"HOME": str(home), "WSL_DISTRO_NAME": "Ubuntu"}

            # Also verify /mnt/ evidence inside Antigravity transcript never triggers realpath on /mnt/
            ide_brain = home / ".gemini" / "antigravity" / "brain"
            _write_agy_brain_session(
                ide_brain,
                "wsl-mnt-ev-1111",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "check"},
                    {
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "content": "reading",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "/mnt/d/sleeping_hdd/big.txt"}}],
                    },
                ],
                mtime=time.time() - 5,
                workspace_uris=["file:///mnt/d/sleeping_hdd"],
            )
            orig_lstat = os.lstat
            orig_stat = os.stat

            def _no_mnt_lstat(p: object, *a: object, **kw: object):
                s = os.fsdecode(p)  # type: ignore[arg-type]
                if s == "/mnt" or s.startswith("/mnt/"):
                    raise AssertionError(f"lstat called on WSL slow mount: {s}")
                return orig_lstat(p, *a, **kw)  # type: ignore[arg-type]

            def _no_mnt_stat(p: object, *a: object, **kw: object):
                s = os.fsdecode(p)  # type: ignore[arg-type]
                if s == "/mnt" or s.startswith("/mnt/"):
                    raise AssertionError(f"stat called on WSL slow mount: {s}")
                return orig_stat(p, *a, **kw)  # type: ignore[arg-type]

            with mock.patch("agent_voice_input.workspace_ctx.subprocess.run") as mock_run, mock.patch(
                "os.lstat", side_effect=_no_mnt_lstat
            ), mock.patch("os.stat", side_effect=_no_mnt_stat), mock.patch(
                "agent_voice_input.workspace_ctx._HOST_IS_WSL", True
            ):
                # Stripped env ({}) still honors _HOST_IS_WSL; explicit Windows path in JetBrains title never falls back to local repo!
                self.assertTrue(is_wsl_slow_mount_path("/mnt/d/work", env={}))
                self.assertEqual(
                    resolve_ide_workspace_cwd(
                        ForegroundWindow(1, "Code.exe", "a.py — win-proj — Visual Studio Code"), home=home, env=wsl_env
                    ),
                    "",
                )
                self.assertEqual(
                    resolve_ide_workspace_cwd(
                        ForegroundWindow(2, "pycharm64.exe", r"linux-proj – [C:\work\linux-proj] – app.py"),
                        home=home,
                        env=wsl_env,
                    ),
                    "",
                )
                self.assertEqual(
                    resolve_ide_workspace_cwd(
                        ForegroundWindow(3, "pycharm64.exe", r"linux-proj – [\\server\share\linux-proj]"),
                        home=home,
                        env=wsl_env,
                    ),
                    "",
                )
                for bad in ("/mnt/c/Users/demo/win-proj", str(win_link)):
                    self.assertEqual(read_git_status_summary(bad, env=wsl_env), ("", []))
                    self.assertEqual(read_repo_core_files(bad, env=wsl_env), [])
                    self.assertEqual(read_workspace_shallow_files(bad, home=home, env=wsl_env), [])
                self.assertIsNone(resolve_antigravity_ide_session(str(win_link), home=home, env=wsl_env))
                self.assertIsNone(resolve_antigravity_ide_session(str(linux_repo), home=home, env=wsl_env))
                self.assertEqual(mock_run.call_count, 0)

    def test_antigravity_ide_session_resolution_and_incremental_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a, repo_b = home / "repos" / "repo-a", home / "repos" / "repo-b"
            for r in (repo_a, repo_b):
                (r / ".git").mkdir(parents=True)
            (repo_a / "alpha_engine.py").write_text("class AlphaCore: pass\n", encoding="utf-8")

            cli_brain = home / ".gemini" / "antigravity-cli" / "brain"
            ide_brain = home / ".gemini" / "antigravity" / "brain"
            now_ts = 1_760_000_000.0

            # CLI brain, subagent, [agy-run], stale (>6h), and cross-repo sessions MUST be rejected
            _write_agy_brain_session(
                cli_brain, "cli-only-1111",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>cli secret</USER_REQUEST>"}],
                mtime=now_ts - 10, workspace_uris=[repo_a.as_uri()],
            )
            _write_agy_brain_session(
                ide_brain, "subagent-2222",
                [{"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "[Message] timestamp=2026-11-19T00:00:00Z sender=parent <subagent_reminder>"}],
                mtime=now_ts - 5, workspace_uris=[repo_a.as_uri()],
            )
            _write_agy_brain_session(
                ide_brain, "agyrun-3333",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>[agy-run] worker</USER_REQUEST>"}],
                mtime=now_ts - 5, workspace_uris=[repo_a.as_uri()],
            )
            _write_agy_brain_session(
                ide_brain, "stale-4444",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "old"}],
                mtime=now_ts - 25000, workspace_uris=[repo_a.as_uri()],
            )
            _write_agy_brain_session(
                ide_brain, "cross-repo-b-5555",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "check repo b"},
                    {
                        "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "reading files",
                        "tool_calls": [
                            *[{"name": "read_file", "args": {"Path": str(repo_b / f"b{i}.py")}} for i in range(1, 5)],
                            {"name": "read_file", "args": {"Path": str(repo_a / "alpha_engine.py")}},
                        ],
                    },
                ],
                mtime=now_ts - 15,
            )
            # Pasted Working Directory / top-level cwd / SSH remote URI / symlink+'..' combo MUST NOT spoof Repo-A!
            (repo_b / "sub").mkdir()
            os.symlink(repo_b, repo_a / "ext_b_link")
            os.symlink(repo_b / "sub", repo_a / "sub_b_link")
            _write_agy_brain_session(
                ide_brain, "pasted-cwd-veto-5556",
                [
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "cwd": str(repo_a),
                        "content": f"<USER_REQUEST>Command Working Directory: {repo_a}</USER_REQUEST>",
                    },
                ],
                mtime=now_ts - 12,
                workspace_uris=[repo_b.as_uri()],
            )
            _write_agy_brain_session(
                ide_brain, "ssh-remote-veto-5558",
                [
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": f"<USER_REQUEST>hi</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: {repo_a / 'alpha_engine.py'}\n</ADDITIONAL_METADATA>",
                    },
                ],
                mtime=now_ts - 11,
                workspace_uris=["vscode-remote://ssh-remote+prod/home/dev/repo-a"],
            )
            _write_agy_brain_session(
                ide_brain, "dotdot-escape-5557",
                [
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": (
                            f"<USER_REQUEST>hi</USER_REQUEST>\n"
                            f"<ADDITIONAL_METADATA>\nActive Document: {repo_a}/../repo-b/b1.py\n"
                            f"Combo: {repo_a / 'sub_b_link'}/../b1.py\n"
                            f"Other: {repo_a / 'ext_b_link' / 'b2.py'}\n</ADDITIONAL_METADATA>"
                        ),
                    },
                ],
                mtime=now_ts - 10,
            )
            self.assertIsNone(resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=now_ts, wsl=False))

            # Valid Repo-A session matched via L3 tool_calls + L2 <ADDITIONAL_METADATA>
            long_turn = "我们在调试 ZypheronDispatcher 模块的缓存逻辑。" + ("背景填充文本 " * 9000)
            tp_a1 = _write_agy_brain_session(
                ide_brain, "ide-repo-a-6666",
                [
                    {
                        "source": "SYSTEM",
                        "type": "SYSTEM_PROMPT",
                        "content": f"Active Workspaces:\n- {repo_a}\nCommand Working Directory: {repo_a}\n",
                    },
                    {
                        "source": "USER_EXPLICIT", "type": "USER_INPUT",
                        "content": f"<USER_REQUEST>{long_turn}</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: {repo_a / 'alpha_engine.py'}\n</ADDITIONAL_METADATA>",
                    },
                    {
                        "source": "MODEL", "type": "PLANNER_RESPONSE",
                        "content": "好的，已定位 ZypheronDispatcher 与 alpha_engine.py。",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": str(repo_a / "alpha_engine.py")}},
                            {"name": "read_file", "args": {"TargetFile": str(repo_a / "core.py")}},
                            {"name": "read_file", "args": {"Path": str(home / ".gemini" / "SKILL.md")}},
                        ],
                    },
                ],
                mtime=now_ts - 400,
            )
            m1 = resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=now_ts, wsl=False)
            assert m1 is not None
            self.assertEqual((m1.session_id, m1.matched_by), ("ide-repo-a-6666", "ide_brain"))

            # Newer blank 'unknown' session does not suppress matching; parallel same-cwd session picks latest mtime
            tp_blank = _write_agy_brain_session(
                ide_brain, "blank-new-7777",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>你好</USER_REQUEST>"}],
                mtime=now_ts - 30,
            )
            m_with_blank = resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=now_ts, wsl=False)
            assert m_with_blank is not None
            self.assertEqual(m_with_blank.session_id, "ide-repo-a-6666")
            tp_blank.unlink()

            tp_a2 = _write_agy_brain_session(
                ide_brain, "ide-repo-a-8888",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>新窗口任务</USER_REQUEST>"}],
                mtime=now_ts - 200, workspace_uris=[repo_a.as_uri()],
            )
            m_latest = resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=now_ts, wsl=False)
            assert m_latest is not None
            self.assertEqual(m_latest.session_id, "ide-repo-a-8888")
            tp_a2.unlink()

            # 2-turn incremental byte_offset & sealed 50K chunk byte-identity without calling sync_session
            os.utime(tp_a1, (now_ts - 60, now_ts - 60))
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test"}
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc")
            sync_session_calls = [0]
            orig_sync_session = tracker.sync_session

            def _spy_sync_session(*a: object, **kw: object):
                sync_session_calls[0] += 1
                return orig_sync_session(*a, **kw)  # type: ignore[arg-type]

            tracker.sync_session = _spy_sync_session  # type: ignore[method-assign]
            payloads: list[dict[str, object]] = []

            def _transport(req: HttpRequest) -> HttpResponse:
                payloads.append(json.loads(req.body.decode("utf-8")))
                return _json_resp({"choices": [{"message": {"content": "检查 ZypheronDispatcher。"}}]})

            refiner = OpenAICompatibleRefiner(
                cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_transport
            )
            asr = _DummyAsr("检查 zypheron dispatcher", env=env)
            agy_win = ForegroundWindow(
                305, "Antigravity", "alpha_engine.py — repo-a — Antigravity", bundle_id="com.google.antigravity"
            )

            with mock.patch("agent_voice_input.workspace_ctx.time.time", return_value=now_ts):
                r1 = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(agy_win), origin_window=agy_win, audio_source="winmm", dry_run=True, env=env,
                )
            self.assertTrue(r1.ok)
            self.assertEqual(sync_session_calls[0], 0)
            assert r1.session is not None
            self.assertEqual(r1.session.session_key, "agy:ide-repo-a-6666")
            self.assertIn("ZypheronDispatcher", asr.seen_contexts[-1].hotwords)
            offset_1 = r1.session.byte_offset
            self.assertGreater(offset_1, 0)
            chunks_1, _ = build_refiner_request_blocks(
                cfg, "draft1", session_key=r1.session.session_key, tracker=tracker, mode="agent"
            )
            self.assertGreaterEqual(len(chunks_1), 2)
            sealed_chunk_0 = chunks_1[0]
            sys_prompt_1 = payloads[-1]["messages"][0]["content"]  # type: ignore[index]

            with open(tp_a1, "a", encoding="utf-8") as af:
                af.write(
                    json.dumps(
                        {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>再看看 KestrelRouter 的超时设置</USER_REQUEST>"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            os.utime(tp_a1, (now_ts - 10, now_ts - 10))

            with mock.patch("agent_voice_input.workspace_ctx.time.time", return_value=now_ts):
                r2 = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(agy_win), origin_window=agy_win, audio_source="winmm", dry_run=True, env=env,
                )
            self.assertTrue(r2.ok)
            self.assertEqual(sync_session_calls[0], 0)
            assert r2.session is not None
            self.assertGreater(r2.session.byte_offset, offset_1)
            self.assertIn("KestrelRouter", asr.seen_contexts[-1].hotwords)
            chunks_2, _ = build_refiner_request_blocks(
                cfg, "draft2", session_key=r2.session.session_key, tracker=tracker, mode="agent"
            )
            self.assertEqual(chunks_2[0], sealed_chunk_0)
            self.assertEqual(payloads[-1]["messages"][0]["content"], sys_prompt_1)  # type: ignore[index]

            # Switching foreground IDE to a subpackage updates workspace cwd while preserving session turns
            sub_pkg = repo_a / "subpkg"
            sub_pkg.mkdir()
            (sub_pkg / "sub_widget.ts").write_text("export const x = 1;\n", encoding="utf-8")
            snap_sub = tracker.sync_ide_session(
                SessionMatch(
                    cli="agy",
                    session_id="ide-repo-a-6666",
                    jsonl_path=str(tp_a1),
                    cwd=os.path.realpath(sub_pkg),
                    matched_by="ide_brain",
                )
            )
            self.assertEqual(snap_sub.match.cwd, os.path.realpath(sub_pkg))
            _, git_ctx_sub = tracker.get_refiner_context(snap_sub.session_key, workspace_cwd=str(sub_pkg))
            self.assertIn(f"cwd: {os.path.realpath(sub_pkg)}", git_ctx_sub)

    def test_target_app_hint_classification_injection_and_echo_strip(self) -> None:
        for ai_win in (
            ForegroundWindow(1, "ChatGPT", "New chat", bundle_id="com.openai.chat"),
            ForegroundWindow(101, "ChatGPT", "New chat", bundle_id="com.openai.codex"),
            ForegroundWindow(102, "Codex", "Codex", bundle_id="com.openai.codex"),
            ForegroundWindow(103, "ChatGPT.exe", "ChatGPT"),
            ForegroundWindow(104, "Codex.exe", "Codex"),
            ForegroundWindow(105, "Codex.exe", "Refactor auth - Codex"),
            ForegroundWindow(106, "Codex.exe", "New chat — ChatGPT"),
            ForegroundWindow(107, "Codex.app", "Project – Codex"),
            ForegroundWindow(108, "Codex.exe", "Plan—Codex"),
            ForegroundWindow(109, "Codex.exe", "Plan-ChatGPT"),
            ForegroundWindow(110, "Codex.exe", "Run [#42] — ChatGPT"),
            ForegroundWindow(111, "Claude.exe", "Issue [#42] - Claude"),
            ForegroundWindow(112, "Codex.exe", "Review [codex:notes] - Codex"),
            ForegroundWindow(113, "Codex.exe", "Braille ⠋ demo — ChatGPT"),
            ForegroundWindow(114, "Claude.exe", "Asterisk ✳ note - Claude"),
            ForegroundWindow(115, "Codex.exe", "Draft [unclosed - Codex"),
            ForegroundWindow(2, "Claude", "Claude", bundle_id="com.anthropic.claudefordesktop"),
            ForegroundWindow(3, "Claude.exe", "Claude"),
            ForegroundWindow(4, "Claude.exe", "Architecture question - Claude"),
        ):
            self.assertEqual(classify_target_app_hint(ai_win), "ai_chat", msg=f"missed {ai_win}")

        for cli_claude_win in (
            ForegroundWindow(5, "claude.exe", "✳ repo [#abcd1234]"),
            ForegroundWindow(6, "claude.exe", "claude — ~/repos/app"),
            ForegroundWindow(7, "claude.exe", r"C:\Users\demo\.local\bin\claude.exe"),
            ForegroundWindow(8, "claude.exe", ""),
            ForegroundWindow(81, "WindowsTerminal.exe", "codex"),
            ForegroundWindow(82, "WindowsTerminal.exe", "codex [#abcd1234]"),
            ForegroundWindow(83, "WindowsTerminal.exe", "[codex:550e8400-e29b-41d4-a716-446655440000]"),
            ForegroundWindow(84, "Ghostty", "codex — ~/repos/app", bundle_id="com.mitchellh.ghostty"),
            ForegroundWindow(85, "Terminal", "Codex", bundle_id="com.apple.Terminal"),
            ForegroundWindow(86, "codex.exe", "codex [#abcd1234]"),
            ForegroundWindow(87, "codex.exe", "[codex:550e8400-e29b-41d4-a716-446655440000]"),
            ForegroundWindow(88, "codex.exe", "550e8400-e29b-41d4-a716-446655440000"),
            ForegroundWindow(89, "codex.exe", "codex — ~/repos/app"),
            ForegroundWindow(90, "codex.exe", r"C:\Users\demo\.cargo\bin\codex.exe"),
            ForegroundWindow(91, "codex.exe", "⠋ Working - Codex"),
            ForegroundWindow(92, "codex.exe", "repo [codex:abcd1234] - Codex"),
            ForegroundWindow(93, "codex.exe", ""),
            ForegroundWindow(94, "codex.exe", "repo [§abcdef] - Codex"),
            ForegroundWindow(95, "codex.exe", "repo [codex:#abcdef12] - Codex"),
            ForegroundWindow(96, "codex.exe", "- Codex"),
            ForegroundWindow(97, "codex.exe", "mycodex"),
            ForegroundWindow(98, "codex.exe", "⠋ Working"),
            ForegroundWindow(20, "QQMusic.exe", "周杰伦"),
            ForegroundWindow(21, "TelegramUpdater.exe", "Updating"),
            ForegroundWindow(22, "Safari", "GitHub", bundle_id="com.apple.Safari"),
            ForegroundWindow(23, "Notion.exe", "Roadmap"),
        ):
            self.assertEqual(classify_target_app_hint(cli_claude_win), "", msg=f"false positive on {cli_claude_win}")

        for im_win in (
            ForegroundWindow(10, "WeChat", "文件传输助手", bundle_id="com.tencent.xinwechat"),
            ForegroundWindow(11, "Weixin.exe", "群聊"),
            ForegroundWindow(12, "Feishu.exe", "飞书"),
            ForegroundWindow(13, "Slack", "general", bundle_id="com.tinyspeck.slackmacgap"),
            ForegroundWindow(14, "Discord.exe", "#dev"),
            ForegroundWindow(15, "Telegram.exe", "Saved Messages"),
            ForegroundWindow(16, "QQ.exe", "测试群"),
        ):
            self.assertEqual(classify_target_app_hint(im_win), "im_chat", msg=f"missed {im_win}")

        self.assertEqual(_coerce_context(AsrContext(mode="general", target_hint="ai_chat")).target_hint, "ai_chat")
        self.assertEqual(_coerce_context({"mode": "general", "target_hint": "IM_CHAT"}).target_hint, "im_chat")

        self.assertEqual(
            strip_target_hint_echo(f"{TARGET_APP_HINTS['ai_chat']}\n帮我解释一下 KV Cache"),
            "帮我解释一下 KV Cache",
        )
        self.assertEqual(strip_target_hint_echo("<target_app_hint>\n晚上一起吃饭吗\n</target_app_hint>"), "晚上一起吃饭吗")
        self.assertEqual(strip_target_hint_echo("帮我看看这段代码\n</target_app_hint>"), "帮我看看这段代码")
        self.assertEqual(
            strip_target_hint_echo("请看下 <target_app_hint> 标签的位置对不对。"),
            "请看下 <target_app_hint> 标签的位置对不对。",
        )
        self.assertEqual(_clean_out(TARGET_APP_HINTS["im_chat"], fallback="原稿兜底文本"), "原稿兜底文本")
        self.assertEqual(
            _normalize_single_hop_text(f"{TARGET_APP_HINTS['ai_chat']}\n单跳清理结果"), "单跳清理结果"
        )
        t0 = time.perf_counter()
        ws_heavy = "看下 <target_app_hint> 说明" + (" " * 20000) + "结尾正常文本"
        malformed_open = "<target_app_hint" + (" " * 20000) + "x"
        self.assertEqual(strip_target_hint_echo(ws_heavy), ws_heavy)
        self.assertEqual(strip_target_hint_echo(malformed_open), malformed_open)
        self.assertLess(time.perf_counter() - t0, 0.05)

        cfg = Config()
        env = {"OPENAI_API_KEY": "sk-openai-test", "GEMINI_API_KEY": "gm-test", "OPENROUTER_API_KEY": "or-test"}
        creds = resolve_credentials(cfg, env)

        resp_reqs: list[dict[str, object]] = []

        def _resp_transport(req: HttpRequest) -> HttpResponse:
            resp_reqs.append(json.loads(req.body.decode("utf-8")))
            sse = (
                'data: {"type":"response.output_text.delta","delta":"<target_app_hint category=\\"ai_chat\\">echo</target_app_hint>\\n请问 LoRA 微调怎么做？"}\n\n'
                'data: {"type":"response.completed","response":{"usage":{"input_tokens":50,"output_tokens":10}}}\n\n'
            )
            return HttpResponse(200, {"content-type": "text/event-stream"}, sse.encode("utf-8"))

        r_resp = OpenAIResponsesRefiner(cfg, creds, provider_name="openai", env=env, transport=_resp_transport)
        r_resp.refine("请问 lora 微调怎么做", mode="general", target_hint="")
        out_ai = r_resp.refine("请问 lora 微调怎么做", mode="general", target_hint="ai_chat")
        self.assertEqual(out_ai.text, "请问 LoRA 微调怎么做？")
        self.assertEqual(resp_reqs[0]["instructions"], resp_reqs[1]["instructions"])
        user_txt_ai = resp_reqs[1]["input"][-1]["content"][0]["text"]  # type: ignore[index]
        self.assertLess(user_txt_ai.index("<target_app_hint "), user_txt_ai.index("<asr_draft_to_clean>"))
        self.assertLess(user_txt_ai.index("</asr_draft_to_clean>"), user_txt_ai.index("<task_reminder>"))

        gem_reqs: list[dict[str, object]] = []

        def _gem_transport(req: HttpRequest) -> HttpResponse:
            gem_reqs.append(json.loads(req.body.decode("utf-8")))
            return _json_resp({"candidates": [{"content": {"parts": [{"text": "明天见"}]}}]})

        GeminiRefiner(cfg, creds, env=env, transport=_gem_transport).refine("明天见", mode="general", target_hint="im_chat")
        self.assertEqual(gem_reqs[-1]["systemInstruction"]["parts"][0]["text"], REFINER_GENERAL_SYSTEM_PROMPT)  # type: ignore[index]
        self.assertIn('category="im_chat"', gem_reqs[-1]["contents"][0]["parts"][0]["text"])  # type: ignore[index]

        sh_reqs: list[dict[str, object]] = []

        def _sh_gem_transport(req: HttpRequest) -> HttpResponse:
            sh_reqs.append(json.loads(req.body.decode("utf-8")))
            return _json_resp(
                {"candidates": [{"content": {"parts": [{"text": f"{TARGET_APP_HINTS['ai_chat']}\n帮我对比一下 FlashAttention 和 PagedAttention。"}]}}]}
            )

        cfg_sh = Config()
        cfg_sh.asr.provider = "gemini"
        cfg_sh.refiner.provider = "off"
        sh_asr = GeminiAudioProvider(cfg_sh, creds, env=env, transport=_sh_gem_transport)
        chatgpt_win = ForegroundWindow(401, "ChatGPT", "New chat", bundle_id="com.openai.chat")
        with tempfile.TemporaryDirectory() as td:
            tracker = SessionTracker(home_dir=td, runtime_dir=Path(td) / "rt", env=env, proc_root=Path(td) / "np")
            res_sh = run_pipeline_once(
                _voiced_wav(), cfg=cfg_sh, creds=creds, tracker=tracker, asr_provider=sh_asr,
                platform=_DummyPlatform(chatgpt_win), origin_window=chatgpt_win, audio_source="winmm", dry_run=True, env=env,
            )
        self.assertTrue(res_sh.ok)
        self.assertEqual((res_sh.mode, res_sh.final_text), ("general", "帮我对比一下 FlashAttention 和 PagedAttention。"))
        self.assertEqual(sh_reqs[-1]["systemInstruction"]["parts"][0]["text"], SINGLE_HOP_GENERAL_SYSTEM_PROMPT)  # type: ignore[index]
        parts = sh_reqs[-1]["contents"][0]["parts"]  # type: ignore[index]
        self.assertIn('category="ai_chat"', parts[0]["text"])
        self.assertIn("inlineData", parts[1])
        self.assertIn("<task_reminder>", parts[2]["text"])
        for bw in BUILTIN_AGENT_HOTWORDS[:5]:
            self.assertNotIn(bw, parts[0]["text"])

        chat_reqs: list[dict[str, object]] = []

        def _sh_chat_transport(req: HttpRequest) -> HttpResponse:
            chat_reqs.append(json.loads(req.body.decode("utf-8")))
            return _json_resp({"choices": [{"message": {"content": "收到，马上到"}}]})

        cfg_chat = Config()
        cfg_chat.asr.provider = "openrouter"
        cfg_chat.asr.model = "google/gemini-3.5-flash-lite"
        chat_asr = ChatAudioAsrProvider(cfg_chat, creds, provider_name="openrouter", env=env, transport=_sh_chat_transport)
        chat_res = chat_asr.transcribe(_voiced_wav(), AsrContext(mode="general", single_hop_prepared=True, target_hint="im_chat"))
        self.assertEqual(chat_res.text, "收到，马上到")
        u_items = chat_reqs[-1]["messages"][1]["content"]  # type: ignore[index]
        self.assertIn('category="im_chat"', u_items[0]["text"])
        self.assertEqual(u_items[1]["type"], "input_audio")

    def test_daemon_concurrent_stage2_propagates_workspace_cwd_and_target_hint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home, rt = tmp / "home", tmp / "rt"
            rt.mkdir(mode=0o700)
            proj = home / "Documents" / "设计评审文档"
            proj.mkdir(parents=True)
            (proj / "LatencyReport2026.md").write_text("# Latency\n", encoding="utf-8")

            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test"}
            cfg = Config()
            cfg.hotkey.debounce_ms = 0
            cfg.refiner.provider = "openrouter"
            seen_user_msgs: list[str] = []

            def _transport(req: HttpRequest) -> HttpResponse:
                seen_user_msgs.append(str(json.loads(req.body.decode("utf-8"))["messages"][1]["content"]))
                return _json_resp({"choices": [{"message": {"content": "已确认结果"}}]})

            daemon = VoiceInputDaemon(
                cfg,
                env=env,
                runtime_dir=rt,
                platform=_DummyPlatform(),
                asr_provider=_DummyAsr("已确认结果", env=env),
                refiner=OpenAICompatibleRefiner(
                    cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_transport
                ),
                tracker=SessionTracker(home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc"),
                paste_settle_sec=0.0,
            )
            try:
                for idx, win in enumerate(
                    (
                        ForegroundWindow(501, "Cursor", "LatencyReport2026.md — 设计评审文档 — Cursor"),
                        ForegroundWindow(502, "WeChat", "项目群", bundle_id="com.tencent.xinwechat"),
                    ),
                    start=1,
                ):
                    daemon.handle_trigger("toggle", win, silent_hud=True)
                    daemon.handle_trigger("toggle", win, silent_hud=True)
                    deadline = time.monotonic() + 2.5
                    while time.monotonic() < deadline and len(daemon.recent_results) < idx:
                        time.sleep(0.02)
                self.assertEqual(len(seen_user_msgs), 2)
                self.assertIn("LatencyReport2026.md", seen_user_msgs[0])
                self.assertIn('category="im_chat"', seen_user_msgs[1])
            finally:
                daemon.close()

    def test_agy20_desktop_db_title_binding_disambiguation_isolation_and_daemon(self) -> None:
        def _create_agy20_db(db_path: Path, rows: list[tuple[object, ...]]) -> None:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()

        def _write_pbtxt(root: Path, cid: str, mtime: float) -> Path:
            p = root / "annotations" / f"{cid}.pbtxt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("last_user_view_time {}\n", encoding="utf-8")
            os.utime(p, (mtime, mtime))
            return p

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "alpha-app"
            repo_b = home / "repos" / "beta-app"
            repo_a.mkdir(parents=True)
            repo_b.mkdir(parents=True)
            (repo_a / "ZephyrPipeline.py").write_text("class ZephyrEngine: pass\n", encoding="utf-8")
            (repo_b / "OrbitWorker.py").write_text("class OrbitWorker: pass\n", encoding="utf-8")

            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            cli_brain = home / ".gemini" / "antigravity-cli" / "brain"
            db_path = ide_root / "conversation_summaries.db"
            # 2026-11-23T10:00:00Z — newer than the 09:xx DB timestamps below
            now_ts = 1_795_428_000.0

            # 1. Transcripts on disk (AGY 2.0 standalone format: no SYSTEM/subagents L1 evidence!)
            _write_agy_brain_session(
                ide_brain,
                "cid-exact-a",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Refactor QuartzScheduler queue</USER_REQUEST>"},
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Updating QuartzScheduler."},
                ],
                mtime=now_ts - 120,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-old-5d",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Old five day task on OrbitWorker</USER_REQUEST>"},
                ],
                mtime=now_ts - 5 * 86400,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-noworkspace",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Brainstorm NebulaDraft notes</USER_REQUEST>"}],
                mtime=now_ts - 60,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-remote-only",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Check RemoteBoxCluster</USER_REQUEST>"}],
                mtime=now_ts - 50,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-trunc-en",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Long English title session</USER_REQUEST>"}],
                mtime=now_ts - 40,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-trunc-cjk",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>中文长标题会话</USER_REQUEST>"}],
                mtime=now_ts - 35,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-exact-over-prefix",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Exact literal ellipsis title</USER_REQUEST>"}],
                mtime=now_ts - 90,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-short-prefix",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Short prefix trap</USER_REQUEST>"}],
                mtime=now_ts - 30,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-dup-old",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Duplicate older</USER_REQUEST>"}],
                mtime=now_ts - 600,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-dup-active",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Duplicate active via pbtxt</USER_REQUEST>"}],
                mtime=now_ts - 800,
            )
            # Isolation traps: CLI source, avi-refiner, subagent parent, nesting_depth, CLI-only brain, bad cid
            for trap_cid in ("cid-cli-src", "cid-refiner", "cid-child-parent", "cid-nested", "cid-sub-file"):
                _write_agy_brain_session(
                    ide_brain,
                    trap_cid,
                    [
                        {
                            "source": "SYSTEM" if trap_cid == "cid-sub-file" else "USER_EXPLICIT",
                            "type": "SYSTEM_MESSAGE" if trap_cid == "cid-sub-file" else "USER_INPUT",
                            "content": "[Message] sender=parent <subagent_reminder>" if trap_cid == "cid-sub-file" else "trap",
                        }
                    ],
                    mtime=now_ts - 10,
                )
            _write_agy_brain_session(
                cli_brain,
                "cid-only-in-cli-brain",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>CLI brain secret</USER_REQUEST>"}],
                mtime=now_ts - 5,
            )
            # Duplicate title: cid-dup-old has newer DB timestamp, but cid-dup-active has newer annotations/<cid>.pbtxt mtime
            _write_pbtxt(ide_root, "cid-dup-old", now_ts - 500)
            _write_pbtxt(ide_root, "cid-dup-active", now_ts - 5)

            _create_agy20_db(
                db_path,
                [
                    ("cid-exact-a", "Refactor QuartzScheduler", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:00.123456789Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-old-5d", "Audit OrbitWorker Retries", json.dumps([repo_b.as_uri()]), "2026-11-18T08:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-noworkspace", "Brainstorm NebulaDraft", "", "2026-11-23T09:01:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-remote-only", "Remote Prod Debug", json.dumps(["vscode-remote://ssh-remote+prod/home/dev/app", "ssh://prod/srv/app", f"file://{home}"]), "2026-11-23T09:02:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-en", "Investigate Antigravity 2.0 Window Title Binding", json.dumps([repo_a.as_uri()]), "2026-11-23T09:03:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-cjk", "修复语音输入桌面窗口上下文路由问题", json.dumps([repo_a.as_uri()]), "2026-11-23T09:04:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-exact-over-prefix", "Investigate Antigravity 2...", json.dumps([repo_b.as_uri()]), "2026-11-23T08:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-short-prefix", "Fix bug in parser", json.dumps([repo_a.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-dup-old", "Shared Title Session", json.dumps([repo_a.as_uri()]), "2026-11-23T09:10:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-dup-active", "Shared Title Session", json.dumps([repo_a.as_uri()]), "2026-11-23T08:10:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-cli-src", "CLI Source Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CLI", "", 0, ""),
                    ("cid-refiner", "Refiner Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, "avi-refiner"),
                    ("cid-child-parent", "Child Parent Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "cid-exact-a", 0, ""),
                    ("cid-nested", "Nested Depth Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 1, ""),
                    ("cid-sub-file", "Subagent File Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-only-in-cli-brain", "CLI Brain Only Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("../escape-cid", "Path Traversal Trap", json.dumps([repo_a.as_uri()]), "2026-11-23T09:20:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ],
            )

            # 1) Exact match + workspace_uris + multi-window isolation + 5-day-old session still matches
            m_a = resolve_antigravity_ide_session_by_title("  Refactor QuartzScheduler  ", home=home, wsl=False)
            assert m_a is not None
            self.assertEqual((m_a.session_id, m_a.matched_by, m_a.cwd), ("cid-exact-a", "ide_db_title", os.path.realpath(repo_a)))

            m_b = resolve_antigravity_ide_session_by_title("Audit OrbitWorker Retries", home=home, wsl=False)
            assert m_b is not None
            self.assertEqual((m_b.session_id, m_b.cwd), ("cid-old-5d", os.path.realpath(repo_b)))

            # 2) Empty workspace_uris & remote-only/broad URIs -> cwd="" but session still binds
            m_no_ws = resolve_antigravity_ide_session_by_title("Brainstorm NebulaDraft", home=home, wsl=False)
            assert m_no_ws is not None
            self.assertEqual((m_no_ws.session_id, m_no_ws.cwd), ("cid-noworkspace", ""))

            m_rem = resolve_antigravity_ide_session_by_title("Remote Prod Debug", home=home, wsl=False)
            assert m_rem is not None
            self.assertEqual((m_rem.session_id, m_rem.cwd), ("cid-remote-only", ""))

            # 3) Truncated prefix ("..." and Unicode "…"), short prefix rejection, and cross-repo exact+prefix fail-closed
            # "Investigate Antigravity 2..." matches exact cid-exact-over-prefix (repo_b) AND prefix cid-trunc-en (repo_a) -> fail-closed None!
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Investigate Antigravity 2...", home=home, wsl=False)
            )

            m_trunc_en = resolve_antigravity_ide_session_by_title("Investigate Antigravity 2.0 Win...", home=home, wsl=False)
            assert m_trunc_en is not None
            self.assertEqual(m_trunc_en.session_id, "cid-trunc-en")

            m_trunc_uni = resolve_antigravity_ide_session_by_title("Investigate Antigravity 2.0 Win\u2026", home=home, wsl=False)
            assert m_trunc_uni is not None
            self.assertEqual(m_trunc_uni.session_id, "cid-trunc-en")

            m_trunc_cjk = resolve_antigravity_ide_session_by_title("修复语音输入...", home=home, wsl=False)
            assert m_trunc_cjk is not None
            self.assertEqual(m_trunc_cjk.session_id, "cid-trunc-cjk")

            self.assertIsNone(resolve_antigravity_ide_session_by_title("Fix...", home=home, wsl=False))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("修复...", home=home, wsl=False))

            # 4) Duplicate title disambiguated by annotations/<cid>.pbtxt mtime
            m_dup = resolve_antigravity_ide_session_by_title("Shared Title Session", home=home, wsl=False)
            assert m_dup is not None
            self.assertEqual((m_dup.session_id, m_dup.cwd), ("cid-dup-active", os.path.realpath(repo_a)))

            # 5) Strict isolation traps & generic reject titles
            for rejected in (
                "", "   ", "New Conversation", "New Chat", "Antigravity", "Settings", "Welcome",
                "CLI Source Trap", "Refiner Trap", "Child Parent Trap", "Nested Depth Trap",
                "Subagent File Trap", "CLI Brain Only Trap", "Path Traversal Trap",
            ):
                self.assertIsNone(
                    resolve_antigravity_ide_session_by_title(rejected, home=home, wsl=False),
                    msg=f"should reject {rejected!r}",
                )

            # 6) WAL live visibility: uncheckpointed write from open writer is immediately visible via mode=ro
            _write_agy_brain_session(
                ide_brain,
                "cid-live-wal",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Live WAL write</USER_REQUEST>"}],
                mtime=now_ts,
            )
            with contextlib.closing(sqlite3.connect(str(db_path))) as live_w:
                live_w.execute("PRAGMA wal_autocheckpoint=0")
                live_w.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("cid-live-wal", "Live WAL Realtime Title", json.dumps([repo_a.as_uri()]), "2026-11-23T09:59:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                )
                live_w.commit()
                m_wal = resolve_antigravity_ide_session_by_title("Live WAL Realtime Title", home=home, wsl=False)
                assert m_wal is not None
                self.assertEqual(m_wal.session_id, "cid-live-wal")

            # 7) End-to-end daemon pipeline: binds AGY 2.0 session AND populates workspace_cwd for file hotwords/context
            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test"}
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc")
            seen_user_msgs: list[str] = []

            def _transport(req: HttpRequest) -> HttpResponse:
                seen_user_msgs.append(str(json.loads(req.body.decode("utf-8"))["messages"][-1]["content"]))
                return _json_resp({"choices": [{"message": {"content": "检查 QuartzScheduler 和 ZephyrPipeline.py。"}}]})

            refiner = OpenAICompatibleRefiner(
                cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_transport
            )
            asr = _DummyAsr("检查 quartz scheduler 和 zephyr pipeline", env=env)
            agy20_win = ForegroundWindow(
                601, "Antigravity", "Refactor QuartzScheduler", bundle_id="com.google.antigravity"
            )
            res = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr,
                refiner=refiner,
                platform=_DummyPlatform(agy20_win),
                origin_window=agy20_win,
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res.ok)
            assert res.session is not None
            self.assertEqual(res.session.session_key, "agy:cid-exact-a")
            self.assertEqual(res.session.match.matched_by, "ide_db_title")
            self.assertEqual(res.session.match.cwd, os.path.realpath(repo_a))
            self.assertIn("QuartzScheduler", asr.seen_contexts[-1].hotwords)
            self.assertIn("ZephyrPipeline", asr.seen_contexts[-1].hotwords)
            self.assertIn("ZephyrPipeline.py", seen_user_msgs[-1])

            # New Conversation in AGY 2.0 returns no session and does not cross-bind old sessions
            agy20_new = ForegroundWindow(
                602, "Antigravity", "New Conversation", bundle_id="com.google.antigravity"
            )
            res_new = run_pipeline_once(
                _voiced_wav(),
                cfg=cfg,
                tracker=tracker,
                asr_provider=asr,
                refiner=refiner,
                platform=_DummyPlatform(agy20_new),
                origin_window=agy20_new,
                audio_source="winmm",
                dry_run=True,
                env=env,
            )
            self.assertTrue(res_new.ok)
            self.assertIsNone(res_new.session)

            # 8) Minimal schema (missing optional filter cols), symlink DB rejection, corrupt DB graceful degradation,
            #    and legacy VS Code Fork title path compatibility
            ide2_root = home / ".gemini" / "antigravity-ide"
            ide2_brain = ide2_root / "brain"
            _write_agy_brain_session(
                ide2_brain,
                "cid-min-schema",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Minimal schema row</USER_REQUEST>"}],
                mtime=now_ts - 15,
            )
            min_db = ide2_root / "conversation_summaries.db"
            min_db.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(min_db))) as mconn:
                mconn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, workspace_uris TEXT)")
                mconn.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                    ("cid-min-schema", "Minimal Schema Title", json.dumps(["file:///mnt/c/Users/demo/proj", repo_b.as_uri()])),
                )
                mconn.commit()
            m_min = resolve_antigravity_ide_session_by_title("Minimal Schema Title", home=home, wsl=True)
            assert m_min is not None
            self.assertEqual((m_min.session_id, m_min.cwd), ("cid-min-schema", os.path.realpath(repo_b)))
            min_db.unlink()

            # Symlink DB must be ignored
            real_ext_db = tmp / "external.db"
            _create_agy20_db(
                real_ext_db,
                [("cid-min-schema", "Symlink DB Title", "", "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, "")],
            )
            os.symlink(real_ext_db, min_db)
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Symlink DB Title", home=home, wsl=False))
            min_db.unlink()

            db_path.write_bytes(b"not a valid sqlite database")
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Refactor QuartzScheduler", home=home, wsl=False))
            legacy_tp = _write_agy_brain_session(
                ide_brain,
                "cid-legacy-fork",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Legacy Fork Alpha</USER_REQUEST>"}],
                mtime=now_ts - 20,
                workspace_uris=[repo_a.as_uri()],
            )
            self.assertTrue(legacy_tp.is_file())
            legacy_win = ForegroundWindow(
                603, "Antigravity", "ZephyrPipeline.py — alpha-app — Antigravity", bundle_id="com.google.antigravity"
            )
            # Present-but-corrupt DB must fail closed and veto legacy brain/ fallback
            with mock.patch("agent_voice_input.workspace_ctx.time.time", return_value=now_ts):
                res_corrupt = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr,
                    refiner=refiner,
                    platform=_DummyPlatform(legacy_win),
                    origin_window=legacy_win,
                    audio_source="winmm",
                    dry_run=True,
                    env=env,
                )
            self.assertTrue(res_corrupt.ok)
            self.assertIsNone(res_corrupt.session)
            self.assertEqual(res_corrupt.workspace_cwd, "")

            # True legacy fork without conversation_summaries.db still falls back to ide_brain
            db_path.unlink()
            with mock.patch("agent_voice_input.workspace_ctx.time.time", return_value=now_ts):
                res_legacy = run_pipeline_once(
                    _voiced_wav(),
                    cfg=cfg,
                    tracker=tracker,
                    asr_provider=asr,
                    refiner=refiner,
                    platform=_DummyPlatform(legacy_win),
                    origin_window=legacy_win,
                    audio_source="winmm",
                    dry_run=True,
                    env=env,
                )
            self.assertTrue(res_legacy.ok)
            assert res_legacy.session is not None
            self.assertEqual(res_legacy.session.session_key, "agy:cid-legacy-fork")
            self.assertEqual(res_legacy.session.match.matched_by, "ide_brain")
            tracker.close()

    def test_agy20_review_regressions_authoritative_cwd_ranking_sql_pushdown_and_structured_uris(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_b = home / "repos" / "proj-b"
            repo_plain = home / "repos" / "repo"
            repo_dot = home / "repos" / "repo."
            for d in (repo_a, repo_b, repo_plain, repo_dot):
                d.mkdir(parents=True)
            (repo_a / "LocalOnlySentinel.py").write_text("x = 1\n", encoding="utf-8")

            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            # Base timestamp T = 2026-11-23T09:00:00Z
            base_t = 1_795_424_400.0

            def _write_pb(cid: str, mtime: float) -> None:
                p = ide_root / "annotations" / f"{cid}.pbtxt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("viewed {}\n", encoding="utf-8")
                os.utime(p, (mtime, mtime))

            # P1 #3: Project A viewed more recently (ann_mtime = T+10, db_lmt = T+0) MUST beat
            # Project B generating in background (ann_mtime = T-60, db_lmt = T+100)!
            _write_agy_brain_session(
                ide_brain, "cid-view-a",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Foreground A</USER_REQUEST>"}],
                mtime=base_t,
            )
            _write_agy_brain_session(
                ide_brain, "cid-bg-b",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Background B</USER_REQUEST>"}],
                mtime=base_t + 100,
            )
            _write_pb("cid-view-a", base_t + 10)
            _write_pb("cid-bg-b", base_t - 60)

            # P2 #4: >220 newer unrelated rows MUST NOT push out older exact or prefix targets
            _write_agy_brain_session(
                ide_brain, "cid-old-221-exact",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Old 221 exact</USER_REQUEST>"}],
                mtime=base_t - 86400,
            )
            _write_agy_brain_session(
                ide_brain, "cid-old-222-prefix",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Old 222 prefix</USER_REQUEST>"}],
                mtime=base_t - 86401,
            )

            # P2 #5: Trailing-dot directory name preserved & malformed URI skipped without ValueError
            _write_agy_brain_session(
                ide_brain, "cid-dot-dir",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Trailing dot dir</USER_REQUEST>"}],
                mtime=base_t - 10,
            )
            _write_agy_brain_session(
                ide_brain, "cid-bad-uri-skip",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Malformed URI recovery</USER_REQUEST>"}],
                mtime=base_t - 10,
            )

            # P1 #1: Transcript containing SYSTEM cwd must NEVER backfill cwd when ide_db_title gives cwd=""
            tp_empty_cwd = _write_agy_brain_session(
                ide_brain, "cid-auth-empty-cwd",
                [
                    {"source": "SYSTEM", "type": "SYSTEM_PROMPT", "cwd": str(repo_a), "content": f"Working Directory: {repo_a}"},
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Remote task alpha</USER_REQUEST>"},
                ],
                mtime=base_t - 5,
            )
            # Rich prior in repo_a so we can test switching cid from repo_a -> empty cwd clears prior!
            _write_agy_brain_session(
                ide_brain, "cid-prior-repo-a",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Inspect QuartzPriorEngine " + ("x" * 1600)},
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Updated QuartzPriorEngine " + ("y" * 1600)},
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Check QuartzPriorEngine2 " + ("x" * 800)},
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done QuartzPriorEngine2 " + ("y" * 800)},
                ],
                mtime=base_t - 500,
            )

            rows: list[tuple[object, ...]] = [
                ("cid-view-a", "Duplicate Foreground Title", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-bg-b", "Duplicate Foreground Title", json.dumps([repo_b.as_uri()]), "2026-11-23T09:01:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-old-221-exact", "Ancient Exact Session 221", json.dumps([repo_a.as_uri()]), "2025-01-01T00:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-old-222-prefix", "Ancient Truncated Prefix Conversation Beyond 220 Rows", json.dumps([repo_b.as_uri()]), "2025-01-01T00:00:01Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-dot-dir", "Trailing Dot Workspace", json.dumps([repo_dot.as_uri()]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-bad-uri-skip", "Malformed URI Workspace", json.dumps(["file://[invalid/path", repo_a.as_uri()]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-prior-repo-a", "Prior Repo A", json.dumps([repo_a.as_uri()]), "2026-11-22T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
            ]
            # Insert 220 newer unrelated rows
            for i in range(220):
                rows.append((
                    f"cid-noise-{i:03d}",
                    f"Unrelated Noise Conversation {i:03d}",
                    json.dumps([repo_b.as_uri()]),
                    f"2026-11-23T12:{i // 60:02d}:{i % 60:02d}Z",
                    "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                    "",
                    0,
                    "",
                ))

            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                conn.commit()

            # Verify P1-B4a: cross-repo duplicate title (repo_a vs repo_b) fails closed instead of cross-binding
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Duplicate Foreground Title", home=home, wsl=False)
            )

            # Verify P2 #4: SQL pushdown finds row #221 (exact) and #222 (prefix) despite 220 newer unrelated rows
            m_221 = resolve_antigravity_ide_session_by_title("Ancient Exact Session 221", home=home, wsl=False)
            assert m_221 is not None
            self.assertEqual(m_221.session_id, "cid-old-221-exact")
            m_222 = resolve_antigravity_ide_session_by_title("Ancient Truncated Prefix...", home=home, wsl=False)
            assert m_222 is not None
            self.assertEqual(m_222.session_id, "cid-old-222-prefix")

            # Verify P2 #5: Trailing-dot directory preserved (never stripped to 'repo') & bad URI skipped safely
            m_dot = resolve_antigravity_ide_session_by_title("Trailing Dot Workspace", home=home, wsl=False)
            assert m_dot is not None
            self.assertEqual(m_dot.cwd, os.path.realpath(repo_dot))
            self.assertNotEqual(m_dot.cwd, os.path.realpath(repo_plain))

            m_bad_skip = resolve_antigravity_ide_session_by_title("Malformed URI Workspace", home=home, wsl=False)
            assert m_bad_skip is not None
            self.assertEqual(m_bad_skip.cwd, os.path.realpath(repo_a))

            # Verify P1 #1a: First sync with ide_db_title + cwd="" ignores transcript SYSTEM cwd and never scans local git/files
            env = {"HOME": str(home), "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker = SessionTracker(home_dir=home, runtime_dir=tmp / "rt1", env=env, proc_root=tmp / "noproc")
            try:
                with mock.patch.object(tracker, "_get_git_info", wraps=tracker._get_git_info) as spy_git, mock.patch(
                    "agent_voice_input.session_ctx.read_workspace_shallow_files",
                    wraps=read_workspace_shallow_files,
                ) as spy_shallow:
                    snap_empty = tracker.sync_ide_session(
                        SessionMatch(
                            cli="agy",
                            session_id="cid-auth-empty-cwd",
                            jsonl_path=str(tp_empty_cwd),
                            cwd="",
                            matched_by="ide_db_title",
                        )
                    )
                    self.assertEqual(snap_empty.match.cwd, "")
                    st_empty = tracker.get_state(snap_empty.session_key)
                    assert st_empty is not None
                    self.assertEqual(st_empty.cwd, "")
                    self.assertEqual(snap_empty.git_status, "")
                    self.assertNotIn("LocalOnlySentinel", snap_empty.hotwords)
                    self.assertEqual(spy_git.call_count, 0)
                    self.assertEqual(spy_shallow.call_count, 0)
                    prior_blk, _, git_ctx = tracker.get_refiner_context_with_prior(snap_empty.session_key)
                    self.assertEqual(prior_blk, "")
                    self.assertEqual(git_ctx, "")
            finally:
                tracker.close()

            # Verify P1 #1b: Same cid transitioning from local workspace -> empty/remote cwd clears cached cwd & prior refs
            tracker2 = SessionTracker(
                home_dir=home, runtime_dir=tmp / "rt2", env=env, proc_root=tmp / "noproc", cold_start_fallback="auto"
            )
            try:
                snap_local = tracker2.sync_ide_session(
                    SessionMatch(
                        cli="agy",
                        session_id="cid-auth-empty-cwd",
                        jsonl_path=str(tp_empty_cwd),
                        cwd=str(repo_a),
                        matched_by="ide_db_title",
                    )
                )
                self.assertEqual(snap_local.match.cwd, str(repo_a))
                self.assertTrue(snap_local.prior_hotwords_used)
                self.assertIn("QuartzPriorEngine", snap_local.hotwords)
                prior_before, _, git_before = tracker2.get_refiner_context_with_prior(snap_local.session_key)
                self.assertIn("QuartzPriorEngine", prior_before)
                self.assertIn("LocalOnlySentinel.py", git_before)

                with mock.patch.object(tracker2, "_get_git_info", wraps=tracker2._get_git_info) as spy_git2:
                    snap_cleared = tracker2.sync_ide_session(
                        SessionMatch(
                            cli="agy",
                            session_id="cid-auth-empty-cwd",
                            jsonl_path=str(tp_empty_cwd),
                            cwd="",
                            matched_by="ide_db_title",
                        )
                    )
                    self.assertEqual(snap_cleared.match.cwd, "")
                    self.assertFalse(snap_cleared.prior_hotwords_used)
                    self.assertNotIn("QuartzPriorEngine", snap_cleared.hotwords)
                    self.assertNotIn("LocalOnlySentinel", snap_cleared.hotwords)
                    self.assertEqual(spy_git2.call_count, 0)
                    st_after = tracker2.get_state(snap_cleared.session_key)
                    assert st_after is not None
                    self.assertEqual(st_after.cwd, "")
                    self.assertEqual(st_after.cold_start_prior_chunk, "")
                    self.assertEqual(st_after.cold_start_prior_hotwords, ())
                    prior_after, _, git_after = tracker2.get_refiner_context_with_prior(snap_cleared.session_key)
                    self.assertEqual(prior_after, "")
                    self.assertEqual(git_after, "")
            finally:
                tracker2.close()

    def test_agy20_round2_concurrent_empty_cwd_placeholder_51_prefix_fullwidth_and_wsl_uri(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_new_chat = home / "repos" / "New Chat"
            repo_wsl_dot = home / "repos" / "wsl-proj."
            for d in (repo_a, repo_new_chat, repo_wsl_dot):
                d.mkdir(parents=True)
            (repo_new_chat / "NewChatSecret.py").write_text("x = 1\n", encoding="utf-8")
            (repo_new_chat / "main.py").write_text("y = 2\n", encoding="utf-8")

            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0

            def _write_pb(cid: str, mtime: float) -> None:
                p = ide_root / "annotations" / f"{cid}.pbtxt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("viewed {}\n", encoding="utf-8")
                os.utime(p, (mtime, mtime))

            rows: list[tuple[object, ...]] = []
            # 3a) 51 sessions sharing the same display prefix; the OLDEST #51 has newest annotations/<cid>.pbtxt mtime
            for i in range(51):
                cid = f"cid-pfx-{i:02d}"
                _write_agy_brain_session(
                    ide_brain,
                    cid,
                    [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>Prefix item {i}</USER_REQUEST>"}],
                    mtime=base_t + (50 - i),
                )
                # Make i==50 the oldest DB row yet the most recently viewed in annotations/
                lmt_sec = 50 - i
                rows.append((
                    cid,
                    f"Shared Display Prefix Conversation Item {i:02d}",
                    json.dumps([repo_a.as_uri()]),
                    f"2026-11-23T09:00:{lmt_sec:02d}Z",
                    "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                    "",
                    0,
                    "",
                ))
                _write_pb(cid, base_t + (500 if i == 50 else i))

            # 3b) Fullwidth ideographic whitespace around DB title
            _write_agy_brain_session(
                ide_brain,
                "cid-fullwidth",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>全角空格标题</USER_REQUEST>"}],
                mtime=base_t,
            )
            rows.append((
                "cid-fullwidth",
                "\u3000中文标题\u3000",
                json.dumps([repo_a.as_uri()]),
                "2026-11-23T09:10:00Z",
                "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                "",
                0,
                "",
            ))

            # 4) WSL vscode-remote://wsl+Ubuntu/... structured URI (preserving trailing dot; rejecting ssh-remote+)
            _write_agy_brain_session(
                ide_brain,
                "cid-wsl-uri",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>WSL workspace</USER_REQUEST>"}],
                mtime=base_t,
            )
            _write_agy_brain_session(
                ide_brain,
                "cid-ssh-uri",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>SSH workspace</USER_REQUEST>"}],
                mtime=base_t,
            )
            wsl_uri = f"vscode-remote://wsl+Ubuntu{repo_wsl_dot}"
            rows.append((
                "cid-wsl-uri",
                "WSL Structured Workspace",
                json.dumps(["vscode-remote://wsl+Ubuntu/mnt/c/Users/demo/slow", wsl_uri]),
                "2026-11-23T09:11:00Z",
                "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                "",
                0,
                "",
            ))
            rows.append((
                "cid-ssh-uri",
                "SSH Structured Workspace",
                json.dumps(["vscode-remote://ssh-remote+prod/srv/project"]),
                "2026-11-23T09:12:00Z",
                "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                "",
                0,
                "",
            ))

            # 1) Prepare prior & current session for deterministic concurrent empty-cwd writeback race
            _write_agy_brain_session(
                ide_brain,
                "cid-race-prior",
                [
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Inspect RaceGuardEngine " + ("x" * 1600)},
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Updated RaceGuardEngine " + ("y" * 1600)},
                    {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Verify RaceGuardEngine2 " + ("x" * 800)},
                    {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done RaceGuardEngine2 " + ("y" * 800)},
                ],
                mtime=base_t - 500,
            )
            tp_race_cur = _write_agy_brain_session(
                ide_brain,
                "cid-race-cur",
                [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Cold race start</USER_REQUEST>"}],
                mtime=base_t - 10,
            )
            rows.append((
                "cid-race-prior",
                "Race Prior Session",
                json.dumps([repo_a.as_uri()]),
                "2026-11-22T09:00:00Z",
                "CORTEX_TRAJECTORY_SOURCE_CASCADE",
                "",
                0,
                "",
            ))

            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                conn.commit()

            # Verify 3a: 51st (oldest) prefix candidate with newest pbtxt mtime wins (no LIMIT 50 truncation)
            m_51 = resolve_antigravity_ide_session_by_title("Shared Display Prefix...", home=home, wsl=False)
            assert m_51 is not None
            self.assertEqual(m_51.session_id, "cid-pfx-50")

            # Verify 3b: Fullwidth whitespace stripped consistently with Python str.strip()
            m_fw = resolve_antigravity_ide_session_by_title("中文标题", home=home, wsl=False)
            assert m_fw is not None
            self.assertEqual(m_fw.session_id, "cid-fullwidth")

            # Verify 4: WSL vscode-remote://wsl+... resolves on WSL (preserving trailing dot), rejects ssh-remote+ & non-WSL
            env_wsl_ubuntu = {"HOME": str(home), "WSL_DISTRO_NAME": "Ubuntu"}
            m_wsl = resolve_antigravity_ide_session_by_title(
                "WSL Structured Workspace", home=home, wsl=True, env=env_wsl_ubuntu
            )
            assert m_wsl is not None
            self.assertEqual(m_wsl.cwd, os.path.realpath(repo_wsl_dot))
            m_wsl_nonhost = resolve_antigravity_ide_session_by_title(
                "WSL Structured Workspace", home=home, wsl=False, env=env_wsl_ubuntu
            )
            assert m_wsl_nonhost is not None
            self.assertEqual(m_wsl_nonhost.cwd, "")
            m_ssh = resolve_antigravity_ide_session_by_title(
                "SSH Structured Workspace", home=home, wsl=True, env=env_wsl_ubuntu
            )
            assert m_ssh is not None
            self.assertEqual(m_ssh.cwd, "")

            # Verify 1: Deterministic concurrent interleave — switching cid to cwd="" mid-discovery blocks stale prior writeback
            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test", "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            tracker_race = SessionTracker(
                home_dir=home, runtime_dir=tmp / "rt_race", env=env, proc_root=tmp / "noproc", cold_start_fallback="auto"
            )
            try:
                entered_query = threading.Event()
                release_query = threading.Event()
                orig_get_prior = tracker_race._get_prior_candidates_cached

                def _blocking_get_prior(cwd: str, *, scope: str = "cli"):
                    res = orig_get_prior(cwd, scope=scope)
                    entered_query.set()
                    release_query.wait(timeout=5.0)
                    return res

                t_exc: list[BaseException] = []

                def _thread_a() -> None:
                    try:
                        tracker_race.sync_ide_session(
                            SessionMatch(
                                cli="agy",
                                session_id="cid-race-cur",
                                jsonl_path=str(tp_race_cur),
                                cwd=str(repo_a),
                                matched_by="ide_db_title",
                            ),
                            include_git_info=False,
                        )
                    except BaseException as exc:
                        t_exc.append(exc)

                with mock.patch.object(tracker_race, "_get_prior_candidates_cached", side_effect=_blocking_get_prior):
                    th = threading.Thread(target=_thread_a)
                    th.start()
                    self.assertTrue(entered_query.wait(timeout=5.0))
                    # Thread B transitions same cid to authoritative empty cwd="" while Thread A's prior query is in flight
                    tracker_race.sync_ide_session(
                        SessionMatch(
                            cli="agy",
                            session_id="cid-race-cur",
                            jsonl_path=str(tp_race_cur),
                            cwd="",
                            matched_by="ide_db_title",
                        ),
                        include_git_info=False,
                    )
                    release_query.set()
                    th.join(timeout=5.0)
                self.assertEqual(t_exc, [])
                st_race = tracker_race.get_state("agy:cid-race-cur")
                assert st_race is not None
                self.assertEqual(st_race.cwd, "")
                self.assertEqual(st_race.cold_start_prior_chunk, "")
                self.assertEqual(st_race.cold_start_prior_hotwords, ())
            finally:
                tracker_race.close()

            # Verify 2: Bare placeholder "New Chat" (no document) does NOT fall back to ~/repos/New Chat,
            # while multi-segment legacy title & explicit document still bind ~/repos/New Chat!
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            rt_ph = tmp / "rt_ph"
            rt_ph.mkdir(mode=0o700)
            tracker_ph = SessionTracker(home_dir=home, runtime_dir=rt_ph, env=env, proc_root=tmp / "noproc")
            seen_msgs: list[str] = []

            def _transport(req: HttpRequest) -> HttpResponse:
                seen_msgs.append(str(json.loads(req.body.decode("utf-8"))["messages"][-1]["content"]))
                return _json_resp({"choices": [{"message": {"content": "好的。"}}]})

            refiner = OpenAICompatibleRefiner(
                cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_transport
            )
            asr = _DummyAsr("测试文本", env=env)
            try:
                win_bare = ForegroundWindow(701, "Antigravity", "New Chat", bundle_id="com.google.antigravity")
                res_bare = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker_ph, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_bare), origin_window=win_bare, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(res_bare.ok)
                self.assertIsNone(res_bare.session)
                self.assertNotIn("NewChatSecret", asr.seen_contexts[-1].hotwords)
                self.assertNotIn("NewChatSecret.py", seen_msgs[-1])

                win_legacy = ForegroundWindow(
                    702, "Antigravity", "main.py — New Chat — Antigravity", bundle_id="com.google.antigravity"
                )
                res_legacy = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker_ph, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_legacy), origin_window=win_legacy, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(res_legacy.ok)
                self.assertIn("NewChatSecret", asr.seen_contexts[-1].hotwords)
                self.assertIn("NewChatSecret.py", seen_msgs[-1])

                win_with_doc = ForegroundWindow(
                    703, "Antigravity", "New Chat", bundle_id="com.google.antigravity",
                    document=(repo_new_chat / "main.py").as_uri(),
                )
                res_doc = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker_ph, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_with_doc), origin_window=win_with_doc, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(res_doc.ok)
                self.assertIn("NewChatSecret", asr.seen_contexts[-1].hotwords)
                self.assertIn("NewChatSecret.py", seen_msgs[-1])
            finally:
                tracker_ph.close()

    def test_is_codex_desktop_window_and_category_invariants(self) -> None:
        # macOS unified ChatGPT/Codex desktop & Windows ChatGPT.exe / Codex.exe desktop titles
        for desktop_win in (
            ForegroundWindow(1, "ChatGPT", "Refactor auth", bundle_id="com.openai.codex"),
            ForegroundWindow(2, "Codex", "New Chat", bundle_id="com.openai.codex"),
            ForegroundWindow(3, "ChatGPT.exe", "Refactor auth"),
            ForegroundWindow(4, "ChatGPT.exe", "ChatGPT"),
            ForegroundWindow(5, "Codex.exe", "Codex"),
            ForegroundWindow(6, "Codex.exe", "Refactor auth - Codex"),
            ForegroundWindow(7, "Codex.exe", "Plan — ChatGPT"),
            ForegroundWindow(8, "ChatGPT", "Any title"),
        ):
            self.assertTrue(is_codex_desktop_window(desktop_win), msg=f"missed {desktop_win}")
            self.assertEqual(
                classify_window_category(desktop_win, is_terminal=is_terminal_window),
                "general_gui",
                msg=f"category must remain general_gui for {desktop_win}",
            )

        # Terminals, SSH clients, CLI codex.exe, legacy com.openai.chat, and unrelated apps MUST be False
        for non_desktop_win in (
            ForegroundWindow(10, "WindowsTerminal.exe", "codex"),
            ForegroundWindow(11, "WindowsTerminal.exe", "Refactor auth - Codex"),
            ForegroundWindow(12, "Ghostty", "Codex", bundle_id="com.mitchellh.ghostty"),
            ForegroundWindow(13, "Terminal", "ChatGPT", bundle_id="com.apple.Terminal"),
            ForegroundWindow(14, "putty.exe", "Codex"),
            ForegroundWindow(15, "codex.exe", "codex [#abcd1234]"),
            ForegroundWindow(16, "codex.exe", "⠋ Working - Codex"),
            ForegroundWindow(17, "codex.exe", "codex — ~/repos/app"),
            ForegroundWindow(18, "codex.exe", ""),
            ForegroundWindow(19, "ChatGPT", "New chat", bundle_id="com.openai.chat"),
            ForegroundWindow(20, "Cursor", "a.py — repo — Cursor", bundle_id="com.todesktop.230313mzl4w4u92"),
            ForegroundWindow(21, "Chrome.exe", "ChatGPT"),
        ):
            self.assertFalse(is_codex_desktop_window(non_desktop_win), msg=f"false positive for {non_desktop_win}")

        self.assertTrue(is_terminal_window(ForegroundWindow(30, "WindowsTerminal.exe", "zsh")))
        self.assertTrue(is_terminal_window(ForegroundWindow(31, "Ghostty", "zsh", bundle_id="com.mitchellh.ghostty")))
        self.assertTrue(is_terminal_window(ForegroundWindow(32, "putty.exe", "devbox")))
        self.assertFalse(is_terminal_window(ForegroundWindow(33, "ChatGPT", "Chat", bundle_id="com.openai.codex")))

    def test_codex_desktop_session_binding_cloud_veto_promotion_and_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "nebula-core"
            repo_b = home / "repos" / "other-proj"
            repo_dot = home / "repos" / "repo."
            for r in (repo_a, repo_b, repo_dot):
                r.mkdir(parents=True)
            (repo_a / "NebulaEngine.py").write_text("class NebulaEngine: pass\n", encoding="utf-8")
            (repo_b / "OtherTool.py").write_text("x = 1\n", encoding="utf-8")
            (repo_dot / "DotMod.py").write_text("y = 2\n", encoding="utf-8")

            codex_dir = home / ".codex"
            sessions_day = codex_dir / "sessions" / "2026" / "11" / "23"
            sessions_day.mkdir(parents=True)
            sqlite_dir = codex_dir / "sqlite"
            sqlite_dir.mkdir(parents=True)
            codex_db = sqlite_dir / "codex.db"
            state_db = codex_dir / "state_5.sqlite"

            def _write_codex_rollout(
                path: Path,
                sid: str,
                cwd: str,
                turns: list[tuple[str, str]],
                *,
                source: object = "vscode",
                extra_meta: dict[str, object] | None = None,
            ) -> Path:
                path.parent.mkdir(parents=True, exist_ok=True)
                meta_payload: dict[str, object] = {"id": sid, "cwd": cwd, "source": source}
                if extra_meta:
                    meta_payload.update(extra_meta)
                lines = [json.dumps({"type": "session_meta", "payload": meta_payload}, ensure_ascii=False)]
                for role, text in turns:
                    lines.append(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": role,
                                    "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return path

            long_pad = "背景上下文填充数据 " * 8000
            rp_local_a = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-00-00-tid-local-a.jsonl",
                "tid-local-a",
                str(repo_a),
                [
                    ("user", f"请排查 QuartzBatcher 的缓存过期问题。{long_pad}"),
                    ("assistant", "好的，已定位 NebulaEngine.py 中的 QuartzBatcher。"),
                ],
                source="vscode",
            )
            rp_dup_old = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T09-00-00-tid-dup-old.jsonl",
                "tid-dup-old",
                str(repo_a),
                [("user", "old dup"), ("assistant", "old")],
            )
            rp_dup_new = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-05-00-tid-dup-new.jsonl",
                "tid-dup-new",
                str(repo_a),
                [("user", "new dup"), ("assistant", "new")],
            )
            rp_fw = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-06-00-tid-fullwidth.jsonl",
                "tid-fullwidth",
                str(repo_a),
                [("user", "全角标题会话")],
            )
            rp_trunc_en = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-07-00-tid-trunc-en.jsonl",
                "tid-trunc-en",
                str(repo_a),
                [("user", "long en title")],
            )
            rp_trunc_cjk = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-08-00-tid-trunc-cjk.jsonl",
                "tid-trunc-cjk",
                str(repo_a),
                [("user", "中文截断标题")],
            )
            rp_exact_ellip = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-09-00-tid-exact-ellip.jsonl",
                "tid-exact-ellip",
                str(repo_a),
                [("user", "literal ellipsis")],
            )
            rp_dot = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-10-00-tid-dot-dir.jsonl",
                "tid-dot-dir",
                str(repo_dot),
                [("user", "dot dir")],
            )
            rp_cloud_trap = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T08-00-00-tid-cloud-shadow.jsonl",
                "tid-cloud-shadow",
                str(repo_b),
                [("user", "Old local repo task that must NEVER bind for cloud chat")],
            )
            rp_fallback_scan = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-11-00-tid-sessions-scan.jsonl",
                "tid-sessions-scan",
                str(repo_a),
                [("user", "Found via sessions/ fallback scan")],
            )
            rp_archived = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-12-00-tid-archived.jsonl",
                "tid-archived",
                str(repo_a),
                [("user", "Archived thread")],
            )
            # Security trap rollouts
            rp_outside = _write_codex_rollout(
                tmp / "outside-sessions.jsonl",
                "tid-outside",
                str(repo_a),
                [("user", "Outside sessions root")],
            )
            rp_symlink = sessions_day / "rollout-symlink.jsonl"
            os.symlink(rp_local_a, rp_symlink)
            rp_empty = sessions_day / "rollout-empty.jsonl"
            rp_empty.write_bytes(b"")
            rp_subagent_dict = _write_codex_rollout(
                sessions_day / "rollout-sub-dict.jsonl",
                "tid-sub-dict",
                str(repo_a),
                [("user", "Subagent dict")],
                source={"subagent": "review"},
            )
            rp_subagent_str = _write_codex_rollout(
                sessions_day / "rollout-sub-str.jsonl",
                "tid-sub-str",
                str(repo_a),
                [("user", "Subagent str")],
                source="subagent",
            )
            rp_subagent_parent = _write_codex_rollout(
                sessions_day / "rollout-sub-parent.jsonl",
                "tid-sub-parent",
                str(repo_a),
                [("user", "Subagent parent")],
                extra_meta={"parent_thread_id": "tid-local-a"},
            )
            # Rich historical CLI Codex session in repo_a for cold-start prior reuse
            _write_codex_rollout(
                codex_dir / "sessions" / "2026" / "11" / "22" / "rollout-2026-11-22T10-00-00-tid-prior-cli.jsonl",
                "tid-prior-cli",
                str(repo_a),
                [
                    ("user", "Analyze HeliosPriorToken " + ("a" * 1600)),
                    ("assistant", "Fixed HeliosPriorToken " + ("b" * 1600)),
                    ("user", "Verify HeliosPriorToken2 " + ("c" * 800)),
                    ("assistant", "Confirmed HeliosPriorToken2 " + ("d" * 800)),
                ],
                source="cli",
            )
            rp_cold = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T10-15-00-tid-cold-new.jsonl",
                "tid-cold-new",
                str(repo_a),
                [("user", "刚开启的新 Codex 桌面任务")],
                source="vscode",
            )

            with contextlib.closing(sqlite3.connect(str(state_db))) as sconn:
                sconn.execute("PRAGMA journal_mode=WAL")
                sconn.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        cwd TEXT,
                        rollout_path TEXT,
                        originator TEXT,
                        archived INTEGER DEFAULT 0,
                        recency_at_ms INTEGER,
                        updated_at INTEGER
                    )
                    """
                )
                sconn.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("tid-local-a", "Refactor QuartzBatcher", str(repo_a), str(rp_local_a), "Codex Desktop", 0, 1_760_000_100_000, 1_760_000_100),
                        ("tid-dup-old", "Duplicate Local Title", str(repo_a), str(rp_dup_old), "Codex Desktop", 0, 1_760_000_050_000, 1_760_000_050),
                        ("tid-dup-new", "Duplicate Local Title", str(repo_a), str(rp_dup_new), "codex_work_desktop", 0, 1_760_000_200_000, 1_760_000_200),
                        ("tid-fullwidth", "\u3000全角中文任务\u3000", str(repo_a), str(rp_fw), "Codex Desktop", 0, 1_760_000_110_000, 1_760_000_110),
                        ("tid-trunc-en", "Investigate Codex Desktop Session Binding", str(repo_a), str(rp_trunc_en), "Codex Desktop", 0, 1_760_000_120_000, 1_760_000_120),
                        ("tid-trunc-cjk", "修复桌面端会话分流与热词作用域问题", str(repo_a), str(rp_trunc_cjk), "Codex Desktop", 0, 1_760_000_130_000, 1_760_000_130),
                        ("tid-exact-ellip", "Investigate Codex Desktop...", str(repo_a), str(rp_exact_ellip), "Codex Desktop", 0, 1_760_000_090_000, 1_760_000_090),
                        ("tid-dot-dir", "Trailing Dot Repo Task", str(repo_dot), str(rp_dot), "Codex Desktop", 0, 1_760_000_140_000, 1_760_000_140),
                        # Same title as cloud ChatGPT row! Must NEVER be bound when codex.db says cloud!
                        ("tid-cloud-shadow", "Weekend Travel Plan", str(repo_b), str(rp_cloud_trap), "Codex Desktop", 0, 1_760_000_300_000, 1_760_000_300),
                        ("tid-archived", "Archived Task", str(repo_a), str(rp_archived), "Codex Desktop", 1, 1_760_000_400_000, 1_760_000_400),
                        ("tid-cli-only", "CLI Only Thread", str(repo_a), str(rp_local_a), "codex_cli_rs", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-outside", "Outside Rollout Trap", str(repo_a), str(rp_outside), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-symlink", "Symlink Rollout Trap", str(repo_a), str(rp_symlink), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-empty", "Empty Rollout Trap", str(repo_a), str(rp_empty), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-sub-dict", "Subagent Dict Trap", str(repo_a), str(rp_subagent_dict), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-sub-str", "Subagent Str Trap", str(repo_a), str(rp_subagent_str), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-sub-parent", "Subagent Parent Trap", str(repo_a), str(rp_subagent_parent), "Codex Desktop", 0, 1_760_000_150_000, 1_760_000_150),
                        ("tid-cold-new", "Cold Start Desktop Task", str(repo_a), str(rp_cold), "Codex Desktop", 0, 1_760_000_160_000, 1_760_000_160),
                    ],
                )
                sconn.commit()

            with contextlib.closing(sqlite3.connect(str(codex_db))) as cconn:
                cconn.execute("PRAGMA journal_mode=WAL")
                cconn.execute(
                    """
                    CREATE TABLE local_thread_catalog (
                        thread_id TEXT,
                        display_title TEXT,
                        source_kind TEXT,
                        host_id TEXT,
                        cwd TEXT,
                        missing_candidate INTEGER DEFAULT 0,
                        source_recency_at TEXT,
                        updated_at INTEGER
                    )
                    """
                )
                cconn.executemany(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("tid-local-a", "Refactor QuartzBatcher", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_100),
                        # Duplicate local title in SAME repo: tid-dup-new has higher source_recency_at even though tid-dup-old has newer updated_at
                        ("tid-dup-old", "Duplicate Local Title", "codex", "local", str(repo_a), 0, "1760000100000", 1_760_000_999),
                        ("tid-dup-new", "Duplicate Local Title", "codex", "local", str(repo_a), 0, "1760000200000", 1_760_000_100),
                        ("tid-fullwidth", "\u3000全角中文任务\u3000", "codex", "local", str(repo_a), 0, "2026-11-23T10:06:00Z", 1_760_000_110),
                        ("tid-trunc-en", "Investigate Codex Desktop Session Binding", "codex", "local", str(repo_a), 0, "2026-11-23T10:07:00Z", 1_760_000_120),
                        ("tid-trunc-cjk", "修复桌面端会话分流与热词作用域问题", "codex", "local", str(repo_a), 0, "2026-11-23T10:08:00Z", 1_760_000_130),
                        ("tid-exact-ellip", "Investigate Codex Desktop...", "codex", "local", str(repo_a), 0, "2026-11-23T09:00:00Z", 1_760_000_090),
                        ("tid-dot-dir", "Trailing Dot Repo Task", "codex", "local", str(repo_dot), 0, "2026-11-23T10:10:00Z", 1_760_000_140),
                        # Cloud ChatGPT rows (explicit veto even though older local row & state_5.sqlite share title!)
                        (None, "Weekend Travel Plan", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-cloud-shadow", "Weekend Travel Plan", "codex", "local", str(repo_b), 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                        ("tid-cloud-host", "Remote Cloud Host Chat", "codex", "remote:box1", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_200),
                        ("tid-empty-cwd", "Empty CWD Chat", "codex", "local", "", 0, "2026-11-23T11:00:00Z", 1_760_000_200),
                        ("tid-missing-cand", "Missing Candidate Task", "codex", "local", str(repo_a), 1, "2026-11-23T11:00:00Z", 1_760_000_200),
                        # Missing in state_5.sqlite -> resolved by sessions/ fallback scan
                        ("tid-sessions-scan", "Sessions Fallback Scan Task", "codex", "local", str(repo_a), 0, "2026-11-23T10:11:00Z", 1_760_000_145),
                        ("tid-archived", "Archived Task", "codex", "local", str(repo_a), 0, "2026-11-23T10:12:00Z", 1_760_000_146),
                        # Security traps
                        ("tid-outside", "Outside Rollout Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-symlink", "Symlink Rollout Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-empty", "Empty Rollout Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-sub-dict", "Subagent Dict Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-sub-str", "Subagent Str Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-sub-parent", "Subagent Parent Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("../escape-tid", "Traversal Thread ID Trap", "codex", "local", str(repo_a), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-broad-home", "Broad Home CWD Trap", "codex", "local", str(home), 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-broad-root", "Broad Root CWD Trap", "codex", "local", "/", 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-wsl-mnt", "WSL Slow Mount Trap", "codex", "local", "/mnt/c/Users/demo/proj", 0, "2026-11-23T10:15:00Z", 1_760_000_150),
                        ("tid-cold-new", "Cold Start Desktop Task", "codex", "local", str(repo_a), 0, "2026-11-23T10:16:00Z", 1_760_000_160),
                    ],
                )
                cconn.commit()

            # 1) Exact match, duplicate recency ordering, fullwidth strip, truncated prefix, trailing-dot cwd, Windows suffix
            m_local = resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False)
            assert m_local is not None
            self.assertEqual(
                (m_local.cli, m_local.session_id, m_local.matched_by, m_local.cwd, m_local.jsonl_path),
                ("codex", "tid-local-a", "codex_desktop_db", os.path.realpath(repo_a), os.path.realpath(rp_local_a)),
            )

            m_win_suf = resolve_codex_desktop_session_by_title("Refactor QuartzBatcher - Codex", home=home, wsl=False)
            assert m_win_suf is not None
            self.assertEqual(m_win_suf.session_id, "tid-local-a")

            m_dup = resolve_codex_desktop_session_by_title("Duplicate Local Title", home=home, wsl=False)
            assert m_dup is not None
            self.assertEqual((m_dup.session_id, m_dup.cwd), ("tid-dup-new", os.path.realpath(repo_a)))

            m_fw = resolve_codex_desktop_session_by_title("全角中文任务", home=home, wsl=False)
            assert m_fw is not None
            self.assertEqual(m_fw.session_id, "tid-fullwidth")

            m_exact_over_pfx = resolve_codex_desktop_session_by_title("Investigate Codex Desktop...", home=home, wsl=False)
            assert m_exact_over_pfx is not None
            self.assertEqual(m_exact_over_pfx.session_id, "tid-exact-ellip")

            m_trunc_en = resolve_codex_desktop_session_by_title("Investigate Codex Desktop Session...", home=home, wsl=False)
            assert m_trunc_en is not None
            self.assertEqual(m_trunc_en.session_id, "tid-trunc-en")

            m_trunc_uni = resolve_codex_desktop_session_by_title("Investigate Codex Desktop Session\u2026", home=home, wsl=False)
            assert m_trunc_uni is not None
            self.assertEqual(m_trunc_uni.session_id, "tid-trunc-en")

            m_trunc_cjk = resolve_codex_desktop_session_by_title("修复桌面端会话...", home=home, wsl=False)
            assert m_trunc_cjk is not None
            self.assertEqual(m_trunc_cjk.session_id, "tid-trunc-cjk")

            m_dot = resolve_codex_desktop_session_by_title("Trailing Dot Repo Task", home=home, wsl=False)
            assert m_dot is not None
            self.assertEqual(m_dot.cwd, os.path.realpath(repo_dot))

            m_scan = resolve_codex_desktop_session_by_title("Sessions Fallback Scan Task", home=home, wsl=False)
            assert m_scan is not None
            self.assertEqual((m_scan.session_id, m_scan.jsonl_path), ("tid-sessions-scan", os.path.realpath(rp_fallback_scan)))

            # 2) Cloud ChatGPT explicit veto, placeholders, short prefixes, archived, and security traps
            for rejected in (
                "", "   ", "ChatGPT", "Codex", "New Chat", "New Conversation",
                "Settings", "Preferences", "Welcome", "About", "Search",
                "Fix...", "修复...",
                "Weekend Travel Plan", "Remote Cloud Host Chat", "Empty CWD Chat",
                "Missing Candidate Task", "Archived Task",
                "Outside Rollout Trap", "Symlink Rollout Trap", "Empty Rollout Trap",
                "Subagent Dict Trap", "Subagent Str Trap", "Subagent Parent Trap",
                "Traversal Thread ID Trap", "Broad Home CWD Trap", "Broad Root CWD Trap",
            ):
                self.assertIsNone(
                    resolve_codex_desktop_session_by_title(rejected, home=home, wsl=False),
                    msg=f"should reject {rejected!r}",
                )

            # WSL /mnt/ slow mount cwd rejected with zero stat/lstat on /mnt/
            orig_lstat, orig_stat = os.lstat, os.stat

            def _guard_mnt(fn):
                def _wrapped(p: object, *a: object, **kw: object):
                    s = os.fsdecode(p)  # type: ignore[arg-type]
                    if s == "/mnt" or s.startswith("/mnt/"):
                        raise AssertionError(f"touched /mnt/: {s}")
                    return fn(p, *a, **kw)
                return _wrapped

            with mock.patch("os.lstat", side_effect=_guard_mnt(orig_lstat)), mock.patch(
                "os.stat", side_effect=_guard_mnt(orig_stat)
            ):
                self.assertIsNone(
                    resolve_codex_desktop_session_by_title("WSL Slow Mount Trap", home=home, wsl=True)
                )

            # 3) WAL uncheckpointed live visibility via mode=ro
            rp_wal = _write_codex_rollout(
                sessions_day / "rollout-2026-11-23T11-00-00-tid-live-wal.jsonl",
                "tid-live-wal",
                str(repo_a),
                [("user", "WAL live task")],
            )
            with contextlib.closing(sqlite3.connect(str(codex_db))) as live_w:
                live_w.execute("PRAGMA wal_autocheckpoint=0")
                live_w.execute(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("tid-live-wal", "Live WAL Codex Task", "codex", "local", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_800),
                )
                live_w.commit()
                m_wal = resolve_codex_desktop_session_by_title("Live WAL Codex Task", home=home, wsl=False)
                assert m_wal is not None
                self.assertEqual((m_wal.session_id, m_wal.jsonl_path), ("tid-live-wal", os.path.realpath(rp_wal)))

            # 4) End-to-end daemon pipeline: dynamic promotion vs cloud ChatGPT general+ai_chat isolation
            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test", "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            rt = tmp / "rt"
            rt.mkdir(mode=0o700)
            tracker = SessionTracker(
                home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc", cold_start_fallback="auto"
            )
            seen_payloads: list[dict[str, object]] = []

            def _transport(req: HttpRequest) -> HttpResponse:
                seen_payloads.append(json.loads(req.body.decode("utf-8")))
                return _json_resp({"choices": [{"message": {"content": "检查 QuartzBatcher。"}}]})

            refiner = OpenAICompatibleRefiner(
                cfg, resolve_credentials(cfg, env), provider_name="openrouter", env=env, transport=_transport
            )
            asr = _DummyAsr("检查 quartz batcher", env=env)
            try:
                # 4a) Local Codex Desktop task promotes general_gui -> agent + coding + workspace + 50K chunk cache
                win_local = ForegroundWindow(801, "ChatGPT", "Refactor QuartzBatcher", bundle_id="com.openai.codex")
                r1 = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_local), origin_window=win_local, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(r1.ok)
                self.assertEqual(r1.mode, "agent")
                assert r1.session is not None
                self.assertEqual(r1.session.session_key, "codex:tid-local-a")
                self.assertEqual(r1.session.match.matched_by, "codex_desktop_db")
                self.assertEqual(r1.session.match.cwd, os.path.realpath(repo_a))
                ctx1 = asr.seen_contexts[-1]
                self.assertEqual(ctx1.mode, "agent")
                self.assertEqual(ctx1.hotwords_scope, "coding")
                self.assertEqual(ctx1.target_hint, "")
                self.assertIn("QuartzBatcher", ctx1.hotwords)
                self.assertIn("NebulaEngine", ctx1.hotwords)
                msgs1 = seen_payloads[-1]["messages"]  # type: ignore[index]
                self.assertNotIn("<target_app_hint>", str(msgs1[-1]["content"]))
                self.assertIn("NebulaEngine.py", str(msgs1[-1]["content"]))
                offset1 = r1.session.byte_offset
                self.assertGreater(offset1, 0)
                chunks1, _ = build_refiner_request_blocks(
                    cfg, "draft1", session_key=r1.session.session_key, tracker=tracker, mode="agent"
                )
                self.assertGreaterEqual(len(chunks1), 2)

                # Turn 2 incremental append preserves sealed chunk 0
                with open(rp_local_a, "a", encoding="utf-8") as af:
                    af.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "再核对 VortexPool 的并发锁"}],
                                },
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                r2 = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_local), origin_window=win_local, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(r2.ok)
                assert r2.session is not None
                self.assertGreater(r2.session.byte_offset, offset1)
                self.assertIn("VortexPool", asr.seen_contexts[-1].hotwords)
                chunks2, _ = build_refiner_request_blocks(
                    cfg, "draft2", session_key=r2.session.session_key, tracker=tracker, mode="agent"
                )
                self.assertEqual(chunks2[0], chunks1[0])

                # 4b) Cold-start Codex Desktop task reuses same-repo historical Codex CLI session!
                win_cold = ForegroundWindow(802, "ChatGPT", "Cold Start Desktop Task", bundle_id="com.openai.codex")
                r_cold = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_cold), origin_window=win_cold, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(r_cold.ok)
                self.assertEqual(r_cold.mode, "agent")
                assert r_cold.session is not None
                self.assertIn("HeliosPriorToken", asr.seen_contexts[-1].hotwords)
                prior_blk, _, _ = tracker.get_refiner_context_with_prior(r_cold.session.session_key)
                self.assertIn("HeliosPriorToken", prior_blk)

                # 4c) Cloud ChatGPT chat stays general + ai_chat + chat hotwords + zero workspace/session!
                win_cloud = ForegroundWindow(803, "ChatGPT", "Weekend Travel Plan", bundle_id="com.openai.codex")
                r_cloud = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_cloud), origin_window=win_cloud, audio_source="winmm", dry_run=True, env=env,
                )
                self.assertTrue(r_cloud.ok)
                self.assertEqual(r_cloud.mode, "general")
                self.assertIsNone(r_cloud.session)
                ctx_cloud = asr.seen_contexts[-1]
                self.assertEqual(ctx_cloud.mode, "general")
                self.assertEqual(ctx_cloud.hotwords_scope, "chat")
                self.assertEqual(ctx_cloud.target_hint, "ai_chat")
                self.assertNotIn("NebulaEngine", ctx_cloud.hotwords)
                self.assertNotIn("OtherTool", ctx_cloud.hotwords)
                self.assertIn(TARGET_APP_HINTS["ai_chat"], str(seen_payloads[-1]["messages"][-1]["content"]))  # type: ignore[index]

                # 4d) Explicit mode="general" must NOT promote even for a local Codex Desktop task
                r_forced_gen = run_pipeline_once(
                    _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                    platform=_DummyPlatform(win_local), origin_window=win_local, audio_source="winmm", dry_run=True, env=env,
                    mode="general",
                )
                self.assertTrue(r_forced_gen.ok)
                self.assertEqual(r_forced_gen.mode, "general")
                self.assertIsNone(r_forced_gen.session)

                # 4e) Concurrent writeback guard for matched_by="codex_desktop_db"
                entered_q = threading.Event()
                release_q = threading.Event()
                orig_prior_fn = tracker._get_prior_candidates_cached

                def _block_prior(cwd: str, *, scope: str = "cli"):
                    res_p = orig_prior_fn(cwd, scope=scope)
                    entered_q.set()
                    release_q.wait(timeout=5.0)
                    return res_p

                race_errs: list[BaseException] = []

                def _race_worker() -> None:
                    try:
                        tracker.sync_ide_session(
                            SessionMatch(
                                cli="codex",
                                session_id="tid-race-codex",
                                jsonl_path=str(rp_cold),
                                cwd=str(repo_a),
                                matched_by="codex_desktop_db",
                            ),
                            include_git_info=False,
                        )
                    except BaseException as exc:
                        race_errs.append(exc)

                with mock.patch.object(tracker, "_get_prior_candidates_cached", side_effect=_block_prior):
                    th = threading.Thread(target=_race_worker)
                    th.start()
                    self.assertTrue(entered_q.wait(timeout=5.0))
                    tracker.sync_ide_session(
                        SessionMatch(
                            cli="codex",
                            session_id="tid-race-codex",
                            jsonl_path=str(rp_cold),
                            cwd=str(repo_b),
                            matched_by="codex_desktop_db",
                        ),
                        include_git_info=False,
                    )
                    release_q.set()
                    th.join(timeout=5.0)
                self.assertEqual(race_errs, [])
                st_cx_race = tracker.get_state("codex:tid-race-codex")
                assert st_cx_race is not None
                self.assertEqual(st_cx_race.cwd, str(repo_b))
                self.assertEqual(st_cx_race.cold_start_prior_chunk, "")
                self.assertEqual(st_cx_race.cold_start_prior_hotwords, ())
            finally:
                tracker.close()

            # 5) Degradation paths:
            # 5a) Corrupt codex.db -> graceful None (no exception, no fallback to state_5)
            backup_codex_bytes = codex_db.read_bytes()
            for wal_ext in ("-wal", "-shm"):
                p_ext = codex_db.with_name(codex_db.name + wal_ext)
                if p_ext.exists():
                    p_ext.unlink()
            codex_db.write_bytes(b"corrupt sqlite header")
            self.assertIsNone(resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False))

            # 5b) Symlink codex.db -> rejected cleanly
            ext_db = tmp / "ext_codex.db"
            ext_db.write_bytes(backup_codex_bytes)
            codex_db.unlink()
            os.symlink(ext_db, codex_db)
            self.assertIsNone(resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False))

            # 5c) codex.db absent -> falls back to state_5.sqlite (filters archived=1 & originator='codex_cli_rs')
            codex_db.unlink()
            m_fb_desk = resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False)
            assert m_fb_desk is not None
            self.assertEqual((m_fb_desk.session_id, m_fb_desk.cwd), ("tid-local-a", os.path.realpath(repo_a)))
            m_fb_work = resolve_codex_desktop_session_by_title("Duplicate Local Title", home=home, wsl=False)
            assert m_fb_work is not None
            self.assertEqual(m_fb_work.session_id, "tid-dup-new")
            self.assertIsNone(resolve_codex_desktop_session_by_title("Archived Task", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("CLI Only Thread", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Outside Rollout Trap", home=home, wsl=False))

            # 5d) state_5.sqlite missing while codex.db present -> falls back to sessions/ scan by thread_id
            codex_db.write_bytes(backup_codex_bytes)
            for wal_ext in ("", "-wal", "-shm"):
                p_st = state_db.with_name(state_db.name + wal_ext)
                if p_st.exists():
                    p_st.unlink()
            m_no_state = resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False)
            assert m_no_state is not None
            self.assertEqual((m_no_state.session_id, m_no_state.jsonl_path), ("tid-local-a", os.path.realpath(rp_local_a)))

            # 5e) Both codex.db absent and state_5.sqlite corrupt -> graceful None
            codex_db.unlink()
            state_db.write_bytes(b"corrupt state_5")
            self.assertIsNone(resolve_codex_desktop_session_by_title("Refactor QuartzBatcher", home=home, wsl=False))

    def test_codex_desktop_astra_review_regressions(self) -> None:
        """Regression tests for Astra cold review: P1 #1/#2 & P2 #3/#4/#5/#6."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "alpha-repo"
            repo_b = home / "repos" / "beta-repo"
            for r in (repo_a, repo_b):
                r.mkdir(parents=True)
            (repo_a / "AlphaCore.py").write_text("class AlphaCore: pass\n", encoding="utf-8")
            (repo_b / "BetaCore.py").write_text("class BetaCore: pass\n", encoding="utf-8")

            codex_dir = home / ".codex"
            sessions_day = codex_dir / "sessions" / "2026" / "11" / "23"
            sessions_day.mkdir(parents=True)
            sqlite_dir = codex_dir / "sqlite"
            sqlite_dir.mkdir(parents=True)
            codex_db = sqlite_dir / "codex.db"
            state_db = codex_dir / "state_5.sqlite"

            def _mk_rollout(path: Path, sid: str, cwd: str, text: str, *, source: object = "vscode") -> Path:
                path.parent.mkdir(parents=True, exist_ok=True)
                lines = [
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": source}}, ensure_ascii=False),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
                        },
                        ensure_ascii=False,
                    ),
                ]
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return path

            rp_newer_local = _mk_rollout(sessions_day / "rollout-tid-newer-local.jsonl", "tid-newer-local", str(repo_a), "local newer")
            rp_pfx_local = _mk_rollout(sessions_day / "rollout-tid-pfx-local.jsonl", "tid-pfx-local", str(repo_a), "prefix local")
            rp_win_plan = _mk_rollout(sessions_day / "rollout-tid-win-plan.jsonl", "tid-win-plan", str(repo_a), "win plan local")
            rp_xrepo_a = _mk_rollout(sessions_day / "rollout-tid-xrepo-a.jsonl", "tid-xrepo-a", str(repo_a), "xrepo a")
            rp_xrepo_b = _mk_rollout(sessions_day / "rollout-tid-xrepo-b.jsonl", "tid-xrepo-b", str(repo_b), "xrepo b")
            rp_drift_a = _mk_rollout(sessions_day / "rollout-tid-drift-a.jsonl", "tid-drift-a", str(repo_a), "drift thread a")
            rp_drift_b = _mk_rollout(sessions_day / "rollout-tid-drift-b.jsonl", "tid-drift-b", str(repo_b), "drift thread b")
            rp_arch_keep = _mk_rollout(sessions_day / "rollout-tid-arch-keep.jsonl", "tid-arch-keep", str(repo_a), "archived rollout")
            rp_rel_dot = _mk_rollout(sessions_day / "rollout-tid-rel-dot.jsonl", "tid-rel-dot", str(repo_a), "relative dot")
            rp_rel_sub = _mk_rollout(sessions_day / "rollout-tid-rel-sub.jsonl", "tid-rel-sub", str(repo_a), "relative sub")
            rp_file_uri = _mk_rollout(sessions_day / "rollout-tid-file-uri.jsonl", "tid-file-uri", str(repo_a), "file uri cwd")
            rp_lit_build = _mk_rollout(sessions_day / "rollout-tid-lit-build.jsonl", "tid-lit-build", str(repo_a), "literal ellipsis local")

            # P2 #4 invalid meta files
            rp_no_meta = sessions_day / "rollout-tid-no-meta.jsonl"
            rp_no_meta.write_text(
                json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": []}}) + "\n",
                encoding="utf-8",
            )
            rp_bad_json = sessions_day / "rollout-tid-bad-json.jsonl"
            rp_bad_json.write_text("{broken json line\n", encoding="utf-8")
            rp_id_mismatch = _mk_rollout(
                sessions_day / "rollout-tid-id-mismatch.jsonl", "tid-someone-else", str(repo_a), "wrong session id"
            )
            rp_exceeded = _mk_rollout(sessions_day / "rollout-tid-exceeded.jsonl", "tid-exceeded", str(repo_a), "exceeded")

            with contextlib.closing(sqlite3.connect(str(state_db))) as sconn:
                sconn.execute("PRAGMA journal_mode=WAL")
                sconn.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        cwd TEXT,
                        rollout_path TEXT,
                        originator TEXT,
                        archived INTEGER DEFAULT 0,
                        recency_at_ms INTEGER,
                        updated_at INTEGER
                    )
                    """
                )
                sconn.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("tid-newer-local", "Dual Cloud Local Title", str(repo_a), str(rp_newer_local), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                        ("tid-pfx-local", "Shared Cloud Prefix Local Task", str(repo_a), str(rp_pfx_local), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                        ("tid-win-plan", "Plan — ChatGPT", str(repo_a), str(rp_win_plan), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                        ("tid-xrepo-a", "Cross Repo Ambiguous Title", str(repo_a), str(rp_xrepo_a), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                        ("tid-xrepo-b", "Cross Repo Ambiguous Title", str(repo_b), str(rp_xrepo_b), "Codex Desktop", 0, 1_760_000_100_000, 1_760_000_100),
                        ("tid-drift-a", "Optimize AlphaCore Pipeline", str(repo_a), str(rp_drift_a), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-drift-b", "Inspect BetaCore Metrics", str(repo_b), str(rp_drift_b), "Codex Desktop", 0, 1_760_000_400_000, 1_760_000_400),
                        ("tid-arch-keep", "Archived Guard Task", str(repo_a), str(rp_arch_keep), "Codex Desktop", 1, 1_760_000_500_000, 1_760_000_500),
                        ("tid-no-meta", "Missing Session Meta", str(repo_a), str(rp_no_meta), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-bad-json", "Corrupt Session Meta", str(repo_a), str(rp_bad_json), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-id-mismatch", "Mismatched Meta ID", str(repo_a), str(rp_id_mismatch), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-exceeded", "Exceeded Meta Line", str(repo_a), str(rp_exceeded), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-rel-dot", "Relative Dot CWD", ".", str(rp_rel_dot), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-rel-sub", "Relative Sub CWD", "relative/dir", str(rp_rel_sub), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-file-uri", "File URI CWD Task", repo_a.as_uri(), str(rp_file_uri), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-lit-build", "Investigate build...", str(repo_a), str(rp_lit_build), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                    ],
                )
                sconn.commit()

            with contextlib.closing(sqlite3.connect(str(codex_db))) as cconn:
                cconn.execute("PRAGMA journal_mode=WAL")
                cconn.execute(
                    """
                    CREATE TABLE local_thread_catalog (
                        thread_id TEXT,
                        display_title TEXT,
                        source_kind TEXT,
                        host_id TEXT,
                        cwd TEXT,
                        missing_candidate INTEGER DEFAULT 0,
                        source_recency_at TEXT,
                        updated_at INTEGER
                    )
                    """
                )
                cconn.executemany(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        # P1 #1(a): Local row is NEWER than cloud row with the same title -> MUST still veto!
                        ("tid-newer-local", "Dual Cloud Local Title", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-older-cloud", "Dual Cloud Local Title", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                        # P1 #1(b): Truncated prefix hits both newer local and older cloud -> MUST veto!
                        ("tid-pfx-local", "Shared Cloud Prefix Local Task", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-pfx-cloud", "Shared Cloud Prefix Cloud Chat", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                        # P1 #1(c): Windows raw title "Plan — ChatGPT" matches local AND stripped "Plan" matches cloud -> MUST veto!
                        ("tid-win-plan", "Plan — ChatGPT", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-cloud-plan", "Plan", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                        # P1 #1(d): Cross-repo same-title ambiguity -> MUST return None!
                        ("tid-xrepo-a", "Cross Repo Ambiguous Title", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-xrepo-b", "Cross Repo Ambiguous Title", "codex", "local", str(repo_b), 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                        # P1 #2 delivery drift targets
                        ("tid-drift-a", "Optimize AlphaCore Pipeline", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-drift-b", "Inspect BetaCore Metrics", "codex", "local", str(repo_b), 0, "2026-11-23T09:00:00Z", 1_760_000_400),
                        ("tid-cloud-drift", "Casual Cloud Question", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T11:00:00Z", 1_760_000_600),
                        # P2 #3 archived guard target
                        ("tid-arch-keep", "Archived Guard Task", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        # P2 #4 invalid meta targets
                        ("tid-no-meta", "Missing Session Meta", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-bad-json", "Corrupt Session Meta", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-id-mismatch", "Mismatched Meta ID", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-exceeded", "Exceeded Meta Line", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        # P2 #5 relative cwd & file:// URI targets
                        ("tid-rel-dot", "Relative Dot CWD", "codex", "local", ".", 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-rel-sub", "Relative Sub CWD", "codex", "local", "relative/dir", 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        ("tid-file-uri", "File URI CWD Task", "codex", "local", repo_a.as_uri(), 0, "2026-11-23T10:00:00Z", 1_760_000_500),
                        # Merged exact+prefix cloud veto: local literal "Investigate build..." vs cloud "Investigate build failures"
                        ("tid-lit-build", "Investigate build...", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-cloud-build", "Investigate build failures", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T08:00:00Z", 1_760_000_100),
                    ],
                )
                cconn.commit()

            # --- Verify P1 #1(a)(b)(c)(d) + merged exact/prefix cloud veto ---
            self.assertIsNone(resolve_codex_desktop_session_by_title("Dual Cloud Local Title", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Shared Cloud Prefix...", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Plan — ChatGPT", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Cross Repo Ambiguous Title", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Investigate build...", home=home, wsl=False))

            # --- Verify P2 #4 (missing/corrupt/mismatched/exceeded session_meta) ---
            for bad_meta_title in ("Missing Session Meta", "Corrupt Session Meta", "Mismatched Meta ID"):
                self.assertIsNone(
                    resolve_codex_desktop_session_by_title(bad_meta_title, home=home, wsl=False),
                    msg=f"should reject {bad_meta_title}",
                )
            with mock.patch("agent_voice_input.workspace_ctx._read_bounded_line", return_value=(b'{"type":"session_meta"}', True)):
                self.assertIsNone(resolve_codex_desktop_session_by_title("Exceeded Meta Line", home=home, wsl=False))

            # --- Verify P2 #5 (relative cwd rejected; file:// URI accepted) ---
            self.assertIsNone(resolve_codex_desktop_session_by_title("Relative Dot CWD", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Relative Sub CWD", home=home, wsl=False))
            m_uri = resolve_codex_desktop_session_by_title("File URI CWD Task", home=home, wsl=False)
            assert m_uri is not None
            self.assertEqual((m_uri.session_id, m_uri.cwd), ("tid-file-uri", os.path.realpath(repo_a)))

            # --- Verify P1 #2 (same-window Codex Desktop pre-delivery drift check via real MacOSPlatform.deliver_text) ---
            env = {"HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test", "WSL_DISTRO_NAME": "", "WSL_INTEROP": ""}
            cfg = Config()
            cfg.refiner.provider = "openrouter"
            rt = tmp / "rt_drift"
            rt.mkdir(mode=0o700)
            tracker = SessionTracker(home_dir=home, runtime_dir=rt, env=env, proc_root=tmp / "noproc")
            refiner = OpenAICompatibleRefiner(
                cfg,
                resolve_credentials(cfg, env),
                provider_name="openrouter",
                env=env,
                transport=lambda _req: _json_resp({"choices": [{"message": {"content": "优化 AlphaCore 管线。"}}]}),
            )
            asr = _DummyAsr("优化 alpha core 管线", env=env)
            plat = MacOSPlatform(cfg, env=env, shim_cmd_factory=lambda: None)
            try:
                orig_win = ForegroundWindow(901, "ChatGPT", "Optimize AlphaCore Pipeline", bundle_id="com.openai.codex")
                copied_texts: list[str] = []
                pasted_texts: list[str] = []

                def _fake_copy(txt: str) -> bool:
                    copied_texts.append(txt)
                    return True

                def _fake_post_cmd_v(*_a: object, **_kw: object) -> tuple[bool, str, str]:
                    pasted_texts.append("cmd_v")
                    return True, "ok", "ChatGPT"

                # (i) Same hwnd switches to cloud ChatGPT chat -> intercepted, clipboard only, focus_changed
                cloud_fg = ForegroundWindow(901, "ChatGPT", "Casual Cloud Question", bundle_id="com.openai.codex")
                with mock.patch.object(plat, "get_foreground_window", return_value=cloud_fg), mock.patch.object(
                    plat, "_query_paste_foreground_window", return_value=cloud_fg
                ), mock.patch.object(plat, "_copy_to_clipboard", side_effect=_fake_copy), mock.patch.object(
                    plat, "beep"
                ), mock.patch.object(plat, "_post_cmd_v", side_effect=_fake_post_cmd_v):
                    res_cloud_drift = run_pipeline_once(
                        _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                        platform=plat, origin_window=orig_win, audio_source="winmm", dry_run=False, env=env,
                    )
                self.assertTrue(res_cloud_drift.ok)
                assert res_cloud_drift.paste is not None
                self.assertFalse(res_cloud_drift.paste.pasted)
                self.assertTrue(res_cloud_drift.paste.clipboard)
                self.assertEqual(res_cloud_drift.paste.reason, "focus_changed")
                sum_cd = build_pipeline_summary(1, res_cloud_drift)
                self.assertEqual(sum_cd["delivery_status"], "copied")
                self.assertEqual(sum_cd["paste_reason"], "focus_changed")
                self.assertEqual(pasted_texts, [])
                self.assertEqual(len(copied_texts), 1)

                # (ii) Same hwnd switches to ANOTHER local Codex thread -> intercepted, clipboard only, focus_changed
                other_fg = ForegroundWindow(901, "ChatGPT", "Inspect BetaCore Metrics", bundle_id="com.openai.codex")
                with mock.patch.object(plat, "get_foreground_window", return_value=other_fg), mock.patch.object(
                    plat, "_query_paste_foreground_window", return_value=other_fg
                ), mock.patch.object(plat, "_copy_to_clipboard", side_effect=_fake_copy), mock.patch.object(
                    plat, "beep"
                ), mock.patch.object(plat, "_post_cmd_v", side_effect=_fake_post_cmd_v):
                    res_other_drift = run_pipeline_once(
                        _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                        platform=plat, origin_window=orig_win, audio_source="winmm", dry_run=False, env=env,
                    )
                self.assertTrue(res_other_drift.ok)
                assert res_other_drift.paste is not None
                self.assertFalse(res_other_drift.paste.pasted)
                self.assertTrue(res_other_drift.paste.clipboard)
                self.assertEqual(res_other_drift.paste.reason, "focus_changed")
                self.assertEqual(build_pipeline_summary(2, res_other_drift)["delivery_status"], "copied")
                self.assertEqual(pasted_texts, [])

                # (iii) Same hwnd title tweaks slightly (truncated) but still resolves to SAME thread_id -> pastes normally!
                same_tid_fg = ForegroundWindow(901, "ChatGPT", "Optimize AlphaCore...", bundle_id="com.openai.codex")
                with mock.patch.object(plat, "get_foreground_window", return_value=same_tid_fg), mock.patch.object(
                    plat, "_query_paste_foreground_window", return_value=same_tid_fg
                ), mock.patch.object(plat, "_copy_to_clipboard", side_effect=_fake_copy), mock.patch.object(
                    plat, "beep"
                ), mock.patch.object(plat, "_post_cmd_v", side_effect=_fake_post_cmd_v):
                    res_same_tid = run_pipeline_once(
                        _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                        platform=plat, origin_window=orig_win, audio_source="winmm", dry_run=False, env=env,
                    )
                self.assertTrue(res_same_tid.ok)
                assert res_same_tid.paste is not None
                self.assertTrue(res_same_tid.paste.pasted)
                self.assertEqual(build_pipeline_summary(3, res_same_tid)["delivery_status"], "pasted")
                self.assertEqual(len(pasted_texts), 1)

                # (iv) Sampling exception during verification -> conservatively falls back to copied/focus_changed!
                with mock.patch.object(
                    plat, "_query_paste_foreground_window", side_effect=RuntimeError("AX timeout")
                ), mock.patch.object(plat, "_copy_to_clipboard", side_effect=_fake_copy), mock.patch.object(
                    plat, "beep"
                ), mock.patch.object(plat, "_post_cmd_v", side_effect=_fake_post_cmd_v):
                    res_sample_err = run_pipeline_once(
                        _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                        platform=plat, origin_window=orig_win, audio_source="winmm", dry_run=False, env=env,
                    )
                self.assertTrue(res_sample_err.ok)
                assert res_sample_err.paste is not None
                self.assertFalse(res_sample_err.paste.pasted)
                self.assertTrue(res_sample_err.paste.clipboard)
                self.assertEqual(res_sample_err.paste.reason, "focus_changed")
                self.assertEqual(build_pipeline_summary(4, res_sample_err)["delivery_status"], "copied")
                self.assertEqual(len(pasted_texts), 1)

                # (v) Window switches from local Codex thread -> cloud ChatGPT DURING key-release wait -> post-wait check intercepts!
                fg_seq = iter([orig_win, cloud_fg])
                key_seq = iter([True, False])
                with mock.patch.object(
                    plat, "_query_paste_foreground_window", side_effect=lambda: next(fg_seq)
                ), mock.patch.object(plat, "_has_managed_shim", return_value=True), mock.patch.object(
                    plat, "_query_shim_key_state", side_effect=lambda: next(key_seq)
                ), mock.patch.object(plat, "_flush_shim_events"), mock.patch.object(
                    plat, "_shim_supports_paste_clip", return_value=False
                ), mock.patch.object(plat, "_copy_to_clipboard", side_effect=_fake_copy), mock.patch.object(
                    plat, "beep"
                ), mock.patch.object(plat, "_post_cmd_v", side_effect=_fake_post_cmd_v):
                    res_key_wait_drift = run_pipeline_once(
                        _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                        platform=plat, origin_window=orig_win, audio_source="winmm", dry_run=False, env=env,
                    )
                self.assertTrue(res_key_wait_drift.ok)
                assert res_key_wait_drift.paste is not None
                self.assertFalse(res_key_wait_drift.paste.pasted)
                self.assertTrue(res_key_wait_drift.paste.clipboard)
                self.assertEqual(res_key_wait_drift.paste.reason, "focus_changed")
                self.assertEqual(build_pipeline_summary(5, res_key_wait_drift)["delivery_status"], "copied")
                self.assertEqual(len(pasted_texts), 1)

                # (vi) Real WslPlatform.deliver_text: empty ForegroundWindow(hwnd=0, process="", title="")
                # MUST fail closed via _make_codex_desktop_same_window_verifier and send force_clipboard_only=True
                wsl_plat = WslPlatform(cfg, env=env, shim_cmd_factory=lambda: ["true"])
                try:
                    wsl_rpcs: list[dict[str, object]] = []

                    def _fake_wsl_rpc(req: dict[str, object], timeout_sec: float = 5.0) -> dict[str, object]:
                        del timeout_sec
                        wsl_rpcs.append(dict(req))
                        return {"pasted": False, "clipboard": True, "reason": "focus_changed", "fg_process": ""}

                    orig_wsl_win = ForegroundWindow(901, "ChatGPT.exe", "Optimize AlphaCore Pipeline")
                    with mock.patch.object(
                        wsl_plat, "get_foreground_window", return_value=ForegroundWindow(hwnd=0, process="", title="")
                    ), mock.patch.object(wsl_plat, "_dispatch_paste_rpc", side_effect=_fake_wsl_rpc):
                        res_wsl_empty = run_pipeline_once(
                            _voiced_wav(), cfg=cfg, tracker=tracker, asr_provider=asr, refiner=refiner,
                            platform=wsl_plat, origin_window=orig_wsl_win, audio_source="winmm", dry_run=False, env=env,
                        )
                    self.assertTrue(res_wsl_empty.ok)
                    assert res_wsl_empty.paste is not None
                    self.assertFalse(res_wsl_empty.paste.pasted)
                    self.assertTrue(res_wsl_empty.paste.clipboard)
                    self.assertEqual(res_wsl_empty.paste.reason, "focus_changed")
                    self.assertEqual(len(wsl_rpcs), 1)
                    self.assertEqual(wsl_rpcs[0].get("cmd"), "paste")
                    self.assertIs(wsl_rpcs[0].get("force_clipboard_only"), True)
                finally:
                    wsl_plat.close()
            finally:
                plat.close()
                tracker.close()

            # --- Verify P2 #3 (state_5.sqlite present-but-unverifiable MUST NOT bypass archived=1) ---
            state_backup = state_db.read_bytes()
            for ext in ("-wal", "-shm"):
                p_ext = state_db.with_name(state_db.name + ext)
                if p_ext.exists():
                    p_ext.unlink()

            # (3a) Locked state_5.sqlite (OperationalError) -> None
            orig_open_ro = __import__("agent_voice_input.workspace_ctx", fromlist=["_open_readonly_sqlite"])._open_readonly_sqlite

            class _LockedConn:
                def execute(self, *_a: object, **_kw: object):
                    raise sqlite3.OperationalError("database is locked")

                def close(self) -> None:
                    pass

            with mock.patch(
                "agent_voice_input.workspace_ctx._open_readonly_sqlite",
                side_effect=lambda dbp, *, wsl: _LockedConn() if dbp.name == "state_5.sqlite" else orig_open_ro(dbp, wsl=wsl),
            ):
                self.assertIsNone(resolve_codex_desktop_session_by_title("Archived Guard Task", home=home, wsl=False))

            # (3b) Corrupt state_5.sqlite -> None (never falls back to sessions/ scan to resurrect archived=1!)
            state_db.write_bytes(b"corrupt state_5 bytes")
            self.assertIsNone(resolve_codex_desktop_session_by_title("Archived Guard Task", home=home, wsl=False))

            # (3c) state_5.sqlite missing 'archived' column -> None
            state_db.unlink()
            with contextlib.closing(sqlite3.connect(str(state_db))) as no_arch_conn:
                no_arch_conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, cwd TEXT, rollout_path TEXT, originator TEXT)")
                no_arch_conn.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                    ("tid-arch-keep", "Archived Guard Task", str(repo_a), str(rp_arch_keep), "Codex Desktop"),
                )
                no_arch_conn.commit()
            self.assertIsNone(resolve_codex_desktop_session_by_title("Archived Guard Task", home=home, wsl=False))

            # Restore valid state_5.sqlite
            state_db.write_bytes(state_backup)

            # --- Verify P1 #1(e) & missing source_kind fail-closed & P2 #5 on state_5.sqlite fallback ---
            # codex.db exists but locked/corrupt -> MUST NOT fall back to state_5.sqlite
            with mock.patch(
                "agent_voice_input.workspace_ctx._open_readonly_sqlite",
                side_effect=lambda dbp, *, wsl: _LockedConn() if dbp.name == "codex.db" else orig_open_ro(dbp, wsl=wsl),
            ):
                self.assertIsNone(resolve_codex_desktop_session_by_title("Optimize AlphaCore Pipeline", home=home, wsl=False))

            # codex.db missing required 'source_kind' column -> fail-closed None (never falls back to state_5.sqlite!)
            for ext in ("", "-wal", "-shm"):
                p_c = codex_db.with_name(codex_db.name + ext)
                if p_c.exists():
                    p_c.unlink()
            with contextlib.closing(sqlite3.connect(str(codex_db))) as no_sk_conn:
                no_sk_conn.execute(
                    "CREATE TABLE local_thread_catalog (thread_id TEXT, display_title TEXT, host_id TEXT, cwd TEXT)"
                )
                no_sk_conn.execute(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?)",
                    ("tid-drift-a", "Optimize AlphaCore Pipeline", "local", str(repo_a)),
                )
                no_sk_conn.commit()
            self.assertIsNone(resolve_codex_desktop_session_by_title("Optimize AlphaCore Pipeline", home=home, wsl=False))

            # Remove codex.db -> state_5.sqlite fallback also rejects relative cwd "." / "relative/dir" & cross-repo dup
            for ext in ("", "-wal", "-shm"):
                p_c = codex_db.with_name(codex_db.name + ext)
                if p_c.exists():
                    p_c.unlink()
            self.assertIsNone(resolve_codex_desktop_session_by_title("Relative Dot CWD", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Relative Sub CWD", home=home, wsl=False))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Cross Repo Ambiguous Title", home=home, wsl=False))

            # --- Verify P2 #6: concurrent repo A -> repo B cold-start prior read-back isolation ---
            _mk_rollout(
                codex_dir / "sessions" / "2026" / "11" / "21" / "rollout-prior-a.jsonl",
                "tid-prior-a",
                str(repo_a),
                "Inspect AlphaOnlyPriorToken " + ("a" * 3500),
                source="cli",
            )
            # Ensure >= 4 turns for sweet-spot prior in both repo_a and repo_b
            for p_file, sid_p, cwd_p, tok_p in (
                (codex_dir / "sessions" / "2026" / "11" / "21" / "rollout-prior-a.jsonl", "tid-prior-a", str(repo_a), "AlphaOnlyPriorToken"),
                (codex_dir / "sessions" / "2026" / "11" / "21" / "rollout-prior-b.jsonl", "tid-prior-b", str(repo_b), "BetaOnlyPriorToken"),
            ):
                p_file.parent.mkdir(parents=True, exist_ok=True)
                p_lines = [
                    json.dumps({"type": "session_meta", "payload": {"id": sid_p, "cwd": cwd_p, "source": "cli"}}),
                    *[
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": r,
                                    "content": [{"type": "input_text" if r == "user" else "output_text", "text": f"{tok_p} " + ("z" * 1000)}],
                                },
                            }
                        )
                        for r in ("user", "assistant", "user", "assistant")
                    ],
                ]
                p_file.write_text("\n".join(p_lines) + "\n", encoding="utf-8")

            tracker_ab = SessionTracker(
                home_dir=home, runtime_dir=tmp / "rt_ab", env=env, proc_root=tmp / "noproc", cold_start_fallback="auto"
            )
            try:
                a_entered = threading.Event()
                a_release = threading.Event()
                orig_prior = tracker_ab._get_prior_candidates_cached

                def _interleaved_prior(cwd: str, *, scope: str = "cli"):
                    res_p = orig_prior(cwd, scope=scope)
                    if os.path.realpath(cwd) == os.path.realpath(repo_a):
                        a_entered.set()
                        a_release.wait(timeout=5.0)
                    return res_p

                snap_a_box: list[object] = []
                err_box: list[BaseException] = []

                def _thread_a() -> None:
                    try:
                        snap_a_box.append(
                            tracker_ab.sync_ide_session(
                                SessionMatch(
                                    cli="codex",
                                    session_id="tid-ab-race",
                                    jsonl_path=str(rp_drift_a),
                                    cwd=str(repo_a),
                                    matched_by="codex_desktop_db",
                                ),
                                include_git_info=False,
                            )
                        )
                    except BaseException as exc:
                        err_box.append(exc)

                with mock.patch.object(tracker_ab, "_get_prior_candidates_cached", side_effect=_interleaved_prior):
                    th_a = threading.Thread(target=_thread_a)
                    th_a.start()
                    self.assertTrue(a_entered.wait(timeout=5.0))
                    # Thread B switches same session_key to repo_b and populates BetaOnlyPriorToken
                    snap_b = tracker_ab.sync_ide_session(
                        SessionMatch(
                            cli="codex",
                            session_id="tid-ab-race",
                            jsonl_path=str(rp_drift_a),
                            cwd=str(repo_b),
                            matched_by="codex_desktop_db",
                        ),
                        include_git_info=False,
                    )
                    self.assertIn("BetaOnlyPriorToken", snap_b.hotwords)
                    a_release.set()
                    th_a.join(timeout=5.0)

                self.assertEqual(err_box, [])
                self.assertEqual(len(snap_a_box), 1)
                snap_a = snap_a_box[0]
                self.assertEqual(snap_a.match.cwd, str(repo_a))  # type: ignore[attr-defined]
                self.assertNotIn("BetaOnlyPriorToken", snap_a.hotwords)  # type: ignore[attr-defined]
                st_ab = tracker_ab.get_state("codex:tid-ab-race")
                assert st_ab is not None
                self.assertEqual(st_ab.cold_start_prior_cwd, os.path.realpath(repo_b))

                # P2 #6 non-destructive read-back: stale repo_a read returns "" WITHOUT clearing repo_b's cached prior!
                stale_prior_a, _, _ = tracker_ab.get_refiner_context_with_prior(
                    "codex:tid-ab-race", workspace_cwd=str(repo_a)
                )
                self.assertEqual(stale_prior_a, "")
                self.assertFalse(st_ab.cold_start_prior_retired)
                self.assertIn("BetaOnlyPriorToken", st_ab.cold_start_prior_hotwords)
                fresh_prior_b, _, _ = tracker_ab.get_refiner_context_with_prior(
                    "codex:tid-ab-race", workspace_cwd=str(repo_b)
                )
                self.assertIn("BetaOnlyPriorToken", fresh_prior_b)
            finally:
                tracker_ab.close()

    def test_batch1_agy_cross_repo_disambiguation_and_ann_mtime_ranking(self) -> None:
        """P1-B4a/b: AGY 2.0 cross-repo ambiguity fails closed; same-repo ranks by max(ann_mtime, db_lmt, tp_mtime)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_b = home / "repos" / "proj-b"
            for d in (repo_a, repo_b):
                d.mkdir(parents=True)
            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0  # 2026-11-23T09:00:00Z

            def _write_pb(cid: str, mtime: float) -> None:
                p = ide_root / "annotations" / f"{cid}.pbtxt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("viewed {}\n", encoding="utf-8")
                os.utime(p, (mtime, mtime))

            def _mk_sess(cid: str, mtime: float) -> None:
                _write_agy_brain_session(
                    ide_brain,
                    cid,
                    [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>{cid}</USER_REQUEST>"}],
                    mtime=mtime,
                )

            # 1) Cross-repo exact duplicate & truncated prefix collision -> MUST return None
            _mk_sess("cid-xrepo-a", base_t + 100)
            _mk_sess("cid-xrepo-b", base_t + 10)
            _write_pb("cid-xrepo-a", base_t + 500)
            _mk_sess("cid-xpfx-a", base_t + 100)
            _mk_sess("cid-xpfx-b", base_t + 10)
            # 2) Ambiguous local vs empty workspace & remote-only vs remote-only -> MUST return None
            _mk_sess("cid-mix-local", base_t + 100)
            _mk_sess("cid-mix-empty", base_t + 10)
            _mk_sess("cid-rem-1", base_t + 100)
            _mk_sess("cid-rem-2", base_t + 10)
            # 3) Same-repo: old session WITH annotation vs brand-new session WITHOUT annotation -> new wins!
            _mk_sess("cid-same-old-ann", base_t + 10)
            _write_pb("cid-same-old-ann", base_t + 20)
            _mk_sess("cid-same-new-noann", base_t + 500)
            # 4) Same-repo equal db_lmt/tp_mtime: recently viewed annotation wins tiebreak
            _mk_sess("cid-tie-plain", base_t + 300)
            _mk_sess("cid-tie-ann", base_t + 300)
            _write_pb("cid-tie-ann", base_t + 300)
            # 5) Both empty workspace -> allowed to rank by latest real activity
            _mk_sess("cid-empty-old", base_t + 10)
            _write_pb("cid-empty-old", base_t + 20)
            _mk_sess("cid-empty-new", base_t + 400)

            rows = [
                ("cid-xrepo-a", "Cross Repo Exact Dup", json.dumps([repo_a.as_uri()]), "2026-11-23T09:01:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-xrepo-b", "Cross Repo Exact Dup", json.dumps([repo_b.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-xpfx-a", "Shared Prefix Cross Repo Alpha", json.dumps([repo_a.as_uri()]), "2026-11-23T09:01:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-xpfx-b", "Shared Prefix Cross Repo Beta", json.dumps([repo_b.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-mix-local", "Mixed Local Empty Dup", json.dumps([repo_a.as_uri()]), "2026-11-23T09:01:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-mix-empty", "Mixed Local Empty Dup", "", "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-rem-1", "Two Remote Only Dup", json.dumps(["vscode-remote://ssh-remote+box1/srv/a"]), "2026-11-23T09:01:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-rem-2", "Two Remote Only Dup", json.dumps(["vscode-remote://ssh-remote+box2/srv/b"]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-same-old-ann", "Same Repo Old Ann Vs New", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-same-new-noann", "Same Repo Old Ann Vs New", json.dumps([repo_a.as_uri()]), "2026-11-23T09:08:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-tie-plain", "Same Repo Tiebreak", json.dumps([repo_a.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-tie-ann", "Same Repo Tiebreak", json.dumps([repo_a.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-empty-old", "Both Empty Workspace", "", "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ("cid-empty-new", "Both Empty Workspace", "", "2026-11-23T09:06:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
            ]
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                conn.commit()

            env = {"HOME": str(home), "WSL_DISTRO_NAME": ""}
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Cross Repo Exact Dup", home=home, wsl=False, env=env))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Shared Prefix Cross Repo...", home=home, wsl=False, env=env))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Mixed Local Empty Dup", home=home, wsl=False, env=env))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Two Remote Only Dup", home=home, wsl=False, env=env))

            m_new = resolve_antigravity_ide_session_by_title("Same Repo Old Ann Vs New", home=home, wsl=False, env=env)
            assert m_new is not None
            self.assertEqual((m_new.session_id, m_new.cwd), ("cid-same-new-noann", os.path.realpath(repo_a)))

            m_tie = resolve_antigravity_ide_session_by_title("Same Repo Tiebreak", home=home, wsl=False, env=env)
            assert m_tie is not None
            self.assertEqual(m_tie.session_id, "cid-tie-ann")

            m_emp = resolve_antigravity_ide_session_by_title("Both Empty Workspace", home=home, wsl=False, env=env)
            assert m_emp is not None
            self.assertEqual((m_emp.session_id, m_emp.cwd), ("cid-empty-new", ""))

    def test_batch1_agy_prior_db_read_failure_fails_closed(self) -> None:
        """P1-B5: Present-but-unreadable conversation_summaries.db skips legacy brain/ fallback for that root."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_a.mkdir(parents=True)
            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            ide2_root = home / ".gemini" / "antigravity-ide"
            ide2_brain = ide2_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0

            rich_turns = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Inspect VetoLeakToken " + ("a" * 1600)},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Updated VetoLeakToken " + ("b" * 1600)},
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Verify VetoLeakToken2 " + ("c" * 800)},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done VetoLeakToken2 " + ("d" * 800)},
            ]
            _write_agy_brain_session(ide_brain, "cid-legacy-veto", rich_turns, mtime=base_t - 10, workspace_uris=[repo_a.as_uri()])
            _write_agy_brain_session(ide2_brain, "cid-other-root", rich_turns, mtime=base_t - 20, workspace_uris=[repo_a.as_uri()])

            env = {"HOME": str(home), "WSL_DISTRO_NAME": ""}
            # DB absent -> both roots discover via legacy brain/
            cands_no_db = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=False, env=env)
            self.assertEqual({c["session_id"] for c in cands_no_db}, {"cid-legacy-veto", "cid-other-root"})

            # 1) Corrupt DB -> root 1 fails closed (never resurrects cid-legacy-veto), root 2 unaffected
            db_path.write_bytes(b"corrupt sqlite bytes")
            cands_corrupt = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=False, env=env)
            self.assertEqual({c["session_id"] for c in cands_corrupt}, {"cid-other-root"})

            # 2) Missing required conversation_id column -> root 1 fails closed
            db_path.unlink()
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute("CREATE TABLE conversation_summaries (title TEXT)")
                conn.commit()
            cands_bad_schema = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=False, env=env)
            self.assertEqual({c["session_id"] for c in cands_bad_schema}, {"cid-other-root"})

            # 3) Locked DB (OperationalError on query) -> root 1 fails closed
            class _LockedConn:
                def execute(self, *_a: object, **_kw: object):
                    raise sqlite3.OperationalError("database is locked")

                def close(self) -> None:
                    pass

            with mock.patch("agent_voice_input.workspace_ctx._open_agy_ide_db_ro", return_value=_LockedConn()):
                cands_locked = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=False, env=env)
            self.assertEqual({c["session_id"] for c in cands_locked}, {"cid-other-root"})

            # 4) Symlink DB -> root 1 fails closed
            ext_db = tmp / "ext.db"
            ext_db.write_bytes(b"")
            db_path.unlink()
            os.symlink(ext_db, db_path)
            cands_symlink = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=False, env=env)
            self.assertEqual({c["session_id"] for c in cands_symlink}, {"cid-other-root"})

    def test_batch1_wsl_uri_percent2b_distro_check_codex_archived_and_tight_dash(self) -> None:
        """P1-B6a/b (wsl%2B + distro check), Codex archived multi-cand filtering, and P2-bridge-misc (c) tight-dash strip."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_a.mkdir(parents=True)

            # --- Part A: WSL vscode-remote://wsl%2B<distro> decoding & WSL_DISTRO_NAME check ---
            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            agy_db = ide_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0
            rich_turns = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Inspect WslDistroToken " + ("a" * 1600)},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Updated WslDistroToken " + ("b" * 1600)},
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Verify WslDistroToken2 " + ("c" * 800)},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done WslDistroToken2 " + ("d" * 800)},
            ]
            for cid in ("cid-enc-u", "cid-other-d", "cid-empty-d"):
                _write_agy_brain_session(ide_brain, cid, rich_turns, mtime=base_t)

            agy_db.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(agy_db))) as conn:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("cid-enc-u", "Encoded Ubuntu URI", json.dumps([f"vscode-remote://wsl%2BUbuntu{repo_a}"]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-other-d", "Other Distro URI", json.dumps([f"vscode-remote://wsl%2BDebian{repo_a}"]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-empty-d", "Empty Distro URI", json.dumps([f"vscode-remote://wsl%2B{repo_a}"]), "2026-11-23T09:00:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ],
                )
                conn.commit()

            env_ubuntu = {"HOME": str(home), "WSL_DISTRO_NAME": "ubuntu"}
            env_unset = {"HOME": str(home), "WSL_DISTRO_NAME": ""}
            m_enc = resolve_antigravity_ide_session_by_title("Encoded Ubuntu URI", home=home, wsl=True, env=env_ubuntu)
            assert m_enc is not None
            self.assertEqual(m_enc.cwd, os.path.realpath(repo_a))

            m_other_d = resolve_antigravity_ide_session_by_title("Other Distro URI", home=home, wsl=True, env=env_ubuntu)
            assert m_other_d is not None
            self.assertEqual(m_other_d.cwd, "")

            m_empty_d = resolve_antigravity_ide_session_by_title("Empty Distro URI", home=home, wsl=True, env=env_ubuntu)
            assert m_empty_d is not None
            self.assertEqual(m_empty_d.cwd, "")

            # Unset WSL_DISTRO_NAME remains backward-compatible
            m_compat = resolve_antigravity_ide_session_by_title("Encoded Ubuntu URI", home=home, wsl=True, env=env_unset)
            assert m_compat is not None
            self.assertEqual(m_compat.cwd, os.path.realpath(repo_a))

            # Prior scan also enforces distro match
            priors_u = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=True, env=env_ubuntu)
            self.assertEqual([c["session_id"] for c in priors_u], ["cid-enc-u"])

            # --- Part B: Codex same-repo duplicate archived filtering & WSL distro URI & tight-dash suffix ---
            codex_dir = home / ".codex"
            sessions_day = codex_dir / "sessions" / "2026" / "11" / "23"
            sessions_day.mkdir(parents=True)
            sqlite_dir = codex_dir / "sqlite"
            sqlite_dir.mkdir(parents=True)
            codex_db = sqlite_dir / "codex.db"
            state_db = codex_dir / "state_5.sqlite"

            def _mk_rollout(sid: str) -> Path:
                p = sessions_day / f"rollout-{sid}.jsonl"
                p.write_text(
                    json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(repo_a), "source": "vscode"}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": sid}]}}) + "\n",
                    encoding="utf-8",
                )
                return p

            rp_arch_new = _mk_rollout("tid-arch-new")
            rp_act_mid = _mk_rollout("tid-act-mid")
            rp_act_old = _mk_rollout("tid-act-old")
            rp_all_arch1 = _mk_rollout("tid-all-arch-1")
            rp_all_arch2 = _mk_rollout("tid-all-arch-2")
            rp_cloud_act = _mk_rollout("tid-cloud-act")
            rp_cloud_arch = _mk_rollout("tid-cloud-arch")
            rp_wsl_ok = _mk_rollout("tid-wsl-ok")
            rp_wsl_bad = _mk_rollout("tid-wsl-bad")
            rp_tight = _mk_rollout("tid-tight-dash")

            with contextlib.closing(sqlite3.connect(str(state_db))) as sconn:
                sconn.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        name TEXT,
                        cwd TEXT,
                        rollout_path TEXT,
                        originator TEXT,
                        archived INTEGER DEFAULT 0,
                        recency_at_ms INTEGER,
                        updated_at INTEGER
                    )
                    """
                )
                sconn.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("tid-arch-new", "Dup With Archived", str(repo_a), str(rp_arch_new), "Codex Desktop", 1, 1_760_000_900_000, 1_760_000_900),
                        ("tid-act-mid", "Dup With Archived", str(repo_a), str(rp_act_mid), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-act-old", "Dup With Archived", str(repo_a), str(rp_act_old), "Codex Desktop", 0, 1_760_000_100_000, 1_760_000_100),
                        ("tid-all-arch-1", "All Archived Dup", str(repo_a), str(rp_all_arch1), "Codex Desktop", 1, 1_760_000_900_000, 1_760_000_900),
                        ("tid-all-arch-2", "All Archived Dup", str(repo_a), str(rp_all_arch2), "Codex Desktop", 1, 1_760_000_500_000, 1_760_000_500),
                        ("tid-cloud-act", "Cloud Veto With Arch", str(repo_a), str(rp_cloud_act), "Codex Desktop", 0, 1_760_000_900_000, 1_760_000_900),
                        ("tid-cloud-arch", "Cloud Veto With Arch", str(repo_a), str(rp_cloud_arch), "Codex Desktop", 1, 1_760_000_500_000, 1_760_000_500),
                        ("tid-wsl-ok", "Codex WSL Good Distro", f"vscode-remote://wsl%2BUbuntu{repo_a}", str(rp_wsl_ok), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-wsl-bad", "Codex WSL Bad Distro", f"vscode-remote://wsl%2BDebian{repo_a}", str(rp_wsl_bad), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                        ("tid-tight-dash", "Fix Parser", str(repo_a), str(rp_tight), "Codex Desktop", 0, 1_760_000_500_000, 1_760_000_500),
                    ],
                )
                sconn.commit()

            with contextlib.closing(sqlite3.connect(str(codex_db))) as cconn:
                cconn.execute(
                    """
                    CREATE TABLE local_thread_catalog (
                        thread_id TEXT,
                        display_title TEXT,
                        source_kind TEXT,
                        host_id TEXT,
                        cwd TEXT,
                        missing_candidate INTEGER DEFAULT 0,
                        source_recency_at TEXT,
                        updated_at INTEGER
                    )
                    """
                )
                cconn.executemany(
                    "INSERT INTO local_thread_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("tid-arch-new", "Dup With Archived", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-act-mid", "Dup With Archived", "codex", "local", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                        ("tid-act-old", "Dup With Archived", "codex", "local", str(repo_a), 0, "2026-11-23T10:00:00Z", 1_760_000_100),
                        ("tid-all-arch-1", "All Archived Dup", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-all-arch-2", "All Archived Dup", "codex", "local", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                        ("tid-cloud-act", "Cloud Veto With Arch", "codex", "local", str(repo_a), 0, "2026-11-23T12:00:00Z", 1_760_000_900),
                        ("tid-cloud-arch", "Cloud Veto With Arch", "codex", "local", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                        ("tid-cloud-row", "Cloud Veto With Arch", "chatgpt", "chatgpt:u1", None, 0, "2026-11-23T08:00:00Z", 1_760_000_050),
                        ("tid-wsl-ok", "Codex WSL Good Distro", "codex", "local", f"vscode-remote://wsl%2BUbuntu{repo_a}", 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                        ("tid-wsl-bad", "Codex WSL Bad Distro", "codex", "local", f"vscode-remote://wsl%2BDebian{repo_a}", 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                        ("tid-tight-dash", "Fix Parser", "codex", "local", str(repo_a), 0, "2026-11-23T11:00:00Z", 1_760_000_500),
                    ],
                )
                cconn.commit()

            # Same-repo duplicate with newer archived=1 -> skips archived and picks active tid-act-mid!
            m_act = resolve_codex_desktop_session_by_title("Dup With Archived", home=home, wsl=False, env=env_unset)
            assert m_act is not None
            self.assertEqual(m_act.session_id, "tid-act-mid")

            # All archived -> None
            self.assertIsNone(resolve_codex_desktop_session_by_title("All Archived Dup", home=home, wsl=False, env=env_unset))

            # Cloud veto still blocks even with archived siblings
            self.assertIsNone(resolve_codex_desktop_session_by_title("Cloud Veto With Arch", home=home, wsl=False, env=env_unset))

            # Codex WSL %2B + distro match
            m_cx_wsl = resolve_codex_desktop_session_by_title("Codex WSL Good Distro", home=home, wsl=True, env=env_ubuntu)
            assert m_cx_wsl is not None
            self.assertEqual((m_cx_wsl.session_id, m_cx_wsl.cwd), ("tid-wsl-ok", os.path.realpath(repo_a)))
            self.assertIsNone(resolve_codex_desktop_session_by_title("Codex WSL Bad Distro", home=home, wsl=True, env=env_ubuntu))

            # Tight-dash Windows Codex title stripping ("Fix Parser-Codex", "Fix Parser—Codex", "Fix Parser–Codex", "Fix Parser-ChatGPT")
            for tight_title in ("Fix Parser-Codex", "Fix Parser\u2014Codex", "Fix Parser\u2013Codex", "Fix Parser-ChatGPT"):
                self.assertTrue(is_codex_desktop_window(ForegroundWindow(1, "Codex.exe", tight_title)))
                m_td = resolve_codex_desktop_session_by_title(tight_title, home=home, wsl=False, env=env_unset)
                assert m_td is not None, f"failed to strip tight-dash title {tight_title!r}"
                self.assertEqual(m_td.session_id, "tid-tight-dash")

            # IDE parser must NOT strip hyphenated folder names like "claude-code"
            self.assertEqual(
                parse_ide_window_title(ForegroundWindow(2, "Code.exe", "app.py — claude-code — Visual Studio Code")),
                IdeTitleInfo(root_name="claude-code"),
            )

    def test_r2_agy_title_no_transcript_multiroots_db_failclosed_legacy_wsl_uri_and_git_env(self) -> None:
        """R2 polish regressions: AGY full-row veto/multi-root set/DB fail-closed, legacy wsl%2B URI, and git env isolation."""
        from agent_voice_input.workspace_ctx import (
            _AGY_REMOTE_URI_SENTINEL,
            _decode_agy_uri_or_path,
            _query_agy_ide_db_title_rows,
        )

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            repo_a = home / "repos" / "proj-a"
            repo_b = home / "repos" / "proj-b"
            repo_c = home / "repos" / "proj-c"
            for d in (repo_a, repo_b, repo_c):
                d.mkdir(parents=True)
            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            ide2_root = home / ".gemini" / "antigravity-ide"
            ide2_brain = ide2_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            db2_path = ide2_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0

            def _mk_sess(broot: Path, cid: str, mtime: float, **kw: object) -> Path:
                return _write_agy_brain_session(
                    broot,
                    cid,
                    [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>{cid}</USER_REQUEST>"}],
                    mtime=mtime,
                    **kw,  # type: ignore[arg-type]
                )

            def _write_db(path: Path, rows: list[tuple[object, ...]]) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.closing(sqlite3.connect(str(path))) as conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS conversation_summaries (
                            conversation_id TEXT PRIMARY KEY,
                            title TEXT,
                            workspace_uris TEXT,
                            last_modified_time TEXT,
                            source TEXT,
                            parent_conversation_id TEXT,
                            nesting_depth INTEGER,
                            agent_name TEXT
                        )
                        """
                    )
                    conn.execute("DELETE FROM conversation_summaries")
                    conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    conn.commit()

            # --- Item 1: No-transcript cross-repo veto, multi-root workspace full set, and truncated exact+prefix ---
            _mk_sess(ide_brain, "cid-xrepo-has-tp", base_t + 10)
            # cid-xrepo-no-tp intentionally has NO transcript file on disk yet
            _mk_sess(ide_brain, "cid-same-has-tp", base_t + 10)
            # cid-same-no-tp intentionally has NO transcript file on disk yet (same repo_a)
            _mk_sess(ide_brain, "cid-multi-ab", base_t + 10)
            _mk_sess(ide_brain, "cid-multi-ac", base_t + 20)
            _mk_sess(ide_brain, "cid-multi-same-1", base_t + 10)
            _mk_sess(ide_brain, "cid-multi-same-2", base_t + 300)
            _mk_sess(ide_brain, "cid-trunc-exact-a", base_t + 10)
            _mk_sess(ide_brain, "cid-trunc-pfx-b", base_t + 200)
            _mk_sess(ide_brain, "cid-trunc-same-exact", base_t + 10)
            _mk_sess(ide_brain, "cid-trunc-same-pfx", base_t + 400)
            _mk_sess(ide_brain, "cid-multi-unres-local", base_t + 10)
            _mk_sess(ide_brain, "cid-multi-unres-remote", base_t + 20)

            _write_db(
                db_path,
                [
                    ("cid-xrepo-has-tp", "New Session Cross Repo Veto", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-xrepo-no-tp", "New Session Cross Repo Veto", json.dumps([repo_b.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-same-has-tp", "New Session Same Repo OK", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-same-no-tp", "New Session Same Repo OK", json.dumps([repo_a.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-ab", "Multi Root Diff Set", json.dumps([repo_a.as_uri(), repo_b.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-ac", "Multi Root Diff Set", json.dumps([repo_a.as_uri(), repo_c.as_uri()]), "2026-11-23T09:00:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-same-1", "Multi Root Same Set", json.dumps([repo_a.as_uri(), repo_b.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-same-2", "Multi Root Same Set", json.dumps([repo_b.as_uri(), repo_a.as_uri()]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-exact-a", "Deploy Cluster Alpha...", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-pfx-b", "Deploy Cluster Alpha Production", json.dumps([repo_b.as_uri()]), "2026-11-23T09:03:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-same-exact", "Build Pipeline Beta\u2026", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-trunc-same-pfx", "Build Pipeline Beta Release", json.dumps([repo_a.as_uri()]), "2026-11-23T09:06:40Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-unres-local", "Unresolved URI Multi", json.dumps([repo_a.as_uri()]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ("cid-multi-unres-remote", "Unresolved URI Multi", json.dumps(["vscode-remote://ssh-remote+prod/srv/x"]), "2026-11-23T09:00:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                ],
            )
            env_local = {"HOME": str(home), "WSL_DISTRO_NAME": ""}
            # No-transcript row in repo_b vetoes cross-repo match on repo_a!
            self.assertIsNone(resolve_antigravity_ide_session_by_title("New Session Cross Repo Veto", home=home, wsl=False, env=env_local))
            # No-transcript row in SAME repo_a converges and selects the valid-transcript candidate
            m_same_nt = resolve_antigravity_ide_session_by_title("New Session Same Repo OK", home=home, wsl=False, env=env_local)
            assert m_same_nt is not None
            self.assertEqual((m_same_nt.session_id, m_same_nt.cwd), ("cid-same-has-tp", os.path.realpath(repo_a)))
            # Multi-root workspace: [A, B] vs [A, C] fails closed even though primary ws is A for both
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Multi Root Diff Set", home=home, wsl=False, env=env_local))
            # Multi-root workspace: [A, B] vs [B, A] has identical frozenset -> picks latest activity
            m_multi_same = resolve_antigravity_ide_session_by_title("Multi Root Same Set", home=home, wsl=False, env=env_local)
            assert m_multi_same is not None
            self.assertEqual(m_multi_same.session_id, "cid-multi-same-2")
            # Truncated title ("..." / "…"): cross-repo exact vs prefix fails closed; same-repo converges and picks latest
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Deploy Cluster Alpha...", home=home, wsl=False, env=env_local))
            m_trunc_same = resolve_antigravity_ide_session_by_title("Build Pipeline Beta\u2026", home=home, wsl=False, env=env_local)
            assert m_trunc_same is not None
            self.assertEqual(m_trunc_same.session_id, "cid-trunc-same-pfx")
            # Unresolved raw URI in multi-row pool fails closed
            self.assertIsNone(resolve_antigravity_ide_session_by_title("Unresolved URI Multi", home=home, wsl=False, env=env_local))

            # --- Item 2: _query_agy_ide_db_title_rows (rows, db_ok) & dual-root fail-closed ---
            ide2_root.mkdir(parents=True, exist_ok=True)
            self.assertEqual(_query_agy_ide_db_title_rows(ide2_root, wsl=False), ([], True))
            # Present-but-corrupt second root DB fails closed even when first root DB matches!
            db2_path.write_bytes(b"corrupt secondary agy db")
            self.assertEqual(_query_agy_ide_db_title_rows(ide2_root, wsl=False), ([], False))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("New Session Same Repo OK", home=home, wsl=False, env=env_local))
            # Missing required column in second root DB -> fail closed
            db2_path.unlink()
            with contextlib.closing(sqlite3.connect(str(db2_path))) as c_bad:
                c_bad.execute("CREATE TABLE conversation_summaries (conversation_id TEXT)")
                c_bad.commit()
            self.assertEqual(_query_agy_ide_db_title_rows(ide2_root, wsl=False), ([], False))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("New Session Same Repo OK", home=home, wsl=False, env=env_local))
            # Symlink DB in second root -> fail closed
            ext_db = tmp / "ext_r2.db"
            _write_db(ext_db, [])
            db2_path.unlink()
            os.symlink(ext_db, db2_path)
            self.assertEqual(_query_agy_ide_db_title_rows(ide2_root, wsl=False), ([], False))
            self.assertIsNone(resolve_antigravity_ide_session_by_title("New Session Same Repo OK", home=home, wsl=False, env=env_local))
            # Lock timeout on second root DB -> fail closed
            db2_path.unlink()
            _write_db(db2_path, [])
            orig_open_agy = __import__("agent_voice_input.workspace_ctx", fromlist=["_open_agy_ide_db_ro"])._open_agy_ide_db_ro

            class _LockedAgyConn:
                def execute(self, *_a: object, **_kw: object):
                    raise sqlite3.OperationalError("database is locked")

                def close(self) -> None:
                    pass

            with mock.patch(
                "agent_voice_input.workspace_ctx._open_agy_ide_db_ro",
                side_effect=lambda r, *, wsl: _LockedAgyConn() if r.name == "antigravity-ide" else orig_open_agy(r, wsl=wsl),
            ):
                self.assertIsNone(resolve_antigravity_ide_session_by_title("New Session Same Repo OK", home=home, wsl=False, env=env_local))
            db2_path.unlink()
            db_path.unlink()

            # --- Item 3: Legacy brain/ _decode_agy_uri_or_path aligns wsl%2B + WSL_DISTRO_NAME & cache key ---
            env_u = {"HOME": str(home), "WSL_DISTRO_NAME": "Ubuntu"}
            env_d = {"HOME": str(home), "WSL_DISTRO_NAME": "Debian"}
            uri_u = f"vscode-remote://wsl%2BUbuntu{repo_a}"
            uri_d = f"vscode-remote://wsl%2BDebian{repo_a}"
            self.assertEqual(_decode_agy_uri_or_path(uri_u, home, wsl=True, env=env_u), str(repo_a))
            self.assertEqual(_decode_agy_uri_or_path(uri_d, home, keep_remote_sentinel=True, wsl=True, env=env_u), _AGY_REMOTE_URI_SENTINEL)
            self.assertEqual(_decode_agy_uri_or_path(uri_d, home, keep_remote_sentinel=False, wsl=True, env=env_u), "")
            self.assertEqual(_decode_agy_uri_or_path(uri_u, home, keep_remote_sentinel=True, wsl=False, env=env_local), _AGY_REMOTE_URI_SENTINEL)

            # End-to-end legacy brain/ resolution & prior discovery with wsl%2B distro isolation (and cache key isolation)
            for p in list(ide_brain.iterdir()):
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
            rich_wsl = [
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>Work on {repo_a}</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: {repo_a / 'a.py'}\n</ADDITIONAL_METADATA>"},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Done " + ("x" * 2000)},
                {"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "Next " + ("y" * 1000)},
                {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "All good " + ("z" * 1000)},
            ]
            _write_agy_brain_session(ide_brain, "cid-legacy-wsl-u", rich_wsl, mtime=base_t - 5, workspace_uris=[uri_u])
            # Same file probed under Debian MUST NOT match (and must not poison cache for Ubuntu!)
            self.assertIsNone(resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=base_t, wsl=True, env=env_d))
            self.assertEqual(discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=True, env=env_d), [])
            # Now under Ubuntu it MUST resolve and discover as prior!
            m_leg_u = resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=base_t, wsl=True, env=env_u)
            assert m_leg_u is not None
            self.assertEqual(m_leg_u.session_id, "cid-legacy-wsl-u")
            priors_leg_u = discover_antigravity_ide_prior_sessions(str(repo_a), home=home, wsl=True, env=env_u)
            self.assertEqual([c["session_id"] for c in priors_leg_u], ["cid-legacy-wsl-u"])
            # Under non-WSL host, vscode-remote://wsl%2B... must NOT match local repo
            self.assertIsNone(resolve_antigravity_ide_session(str(repo_a), home=home, now_ts=base_t, wsl=False, env=env_local))

            # --- Item 4: read_git_status_summary & read_repo_core_files honor caller env (no host GIT_DIR leak) ---
            git_repo = home / "repos" / "isolated-git"
            git_repo.mkdir(parents=True)
            base_git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            base_git_env.update({"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"})
            subprocess.run(["git", "init", "-q"], cwd=str(git_repo), env=base_git_env, check=True)
            (git_repo / "core_engine.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "core_engine.py"], cwd=str(git_repo), env=base_git_env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(git_repo), env=base_git_env, check=True)
            (git_repo / "dirty_module.py").write_text("y = 2\n", encoding="utf-8")

            iso_git_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
            seen_envs: list[dict[str, str]] = []
            orig_subproc_run = subprocess.run

            def _spy_git_run(*args: object, **kwargs: object):
                if isinstance(kwargs.get("env"), dict):
                    seen_envs.append(dict(kwargs["env"]))  # type: ignore[arg-type]
                return orig_subproc_run(*args, **kwargs)  # type: ignore[arg-type]

            with mock.patch.dict(
                os.environ,
                {"GIT_DIR": str(tmp / "nonexistent-host-git-dir"), "GIT_WORK_TREE": str(tmp / "nonexistent-wt")},
                clear=False,
            ), mock.patch("agent_voice_input.workspace_ctx.subprocess.run", side_effect=_spy_git_run):
                status_txt, status_hw = read_git_status_summary(str(git_repo), wsl=False, env=iso_git_env)
                core_files = read_repo_core_files(str(git_repo), wsl=False, env=iso_git_env)

            self.assertIn("dirty_module.py", status_txt)
            self.assertIn("dirty_module.py", status_hw)
            self.assertIn("core_engine.py", core_files)
            self.assertGreaterEqual(len(seen_envs), 2)
            for captured in seen_envs:
                self.assertEqual(captured.get("GIT_NO_LAZY_FETCH"), "1")
                self.assertNotIn("GIT_DIR", captured)
                self.assertNotIn("GIT_WORK_TREE", captured)

    def test_r3_agy_veto_out_unresolved_multiroot_and_legacy_sys_cwd_keep_remote(self) -> None:
        """R3 polish items 1-3: veto_out reasons, unresolved multi-root disambiguation, and legacy sys_cwds keep_remote."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            shared = home / "repos" / "shared-core"
            shared.mkdir(parents=True)
            ide_root = home / ".gemini" / "antigravity"
            ide_brain = ide_root / "brain"
            db_path = ide_root / "conversation_summaries.db"
            base_t = 1_795_424_400.0
            env_u = {"HOME": str(home), "WSL_DISTRO_NAME": "Ubuntu"}

            def _mk_sess(cid: str, mtime: float) -> Path:
                return _write_agy_brain_session(
                    ide_brain,
                    cid,
                    [{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": f"<USER_REQUEST>{cid}</USER_REQUEST>"}],
                    mtime=mtime,
                )

            _mk_sess("cid-del-a", base_t + 10)
            _mk_sess("cid-del-b", base_t + 20)
            _mk_sess("cid-same-del-1", base_t + 10)
            _mk_sess("cid-same-del-2", base_t + 300)
            _mk_sess("cid-wsl-mix-1", base_t + 10)
            _mk_sess("cid-wsl-mix-2", base_t + 20)
            # cid-single-no-tp and cid-multi-no-tp-1/2 intentionally have NO transcripts on disk

            deleted_a = (home / "repos" / "deleted-a").as_uri()
            deleted_b = (home / "repos" / "deleted-b").as_uri()
            debian_uri = f"vscode-remote://wsl%2BDebian{home / 'repos' / 'other-wsl'}"

            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    """
                    CREATE TABLE conversation_summaries (
                        conversation_id TEXT PRIMARY KEY,
                        title TEXT,
                        workspace_uris TEXT,
                        last_modified_time TEXT,
                        source TEXT,
                        parent_conversation_id TEXT,
                        nesting_depth INTEGER,
                        agent_name TEXT
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("cid-del-a", "Shared Plus Deleted Diff", json.dumps([shared.as_uri(), deleted_a]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-del-b", "Shared Plus Deleted Diff", json.dumps([shared.as_uri(), deleted_b]), "2026-11-23T09:00:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-same-del-1", "Shared Plus Deleted Same", json.dumps([shared.as_uri(), deleted_a]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-same-del-2", "Shared Plus Deleted Same", json.dumps([shared.as_uri(), deleted_a + "/"]), "2026-11-23T09:05:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-wsl-mix-1", "Shared Plus Foreign WSL", json.dumps([shared.as_uri(), debian_uri]), "2026-11-23T09:00:10Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-wsl-mix-2", "Shared Plus Foreign WSL", json.dumps([shared.as_uri()]), "2026-11-23T09:00:20Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-single-no-tp", "Brand New No Transcript", json.dumps([shared.as_uri()]), "2026-11-23T09:01:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-multi-no-tp-1", "Multi No Transcript", json.dumps([shared.as_uri()]), "2026-11-23T09:01:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                        ("cid-multi-no-tp-2", "Multi No Transcript", json.dumps([shared.as_uri()]), "2026-11-23T09:02:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
                    ],
                )
                conn.commit()

            # 1. [shared, deleted-A] vs [shared, deleted-B] -> ambiguous_workspace veto
            v_del: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Shared Plus Deleted Diff", home=home, wsl=True, env=env_u, veto_out=v_del)
            )
            self.assertEqual(v_del, ["ambiguous_workspace"])

            # 2. [shared, deleted-A] vs [shared, deleted-A/] -> same normalized unresolved set -> converges
            v_same: list[str] = []
            m_same = resolve_antigravity_ide_session_by_title("Shared Plus Deleted Same", home=home, wsl=True, env=env_u, veto_out=v_same)
            assert m_same is not None
            self.assertEqual(m_same.session_id, "cid-same-del-2")
            self.assertEqual(v_same, [])

            # 3. [shared, wsl+Debian/...] vs [shared] -> ambiguous_workspace veto
            v_wsl: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Shared Plus Foreign WSL", home=home, wsl=True, env=env_u, veto_out=v_wsl)
            )
            self.assertEqual(v_wsl, ["ambiguous_workspace"])

            # 4. Single/multi matched DB rows without valid transcript -> no_transcript veto
            v_nt1: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Brand New No Transcript", home=home, wsl=True, env=env_u, veto_out=v_nt1)
            )
            self.assertEqual(v_nt1, ["no_transcript"])

            v_nt2: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Multi No Transcript", home=home, wsl=True, env=env_u, veto_out=v_nt2)
            )
            self.assertEqual(v_nt2, ["no_transcript"])

            # 5. Clean title miss leaves veto_out empty (allows legacy fallback)
            v_miss: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Unmatched Legacy Title", home=home, wsl=True, env=env_u, veto_out=v_miss)
            )
            self.assertEqual(v_miss, [])

            # 6. Corrupt DB -> db_error veto
            db_path.write_bytes(b"corrupted sqlite header")
            v_err: list[str] = []
            self.assertIsNone(
                resolve_antigravity_ide_session_by_title("Unmatched Legacy Title", home=home, wsl=True, env=env_u, veto_out=v_err)
            )
            self.assertEqual(v_err, ["db_error"])
            db_path.unlink()

            # 7. Item 3: Legacy brain/ top-level cwd with foreign WSL URI keeps remote sentinel and blocks L2 fallback
            import shutil

            shutil.rmtree(ide_brain)
            _write_agy_brain_session(
                ide_brain,
                "cid-remote-top-cwd",
                [
                    {"source": "SYSTEM", "type": "SYSTEM_INFO", "cwd": f"vscode-remote://wsl%2BDebian{shared}", "content": "init"},
                    {
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "content": f"<USER_REQUEST>Check local</USER_REQUEST>\n<ADDITIONAL_METADATA>\nActive Document: {shared / 'main.py'}\n</ADDITIONAL_METADATA>",
                    },
                ],
                mtime=base_t - 5,
            )
            # Foreign distro top-level cwd MUST classify as "other" and NOT fall through to L2 local metadata!
            self.assertIsNone(resolve_antigravity_ide_session(str(shared), home=home, now_ts=base_t, wsl=True, env=env_u))
            # Under matching Debian distro, top-level cwd decodes locally and matches!
            env_d = {"HOME": str(home), "WSL_DISTRO_NAME": "Debian"}
            m_deb = resolve_antigravity_ide_session(str(shared), home=home, now_ts=base_t, wsl=True, env=env_d)
            assert m_deb is not None
            self.assertEqual(m_deb.session_id, "cid-remote-top-cwd")


if __name__ == "__main__":
    unittest.main()
