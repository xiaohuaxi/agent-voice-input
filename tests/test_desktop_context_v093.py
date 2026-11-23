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
    is_antigravity_ide_window,
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
from agent_voice_input.daemon import VoiceInputDaemon, _window_from_mapping, run_pipeline_once  # noqa: E402
from agent_voice_input.platform_macos import MAC_SHIM_SCRIPT, SHIM_SCHEMA_VERSION, MacOSPlatform, _APPLE_SEP  # noqa: E402
from agent_voice_input.platform_wsl import ForegroundWindow, PasteOutcome  # noqa: E402
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
    is_wsl_slow_mount_path,
    read_git_status_summary,
    read_repo_core_files,
    read_workspace_shallow_files,
    resolve_antigravity_ide_session,
    resolve_antigravity_ide_session_by_title,
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
                "/mnt/c/Users/xhx/proj", "/mnt/d/work", "/mnt/wslg/distro",
                "//server/share/repo", r"\\wsl$\Ubuntu\home\user", r"C:\Users\xhx\repo",
            ):
                self.assertTrue(is_wsl_slow_mount_path(slow, wsl=True))
            self.assertFalse(is_wsl_slow_mount_path("/mnt/c/Users/xhx/proj", wsl=False))

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
                    ("cid-dup-old", "Shared Title Session", json.dumps([repo_b.as_uri()]), "2026-11-23T09:10:00Z", "CORTEX_TRAJECTORY_SOURCE_CASCADE", "", 0, ""),
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

            # 3) Truncated prefix ("..." and Unicode "…"), short prefix rejection, and exact-over-prefix priority
            m_exact_wins = resolve_antigravity_ide_session_by_title("Investigate Antigravity 2...", home=home, wsl=False)
            assert m_exact_wins is not None
            self.assertEqual(m_exact_wins.session_id, "cid-exact-over-prefix")

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

            # Verify P1 #3: ann_mtime (foreground view) beats newer db_lmt (background generation)
            m_rank = resolve_antigravity_ide_session_by_title("Duplicate Foreground Title", home=home, wsl=False)
            assert m_rank is not None
            self.assertEqual((m_rank.session_id, m_rank.cwd), ("cid-view-a", os.path.realpath(repo_a)))

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
            m_wsl = resolve_antigravity_ide_session_by_title("WSL Structured Workspace", home=home, wsl=True)
            assert m_wsl is not None
            self.assertEqual(m_wsl.cwd, os.path.realpath(repo_wsl_dot))
            m_wsl_nonhost = resolve_antigravity_ide_session_by_title("WSL Structured Workspace", home=home, wsl=False)
            assert m_wsl_nonhost is not None
            self.assertEqual(m_wsl_nonhost.cwd, "")
            m_ssh = resolve_antigravity_ide_session_by_title("SSH Structured Workspace", home=home, wsl=True)
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


if __name__ == "__main__":
    unittest.main()
