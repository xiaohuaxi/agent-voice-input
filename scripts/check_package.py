#!/usr/bin/env python3
"""Unified package, ASCII shim, and release-tag verification for agent-voice-input.

Pure Python 3.10+ standard library script shared by ``ci.yml`` and ``publish.yml``.
Verifies:
1. Version consistency between ``pyproject.toml`` and ``src/agent_voice_input/__init__.py``
   (plus ``vX.Y.Z`` tag and non-empty ``CHANGELOG.md`` ``## [X.Y.Z]`` section when
   ``--check-release-tag`` is given).
2. Strict 7-bit ASCII (no UTF-8 BOM, no byte > 0x7F) for ``win_shim.ps1`` and
   ``win_shim/*.cs`` in both the source tree and built distributions.
3. ``dist/`` contains exactly one ``.whl`` and one ``.tar.gz`` with all required
   source/shim/doc/integration files, valid entry points & metadata version, and no
   junk, secret, or private agent files (``.agents/``, ``.claude/``, ``.env``, etc.).
4. Optional ``python -m twine check`` execution via ``--run-twine``.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
from typing import Any
import zipfile

try:  # pragma: no cover - Python 3.11+ stdlib
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]

CORE_WHEEL_ENTRIES: tuple[str, ...] = (
    "agent_voice_input/__init__.py",
    "agent_voice_input/cli.py",
    "agent_voice_input/daemon.py",
    "agent_voice_input/prompts.py",
    "agent_voice_input/app_scope.py",
    "agent_voice_input/http_transport.py",
    "agent_voice_input/llm_common.py",
    "agent_voice_input/platform_common.py",
    "agent_voice_input/workspace_ctx.py",
    "agent_voice_input/_remote_probe.py",
    "agent_voice_input/remote_ssh.py",
    "agent_voice_input/win_shim.ps1",
    "agent_voice_input/win_shim/NativeBridge.cs",
    "agent_voice_input/mac_shim/BridgeMain.swift",
)

CORE_SDIST_SUFFIXES: tuple[str, ...] = (
    "/LICENSE",
    "/AGENTS.md",
    "/CHANGELOG.md",
    "/ROADMAP.md",
    "/README.md",
    "/README.zh-CN.md",
    "/SECURITY.md",
    "/config.example.toml",
    "/config.example.env",
    "/bin/agent-voice-input",
    "/docs/context-routing.md",
    "/scripts/check_coverage.py",
    "/scripts/check_package.py",
    "/integrations/remote/remote-title-hook.sh",
    "/.github/workflows/ci.yml",
    "/.github/workflows/publish.yml",
    "/src/agent_voice_input/prompts.py",
    "/src/agent_voice_input/app_scope.py",
    "/src/agent_voice_input/http_transport.py",
    "/src/agent_voice_input/llm_common.py",
    "/src/agent_voice_input/platform_common.py",
    "/src/agent_voice_input/workspace_ctx.py",
    "/src/agent_voice_input/_remote_probe.py",
    "/src/agent_voice_input/remote_ssh.py",
    "/src/agent_voice_input/win_shim.ps1",
    "/src/agent_voice_input/win_shim/NativeBridge.cs",
    "/src/agent_voice_input/mac_shim/BridgeMain.swift",
)

_WHEEL_FORBIDDEN_DIRS = frozenset(
    {".agents", ".claude", ".codex", ".git", "__pycache__", "tests", "scripts"}
)
_SDIST_FORBIDDEN_DIRS = frozenset(
    {".agents", ".claude", ".codex", ".git", "__pycache__"}
)
_FORBIDDEN_FILENAMES = frozenset({".DS_Store", ".env"})
_FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".wav", ".mp3", ".db", ".sqlite", ".sqlite3")


class PackageCheckError(ValueError):
    """Raised when any package, ASCII, version, or release-tag check fails."""


def parse_pyproject_version(pyproject_path: Path) -> str:
    """Return ``[project].version`` from ``pyproject.toml``."""
    if not pyproject_path.is_file():
        raise PackageCheckError(f"missing pyproject.toml: {pyproject_path}")
    raw = pyproject_path.read_text(encoding="utf-8")
    if tomllib is not None:
        try:
            data = tomllib.loads(raw)
        except Exception as exc:
            raise PackageCheckError(f"failed to parse {pyproject_path}: {exc}") from exc
        proj = data.get("project")
        ver = proj.get("version") if isinstance(proj, dict) else None
        if isinstance(ver, str) and ver.strip():
            return ver.strip()
        raise PackageCheckError(f"[project].version missing in {pyproject_path}")

    # Python 3.10 stdlib fallback (no tomllib).
    in_project = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            m = re.match(r'^version\s*=\s*["\']([^"\']+)["\'](?:\s*#.*)?$', stripped)
            if m and m.group(1).strip():
                return m.group(1).strip()
    raise PackageCheckError(f"[project].version missing in {pyproject_path}")


def parse_init_version(init_path: Path) -> str:
    """Return top-level ``__version__`` string constant from ``__init__.py``."""
    if not init_path.is_file():
        raise PackageCheckError(f"missing __init__.py: {init_path}")
    raw = init_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(raw, filename=str(init_path))
    except SyntaxError as exc:
        raise PackageCheckError(f"failed to parse {init_path}: {exc}") from exc
    for node in tree.body:
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value_node = node.value
                    break
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__version__"
        ):
            value_node = node.value
        if (
            isinstance(value_node, ast.Constant)
            and isinstance(value_node.value, str)
            and value_node.value.strip()
        ):
            return value_node.value.strip()
    raise PackageCheckError(f"__version__ string constant missing in {init_path}")


def check_version_sync(repo_root: Path) -> str:
    """Ensure ``pyproject.toml`` and ``__init__.py`` declare the exact same version."""
    pyproject_ver = parse_pyproject_version(repo_root / "pyproject.toml")
    init_ver = parse_init_version(repo_root / "src" / "agent_voice_input" / "__init__.py")
    if pyproject_ver != init_ver:
        raise PackageCheckError(
            f"version mismatch: pyproject.toml={pyproject_ver!r} != __init__.__version__={init_ver!r}"
        )
    return pyproject_ver


def extract_changelog_section(changelog_path: Path, version: str) -> str:
    """Extract and validate a non-empty ``## [<version>]`` section from ``CHANGELOG.md``."""
    if not changelog_path.is_file():
        raise PackageCheckError(f"missing CHANGELOG.md: {changelog_path}")
    text = changelog_path.read_text(encoding="utf-8")
    ver_esc = re.escape(version)
    m = re.search(
        rf"^## \[{ver_esc}\](?=[ \t\-—–]|$)[^\n]*\n+(.*?)(?=^## \[|^\[[^\]\n]+\]:\s|\Z)",
        text,
        re.M | re.S,
    )
    if not m or not m.group(1).strip():
        raise PackageCheckError(
            f"CHANGELOG.md missing non-empty '## [{version}]' release section ({changelog_path})"
        )
    return m.group(1).strip() + "\n"


