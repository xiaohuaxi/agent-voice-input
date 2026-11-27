"""Offline unit tests for ``scripts/check_package.py``."""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_package.py"

_SPEC = importlib.util.spec_from_file_location("check_package", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
check_package = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_package)


def _create_minimal_repo(
    root: Path,
    *,
    pyproject_ver: str = "0.18.0",
    init_ver: str = "0.18.0",
    changelog: str | None = None,
    win_shim_bytes: bytes = b"# pure ascii\n",
    win_cs_bytes: bytes = b"namespace Avi {}\n",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "agent-voice-input"\nversion = "{pyproject_ver}"\n',
        encoding="utf-8",
    )
    if changelog is None:
        changelog = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            f"## [{pyproject_ver}] - 2026-11-27\n\n"
            "### Fixed\n- Hardened publish checks.\n\n"
            "## [0.17.0] - 2026-11-20\n\n- Previous release.\n"
        )
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for rel in (
        "LICENSE",
        "AGENTS.md",
        "ROADMAP.md",
        "README.md",
        "README.zh-CN.md",
        "SECURITY.md",
        "config.example.toml",
        "config.example.env",
        "bin/agent-voice-input",
        "docs/context-routing.md",
        "scripts/check_coverage.py",
        "scripts/check_package.py",
        "integrations/remote/remote-title-hook.sh",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"placeholder {rel}\n", encoding="utf-8")

    pkg = root / "src" / "agent_voice_input"
    (pkg / "win_shim").mkdir(parents=True, exist_ok=True)
    (pkg / "mac_shim").mkdir(parents=True, exist_ok=True)
    for mod in (
        "cli.py",
        "daemon.py",
        "prompts.py",
        "app_scope.py",
        "http_transport.py",
        "llm_common.py",
        "platform_common.py",
        "workspace_ctx.py",
        "_remote_probe.py",
        "remote_ssh.py",
    ):
        (pkg / mod).write_text("# mod\n", encoding="utf-8")
    (pkg / "__init__.py").write_text(f'__version__ = "{init_ver}"\n', encoding="utf-8")
    (pkg / "win_shim.ps1").write_bytes(win_shim_bytes)
    (pkg / "win_shim" / "NativeBridge.cs").write_bytes(win_cs_bytes)
    (pkg / "mac_shim" / "BridgeMain.swift").write_text("// swift\n", encoding="utf-8")


