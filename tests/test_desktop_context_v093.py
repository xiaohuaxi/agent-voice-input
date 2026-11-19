"""Offline unit tests for v0.9-3 desktop GUI scene awareness & non-Git workspace support."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
            with mock.patch("agent_voice_input.session_ctx.os.scandir") as mock_scandir:
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

            with mock.patch("agent_voice_input.session_ctx.os.scandir") as mock_scan2:
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

            with mock.patch("agent_voice_input.session_ctx.subprocess.run") as mock_run, mock.patch(
                "os.lstat", side_effect=_no_mnt_lstat
            ), mock.patch("os.stat", side_effect=_no_mnt_stat), mock.patch(
                "agent_voice_input.session_ctx._HOST_IS_WSL", True
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

            with mock.patch("agent_voice_input.session_ctx.time.time", return_value=now_ts):
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

            with mock.patch("agent_voice_input.session_ctx.time.time", return_value=now_ts):
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


if __name__ == "__main__":
    unittest.main()