def check_release_tag(
    repo_root: Path,
    tag: str,
    *,
    write_notes: Path | None = None,
) -> tuple[str, str]:
    """Strictly verify ``vX.Y.Z`` tag against pyproject.toml, __init__.py, and CHANGELOG.md."""
    clean_tag = tag.strip()
    if not clean_tag.startswith("v") or len(clean_tag) < 2 or any(ch.isspace() for ch in clean_tag):
        raise PackageCheckError(
            f"release tag must start with 'v' followed by version without spaces, got {tag!r}"
        )
    tag_ver = clean_tag[1:]
    pyproject_ver = parse_pyproject_version(repo_root / "pyproject.toml")
    init_ver = parse_init_version(repo_root / "src" / "agent_voice_input" / "__init__.py")
    if not (tag_ver == pyproject_ver == init_ver):
        raise PackageCheckError(
            "release version mismatch: "
            f"tag={clean_tag!r} (version={tag_ver!r}), "
            f"pyproject.toml={pyproject_ver!r}, "
            f"__init__.__version__={init_ver!r}"
        )
    notes = extract_changelog_section(repo_root / "CHANGELOG.md", tag_ver)
    if write_notes is not None:
        write_notes.parent.mkdir(parents=True, exist_ok=True)
        write_notes.write_text(notes, encoding="utf-8")
    return tag_ver, notes