def _build_fake_dists(
    repo: Path,
    dist_dir: Path,
    *,
    version: str = "0.18.0",
    wheel_version: str | None = None,
    sdist_version: str | None = None,
    extra_wheel_files: dict[str, bytes] | None = None,
    extra_sdist_files: dict[str, bytes] | None = None,
    omit_wheel_files: set[str] | None = None,
    omit_sdist_files: set[str] | None = None,
) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    w_ver = wheel_version or version
    s_ver = sdist_version or version
    omit_w = omit_wheel_files or set()
    omit_s = omit_sdist_files or set()

    pkg = repo / "src" / "agent_voice_input"
    whl_path = dist_dir / f"agent_voice_input-{w_ver}-py3-none-any.whl"
    with zipfile.ZipFile(whl_path, "w") as zf:
        for p in sorted(pkg.rglob("*")):
            if not p.is_file():
                continue
            arc = f"agent_voice_input/{p.relative_to(pkg).as_posix()}"
            if arc in omit_w:
                continue
            zf.writestr(arc, p.read_bytes())
        dist_info = f"agent_voice_input-{w_ver}.dist-info"
        zf.writestr(f"{dist_info}/METADATA", f"Metadata-Version: 2.1\nName: agent-voice-input\nVersion: {w_ver}\n")
        zf.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\nagent-voice-input = agent_voice_input.cli:main\n",
        )
        zf.writestr(f"{dist_info}/licenses/LICENSE", "Apache-2.0\n")
        for arc, data in (extra_wheel_files or {}).items():
            zf.writestr(arc, data)

    sdist_path = dist_dir / f"agent_voice_input-{s_ver}.tar.gz"
    top = f"agent_voice_input-{s_ver}"
    with tarfile.open(sdist_path, "w:gz") as tf:
        def _add_bytes(rel: str, data: bytes) -> None:
            ti = tarfile.TarInfo(name=f"{top}/{rel.lstrip('/')}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

        for p in sorted(repo.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(repo).as_posix()
            if rel.startswith("dist/") or f"/{rel}" in omit_s or rel in omit_s:
                continue
            _add_bytes(rel, p.read_bytes())
        _add_bytes("PKG-INFO", f"Metadata-Version: 2.1\nName: agent-voice-input\nVersion: {s_ver}\n".encode("utf-8"))
        for rel, data in (extra_sdist_files or {}).items():
            _add_bytes(rel, data)


class CheckPackageTests(unittest.TestCase):
    def test_real_repo_version_sync_and_ascii_pass(self) -> None:
        ver = check_package.check_version_sync(_REPO_ROOT)
        self.assertTrue(ver)
        with mock.patch.object(check_package, "tomllib", None):
            self.assertEqual(
                check_package.parse_pyproject_version(_REPO_ROOT / "pyproject.toml"),
                ver,
            )
        checked = check_package.check_source_win_shim_ascii(_REPO_ROOT)
        self.assertIn("src/agent_voice_input/win_shim.ps1", checked)
        self.assertTrue(any(p.endswith(".cs") for p in checked))

    def test_release_tag_validation_and_notes_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _create_minimal_repo(repo, pyproject_ver="0.18.0", init_ver="0.18.0")
            notes_out = repo / "out" / "notes.md"
            ver, notes = check_package.check_release_tag(
                repo, "v0.18.0", write_notes=notes_out
            )
            self.assertEqual(ver, "0.18.0")
            self.assertIn("Hardened publish checks.", notes)
            self.assertNotIn("Previous release.", notes)
            self.assertEqual(notes_out.read_text(encoding="utf-8"), notes)

    def test_release_tag_mismatch_and_changelog_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _create_minimal_repo(repo, pyproject_ver="0.18.0", init_ver="0.18.0")

            # Missing leading 'v'
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_release_tag(repo, "0.18.0")

            # Tag does not match pyproject.toml / __init__.py
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_release_tag(repo, "v0.19.0")

            # pyproject.toml vs __init__.py drift
            (repo / "src" / "agent_voice_input" / "__init__.py").write_text(
                '__version__ = "0.17.0"\n', encoding="utf-8"
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_version_sync(repo)
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_release_tag(repo, "v0.18.0")

            # Restore __version__, break CHANGELOG (missing section and empty section)
            (repo / "src" / "agent_voice_input" / "__init__.py").write_text(
                '__version__ = "0.18.0"\n', encoding="utf-8"
            )
            (repo / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [Unreleased]\n\n## [0.17.0]\n- Old\n",
                encoding="utf-8",
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_release_tag(repo, "v0.18.0")

            (repo / "CHANGELOG.md").write_text(
                "# Changelog\n\n## [0.18.0] - 2026-11-27\n\n## [0.17.0]\n- Old\n",
                encoding="utf-8",
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.check_release_tag(repo, "v0.18.0")

    def test_win_shim_ascii_rejects_bom_and_non_ascii(self) -> None:
        with self.assertRaises(check_package.PackageCheckError):
            check_package.validate_ascii_bytes(b"\xef\xbb\xbfascii", "win_shim.ps1")
        with self.assertRaises(check_package.PackageCheckError):
            check_package.validate_ascii_bytes("中文".encode("utf-8"), "NativeBridge.cs")
        check_package.validate_ascii_bytes(b"pure 7-bit ascii\r\n", "ok.cs")

    def test_verify_distributions_valid_and_negative_cases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            dist = Path(td) / "dist"
            _create_minimal_repo(repo, pyproject_ver="0.18.0", init_ver="0.18.0")
            _build_fake_dists(repo, dist, version="0.18.0")

            summary = check_package.verify_distributions(
                repo, dist, expected_version="0.18.0"
            )
            self.assertEqual(summary["wheel_version"], "0.18.0")
            self.assertEqual(summary["sdist_version"], "0.18.0")

            # CLI exit 0 on happy path
            rc = check_package.main(
                [
                    "--repo-root",
                    str(repo),
                    "--dist-dir",
                    str(dist),
                    "--check-release-tag",
                    "v0.18.0",
                ]
            )
            self.assertEqual(rc, 0)

        # Reject scripts/check_package.py leaked into wheel
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            dist = Path(td) / "dist"
            _create_minimal_repo(repo)
            _build_fake_dists(
                repo,
                dist,
                extra_wheel_files={"scripts/check_package.py": b"print(1)\n"},
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.verify_distributions(repo, dist, expected_version="0.18.0")

        # Reject .agents/ leaked into sdist
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            dist = Path(td) / "dist"
            _create_minimal_repo(repo)
            _build_fake_dists(
                repo,
                dist,
                extra_sdist_files={".agents/memory/MEMORY.md": b"private\n"},
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.verify_distributions(repo, dist, expected_version="0.18.0")

        # Reject non-ASCII inside wheel win_shim.ps1
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            dist = Path(td) / "dist"
            _create_minimal_repo(repo)
            _build_fake_dists(
                repo,
                dist,
                omit_wheel_files={"agent_voice_input/win_shim.ps1"},
                extra_wheel_files={"agent_voice_input/win_shim.ps1": "# \xe4\xb8\xad\n".encode("latin1")},
            )
            with self.assertRaises(check_package.PackageCheckError):
                check_package.verify_distributions(repo, dist, expected_version="0.18.0")

        # Reject wheel version mismatch
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            dist = Path(td) / "dist"
            _create_minimal_repo(repo, pyproject_ver="0.18.0", init_ver="0.18.0")
            _build_fake_dists(repo, dist, version="0.18.0", wheel_version="0.17.0")
            with self.assertRaises(check_package.PackageCheckError):
                check_package.verify_distributions(repo, dist, expected_version="0.18.0")

    def test_run_twine_check_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dist = Path(td)
            (dist / "a.whl").write_bytes(b"whl")
            (dist / "a.tar.gz").write_bytes(b"tar")
            ok_proc = mock.Mock(returncode=0, stdout="Checking dist: PASSED\n", stderr="")
            with mock.patch.object(check_package.subprocess, "run", return_value=ok_proc) as m_run:
                out = check_package.run_twine_check(dist)
            self.assertIn("PASSED", out)
            cmd = m_run.call_args.args[0]
            self.assertEqual(cmd[1:4], ["-m", "twine", "check"])

            bad_proc = mock.Mock(returncode=1, stdout="", stderr="README syntax error")
            with mock.patch.object(check_package.subprocess, "run", return_value=bad_proc):
                with self.assertRaises(check_package.PackageCheckError):
                    check_package.run_twine_check(dist)


if __name__ == "__main__":
    unittest.main()
