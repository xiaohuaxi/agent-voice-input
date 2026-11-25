"""Offline unit tests for agent_voice_input.integrate (Claude Code, Codex CLI, Antigravity CLI)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_voice_input import integrate as mod
from agent_voice_input.config import _parse_simple_toml, parse_toml


def _no_which(_name: str) -> str | None:
    return None


class TestHelpersAndShellContracts(unittest.TestCase):
    def test_has_codex_voice_title_variants(self) -> None:
        self.assertFalse(mod.has_codex_voice_title(None))  # type: ignore[arg-type]
        self.assertFalse(mod.has_codex_voice_title({}))
        self.assertFalse(mod.has_codex_voice_title({"tui": {"terminal_title": ["spinner"]}}))
        self.assertFalse(mod.has_codex_voice_title({"tui": {"terminal_title": "thread-id"}}))
        self.assertTrue(
            mod.has_codex_voice_title({"tui": {"terminal_title": ["spinner", "thread-id"]}})
        )
        self.assertTrue(mod.has_codex_voice_title({"notify": "agent-voice-input session-hook --cli codex"}))
        self.assertTrue(mod.has_codex_voice_title({"notify": "~/.local/bin/voice-title.sh"}))
        self.assertFalse(mod.has_codex_voice_title({"notify": "echo done"}))
        self.assertTrue(
            mod.has_codex_voice_title({"notify": ["agent-voice-input", "session-hook", "--cli", "codex"]})
        )
        self.assertFalse(mod.has_codex_voice_title({"notify": ["echo", "hello"]}))

    def test_inline_commands_missing_binary_and_stub_execution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            # Isolated env with minimal PATH that has /bin/sh & standard POSIX utils, no agent-voice-input
            base_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

            # 1. Missing binary: Claude exits 0 with empty stdout; AGY exits 0 with exact "{}\n"
            res_c = subprocess.run(
                mod.CLAUDE_HOOK_COMMAND, shell=True, env=base_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(res_c.returncode, 0)
            self.assertEqual(res_c.stdout, "")

            res_a = subprocess.run(
                mod.AGY_HOOK_COMMAND, shell=True, env=base_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(res_a.returncode, 0)
            self.assertEqual(res_a.stdout, "{}\n")

            # 2. Installed in $HOME/.local/bin/agent-voice-input (even if binary prints noise & fails)
            local_bin = home / ".local" / "bin"
            local_bin.mkdir(parents=True)
            log_file = home / "invocations.log"
            stub = local_bin / "agent-voice-input"
            stub.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log_file}'\necho noisy-stdout\nexit 1\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            res_c2 = subprocess.run(
                mod.CLAUDE_HOOK_COMMAND, shell=True, env=base_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(res_c2.returncode, 0)
            self.assertEqual(res_c2.stdout, "")

            res_a2 = subprocess.run(
                mod.AGY_HOOK_COMMAND, shell=True, env=base_env, capture_output=True, text=True, check=False
            )
            self.assertEqual(res_a2.returncode, 0)
            self.assertEqual(res_a2.stdout, "{}\n")

            logged = log_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                logged,
                [
                    "session-hook --cli claude --quiet",
                    "session-hook --cli agy --quiet",
                ],
            )

    def test_normalize_targets_validation(self) -> None:
        self.assertEqual(mod._normalize_targets("all"), (True, mod.CLIS))
        self.assertEqual(mod._normalize_targets(["all"]), (True, mod.CLIS))
        self.assertEqual(mod._normalize_targets(["claude", "codex", "claude"]), (False, ("claude", "codex")))
        with self.assertRaises(ValueError):
            mod._normalize_targets([])
        with self.assertRaises(ValueError):
            mod._normalize_targets("unknown-cli")

    def test_platform_detection_and_env_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            custom_cache = home / "xdg-cache"
            custom_codex = home / "custom-codex"
            custom_codex.mkdir()
            (custom_codex / "config.toml").write_text('[tui]\nterminal_title = ["thread-id"]\n', encoding="utf-8")
            env = {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(custom_cache),
                "CODEX_HOME": str(custom_codex),
                "WSL_DISTRO_NAME": "Ubuntu",
            }
            with mock.patch("sys.platform", "linux"):
                statuses = mod.inspect_integrations(env=env, which_fn=_no_which)
            by_cli = {s.cli: s for s in statuses}
            self.assertEqual(by_cli["codex"].necessity, "recommended")
            self.assertTrue(by_cli["codex"].configured)
            self.assertEqual(by_cli["codex"].config_path, str(custom_codex / "config.toml"))
            self.assertEqual(mod._resolve_cache_dir(None, home, env), custom_cache / "agent-voice-input")
            self.assertEqual(mod._detect_platform("macos", None), "darwin")
            self.assertEqual(mod._detect_platform("linux", None), "linux")
            self.assertEqual(mod._detect_platform("wsl2", None), "wsl")
            with mock.patch("sys.platform", "darwin"):
                self.assertEqual(mod._detect_platform(None, {}), "darwin")
            with mock.patch("sys.platform", "linux"):
                self.assertEqual(mod._detect_platform(None, {}), "linux")
            self.assertEqual(mod._resolve_home(None, None), Path.home())

            # When env=None and home_dir=None, os.environ CODEX_HOME & XDG_CACHE_HOME are respected
            with mock.patch.dict(os.environ, env, clear=True):
                statuses_env = {s.cli: s for s in mod.inspect_integrations(which_fn=_no_which)}
                self.assertEqual(statuses_env["codex"].config_path, str(custom_codex / "config.toml"))
                self.assertTrue(statuses_env["codex"].configured)
                self.assertEqual(mod._resolve_cache_dir(None, home, None), custom_cache / "agent-voice-input")

                # Explicit home_dir with env=None MUST stay isolated from host CODEX_HOME / XDG_CACHE_HOME
                iso_home = home / "isolated"
                iso_home.mkdir()
                statuses_iso = {s.cli: s for s in mod.inspect_integrations(home_dir=iso_home, which_fn=_no_which)}
                self.assertEqual(statuses_iso["codex"].config_path, str(iso_home / ".codex" / "config.toml"))
                self.assertFalse(statuses_iso["codex"].configured)
                self.assertEqual(
                    mod._resolve_cache_dir(None, iso_home, None, home_explicit=True),
                    iso_home / ".cache" / "agent-voice-input",
                )

    def test_integration_example_json_files_match_hook_commands(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        claude_cfg = json.loads((repo_root / "integrations" / "claude" / "settings.hooks.json").read_text(encoding="utf-8"))
        for ev in mod.CLAUDE_HOOK_EVENTS:
            self.assertEqual(claude_cfg["hooks"][ev][0]["hooks"][0]["command"], mod.CLAUDE_HOOK_COMMAND)

        agy_cfg = json.loads((repo_root / "integrations" / "gemini" / "hooks.json").read_text(encoding="utf-8"))
        ns = agy_cfg[mod.AGY_NAMESPACE_KEY]
        for ev in ("SessionStart", "PreInvocation"):
            self.assertEqual(ns[ev][0]["command"], mod.AGY_HOOK_COMMAND)


class TestInspectIntegrations(unittest.TestCase):
    def test_inspect_macos_linux_partial_legacy_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            # Claude: partial config (SessionStart only)
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(
                json.dumps(
                    {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "voice-title.sh"}]}]}}
                ),
                encoding="utf-8",
            )
            # AGY: legacy ~/.gemini/hooks.json
            (home / ".gemini").mkdir()
            (home / ".gemini" / "hooks.json").write_text(
                json.dumps({"voice-session-title": {"SessionStart": [{"command": "agent-voice-input session-hook --cli agy"}]}}),
                encoding="utf-8",
            )
            # Codex: legacy notify
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                'notify = ["agent-voice-input", "session-hook", "--cli", "codex"]\n', encoding="utf-8"
            )

            statuses = mod.inspect_integrations(home_dir=home, platform="darwin", which_fn=_no_which)
            by_cli = {s.cli: s for s in statuses}
            self.assertEqual(by_cli["claude"].necessity, "optional")
            self.assertIn("Ghostty", by_cli["claude"].note)
            self.assertTrue(by_cli["claude"].configured)
            self.assertIn("missing UserPromptSubmit", by_cli["claude"].detected_via)

            self.assertTrue(by_cli["agy"].configured)
            self.assertIn("legacy:", by_cli["agy"].detected_via)

            self.assertTrue(by_cli["codex"].configured)
            self.assertEqual(by_cli["codex"].detected_via, "legacy:notify")

            # Formatters (text + json)
            txt = mod.format_status_text(statuses)
            self.assertIn("Note (optional):", txt)
            self.assertIn("[claude]", txt)
            as_json = mod.format_status_json(statuses)
            self.assertEqual(len(as_json), 3)
            self.assertEqual(as_json[0]["cli"], "claude")

            # Corrupt files + Linux platform note
            (home / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")
            (home / ".gemini" / "config").mkdir()
            (home / ".gemini" / "config" / "hooks.json").write_text("[]", encoding="utf-8")
            (home / ".codex" / "config.toml").write_text("[tui\nbroken", encoding="utf-8")
            bad = {s.cli: s for s in mod.inspect_integrations(home_dir=home, platform="linux", which_fn=_no_which)}
            for cli in mod.CLIS:
                self.assertFalse(bad[cli].configured)
                self.assertEqual(bad[cli].detected_via, "invalid_config")
                self.assertTrue(bad[cli].error)
            self.assertIn("error (", mod.format_status_text(list(bad.values())))

            # Non-matching JSON entries (e.g. non-list event, non-dict entry) stay unconfigured
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {"SessionStart": "bad", "PreToolUse": ["not-dict", {"command": "echo ok"}]}}),
                encoding="utf-8",
            )
            (home / ".gemini" / "config" / "hooks.json").unlink()
            (home / ".gemini" / "hooks.json").unlink()
            (home / ".gemini" / "settings.json").write_text(
                json.dumps({"hooks": {"PreInvocation": [{"command": "agent-voice-input session-hook --cli agy"}]}}),
                encoding="utf-8",
            )
            checked = {s.cli: s for s in mod.inspect_integrations(home_dir=home, platform="linux", which_fn=_no_which)}
            self.assertFalse(checked["claude"].configured)
            self.assertTrue(checked["agy"].configured)
            self.assertIn("settings.json", checked["agy"].detected_via)


class TestClaudeIntegration(unittest.TestCase):
    def test_claude_install_merge_idempotent_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            home.mkdir()
            (home / ".claude").mkdir()
            settings = home / ".claude" / "settings.json"
            existing = {
                "permissions": {"allow": ["Read"]},
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "user-start.sh"}]},
                        "non-dict-preserved",
                    ],
                    "Stop": "keep-non-list-untouched",
                },
            }
            settings.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            # dry-run writes nothing
            dry = mod.apply_integrations("claude", dry_run=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(dry[0].action, "would_install")
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), existing)
            self.assertFalse(cache.exists())

            # real install merges both events and backs up
            res = mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res[0].action, "installed")
            self.assertTrue(Path(res[0].backup_path).is_file())
            self.assertEqual(stat.S_IMODE(Path(res[0].backup_path).stat().st_mode), 0o600)
            after = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(after["permissions"], {"allow": ["Read"]})
            self.assertEqual(after["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
            self.assertEqual(after["hooks"]["Stop"], "keep-non-list-untouched")
            self.assertEqual(len(after["hooks"]["SessionStart"]), 3)
            self.assertEqual(len(after["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(
                after["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                mod.CLAUDE_HOOK_COMMAND,
            )

            # idempotent re-run
            res2 = mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res2[0].action, "already_configured")

            # uninstall removes only managed entries, preserves user-start.sh, PreToolUse, and cleans UserPromptSubmit
            un_dry = mod.apply_integrations("claude", dry_run=True, uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un_dry[0].action, "would_uninstall")
            un = mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "uninstalled")
            restored = json.loads(settings.read_text(encoding="utf-8"))
            self.assertNotIn("UserPromptSubmit", restored["hooks"])
            self.assertEqual(restored["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])
            self.assertEqual(restored["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])

            # second uninstall is idempotent
            un2 = mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un2[0].action, "not_configured")

    def test_claude_uninstall_cleans_empty_hooks_and_preserves_custom_and_voice_title_sh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            home.mkdir()
            # Explicit target creates ~/.claude even if not installed
            res = mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res[0].action, "installed")
            settings = home / ".claude" / "settings.json"
            self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)

            # Uninstall cleans empty 'hooks' key completely
            un = mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "uninstalled")
            self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), {})

            # Mixed matcher (one user hook + one managed hook) + flat managed entry + voice-title.sh
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "matcher": "startup",
                                    "hooks": [
                                        {"type": "command", "command": "keep-me.sh"},
                                        {"type": "command", "command": mod.CLAUDE_HOOK_COMMAND},
                                    ],
                                },
                                {"type": "command", "command": "agent-voice-input session-hook --cli claude"},
                            ],
                            "UserPromptSubmit": [
                                {"hooks": [{"type": "command", "command": "~/.claude/hooks/voice-title.sh"}]}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            # Install skips both events (voice-title.sh counts as already configured)
            self.assertEqual(
                mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "already_configured",
            )
            # Uninstall removes managed entries only, keeps keep-me.sh and voice-title.sh
            self.assertEqual(
                mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "uninstalled",
            )
            after = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(len(after["hooks"]["SessionStart"]), 1)
            self.assertEqual(after["hooks"]["SessionStart"][0]["hooks"], [{"type": "command", "command": "keep-me.sh"}])
            self.assertEqual(len(after["hooks"]["UserPromptSubmit"]), 1)

    def test_claude_fail_closed_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".claude").mkdir(parents=True)
            settings = home / ".claude" / "settings.json"

            # Uninstall when file missing
            self.assertEqual(
                mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "not_configured",
            )
            for payload in ("{bad json", '["not-obj"]', '{"hooks": "bad"}', '{"hooks": {"SessionStart": "bad"}}'):
                settings.write_text(payload, encoding="utf-8")
                act = mod.apply_integrations("claude", home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
                self.assertEqual(act.action, "error")
                self.assertEqual(settings.read_text(encoding="utf-8"), payload)
            # Uninstall with non-dict hooks fails closed
            settings.write_text('{"hooks": []}', encoding="utf-8")
            self.assertEqual(
                mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "error",
            )
            # Uninstall with no hooks key -> not_configured
            settings.write_text('{"theme": "dark"}', encoding="utf-8")
            self.assertEqual(
                mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "not_configured",
            )


class TestAgyIntegration(unittest.TestCase):
    def test_agy_install_preserve_siblings_legacy_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            cfg_dir = home / ".gemini" / "config"
            cfg_dir.mkdir(parents=True)
            hooks_file = cfg_dir / "hooks.json"
            existing = {
                "agy-agent-depth-guard": {"PreInvocation": [{"type": "command", "command": "guard.py", "timeout": 5}]}
            }
            hooks_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

            res = mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res[0].action, "installed")
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            self.assertEqual(data["agy-agent-depth-guard"], existing["agy-agent-depth-guard"])
            ns = data[mod.AGY_NAMESPACE_KEY]
            self.assertEqual(ns["SessionStart"][0]["timeout"], 5)
            self.assertEqual(ns["PreInvocation"][0]["command"], mod.AGY_HOOK_COMMAND)

            # Idempotent re-run
            self.assertEqual(
                mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "already_configured",
            )

            # Uninstall removes only voice-session-title
            un = mod.apply_integrations("agy", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "uninstalled")
            self.assertEqual(json.loads(hooks_file.read_text(encoding="utf-8")), existing)
            self.assertEqual(
                mod.apply_integrations("agy", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "not_configured",
            )

            # Corrupt namespace type fails closed
            hooks_file.write_text(json.dumps({mod.AGY_NAMESPACE_KEY: "bad"}), encoding="utf-8")
            self.assertEqual(
                mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "error",
            )
            # Corrupt JSON fails closed
            hooks_file.write_text("{bad", encoding="utf-8")
            self.assertEqual(
                mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "error",
            )

    def test_agy_legacy_detection_prevents_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".gemini").mkdir(parents=True)
            legacy = home / ".gemini" / "hooks.json"
            legacy.write_text(
                json.dumps({"voice-session-title": {"SessionStart": [{"command": "agent-voice-input session-hook --cli agy"}]}}),
                encoding="utf-8",
            )
            # Primary does not exist yet
            res = mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res[0].action, "already_configured")
            self.assertIn("legacy", res[0].detail)
            self.assertFalse((home / ".gemini" / "config" / "hooks.json").exists())

            # Uninstall when primary missing mentions legacy
            un = mod.apply_integrations("agy", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "not_configured")
            self.assertIn("legacy", un[0].detail)

            # Primary exists with unrelated key, legacy still stops duplication
            (home / ".gemini" / "config").mkdir()
            (home / ".gemini" / "config" / "hooks.json").write_text('{"other": {}}', encoding="utf-8")
            res2 = mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res2[0].action, "already_configured")
            self.assertIn("legacy", res2[0].detail)

            # Brand new primary install (no legacy)
            legacy.unlink()
            (home / ".gemini" / "config" / "hooks.json").unlink()
            res3 = mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res3[0].action, "installed")
            self.assertTrue((home / ".gemini" / "config" / "hooks.json").is_file())


class TestCodexIntegration(unittest.TestCase):
    def test_codex_create_append_preserve_comments_and_multiline_strings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            codex_cfg = home / ".codex" / "config.toml"

            # Uninstall on missing file
            self.assertEqual(
                mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action,
                "not_configured",
            )
            # Create fresh file
            res = mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res[0].action, "installed")
            self.assertEqual(parse_toml(codex_cfg.read_text(encoding="utf-8"))["tui"]["terminal_title"], list(mod.CODEX_DEFAULT_TERMINAL_TITLE))

            # Uninstall cleans empty [tui]
            un = mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "uninstalled")
            self.assertEqual(codex_cfg.read_text(encoding="utf-8").strip(), "")

            # File with comments, multiline strings containing fake "[tui]", and no trailing newline
            orig = (
                '# Top comment\n'
                'model = "gpt-5"\n'
                'note = """\n[tui]\nnot a real section \\"quoted\\"\n"""\n'
                "lit = '''\n[tui]\n'''"
            )
            codex_cfg.write_text(orig, encoding="utf-8")
            res2 = mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(res2[0].action, "installed")
            txt = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("# Top comment", txt)
            self.assertIn("not a real section", txt)
            parsed = parse_toml(txt)
            self.assertEqual(parsed["tui"]["terminal_title"], list(mod.CODEX_DEFAULT_TERMINAL_TITLE))

            # Uninstall restores without leaving stray empty [tui]
            un2 = mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un2[0].action, "uninstalled")
            txt_un = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("# Top comment", txt_un)
            self.assertNotIn("terminal_title", txt_un)
            self.assertEqual(parse_toml(txt_un)["model"], "gpt-5")

    def test_codex_existing_tui_section_and_array_variants(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".codex").mkdir(parents=True)
            cfg = home / ".codex" / "config.toml"

            # 1. Existing [tui] with other keys/comments, no terminal_title (in middle of file)
            cfg.write_text("[tui]\n# keep tui comment\nanimations = true\n\n[other]\nx = 1\n", encoding="utf-8")
            self.assertEqual(mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "installed")
            txt = cfg.read_text(encoding="utf-8")
            self.assertIn("# keep tui comment", txt)
            self.assertIn("animations = true", txt)
            self.assertEqual(parse_toml(txt)["tui"]["terminal_title"], list(mod.CODEX_DEFAULT_TERMINAL_TITLE))
            # Uninstall keeps [tui] because animations & comment remain
            self.assertEqual(mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "uninstalled")
            txt2 = cfg.read_text(encoding="utf-8")
            self.assertIn("[tui]", txt2)
            self.assertIn("animations = true", txt2)
            self.assertNotIn("terminal_title", txt2)

            # 2. Existing [tui] in middle of file with ONLY default terminal_title -> uninstall drops empty [tui]
            cfg.write_text('model = "x"\n\n[tui]\nterminal_title = ["spinner", "project-name", "thread-title", "thread-id"]\n\n[other]\nx = 1\n', encoding="utf-8")
            self.assertEqual(mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "uninstalled")
            txt_mid = cfg.read_text(encoding="utf-8")
            self.assertNotIn("[tui]", txt_mid)
            self.assertEqual(parse_toml(txt_mid)["other"]["x"], 1)

            # 3. Single-line custom arrays: empty, trailing comma, single quotes, embedded #/], trailing comment
            cases = [
                ('[tui]\nterminal_title = [] # empty\n', ["thread-id"], []),
                ('[tui]\nterminal_title = ["spinner",] # comma\n', ["spinner", "thread-id"], ["spinner"]),
                ("[tui]\nterminal_title = ['a#b', \"c]d\\\"\"] # keep\n", ["a#b", 'c]d"', "thread-id"], ["a#b", 'c]d"']),
            ]
            for raw_in, expected_after, expected_un in cases:
                cfg.write_text(raw_in, encoding="utf-8")
                self.assertEqual(mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "installed")
                after_txt = cfg.read_text(encoding="utf-8")
                self.assertEqual(parse_toml(after_txt)["tui"]["terminal_title"], expected_after)
                self.assertIn("#", after_txt)
                # Idempotent
                self.assertEqual(mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "already_configured")
                # Uninstall removes only thread-id
                self.assertEqual(mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "uninstalled")
                un_txt = cfg.read_text(encoding="utf-8")
                self.assertEqual(parse_toml(un_txt)["tui"]["terminal_title"], expected_un)
                self.assertIn("#", un_txt)

            # 4. Simple comment-free multiline array -> install appends & default multiline uninstall removes
            cfg.write_text('[tui]\nterminal_title = [\n  "spinner",\n  "project-name"\n]\n', encoding="utf-8")
            self.assertEqual(mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "installed")
            self.assertEqual(
                parse_toml(cfg.read_text(encoding="utf-8"))["tui"]["terminal_title"],
                ["spinner", "project-name", "thread-id"],
            )
            # Custom multiline uninstall fails closed
            self.assertEqual(mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "error")

            # Default 4-item comment-free multiline -> uninstall succeeds cleanly
            cfg.write_text(
                '[tui]\nterminal_title = [\n  "spinner",\n  "project-name",\n  "thread-title",\n  "thread-id",\n]\n',
                encoding="utf-8",
            )
            self.assertEqual(mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0].action, "uninstalled")
            self.assertEqual(cfg.read_text(encoding="utf-8").strip(), "")

    def test_codex_fail_closed_edge_cases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".codex").mkdir(parents=True)
            cfg = home / ".codex" / "config.toml"

            bad_installs = [
                "[tui\nbroken",
                '[tui]\nterminal_title = "not-array"\n',
                'tui.theme = "dark"\n',  # dotted key without explicit [tui] header -> fail-closed across py3.10-3.14
                'tui = { theme = "dark" }\n',  # inline table without explicit [tui] header -> fail-closed
                '[tui]\nterminal_title = [\n  "spinner", # inline comment\n  "project-name",\n]\n',
                '[tui]\nterminal_title = [["nested"]]\n',
            ]
            for raw in bad_installs:
                cfg.write_text(raw, encoding="utf-8")
                act = mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
                self.assertEqual(act.action, "error", msg=f"expected error for {raw!r}")
                self.assertEqual(cfg.read_text(encoding="utf-8"), raw)

            # Simulate Python 3.10 fallback parser (_parse_simple_toml) where appending [tui] after
            # dotted tui.* keys would not be rejected by the parser alone: must still fail closed.
            with mock.patch.object(mod, "parse_toml", _parse_simple_toml):
                for raw in ('tui.theme = "dark"\n', 'tui = { theme = "dark" }\n'):
                    cfg.write_text(raw, encoding="utf-8")
                    act = mod.apply_integrations("codex", home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
                    self.assertEqual(act.action, "error", msg=f"expected py310 fallback error for {raw!r}")
                    self.assertEqual(cfg.read_text(encoding="utf-8"), raw)

            # Fail-closed uninstall cases
            bad_uninstalls = [
                "[tui\nbroken",
                'tui.terminal_title = ["thread-id"]\n',  # no [tui] section header
                '[tui]\nterminal_title = [\n  "spinner", # inline comment\n  "project-name",\n  "thread-title",\n  "thread-id",\n]\n',
            ]
            for raw in bad_uninstalls:
                cfg.write_text(raw, encoding="utf-8")
                act = mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
                self.assertEqual(act.action, "error", msg=f"expected uninstall error for {raw!r}")
                self.assertEqual(cfg.read_text(encoding="utf-8"), raw)

            # Legacy notify on uninstall returns not_configured with helpful detail
            cfg.write_text('notify = ["agent-voice-input", "session-hook"]\n', encoding="utf-8")
            act_leg = mod.apply_integrations("codex", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
            self.assertEqual(act_leg.action, "not_configured")
            self.assertIn("notify", act_leg.detail)

            # Direct parser edge checks
            self.assertIsNone(mod._split_single_line_toml_array("not-array"))
            self.assertIsNone(mod._split_single_line_toml_array('["a"] trailing_junk'))
            self.assertIsNone(mod._split_single_line_toml_array('["a" # comment'))
            self.assertIsNone(mod._find_simple_multiline_end(['terminal_title = ["a"]\n'], 0, 1))
            self.assertIsNone(mod._find_simple_multiline_end(['terminal_title = [\n', '  "a"\n', '] junk\n'], 0, 3))
            self.assertIsNone(mod._find_simple_multiline_end(['terminal_title = [\n', '  "unterminated\n', ']\n'], 0, 3))
            self.assertIsNone(mod._find_simple_multiline_end(['terminal_title = [\n', '  "a"\n'], 0, 2))


class TestSymlinksBackupsAndTargetsAll(unittest.TestCase):
    def test_dotfiles_symlink_preserved_and_backups_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            cache = root / "cache"
            dotfiles = root / "dotfiles"
            home.mkdir()
            dotfiles.mkdir()

            real_claude = dotfiles / "claude-settings.json"
            real_claude.write_text('{"model": "opus"}\n', encoding="utf-8")
            real_claude.chmod(0o644)
            (home / ".claude").mkdir()
            link_claude = home / ".claude" / "settings.json"
            link_claude.symlink_to(real_claude)

            # Inspect reports symlink & resolved path
            st = {s.cli: s for s in mod.inspect_integrations(home_dir=home, platform="darwin", which_fn=_no_which)}
            self.assertTrue(st["claude"].is_symlink)
            self.assertEqual(st["claude"].resolved_path, str(real_claude.resolve()))
            self.assertIn("->", mod.format_status_text([st["claude"]]))

            # targets="all" touches ONLY installed CLIs (claude dir exists; codex & agy do NOT)
            actions = mod.apply_integrations("all", home_dir=home, cache_dir=cache, which_fn=_no_which)
            by_cli = {a.cli: a for a in actions}
            self.assertEqual(by_cli["claude"].action, "installed")
            self.assertTrue(by_cli["claude"].is_symlink)
            self.assertEqual(by_cli["claude"].resolved_path, str(real_claude.resolve()))
            self.assertEqual(by_cli["codex"].action, "not_installed")
            self.assertEqual(by_cli["agy"].action, "not_installed")
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".gemini").exists())

            # Symlink is STILL a symlink pointing to real_claude, permissions preserved, dotfiles dir clean
            self.assertTrue(link_claude.is_symlink())
            self.assertEqual(link_claude.resolve(), real_claude.resolve())
            self.assertEqual(stat.S_IMODE(real_claude.stat().st_mode), 0o644)
            self.assertEqual([p.name for p in dotfiles.iterdir()], ["claude-settings.json"])

            # Backup lives under cache/integrate-backups/<stamp>/ with 0700 dir & 0600 file
            bk1 = Path(by_cli["claude"].backup_path)
            self.assertTrue(bk1.is_file())
            self.assertEqual(bk1.read_text(encoding="utf-8"), '{"model": "opus"}\n')
            self.assertEqual(stat.S_IMODE(bk1.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(bk1.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(bk1.parent.parent.stat().st_mode), 0o700)

            # Same-second second backup (uninstall) gets suffixed instead of colliding
            un = mod.apply_integrations("claude", uninstall=True, home_dir=home, cache_dir=cache, which_fn=_no_which)
            self.assertEqual(un[0].action, "uninstalled")
            bk2 = Path(un[0].backup_path)
            self.assertTrue(bk2.is_file())
            self.assertNotEqual(bk1, bk2)
            self.assertTrue(link_claude.is_symlink())

            # Action formatters (text + json)
            txt = mod.format_actions_text(actions, dry_run=False)
            self.assertIn("symlink ->", txt)
            self.assertIn("backup:", txt)
            txt_dry = mod.format_actions_text(actions, dry_run=True)
            self.assertTrue(txt_dry.startswith("[dry-run] "))
            self.assertEqual(len(mod.format_actions_json(actions)), 3)

    def test_dangling_symlink_non_regular_file_and_uid_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            cache = Path(td) / "cache"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            (home / ".gemini" / "config").mkdir(parents=True)

            # Dangling symlink
            (home / ".claude" / "settings.json").symlink_to(home / "does-not-exist.json")
            # Directory instead of file
            (home / ".codex" / "config.toml").mkdir()
            # Regular file with simulated foreign UID
            agy_file = home / ".gemini" / "config" / "hooks.json"
            agy_file.write_text("{}\n", encoding="utf-8")

            insp = {s.cli: s for s in mod.inspect_integrations(home_dir=home, which_fn=_no_which)}
            self.assertIn("dangling symlink", insp["claude"].error)
            self.assertIn("not a regular file", insp["codex"].error)

            with mock.patch("os.getuid", return_value=os.getuid() + 1):
                acts = {a.cli: a for a in mod.apply_integrations("all", home_dir=home, cache_dir=cache, which_fn=_no_which)}
            self.assertEqual(acts["claude"].action, "error")
            self.assertEqual(acts["codex"].action, "error")
            self.assertEqual(acts["agy"].action, "error")
            self.assertIn("uid=", acts["agy"].detail)

            # Atomic write failure cleans temp file and bubbles to action error
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                act_fail = mod.apply_integrations("agy", home_dir=home, cache_dir=cache, which_fn=_no_which)[0]
            self.assertEqual(act_fail.action, "error")
            self.assertIn("disk full", act_fail.detail)
            leftovers = [p for p in agy_file.parent.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