def validate_ascii_bytes(data: bytes, label: str) -> None:
    """Verify bytes contain no UTF-8 BOM and strictly 7-bit ASCII (<= 0x7F)."""
    if data.startswith(b"\xef\xbb\xbf"):
        raise PackageCheckError(f"UTF-8 BOM found in {label}")
    for offset, byte in enumerate(data):
        if byte > 0x7F:
            raise PackageCheckError(
                f"non-ASCII byte 0x{byte:02x} at offset {offset} in {label}"
            )


def check_source_win_shim_ascii(repo_root: Path) -> list[str]:
    """Validate ``win_shim.ps1`` and ``win_shim/*.cs`` in the source tree are 7-bit ASCII."""
    pkg_root = repo_root / "src" / "agent_voice_input"
    shim_ps1 = pkg_root / "win_shim.ps1"
    if not shim_ps1.is_file():
        raise PackageCheckError(f"missing source shim script: {shim_ps1}")
    cs_files = sorted(p for p in (pkg_root / "win_shim").glob("*.cs") if p.is_file())
    if not cs_files:
        raise PackageCheckError(f"no win_shim/*.cs files found under {pkg_root / 'win_shim'}")
    checked: list[str] = []
    for path in (shim_ps1, *cs_files):
        rel = path.relative_to(repo_root).as_posix()
        validate_ascii_bytes(path.read_bytes(), rel)
        checked.append(rel)
    return checked


def _find_forbidden_entries(
    rel_paths: set[str],
    *,
    forbidden_dirs: frozenset[str],
) -> list[str]:
    bad: list[str] = []
    for raw in sorted(rel_paths):
        norm = raw.lstrip("/")
        if not norm:
            continue
        parts = PurePosixPath(norm).parts
        if any(part in forbidden_dirs for part in parts):
            bad.append(raw)
            continue
        name = parts[-1]
        if name in _FORBIDDEN_FILENAMES or name.endswith(_FORBIDDEN_SUFFIXES):
            bad.append(raw)
    return bad


def _parse_metadata_version(metadata_text: str, label: str) -> str:
    for line in metadata_text.splitlines():
        if not line.strip():
            break
        if line.lower().startswith("version:"):
            val = line.split(":", 1)[1].strip()
            if val:
                return val
    raise PackageCheckError(f"missing Version header in {label}")


