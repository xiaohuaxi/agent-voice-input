#!/usr/bin/env python3
"""Tiered statement and pure-branch coverage gate for agent-voice-input.

Supports two execution modes using zero runtime package dependencies:
1. Primary mode (when ``coverage`` is installed, e.g. CI or a temporary venv):
   Runs ``coverage run --branch`` over ``unittest discover -s tests`` and computes
   independent **Statement %** (``covered_lines / num_statements``) and **Pure Branch %**
   (``covered_branches / num_branches``) per module and per tier—never relying on
   ``coverage.py``'s blended percentage column.
2. Stdlib fallback mode (``--stdlib-trace`` or when ``coverage`` is not installed locally
   and ``--require-branch`` is not set):
   Uses Python's built-in ``sys.settrace``/``threading.settrace`` + ``ast`` modules to verify
   statement coverage gates and ``# pragma: no cover - <reason>`` hygiene without third-party packages.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import tokenize
from types import FrameType
from typing import Any
import unittest
import warnings

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PKG_DIR = SRC_DIR / "agent_voice_input"
TESTS_DIR = REPO_ROOT / "tests"

TIER_1_MODULES: tuple[str, ...] = (
    "__init__",
    "hud",
    "app_scope",
    "prompts",
    "llm_common",
    "config",
)

TIER_2_MODULES: tuple[str, ...] = (
    "daemon",
    "http_transport",
    "session_ctx",
    "_remote_probe",
    "remote_ssh",
    "workspace_ctx",
    "session_title",
    "_tmux",
    "audio_proc",
)

TIER_3_MODULES: tuple[str, ...] = (
    "platform_common",
    "platform_wsl",
    "platform_macos",
    "asr_providers",
    "refiner",
    "cli",
)

TIER_1_MIN_STMT_PCT = 95.0
TIER_1_MIN_BRANCH_PCT = 90.0
TIER_2_MIN_STMT_PCT = 85.0
TIER_2_MIN_BRANCH_PCT = 74.0
TOTAL_MIN_STMT_PCT = 85.0
TOTAL_MIN_BRANCH_PCT = 74.0

_NO_COVER_TOKEN_RE = re.compile(r"#\s*pragma:\s*no\s+cover\b", re.IGNORECASE)
_NO_COVER_WITH_REASON_RE = re.compile(
    r"#\s*pragma:\s*no\s+cover\s*[-:]\s*\S+", re.IGNORECASE
)


@dataclass(frozen=True)
class ModuleMetrics:
    name: str
    covered_stmts: int
    total_stmts: int
    covered_branches: int | None = None
    total_branches: int | None = None

    @property
    def stmt_pct(self) -> float:
        if self.total_stmts <= 0:
            return 100.0
        return (self.covered_stmts / self.total_stmts) * 100.0

    @property
    def branch_pct(self) -> float | None:
        if self.covered_branches is None or self.total_branches is None:
            return None
        if self.total_branches <= 0:
            return 100.0
        return (self.covered_branches / self.total_branches) * 100.0


def _aggregate_metrics(name: str, items: list[ModuleMetrics]) -> ModuleMetrics:
    cov_s = sum(m.covered_stmts for m in items)
    tot_s = sum(m.total_stmts for m in items)
    if items and all(m.covered_branches is not None and m.total_branches is not None for m in items):
        cov_b = sum(m.covered_branches or 0 for m in items)
        tot_b = sum(m.total_branches or 0 for m in items)
        return ModuleMetrics(name, cov_s, tot_s, cov_b, tot_b)
    return ModuleMetrics(name, cov_s, tot_s, None, None)


def check_pragma_no_cover_hygiene(pkg_dir: Path = PKG_DIR) -> tuple[int, list[str]]:
    """Return ``(total_count, violations)`` across ``pkg_dir/*.py``.

    Every ``# pragma: no cover`` comment must carry an explicit reason on the same line
    (e.g. ``# pragma: no cover - py310 fallback import``). No arbitrary numeric cap is imposed.
    """
    count = 0
    violations: list[str] = []
    for py_path in sorted(pkg_dir.glob("*.py")):
        raw_bytes = py_path.read_bytes()
        for tok in tokenize.tokenize(io.BytesIO(raw_bytes).readline):
            if tok.type != tokenize.COMMENT:
                continue
            if _NO_COVER_TOKEN_RE.search(tok.string):
                count += 1
                if not _NO_COVER_WITH_REASON_RE.search(tok.string):
                    violations.append(
                        f"{py_path.name}:{tok.start[0]} missing reason in {tok.string.strip()!r} "
                        "(expected '# pragma: no cover - <reason>')"
                    )
    return count, violations


def _verify_module_partition(discovered: set[str]) -> list[str]:
    configured = set(TIER_1_MODULES) | set(TIER_2_MODULES) | set(TIER_3_MODULES)
    errors: list[str] = []
    missing = sorted(discovered - configured)
    extra = sorted(configured - discovered)
    if missing:
        errors.append(f"Unclassified package modules missing from tier table: {missing}")
    if extra:
        errors.append(f"Tier table references non-existent modules: {extra}")
    return errors


def _run_coverage_py_mode(*, verbose_tests: bool = False) -> dict[str, ModuleMetrics]:
    with tempfile.TemporaryDirectory(prefix="avi-cov-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        data_file = tmp_path / ".coverage"
        json_file = tmp_path / "coverage.json"
        env = dict(os.environ)
        existing_pp = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        env["COVERAGE_FILE"] = str(data_file)

        discover_args = ["-m", "unittest", "discover", "-s", str(TESTS_DIR)]
        if verbose_tests:
            discover_args.append("-v")

        run_cmd = [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-m",
            "coverage",
            "run",
            f"--data-file={data_file}",
            "--branch",
            f"--source={PKG_DIR}",
            *discover_args,
        ]
        proc = subprocess.run(
            run_cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=None if verbose_tests else subprocess.PIPE,
            stderr=None if verbose_tests else subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            if not verbose_tests and proc.stdout:
                sys.stdout.write(proc.stdout)
            raise SystemExit(proc.returncode)

        json_cmd = [
            sys.executable,
            "-m",
            "coverage",
            "json",
            f"--data-file={data_file}",
            "-o",
            str(json_file),
        ]
        proc_json = subprocess.run(
            json_cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_json.returncode != 0:
            sys.stderr.write(proc_json.stderr or proc_json.stdout)
            raise SystemExit(proc_json.returncode)

        payload = json.loads(json_file.read_text(encoding="utf-8"))

    files_map = payload.get("files", {})
    if not isinstance(files_map, dict):
        sys.stderr.write("ERROR: malformed coverage JSON report (missing 'files' mapping).\n")
        raise SystemExit(2)

    required_keys = ("covered_lines", "num_statements", "covered_branches", "num_branches")
    metrics: dict[str, ModuleMetrics] = {}
    for py_path in sorted(PKG_DIR.glob("*.py")):
        stem = py_path.stem
        matched_summary = None
        resolved_target = py_path.resolve()
        for raw_file, info in files_map.items():
            cand = (REPO_ROOT / raw_file).resolve() if not os.path.isabs(raw_file) else Path(raw_file).resolve()
            if cand == resolved_target:
                matched_summary = info.get("summary") if isinstance(info, dict) else None
                break
        if not isinstance(matched_summary, dict):
            sys.stderr.write(
                f"ERROR: module {py_path.name} missing from coverage JSON report.\n"
            )
            raise SystemExit(2)
        missing_fields = [k for k in required_keys if k not in matched_summary]
        if missing_fields:
            sys.stderr.write(
                f"ERROR: module {py_path.name} missing summary fields {missing_fields} "
                "(ensure coverage was run with --branch).\n"
            )
            raise SystemExit(2)
        metrics[stem] = ModuleMetrics(
            name=stem,
            covered_stmts=int(matched_summary["covered_lines"]),
            total_stmts=int(matched_summary["num_statements"]),
            covered_branches=int(matched_summary["covered_branches"]),
            total_branches=int(matched_summary["num_branches"]),
        )
    return metrics


def _no_cover_comment_lines(raw_bytes: bytes) -> set[int]:
    lines: set[int] = set()
    for tok in tokenize.tokenize(io.BytesIO(raw_bytes).readline):
        if tok.type == tokenize.COMMENT and _NO_COVER_TOKEN_RE.search(tok.string):
            lines.add(tok.start[0])
    return lines


def _is_docstring_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _ast_executable_lines(py_path: Path) -> tuple[set[int], set[int]]:
    """Return ``(executable_statement_lines, excluded_lines)`` for stdlib ``trace`` mode."""
    raw_bytes = py_path.read_bytes()
    src = raw_bytes.decode("utf-8")
    tree = ast.parse(src, filename=str(py_path))
    no_cover_lines = _no_cover_comment_lines(raw_bytes)
    excluded: set[int] = set()
    stmts: set[int] = set()

    def _exclude_span(start: int, end: int | None) -> None:
        stop = end if end is not None and end >= start else start
        excluded.update(range(start, stop + 1))

    def _header_has_no_cover(node: ast.AST, body: list[ast.stmt]) -> bool:
        start = getattr(node, "lineno", 0)
        if not start:
            return False
        first_body_line = body[0].lineno if body else getattr(node, "end_lineno", start)
        end_hdr = max(start, first_body_line - 1 if first_body_line > start else start)
        return any(ln in no_cover_lines for ln in range(start, end_hdr + 1))

    def _walk_body(body: list[ast.stmt], *, allow_docstring: bool) -> None:
        for idx, stmt in enumerate(body):
            if idx == 0 and allow_docstring and _is_docstring_expr(stmt):
                continue
            _walk_stmt(stmt)

    def _walk_stmt(stmt: ast.stmt) -> None:
        if isinstance(stmt, ast.If) and _is_main_guard(stmt):
            _exclude_span(stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno))
            return

        child_blocks: list[tuple[list[ast.stmt], bool]] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _header_has_no_cover(stmt, stmt.body):
                _exclude_span(stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno))
                return
            stmts.add(stmt.lineno)
            _walk_body(stmt.body, allow_docstring=True)
            return

        if isinstance(stmt, ast.If):
            if _header_has_no_cover(stmt, stmt.body):
                for s in stmt.body:
                    _exclude_span(s.lineno, getattr(s, "end_lineno", s.lineno))
            else:
                stmts.add(stmt.lineno)
                child_blocks.append((stmt.body, False))
            if stmt.orelse:
                if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
                    _walk_stmt(stmt.orelse[0])
                else:
                    else_start = getattr(stmt.body[-1], "end_lineno", stmt.lineno) + 1
                    first_else_line = stmt.orelse[0].lineno
                    if first_else_line > else_start and any(
                        ln in no_cover_lines for ln in range(else_start, first_else_line)
                    ):
                        for s in stmt.orelse:
                            _exclude_span(s.lineno, getattr(s, "end_lineno", s.lineno))
                    else:
                        child_blocks.append((stmt.orelse, False))
            for blk, allow_doc in child_blocks:
                _walk_body(blk, allow_docstring=allow_doc)
            return

        if isinstance(stmt, ast.Try):
            if _header_has_no_cover(stmt, stmt.body):
                _exclude_span(stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno))
                return
            stmts.add(stmt.lineno)
            _walk_body(stmt.body, allow_docstring=False)
            for handler in stmt.handlers:
                if _header_has_no_cover(handler, handler.body):
                    _exclude_span(handler.lineno, getattr(handler, "end_lineno", handler.lineno))
                else:
                    stmts.add(handler.lineno)
                    _walk_body(handler.body, allow_docstring=False)
            if stmt.orelse:
                _walk_body(stmt.orelse, allow_docstring=False)
            if stmt.finalbody:
                _walk_body(stmt.finalbody, allow_docstring=False)
            return

        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            if _header_has_no_cover(stmt, stmt.body):
                _exclude_span(stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno))
                return
            stmts.add(stmt.lineno)
            _walk_body(stmt.body, allow_docstring=False)
            if hasattr(stmt, "orelse") and stmt.orelse:
                _walk_body(stmt.orelse, allow_docstring=False)
            return

        end_ln = getattr(stmt, "end_lineno", stmt.lineno)
        if any(ln in no_cover_lines for ln in range(stmt.lineno, end_ln + 1)):
            _exclude_span(stmt.lineno, end_ln)
            return
        if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
            # Bare type annotations without runtime assignment are often skipped by frame tracing.
            return
        stmts.add(stmt.lineno)

    _walk_body(tree.body, allow_docstring=True)
    return stmts - excluded, excluded


def _run_stdlib_trace_mode(*, verbose_tests: bool = False) -> dict[str, ModuleMetrics]:
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))

    # Evict already-imported package modules so module-level definitions are traced cleanly.
    for mod_name in list(sys.modules):
        if mod_name == "agent_voice_input" or mod_name.startswith("agent_voice_input."):
            sys.modules.pop(mod_name, None)

    pkg_files: dict[str, set[int]] = {}
    for py_path in sorted(PKG_DIR.glob("*.py")):
        abs_key = os.path.abspath(str(py_path))
        real_key = os.path.realpath(abs_key)
        bucket: set[int] = set()
        pkg_files[abs_key] = bucket
        pkg_files[real_key] = bucket

    code_cache: dict[object, Any] = {}

    def _fast_pkg_tracer(frame: FrameType, event: str, _arg: Any) -> Any:
        if event != "call":
            return None
        code = frame.f_code
        cached = code_cache.get(code)
        if cached is False:
            return None
        if cached is not None:
            return cached
        line_set = pkg_files.get(os.path.abspath(code.co_filename))
        if line_set is None:
            code_cache[code] = False
            return None

        def _line_tracer(f: FrameType, ev: str, _a: Any) -> Any:
            if ev == "line":
                line_set.add(f.f_lineno)
            return _line_tracer

        code_cache[code] = _line_tracer
        line_set.add(frame.f_lineno)
        return _line_tracer

    prev_sys_trace = sys.gettrace()
    threading.settrace(_fast_pkg_tracer)
    sys.settrace(_fast_pkg_tracer)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            suite = unittest.defaultTestLoader.discover(str(TESTS_DIR))
            runner = unittest.TextTestRunner(verbosity=2 if verbose_tests else 1)
            result = runner.run(suite)
    finally:
        sys.settrace(prev_sys_trace)
        threading.settrace(None)

    if not result.wasSuccessful():
        raise SystemExit(1)

    metrics: dict[str, ModuleMetrics] = {}
    for py_path in sorted(PKG_DIR.glob("*.py")):
        exec_lines, _ = _ast_executable_lines(py_path)
        hits = pkg_files.get(os.path.realpath(str(py_path)), set())
        covered = len(exec_lines & hits)
        metrics[py_path.stem] = ModuleMetrics(
            name=py_path.stem,
            covered_stmts=covered,
            total_stmts=len(exec_lines),
            covered_branches=None,
            total_branches=None,
        )
    return metrics


def _format_pct(val: float | None) -> str:
    return f"{val:6.2f}%" if val is not None else "   n/a "


def _format_ratio(cov: int | None, tot: int | None) -> str:
    if cov is None or tot is None:
        return "        n/a"
    return f"{cov:5d}/{tot:<5d}"


def _print_and_evaluate(
    metrics: dict[str, ModuleMetrics],
    *,
    has_branch: bool,
    no_cover_count: int,
    no_cover_violations: list[str],
) -> list[str]:
    failures: list[str] = []
    failures.extend(_verify_module_partition(set(metrics.keys())))
    failures.extend(no_cover_violations)

    mode_label = (
        "coverage.py --branch (independent Statement % + Pure Branch %)"
        if has_branch
        else "stdlib settrace + ast (Statement % fast-check; install 'coverage' for Pure Branch %)"
    )
    print(f"\n=== agent-voice-input Tiered Coverage Report [{mode_label}] ===")
    header = (
        f"{'Module / Tier':<28} | {'Stmts':>11} | {'Stmt %':>7} | "
        f"{'Branches':>11} | {'Pure Br %':>9} | {'Gate'}"
    )
    sep = "-" * len(header)
    print(sep)
    print(header)
    print(sep)

    tiers = (
        ("Tier 1 (Pure Core)", TIER_1_MODULES, TIER_1_MIN_STMT_PCT, TIER_1_MIN_BRANCH_PCT, True),
        ("Tier 2 (Concurrency & I/O)", TIER_2_MODULES, TIER_2_MIN_STMT_PCT, TIER_2_MIN_BRANCH_PCT, False),
        ("Tier 3 (Platform & Cloud)", TIER_3_MODULES, None, None, False),
    )

    all_items: list[ModuleMetrics] = []
    for tier_title, mod_names, min_stmt, min_br, enforce_per_module in tiers:
        tier_items = [metrics[m] for m in mod_names if m in metrics]
        all_items.extend(tier_items)
        for m in tier_items:
            mod_ok = True
            if enforce_per_module and min_stmt is not None:
                if m.stmt_pct + 1e-9 < min_stmt:
                    mod_ok = False
                    failures.append(
                        f"Tier 1 module {m.name}.py statement coverage {m.stmt_pct:.2f}% < {min_stmt:.1f}%"
                    )
                if has_branch and min_br is not None and (m.branch_pct or 0.0) + 1e-9 < min_br:
                    mod_ok = False
                    failures.append(
                        f"Tier 1 module {m.name}.py pure branch coverage {m.branch_pct:.2f}% < {min_br:.1f}%"
                    )
            gate_str = ("PASS" if mod_ok else "FAIL") if enforce_per_module else "-"
            print(
                f"  {m.name + '.py':<26} | {_format_ratio(m.covered_stmts, m.total_stmts)} | "
                f"{_format_pct(m.stmt_pct)} | {_format_ratio(m.covered_branches, m.total_branches)} | "
                f"{_format_pct(m.branch_pct):>9} | {gate_str}"
            )

        agg = _aggregate_metrics(tier_title, tier_items)
        tier_ok = True
        if min_stmt is not None and agg.stmt_pct + 1e-9 < min_stmt:
            tier_ok = False
            failures.append(
                f"{tier_title} aggregate statement coverage {agg.stmt_pct:.2f}% < {min_stmt:.1f}%"
            )
        if has_branch and min_br is not None and (agg.branch_pct or 0.0) + 1e-9 < min_br:
            tier_ok = False
            failures.append(
                f"{tier_title} aggregate pure branch coverage {agg.branch_pct:.2f}% < {min_br:.1f}%"
            )
        tier_gate = (
            f"{'PASS' if tier_ok else 'FAIL'} (>={min_stmt:.0f}%/{min_br:.0f}%)"
            if min_stmt is not None and min_br is not None
            else "info (CI multi-OS)"
        )
        print(
            f"> {tier_title:<26} | {_format_ratio(agg.covered_stmts, agg.total_stmts)} | "
            f"{_format_pct(agg.stmt_pct)} | {_format_ratio(agg.covered_branches, agg.total_branches)} | "
            f"{_format_pct(agg.branch_pct):>9} | {tier_gate}"
        )
        print(sep)

    total_agg = _aggregate_metrics("Repository Total", all_items)
    total_ok = True
    if total_agg.stmt_pct + 1e-9 < TOTAL_MIN_STMT_PCT:
        total_ok = False
        failures.append(
            f"Repository total statement coverage {total_agg.stmt_pct:.2f}% < {TOTAL_MIN_STMT_PCT:.1f}%"
        )
    if has_branch and (total_agg.branch_pct or 0.0) + 1e-9 < TOTAL_MIN_BRANCH_PCT:
        total_ok = False
        failures.append(
            f"Repository total pure branch coverage {total_agg.branch_pct:.2f}% < {TOTAL_MIN_BRANCH_PCT:.1f}%"
        )
    print(
        f"* {'Repository Total':<26} | {_format_ratio(total_agg.covered_stmts, total_agg.total_stmts)} | "
        f"{_format_pct(total_agg.stmt_pct)} | {_format_ratio(total_agg.covered_branches, total_agg.total_branches)} | "
        f"{_format_pct(total_agg.branch_pct):>9} | "
        f"{'PASS' if total_ok else 'FAIL'} (>={TOTAL_MIN_STMT_PCT:.0f}%/{TOTAL_MIN_BRANCH_PCT:.0f}%)"
    )
    print(sep)
    hygiene_status = "PASS" if not no_cover_violations else f"FAIL ({len(no_cover_violations)} missing reason)"
    print(f"Pragma 'no cover' annotations in src/: {no_cover_count} ({hygiene_status})")
    if not has_branch:
        print(
            "Tip: run inside an isolated venv with `pip install coverage` to verify Pure Branch % gates."
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run unit tests and enforce tiered statement & pure-branch coverage gates."
    )
    parser.add_argument(
        "--stdlib-trace",
        action="store_true",
        help="Force zero-dependency stdlib trace + AST statement coverage mode.",
    )
    parser.add_argument(
        "--require-branch",
        action="store_true",
        help="Require third-party 'coverage' package so Pure Branch %% gates are strictly enforced.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Pass -v to unittest runner.",
    )
    args = parser.parse_args(argv)

    if args.stdlib_trace and args.require_branch:
        parser.error("--stdlib-trace and --require-branch are mutually exclusive")

    has_cov_pkg = importlib.util.find_spec("coverage") is not None
    if args.require_branch and not has_cov_pkg:
        sys.stderr.write(
            "ERROR: --require-branch specified, but 'coverage' package is not installed in "
            f"{sys.executable}.\n"
        )
        return 2

    use_branch_mode = has_cov_pkg and not args.stdlib_trace
    no_cover_count, no_cover_violations = check_pragma_no_cover_hygiene()

    if use_branch_mode:
        metrics = _run_coverage_py_mode(verbose_tests=args.verbose)
    else:
        metrics = _run_stdlib_trace_mode(verbose_tests=args.verbose)

    failures = _print_and_evaluate(
        metrics,
        has_branch=use_branch_mode,
        no_cover_count=no_cover_count,
        no_cover_violations=no_cover_violations,
    )
    if failures:
        sys.stderr.write("\nCoverage gate failed:\n")
        for item in failures:
            sys.stderr.write(f"  - {item}\n")
        return 1

    print("\nAll coverage and hygiene gates PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