def verify_distributions(
    repo_root: Path,
    dist_dir: Path,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Verify sdist (.tar.gz) and wheel (.whl) artifacts under ``dist_dir``."""
    pkg_root = repo_root / "src" / "agent_voice_input"
    if not pkg_root.is_dir():
        raise PackageCheckError(f"missing package directory: {pkg_root}")
    if not dist_dir.is_dir():
        raise PackageCheckError(f"missing dist directory: {dist_dir}")

    pkg_files = sorted(
        p.name for p in pkg_root.iterdir() if p.is_file() and p.suffix in (".py", ".ps1")
    )
    mac_shim_files = sorted(
        p.relative_to(pkg_root).as_posix()
        for p in (pkg_root / "mac_shim").glob("*.swift")
        if p.is_file()
    )
    win_shim_files = sorted(
        p.relative_to(pkg_root).as_posix()
        for p in (pkg_root / "win_shim").glob("*.cs")
        if p.is_file()
    )
    if not pkg_files:
        raise PackageCheckError(f"no package files discovered under {pkg_root}")
    if not mac_shim_files:
        raise PackageCheckError(f"no mac_shim/*.swift files discovered under {pkg_root}")
    if not win_shim_files:
        raise PackageCheckError(f"no win_shim/*.cs files discovered under {pkg_root}")

    expected_wheel = {
        *CORE_WHEEL_ENTRIES,
        *(f"agent_voice_input/{name}" for name in pkg_files),
        *(f"agent_voice_input/{rel}" for rel in mac_shim_files),
        *(f"agent_voice_input/{rel}" for rel in win_shim_files),
    }

    whls = sorted(dist_dir.glob("*.whl"))
    if len(whls) != 1:
        raise PackageCheckError(
            f"expected exactly 1 wheel in {dist_dir}, got {[p.name for p in whls]}"
        )
    whl_path = whls[0]

    with zipfile.ZipFile(whl_path) as zf:
        names = set(zf.namelist())
        bad_wheel = _find_forbidden_entries(names, forbidden_dirs=_WHEEL_FORBIDDEN_DIRS)
        if bad_wheel:
            raise PackageCheckError(
                f"unexpected junk/private/test entries in wheel {whl_path.name}: {bad_wheel}"
            )
        if any(PurePosixPath(n).name == "check_package.py" for n in names):
            raise PackageCheckError(f"scripts/check_package.py must not be packaged in wheel {whl_path.name}")
        missing_wheel = sorted(req for req in expected_wheel if req not in names)
        if missing_wheel:
            raise PackageCheckError(
                f"missing required files in wheel {whl_path.name}: {missing_wheel}"
            )
        if not any("LICENSE" in n for n in names):
            raise PackageCheckError(f"missing LICENSE in wheel {whl_path.name}")
        eps = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        if not eps or "agent-voice-input = agent_voice_input.cli:main" not in zf.read(eps[0]).decode("utf-8"):
            raise PackageCheckError(f"missing or invalid console entry_points.txt in wheel {whl_path.name}")
        metas = [n for n in names if n.endswith(".dist-info/METADATA")]
        if not metas:
            raise PackageCheckError(f"missing .dist-info/METADATA in wheel {whl_path.name}")
        wheel_version = _parse_metadata_version(
            zf.read(metas[0]).decode("utf-8", errors="replace"),
            f"{whl_path.name}:{metas[0]}",
        )
        if expected_version is not None and wheel_version != expected_version:
            raise PackageCheckError(
                f"wheel METADATA version {wheel_version!r} != expected {expected_version!r}"
            )
        for rel in ("agent_voice_input/win_shim.ps1", *(f"agent_voice_input/{r}" for r in win_shim_files)):
            validate_ascii_bytes(zf.read(rel), f"{whl_path.name}:{rel}")

    integ_dir = repo_root / "integrations"
    docs_dir = repo_root / "docs"
    integ_rels = (
        sorted("/" + p.relative_to(repo_root).as_posix() for p in integ_dir.rglob("*") if p.is_file())
        if integ_dir.is_dir()
        else []
    )
    doc_rels = (
        sorted("/" + p.relative_to(repo_root).as_posix() for p in docs_dir.rglob("*.md") if p.is_file())
        if docs_dir.is_dir()
        else []
    )
    expected_sdist_suffixes = {
        *CORE_SDIST_SUFFIXES,
        *(f"/src/agent_voice_input/{name}" for name in pkg_files),
        *(f"/src/agent_voice_input/{rel}" for rel in mac_shim_files),
        *(f"/src/agent_voice_input/{rel}" for rel in win_shim_files),
        *integ_rels,
        *doc_rels,
    }

    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise PackageCheckError(
            f"expected exactly 1 sdist (.tar.gz) in {dist_dir}, got {[p.name for p in sdists]}"
        )
    sdist_path = sdists[0]

    with tarfile.open(sdist_path, "r:gz") as tf:
        members = tf.getmembers()
        rel_to_member: dict[str, tarfile.TarInfo] = {}
        rel_snames: set[str] = set()
        for member in members:
            if "/" not in member.name:
                continue
            rel = "/" + member.name.split("/", 1)[1]
            rel_snames.add(rel)
            rel_to_member[rel] = member

        bad_sdist = _find_forbidden_entries(rel_snames, forbidden_dirs=_SDIST_FORBIDDEN_DIRS)
        if bad_sdist:
            raise PackageCheckError(
                f"unexpected junk/private entries in sdist {sdist_path.name}: {bad_sdist}"
            )
        missing_sdist = sorted(suffix for suffix in expected_sdist_suffixes if suffix not in rel_snames)
        if missing_sdist:
            raise PackageCheckError(
                f"missing required paths in sdist {sdist_path.name}: {missing_sdist}"
            )
        pkg_info = rel_to_member.get("/PKG-INFO")
        if pkg_info is None:
            raise PackageCheckError(f"missing PKG-INFO in sdist {sdist_path.name}")
        pkg_info_fh = tf.extractfile(pkg_info)
        if pkg_info_fh is None:
            raise PackageCheckError(f"unreadable PKG-INFO in sdist {sdist_path.name}")
        sdist_version = _parse_metadata_version(
            pkg_info_fh.read().decode("utf-8", errors="replace"),
            f"{sdist_path.name}:PKG-INFO",
        )
        if expected_version is not None and sdist_version != expected_version:
            raise PackageCheckError(
                f"sdist PKG-INFO version {sdist_version!r} != expected {expected_version!r}"
            )
        for rel in (
            "/src/agent_voice_input/win_shim.ps1",
            *(f"/src/agent_voice_input/{r}" for r in win_shim_files),
        ):
            member = rel_to_member.get(rel)
            if member is None:
                raise PackageCheckError(f"missing {rel} in sdist {sdist_path.name}")
            fh = tf.extractfile(member)
            if fh is None:
                raise PackageCheckError(f"unreadable {rel} in sdist {sdist_path.name}")
            validate_ascii_bytes(fh.read(), f"{sdist_path.name}:{rel}")

    return {
        "wheel": whl_path.name,
        "sdist": sdist_path.name,
        "wheel_version": wheel_version,
        "sdist_version": sdist_version,
        "wheel_required_count": len(expected_wheel),
        "sdist_required_count": len(expected_sdist_suffixes),
    }


def run_twine_check(dist_dir: Path) -> str:
    """Run ``python -m twine check`` on built distributions under ``dist_dir``."""
    artifacts = sorted(
        str(p) for p in (*dist_dir.glob("*.whl"), *dist_dir.glob("*.tar.gz"))
    )
    if not artifacts:
        raise PackageCheckError(f"no distributions found in {dist_dir} for twine check")
    proc = subprocess.run(
        [sys.executable, "-m", "twine", "check", *artifacts],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        raise PackageCheckError(
            f"twine check failed (rc={proc.returncode}):\n{combined}"
        )
    return combined


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify agent-voice-input distributions, win_shim ASCII, and release tags."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root directory (default: script's parent repo).",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="Distribution directory containing .whl and .tar.gz (default: <repo-root>/dist).",
    )
    parser.add_argument(
        "--check-release-tag",
        default=None,
        help="Strictly verify vX.Y.Z tag against pyproject.toml, __init__.py, and CHANGELOG.md.",
    )
    parser.add_argument(
        "--write-release-notes",
        type=Path,
        default=None,
        help="Write validated CHANGELOG release notes to this file (requires --check-release-tag).",
    )
    parser.add_argument(
        "--skip-dist",
        action="store_true",
        help="Skip dist/ wheel & sdist archive inspection (run version/tag/ASCII checks only).",
    )
    parser.add_argument(
        "--run-twine",
        action="store_true",
        help="Also run 'python -m twine check' on built distributions under --dist-dir.",
    )
    args = parser.parse_args(argv)

    if args.write_release_notes is not None and not args.check_release_tag:
        parser.error("--write-release-notes requires --check-release-tag")

    repo_root = args.repo_root.resolve()
    dist_dir = (args.dist_dir if args.dist_dir is not None else repo_root / "dist").resolve()

    try:
        if args.check_release_tag:
            expected_ver, _notes = check_release_tag(
                repo_root,
                args.check_release_tag,
                write_notes=args.write_release_notes,
            )
            print(
                f"[ok] release tag {args.check_release_tag} matches pyproject.toml, "
                f"__init__.py, and CHANGELOG.md (version={expected_ver})"
            )
        else:
            expected_ver = check_version_sync(repo_root)
            print(f"[ok] version sync verified: {expected_ver}")

        ascii_files = check_source_win_shim_ascii(repo_root)
        print(f"[ok] source win_shim 7-bit ASCII verified ({len(ascii_files)} files)")

        if not args.skip_dist:
            summary = verify_distributions(
                repo_root, dist_dir, expected_version=expected_ver
            )
            print(
                f"[ok] distributions verified: wheel={summary['wheel']} "
                f"({summary['wheel_required_count']} required), "
                f"sdist={summary['sdist']} ({summary['sdist_required_count']} required)"
            )
        if args.run_twine:
            twine_out = run_twine_check(dist_dir)
            if twine_out:
                print(twine_out)
            print("[ok] twine check passed")
    except PackageCheckError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
