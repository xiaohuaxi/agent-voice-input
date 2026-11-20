"""Architectural contract guards: module DAG, mock.patch liveness, HUD parity, providers, and terminals."""
from __future__ import annotations

import ast
import builtins
import dis
import importlib
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PKG_DIR = SRC_DIR / "agent_voice_input"
TESTS_DIR = REPO_ROOT / "tests"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from agent_voice_input import hud as hud_mod  # noqa: E402
from agent_voice_input.app_scope import (  # noqa: E402
    DEDICATED_SSH_CLIENT_PATTERNS,
    MACOS_TERMINAL_BUNDLE_IDS,
    MACOS_TERMINALS,
    WINDOWS_CTRL_SHIFT_V_PROCS,
    WINDOWS_SHIFT_INSERT_PROCS,
    WINDOWS_TERMINALS,
    is_windows_terminal_process,
    resolve_windows_paste_chord,
)
from agent_voice_input.asr_providers import BaseAsrProvider, create_asr_provider  # noqa: E402
from agent_voice_input.config import (  # noqa: E402
    ASR_AUTO_PRIORITY,
    CREDENTIAL_ENV_ORDER,
    DEFAULT_ASR_BASE_URLS,
    DEFAULT_ASR_MODELS,
    DEFAULT_REFINER_BASE_URLS,
    DEFAULT_REFINER_MODELS,
    REFINER_AUTO_PRIORITY,
    VALID_ASR_PROVIDERS,
    VALID_REFINER_PROVIDERS,
    AsrConfig,
    Config,
    RefinerConfig,
    resolve_credentials,
)
from agent_voice_input.refiner import BaseRefiner, NoopRefiner, create_refiner  # noqa: E402

# Python 3.10 does not list Python 3.11+ ``tomllib`` in ``sys.stdlib_module_names``.
_ALLOWED_STDLIB_MODULES: frozenset[str] = frozenset(
    set(sys.stdlib_module_names) | {"__future__", "tomllib"}
)

MACOS_ONLY_TERMINALS: frozenset[str] = frozenset(
    {"terminal", "终端", "iterm2", "iterm"}
)
WINDOWS_ONLY_TERMINALS: frozenset[str] = frozenset(
    {"windowsterminal", "openconsole", "mintty", "conhost", "putty"}
)
_PENDING_WINDOWS_TERMINAL_PARITY: frozenset[str] = frozenset()


def _parse_package_asts() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for py_path in sorted(PKG_DIR.glob("*.py")):
        trees[py_path.stem] = ast.parse(
            py_path.read_text(encoding="utf-8"), filename=str(py_path)
        )
    return trees


def _collect_compiled_runtime_loads(
    src: str, filename: str = "<mem>"
) -> tuple[set[str], set[str]]:
    """Return ``(loaded_module_globals, loaded_dotted_attrs)`` from compiled function/method bytecode.

    Uses CPython's own symbol-table compiler (``LOAD_GLOBAL`` + consecutive ``LOAD_ATTR``/``LOAD_METHOD``)
    inside runtime callables/lambdas/comprehensions, while skipping module top-level and ``class`` body
    code objects executed at import time. This handles nested ``def``, ``global``, ``nonlocal``,
    parameter shadowing, and import-time default arguments with 100% Python language fidelity.
    """
    top_code = compile(src, filename, "exec")
    names: set[str] = set()
    dotted: set[str] = set()

    def _is_class_body(code_obj: types.CodeType) -> bool:
        return "__qualname__" in code_obj.co_names and "__module__" in code_obj.co_names

    def _walk(code_obj: types.CodeType, *, is_runtime_fn: bool) -> None:
        if is_runtime_fn:
            chain: list[str] = []
            for instr in dis.get_instructions(code_obj):
                op = instr.opname
                if op == "LOAD_GLOBAL":
                    sym = str(instr.argval)
                    names.add(sym)
                    chain = [sym]
                elif op in ("LOAD_ATTR", "LOAD_METHOD") and chain:
                    chain.append(str(instr.argval))
                    dotted.add(".".join(chain))
                elif op not in ("EXTENDED_ARG", "NOP"):
                    chain = []
        for const in code_obj.co_consts:
            if isinstance(const, types.CodeType):
                _walk(const, is_runtime_fn=not _is_class_body(const))

    _walk(top_code, is_runtime_fn=False)
    return names, dotted


def _verify_module_patch_chain(
    *,
    mod_obj: types.ModuleType | None,
    fn_names: set[str],
    fn_dotted: set[str],
    attr_parts: list[str],
) -> str | None:
    """Return violation reason when ``attr_parts`` is missing or dangling in ``mod_obj`` / AST."""
    if not attr_parts:
        return "empty attribute chain"
    top_attr = attr_parts[0]
    if mod_obj is not None:
        cur: object = mod_obj
        for seg in attr_parts:
            if not hasattr(cur, seg):
                return f"attribute segment {seg!r} does not exist at runtime"
            cur = getattr(cur, seg)

    if len(attr_parts) == 1:
        if top_attr not in fn_names:
            return (
                f"{top_attr!r} is never read inside any function/method body "
                "(dangling re-export, unused constant, or import-time default arg)"
            )
        return None

    first_obj = getattr(mod_obj, top_attr, None) if mod_obj is not None else None
    if isinstance(first_obj, types.ModuleType):
        full_dotted = ".".join(attr_parts)
        if full_dotted not in fn_dotted:
            return (
                f"submodule attribute {full_dotted!r} is never read inside any "
                "function/method body of this module"
            )
    return None


class TestModuleImportDagAndProbeIsolation(unittest.TestCase):
    def test_intra_package_imports_form_strict_dag(self) -> None:
        trees = _parse_package_asts()
        known_modules = set(trees.keys())
        self.assertEqual(len(known_modules), 23)
        init_defined_symbols = {
            t.id
            for stmt in trees["__init__"].body
            if isinstance(stmt, ast.Assign)
            for t in stmt.targets
            if isinstance(t, ast.Name)
        }

        edges: dict[str, set[str]] = {m: set() for m in known_modules}
        unknown_targets: list[str] = []

        for mod_name, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if parts[0] == "agent_voice_input" and len(parts) >= 2:
                            target = parts[1]
                            if target not in known_modules:
                                unknown_targets.append(f"{mod_name}: {alias.name}")
                            elif target != mod_name:
                                edges[mod_name].add(target)
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        if node.level > 1:
                            unknown_targets.append(
                                f"{mod_name}: parent-relative import level={node.level}"
                            )
                            continue
                        if node.module:
                            target = node.module.split(".")[0]
                            if target not in known_modules:
                                unknown_targets.append(f"{mod_name}: .{node.module}")
                            elif target != mod_name:
                                edges[mod_name].add(target)
                        else:
                            for alias in node.names:
                                if alias.name in known_modules:
                                    if alias.name != mod_name:
                                        edges[mod_name].add(alias.name)
                                elif alias.name in init_defined_symbols:
                                    if mod_name != "__init__":
                                        edges[mod_name].add("__init__")
                                else:
                                    unknown_targets.append(f"{mod_name}: from . import {alias.name}")
                    elif node.module:
                        parts = node.module.split(".")
                        if parts[0] == "agent_voice_input":
                            if len(parts) >= 2:
                                target = parts[1]
                                if target not in known_modules:
                                    unknown_targets.append(f"{mod_name}: {node.module}")
                                elif target != mod_name:
                                    edges[mod_name].add(target)
                            else:
                                for alias in node.names:
                                    if alias.name in known_modules and alias.name != mod_name:
                                        edges[mod_name].add(alias.name)

        self.assertEqual(unknown_targets, [], f"Unknown intra-package imports: {unknown_targets}")

        # 3-color DFS cycle detection across BOTH top-level and lazy function-level imports.
        color: dict[str, int] = {m: 0 for m in known_modules}  # 0=unvisited, 1=visiting, 2=done
        stack: list[str] = []
        cycles: list[list[str]] = []

        def _dfs(u: str) -> None:
            color[u] = 1
            stack.append(u)
            for v in sorted(edges[u]):
                if color[v] == 1:
                    idx = stack.index(v)
                    cycles.append(stack[idx:] + [v])
                elif color[v] == 0:
                    _dfs(v)
            stack.pop()
            color[u] = 2

        for mod_name in sorted(known_modules):
            if color[mod_name] == 0:
                _dfs(mod_name)

        self.assertEqual(cycles, [], f"Circular module dependencies detected: {cycles}")
        self.assertEqual(
            edges["_remote_probe"],
            set(),
            "_remote_probe.py must have zero intra-package dependencies",
        )
        for mod_name in (
            "refiner",
            "platform_common",
            "platform_wsl",
            "platform_macos",
            "session_ctx",
            "workspace_ctx",
        ):
            if mod_name in edges:
                self.assertNotIn(
                    "asr_providers",
                    edges[mod_name],
                    f"{mod_name}.py must not import asr_providers; use http_transport or llm_common",
                )
        for mod_name in ("platform_macos", "refiner", "platform_common"):
            self.assertNotIn(
                "platform_wsl",
                edges[mod_name],
                f"{mod_name}.py must not import platform_wsl; use platform_common",
            )

    def test_session_ctx_and_remote_probe_have_zero_duplicate_top_level_definitions(self) -> None:
        trees = _parse_package_asts()
        ctx_tree = trees["session_ctx"]
        probe_tree = trees["_remote_probe"]

        ctx_Callables = {
            n.name for n in ctx_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        probe_callables = {
            n.name for n in probe_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertEqual(
            sorted(ctx_Callables & probe_callables),
            [],
            "Duplicate functions/classes re-introduced between session_ctx.py and _remote_probe.py; "
            "import shared pure helpers from ._remote_probe instead of copy-pasting.",
        )

        def _assigned_names(tree: ast.Module) -> set[str]:
            out: set[str] = set()
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            out.add(t.id)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    out.add(stmt.target.id)
            return out

        self.assertEqual(
            sorted(_assigned_names(ctx_tree) & _assigned_names(probe_tree)),
            [],
            "Duplicate top-level constants re-introduced between session_ctx.py and _remote_probe.py",
        )

    def test_zero_undefined_module_globals_in_compiled_bytecode(self) -> None:
        builtin_names = set(dir(builtins))
        missing_globals: list[str] = []

        def _walk_code(code_obj: types.CodeType, mod_name: str, mod_ns: set[str]) -> None:
            for instr in dis.get_instructions(code_obj):
                if instr.opname == "LOAD_GLOBAL":
                    sym = str(instr.argval)
                    if sym not in mod_ns and sym not in builtin_names:
                        missing_globals.append(
                            f"{mod_name}:{code_obj.co_name}@L{code_obj.co_firstlineno} -> {sym!r}"
                        )
            for const in code_obj.co_consts:
                if isinstance(const, types.CodeType):
                    _walk_code(const, mod_name, mod_ns)

        for py_path in sorted(PKG_DIR.glob("*.py")):
            stem = py_path.stem
            mod = importlib.import_module(
                "agent_voice_input" if stem == "__init__" else f"agent_voice_input.{stem}"
            )
            mod_symbols = {k for k in set(mod.__dict__.keys()) | set(dir(mod)) if hasattr(mod, k)}
            compiled = compile(py_path.read_text(encoding="utf-8"), str(py_path), "exec")
            _walk_code(compiled, stem, mod_symbols)

        self.assertEqual(
            missing_globals,
            [],
            "Undefined global symbol references in compiled bytecode:\n  "
            + "\n  ".join(missing_globals),
        )

    def test_remote_probe_and_package_use_stdlib_only(self) -> None:
        trees = _parse_package_asts()
        probe_tree = trees["_remote_probe"]
        probe_stdlib = set(sys.stdlib_module_names) | {"__future__"}

        for node in ast.walk(probe_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    self.assertIn(
                        root,
                        probe_stdlib,
                        f"_remote_probe.py must be standalone stdlib-only, got import {alias.name!r}",
                    )
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(
                    node.level,
                    0,
                    f"_remote_probe.py must not use relative imports (line {node.lineno})",
                )
                root = (node.module or "").split(".")[0]
                self.assertIn(
                    root,
                    probe_stdlib,
                    f"_remote_probe.py must be standalone stdlib-only, got from {node.module!r}",
                )

        non_stdlib: list[str] = []
        for mod_name, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root != "agent_voice_input" and root not in _ALLOWED_STDLIB_MODULES:
                            non_stdlib.append(f"{mod_name}:{node.lineno} import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    root = node.module.split(".")[0]
                    if root != "agent_voice_input" and root not in _ALLOWED_STDLIB_MODULES:
                        non_stdlib.append(f"{mod_name}:{node.lineno} from {node.module}")

        self.assertEqual(non_stdlib, [], f"Unexpected third-party runtime imports: {non_stdlib}")

    def test_cli_session_hook_fast_path_avoids_heavy_imports_and_no_kwarg_probe_shell(self) -> None:
        import subprocess

        code = (
            "import sys; "
            "from agent_voice_input.cli import main; "
            "sys.stdin = __import__('io').StringIO('{}'); "
            "rc = main(['session-hook', '--cli', 'claude', '--quiet']); "
            "assert rc == 0; "
            "heavy = [m for m in ('asr_providers', 'refiner', 'daemon', 'http_transport', "
            "'platform_common', 'platform_wsl', 'platform_macos', 'audio_proc', 'hud', 'prompts') "
            "if f'agent_voice_input.{m}' in sys.modules]; "
            "assert not heavy, f'session-hook loaded heavy modules: {heavy}'"
        )
        p = subprocess.run(
            [sys.executable, "-c", code],
            env={"PYTHONPATH": str(SRC_DIR)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(p.returncode, 0, p.stderr or p.stdout)

        patch_code = "\n".join(
            [
                "import io, sys",
                "from unittest.mock import patch",
                "import agent_voice_input.cli as cli",
                "assert not hasattr(cli, '_LAZY_CLI_CACHE')",
                "assert not hasattr(cli, '_LAZY_CLI_ATTR_NAMES')",
                "assert '__getattr__' not in cli.__dict__",
                "with patch('agent_voice_input.session_title.run_session_hook', return_value=37) as m_hook:",
                "    assert cli.main(['session-hook', '--cli', 'claude', '--quiet']) == 37",
                "    m_hook.assert_called_once_with(cli_mode='claude', quiet_stdout=True)",
                "with patch('agent_voice_input._remote_probe.run_probe', return_value={'ok': True}) as m_rp:",
                "    fake_in = io.TextIOWrapper(io.BytesIO(b'{}'))",
                "    fake_out = io.TextIOWrapper(io.BytesIO())",
                "    with patch.object(sys, 'stdin', fake_in), patch.object(sys, 'stdout', fake_out):",
                "        assert cli.main(['remote-ctx', '--raw-json']) == 0",
                "    m_rp.assert_called_once()",
            ]
        )
        p_patch = subprocess.run(
            [sys.executable, "-c", patch_code],
            env={"PYTHONPATH": str(SRC_DIR)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(p_patch.returncode, 0, p_patch.stderr or p_patch.stdout)

        for py_path in sorted(PKG_DIR.glob("*.py")):
            src_txt = py_path.read_text(encoding="utf-8")
            self.assertNotIn(
                "_callable_accepts_kwarg",
                src_txt,
                f"{py_path.name} still references legacy _callable_accepts_kwarg shell",
            )
            self.assertNotIn(
                "_deliver_with_origin",
                src_txt,
                f"{py_path.name} still references legacy _deliver_with_origin reflection shim",
            )


def _collect_function_local_import_targets(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """Collect ``(target_module, symbol)`` imported at call time via function-local ``from .mod import sym``."""
    out: dict[str, set[str]] = {m: set() for m in trees}

    def _walk_stmt(node: ast.AST, *, in_fn: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in node.body:
                _walk_stmt(child, in_fn=True)
            return
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                _walk_stmt(child, in_fn=False)
            return
        if in_fn and isinstance(node, ast.ImportFrom):
            target_mod = ""
            if node.level == 1 and node.module:
                target_mod = node.module.split(".")[0]
            elif node.level == 0 and node.module and node.module.startswith("agent_voice_input."):
                target_mod = node.module.split(".", 1)[1].split(".")[0]
            if target_mod in out:
                for alias in node.names:
                    out[target_mod].add(alias.name)
        for child in ast.iter_child_nodes(node):
            _walk_stmt(child, in_fn=in_fn)

    for tree in trees.values():
        for stmt in tree.body:
            _walk_stmt(stmt, in_fn=False)
    return out


class TestMockPatchTargetGuards(unittest.TestCase):
    def test_all_suite_mock_patches_hit_live_runtime_references(self) -> None:
        trees = _parse_package_asts()
        fn_loads = {
            p.stem: _collect_compiled_runtime_loads(
                p.read_text(encoding="utf-8"), filename=str(p)
            )
            for p in sorted(PKG_DIR.glob("*.py"))
        }
        for mod_stem, local_syms in _collect_function_local_import_targets(trees).items():
            fn_loads[mod_stem][0].update(local_syms)
        loaded_mods = {
            m: importlib.import_module(f"agent_voice_input.{m}") for m in trees
        }

        checked_count = 0
        violations: list[str] = []

        for test_path in sorted(TESTS_DIR.glob("test_*.py")):
            test_tree = ast.parse(
                test_path.read_text(encoding="utf-8"), filename=str(test_path)
            )
            mod_aliases: dict[str, str] = {}
            imported_symbols: dict[str, tuple[str, str]] = {}

            for node in ast.walk(test_tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "agent_voice_input":
                            mod_aliases[alias.asname or "agent_voice_input"] = "__pkg__"
                        elif alias.name.startswith("agent_voice_input."):
                            sub = alias.name.split(".", 1)[1]
                            if "." not in sub and sub in trees:
                                mod_aliases[alias.asname or alias.name] = sub
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "agent_voice_input":
                        for alias in node.names:
                            if alias.name in trees:
                                mod_aliases[alias.asname or alias.name] = alias.name
                    elif node.module and node.module.startswith("agent_voice_input."):
                        sub = node.module.split(".", 1)[1]
                        if sub in trees:
                            for alias in node.names:
                                imported_symbols[alias.asname or alias.name] = (
                                    sub,
                                    alias.name,
                                )

            for node in ast.walk(test_tree):
                if not isinstance(node, ast.Call):
                    continue
                fn_expr = ast.unparse(node.func)
                resolved: tuple[str, list[str], str] | None = None

                if fn_expr in ("patch", "mock.patch", "unittest.mock.patch") and node.args:
                    arg0 = node.args[0]
                    if (
                        isinstance(arg0, ast.Constant)
                        and isinstance(arg0.value, str)
                        and arg0.value.startswith("agent_voice_input.")
                    ):
                        parts = arg0.value.split(".")[1:]
                        if not parts or parts[0] not in trees:
                            violations.append(
                                f"{test_path.name}:{node.lineno} unknown module in patch({arg0.value!r})"
                            )
                            continue
                        resolved = (parts[0], parts[1:], arg0.value)

                elif (
                    fn_expr in ("patch.object", "mock.patch.object", "unittest.mock.patch.object")
                    and len(node.args) >= 2
                ):
                    obj_expr = ast.unparse(node.args[0])
                    arg1 = node.args[1]
                    if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str):
                        parts = obj_expr.split(".")
                        if parts[0] in mod_aliases and mod_aliases[parts[0]] != "__pkg__":
                            resolved = (
                                mod_aliases[parts[0]],
                                parts[1:] + [arg1.value],
                                f"{obj_expr}.{arg1.value}",
                            )
                        elif (
                            len(parts) >= 2
                            and parts[0] in mod_aliases
                            and mod_aliases[parts[0]] == "__pkg__"
                            and parts[1] in trees
                        ):
                            resolved = (
                                parts[1],
                                parts[2:] + [arg1.value],
                                f"{obj_expr}.{arg1.value}",
                            )
                        elif parts[0] in imported_symbols:
                            sub, orig_sym = imported_symbols[parts[0]]
                            resolved = (
                                sub,
                                [orig_sym] + parts[1:] + [arg1.value],
                                f"{obj_expr}.{arg1.value}",
                            )

                if resolved is not None:
                    mod_stem, attr_parts, raw_desc = resolved
                    checked_count += 1
                    names, dotted = fn_loads[mod_stem]
                    err = _verify_module_patch_chain(
                        mod_obj=loaded_mods[mod_stem],
                        fn_names=names,
                        fn_dotted=dotted,
                        attr_parts=attr_parts,
                    )
                    if err is not None:
                        violations.append(
                            f"{test_path.name}:{node.lineno} [{mod_stem}] {raw_desc}: {err}"
                        )

        self.assertGreaterEqual(checked_count, 200)
        self.assertEqual(
            violations,
            [],
            "Dangling or invalid mock.patch targets found:\n  " + "\n  ".join(violations),
        )

    def test_dangling_reexport_and_default_arg_trap_detector(self) -> None:
        fake_mod = types.ModuleType("fake_mod")
        fake_mod.helper = lambda: 1  # type: ignore[attr-defined]
        fake_mod.os = importlib.import_module("os")  # type: ignore[attr-defined]

        dangling_src = """
from .other import helper
import os
__all__ = ["helper"]
legacy_alias = helper

class _Holder:
    cls_attr = helper

def only_default_arg(cb=helper):
    return os.stat(".")

def shadowed_local_param(helper):
    return helper()

def nested_local_def():
    def helper():
        return 2
    return helper()
"""
        names, dotted = _collect_compiled_runtime_loads(dangling_src)
        self.assertIsNotNone(
            _verify_module_patch_chain(
                mod_obj=fake_mod,
                fn_names=names,
                fn_dotted=dotted,
                attr_parts=["helper"],
            )
        )
        self.assertIsNotNone(
            _verify_module_patch_chain(
                mod_obj=fake_mod,
                fn_names=names,
                fn_dotted=dotted,
                attr_parts=["os", "scandir"],
            )
        )
        self.assertIsNone(
            _verify_module_patch_chain(
                mod_obj=fake_mod,
                fn_names=names,
                fn_dotted=dotted,
                attr_parts=["os", "stat"],
            )
        )

        live_src = """
from .other import helper

def outer(helper):
    def inner():
        global helper
        return helper()
    return inner()
"""
        live_names, live_dotted = _collect_compiled_runtime_loads(live_src)
        self.assertIsNone(
            _verify_module_patch_chain(
                mod_obj=fake_mod,
                fn_names=live_names,
                fn_dotted=live_dotted,
                attr_parts=["helper"],
            )
        )


class TestHudCrossPlatformContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mac_swift = (PKG_DIR / "mac_shim.swift").read_text(encoding="utf-8")
        cls.win_ps1 = (PKG_DIR / "win_shim.ps1").read_text(encoding="utf-8")

    def test_hud_layer_routing_matches_across_python_macos_and_windows(self) -> None:
        expected_control = set(hud_mod._CONTROL_HUD_STATES)
        expected_main = set(hud_mod._MAIN_HUD_STATES)

        # Extract macOS resolveHudLayer block
        m_mac = re.search(
            r"func resolveHudLayer\([^{]+\{(.*?)\n\}",
            self.mac_swift,
            re.DOTALL,
        )
        self.assertIsNotNone(m_mac)
        mac_body = m_mac.group(1)  # type: ignore[union-attr]
        mac_ctrl_line = mac_body.split("return \"control\"", 1)[0]
        mac_control = set(re.findall(r'st\s*==\s*"([^"]*)"', mac_ctrl_line))
        if "st.isEmpty" in mac_ctrl_line:
            mac_control.add("")
        m_mac_main = re.search(r'case\s+([^\n:]+):\s*\n\s*return\s+"main"', mac_body)
        self.assertIsNotNone(m_mac_main)
        mac_main = set(re.findall(r'"([^"]+)"', m_mac_main.group(1)))  # type: ignore[union-attr]

        # Extract Windows ResolveHudLayer block
        m_win = re.search(
            r"public static string ResolveHudLayer\([^{]+\{(.*?)\n    \}",
            self.win_ps1,
            re.DOTALL,
        )
        self.assertIsNotNone(m_win)
        win_body = m_win.group(1)  # type: ignore[union-attr]
        win_ctrl_line = win_body.split('return "control"', 1)[0]
        win_control = set(re.findall(r'st\s*==\s*"([^"]*)"', win_ctrl_line))
        win_main_line = win_body.split('return "main"', 1)[0].rsplit("if (", 1)[-1]
        win_main = set(re.findall(r'st\s*==\s*"([^"]+)"', win_main_line))

        self.assertEqual(mac_control, expected_control)
        self.assertEqual(win_control, expected_control)
        self.assertEqual(mac_main, expected_main)
        self.assertEqual(win_main, expected_main)

        for st in expected_control:
            self.assertEqual(hud_mod.hud_layer_for_state(st), "control")
        for st in expected_main:
            self.assertEqual(hud_mod.hud_layer_for_state(st), "main")
        for st in set(hud_mod._STATIC_HUD_COPY.keys()) - expected_main:
            self.assertEqual(hud_mod.hud_layer_for_state(st), "toast")

    def test_hud_visual_states_and_durations_match_across_three_ends(self) -> None:
        expected_visual_states = {"recording"} | set(hud_mod._STATIC_HUD_COPY.keys())

        # Parse mac_shim.swift resolveHudVisualStyle switch cases and autoHideMs
        m_mac_style = re.search(
            r"func resolveHudVisualStyle\([^{]+\{(.*?)\n\}",
            self.mac_swift,
            re.DOTALL,
        )
        self.assertIsNotNone(m_mac_style)
        mac_style_body = m_mac_style.group(1)  # type: ignore[union-attr]
        mac_durations: dict[str, int] = {}
        for case_match in re.finditer(
            r"case\s+([^\n:]+):(.*?)(?=\n\s*(?:case\s+|default:))",
            mac_style_body,
            re.DOTALL,
        ):
            labels = re.findall(r'"([^"]+)"', case_match.group(1))
            dur_m = re.search(r"autoHideMs:\s*(\d+)", case_match.group(2))
            self.assertIsNotNone(dur_m, f"Missing autoHideMs for {labels}")
            dur_val = int(dur_m.group(1))  # type: ignore[union-attr]
            for lb in labels:
                mac_durations[lb] = dur_val

        m_mac_default = re.search(
            r"default:(.*)",
            mac_style_body,
            re.DOTALL,
        )
        self.assertIsNotNone(m_mac_default)
        mac_default_ms = int(
            re.search(r"autoHideMs:\s*(\d+)", m_mac_default.group(1)).group(1)  # type: ignore[union-attr]
        )

        # Parse win_shim.ps1 ResolveHudVisualStyle branches and ms values
        m_win_style = re.search(
            r"private static void ResolveHudVisualStyle\([^{]+\{(.*?)\n    \}",
            self.win_ps1,
            re.DOTALL,
        )
        self.assertIsNotNone(m_win_style)
        win_style_body = m_win_style.group(1)  # type: ignore[union-attr]
        win_durations: dict[str, int] = {}
        for br_match in re.finditer(
            r'(?:else\s+)?if\s*\((st\s*==\s*"[^)]+)\)\s*\{(.*?)\}',
            win_style_body,
            re.DOTALL,
        ):
            labels = re.findall(r'st\s*==\s*"([^"]+)"', br_match.group(1))
            dur_m = re.search(r"\bms\s*=\s*(\d+)\s*;", br_match.group(2))
            dur_val = int(dur_m.group(1)) if dur_m else 0
            for lb in labels:
                win_durations[lb] = dur_val

        m_win_else = re.search(
            r"\}\s*else\s*\{([^}]*Transcription failed[^}]*)\}",
            win_style_body,
            re.DOTALL,
        )
        self.assertIsNotNone(m_win_else)
        win_default_ms = int(
            re.search(r"\bms\s*=\s*(\d+)\s*;", m_win_else.group(1)).group(1)  # type: ignore[union-attr]
        )

        self.assertEqual(set(mac_durations.keys()), expected_visual_states)
        self.assertEqual(set(win_durations.keys()), expected_visual_states)
        for st in sorted(expected_visual_states):
            py_ms = hud_mod.default_hud_duration_ms(st)
            self.assertEqual(mac_durations[st], py_ms, f"macOS duration mismatch for {st!r}")
            self.assertEqual(win_durations[st], py_ms, f"Windows duration mismatch for {st!r}")

        self.assertEqual(mac_default_ms, hud_mod.DEFAULT_ERROR_HUD_DURATION_MS)
        self.assertEqual(win_default_ms, hud_mod.DEFAULT_ERROR_HUD_DURATION_MS)

    def test_toast_layer_is_single_slot_without_queue_stacking(self) -> None:
        # macOS: single optional NSPanel slot + in-place timer cancellation
        self.assertRegex(self.mac_swift, r"private\s+var\s+toastPanel:\s*NSPanel\?")
        self.assertRegex(
            self.mac_swift,
            r"private\s+func\s+updateToast\([^{]+\{\s*toastHideWorkItem\?\.cancel\(\)",
        )
        self.assertNotRegex(self.mac_swift, r"\[\s*NSPanel\s*\].*toast|toastQueue|toastStack")

        # Windows: single static HudPillForm _toastHud reused in-place via ShowStatus
        self.assertRegex(self.win_ps1, r"private\s+static\s+HudPillForm\s+_toastHud\s*;")
        self.assertIn("_toastHud.ShowStatus(", self.win_ps1)
        self.assertNotRegex(
            self.win_ps1,
            r"(?:Queue|List|Stack)<\s*HudPillForm\s*>",
        )

        # Mouse pass-through + updated recording prompt copy parity across all three ends
        self.assertIn("p.ignoresMouseEvents = true", self.mac_swift)
        self.assertIn("0x00000020", self.win_ps1)
        self.assertEqual(
            hud_mod.format_hud_text("recording", "zh", hotkey="RControl", trigger_mode="tap_toggle"),
            "● 录音中… (按右 Ctrl 结束)",
        )
        self.assertEqual(
            hud_mod.format_hud_text("recording", "en", hotkey="RControl", trigger_mode="tap_toggle"),
            "● Listening… (press Right Ctrl to end)",
        )
        self.assertIn(r" \u7ED3\u675F)", self.win_ps1)
        self.assertIn(" to end)", self.win_ps1)
        self.assertNotIn(" to finish)", self.win_ps1)
        self.assertIn(" 结束)", self.mac_swift)
        self.assertIn(" to end)", self.mac_swift)
        self.assertNotIn(" to finish)", self.mac_swift)

        # Hold-to-cancel bilingual preview hint + cancellable state set + real-time FSM parity across all three ends
        zh_hold, en_hold = hud_mod.HOLD_CANCEL_HINT_COPY
        self.assertEqual(hud_mod.format_hold_cancel_hint("zh"), zh_hold)
        self.assertEqual(hud_mod.format_hold_cancel_hint("en"), en_hold)
        self.assertIn(zh_hold, self.mac_swift)
        self.assertIn(en_hold, self.mac_swift)
        self.assertIn(r"\u7EE7\u7EED\u6309\u4F4F\u53D6\u6D88\u2026", self.win_ps1)
        self.assertIn(r"keep holding to cancel\u2026", self.win_ps1)
        self.assertIn("OnHoldCancelTimeout", self.win_ps1)
        self.assertIn("onHoldCancelTimeout", self.mac_swift)
        self.assertIn("PruneStaleOtherKeys", self.win_ps1)
        self.assertIn("hasAnyNonModifierKeyPhysicallyDown", self.mac_swift)
        self.assertIn("preserveHoldPreview", self.win_ps1)
        self.assertIn("HoldCancelMs", self.win_ps1)
        self.assertIn("--hold-cancel-ms", self.mac_swift)
        self.assertIn("RunHotkeyFsmSelfCheck", self.win_ps1)
        self.assertIn("hotkey_fsm_checks", self.win_ps1)
        self.assertIn("hotkey_fsm_checks", self.mac_swift)


class TestProviderCompletenessContracts(unittest.TestCase):
    def test_asr_providers_complete_across_models_credentials_and_factory(self) -> None:
        concrete_asr = VALID_ASR_PROVIDERS - {"auto"}
        self.assertEqual(set(DEFAULT_ASR_MODELS.keys()), concrete_asr)
        self.assertEqual(len(ASR_AUTO_PRIORITY), len(set(ASR_AUTO_PRIORITY)))
        self.assertEqual(set(ASR_AUTO_PRIORITY), concrete_asr - {"openai-compatible"})
        self.assertEqual(
            set(DEFAULT_ASR_BASE_URLS.keys()),
            concrete_asr - {"dashscope", "openai-compatible"},
        )

        # Populate synthetic hermetic env with every known vendor credential
        hermetic_env = {
            names[0]: f"test-secret-{group}"
            for group, names in CREDENTIAL_ENV_ORDER.items()
        }
        creds = resolve_credentials(Config(), hermetic_env)

        for prov in sorted(concrete_asr):
            self.assertIsNotNone(
                creds.for_provider(prov),
                f"ProviderCredentials.for_provider({prov!r}) returned None",
            )
            base_url = "https://example.test/v1" if prov == "openai-compatible" else ""
            cfg = Config(asr=AsrConfig(provider=prov, base_url=base_url))
            instance = create_asr_provider(
                cfg,
                creds,
                env=hermetic_env,
                transport=lambda req: (_ for _ in ()).throw(AssertionError("offline test")),
            )
            self.assertIsInstance(instance, BaseAsrProvider)

    def test_refiner_providers_complete_across_models_credentials_and_factory(self) -> None:
        concrete_refiner = VALID_REFINER_PROVIDERS - {"auto", "off"}
        self.assertEqual(
            set(DEFAULT_REFINER_MODELS.keys()),
            concrete_refiner - {"openai-compatible"},
        )
        self.assertEqual(len(REFINER_AUTO_PRIORITY), len(set(REFINER_AUTO_PRIORITY)))
        self.assertEqual(
            set(REFINER_AUTO_PRIORITY),
            concrete_refiner - {"codex", "openai-compatible"},
        )
        self.assertEqual(
            set(DEFAULT_REFINER_BASE_URLS.keys()),
            concrete_refiner - {"codex", "openai-compatible"},
        )

        hermetic_env = {
            names[0]: f"test-secret-{group}"
            for group, names in CREDENTIAL_ENV_ORDER.items()
        }
        creds = resolve_credentials(Config(), hermetic_env)

        with tempfile.TemporaryDirectory() as td:
            rt_dir = Path(td)
            noop = create_refiner(
                Config(refiner=RefinerConfig(provider="off")),
                creds,
                env=hermetic_env,
                runtime_dir=rt_dir,
            )
            self.assertIsInstance(noop, NoopRefiner)

            for prov in sorted(concrete_refiner):
                if prov != "codex":
                    self.assertIsNotNone(
                        creds.for_provider(prov),
                        f"ProviderCredentials.for_provider({prov!r}) returned None",
                    )
                base_url = "https://example.test/v1" if prov == "openai-compatible" else ""
                model = "test-model" if prov == "openai-compatible" else ""
                cfg = Config(
                    refiner=RefinerConfig(provider=prov, base_url=base_url, model=model)
                )
                instance = create_refiner(
                    cfg,
                    creds,
                    env=hermetic_env,
                    runtime_dir=rt_dir,
                    transport=lambda req: (_ for _ in ()).throw(AssertionError("offline test")),
                )
                self.assertIsInstance(instance, BaseRefiner)
                self.assertNotIsInstance(instance, NoopRefiner)

    def test_vendor_default_base_url_literals_live_solely_in_config(self) -> None:
        canonical_urls = (
            set(DEFAULT_ASR_BASE_URLS.values())
            | set(DEFAULT_REFINER_BASE_URLS.values())
            | {
                "https://chatgpt.com/backend-api/codex",
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
            }
        )
        violations: list[str] = []
        for py_path in sorted(PKG_DIR.glob("*.py")):
            if py_path.stem == "config":
                continue
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    s = node.value.strip().rstrip("/")
                    for u in canonical_urls:
                        if s == u or s.startswith(u + "/"):
                            violations.append(f"{py_path.name}:{node.lineno} -> {node.value!r}")
        self.assertEqual(
            violations,
            [],
            f"Hardcoded vendor default URLs found outside config.py: {violations}",
        )


class TestCrossPlatformTerminalSymmetry(unittest.TestCase):
    def test_desktop_terminal_sets_are_symmetric_modulo_explicit_platform_sets(self) -> None:
        self.assertTrue(MACOS_ONLY_TERMINALS.issubset(MACOS_TERMINALS))
        self.assertTrue(MACOS_ONLY_TERMINALS.isdisjoint(WINDOWS_TERMINALS))
        self.assertTrue(WINDOWS_ONLY_TERMINALS.issubset(WINDOWS_TERMINALS))
        self.assertTrue(WINDOWS_ONLY_TERMINALS.isdisjoint(MACOS_TERMINALS))

        self.assertEqual(_PENDING_WINDOWS_TERMINAL_PARITY, frozenset())
        macos_cross_platform = MACOS_TERMINALS - MACOS_ONLY_TERMINALS
        windows_cross_platform = WINDOWS_TERMINALS - WINDOWS_ONLY_TERMINALS
        self.assertEqual(
            macos_cross_platform,
            windows_cross_platform,
            "Cross-platform terminal set drifted between MACOS_TERMINALS and WINDOWS_TERMINALS; "
            "update both platforms or declare genuinely OS-exclusive apps in *_ONLY_TERMINALS.",
        )
        self.assertIn("warp", WINDOWS_TERMINALS)
        self.assertTrue(is_windows_terminal_process("warp.exe"))
        self.assertFalse(is_windows_terminal_process("mintty.app"))
        self.assertEqual(resolve_windows_paste_chord("warp.exe"), "ctrl_shift_v")

        # Verify win_shim.ps1 $AllowedTerminals default array stays aligned with WINDOWS_TERMINALS
        # and accepts -AllowedTerminals CLI override from Python WslPlatform._build_shim_cmd
        win_ps1 = (PKG_DIR / "win_shim.ps1").read_text(encoding="utf-8")
        m_shim = re.search(r"\$AllowedTerminals\s*=\s*@\(([^)]+)\)", win_ps1)
        self.assertIsNotNone(m_shim)
        shim_terms = set(re.findall(r'"([^"]+)"', m_shim.group(1)))  # type: ignore[union-attr]
        self.assertEqual(shim_terms, set(WINDOWS_TERMINALS))
        self.assertIn('[Alias("AllowedTerminals")]', win_ps1)

        # Every Windows paste-chord override proc must be an allowed terminal, dedicated SSH GUI, or emacs
        dedicated_ssh = {
            p.rstrip("*") for p in DEDICATED_SSH_CLIENT_PATTERNS if "." not in p
        }
        paste_procs = set(WINDOWS_SHIFT_INSERT_PROCS) | set(WINDOWS_CTRL_SHIFT_V_PROCS)
        unaccounted = paste_procs - (set(WINDOWS_TERMINALS) | dedicated_ssh | {"emacs"})
        self.assertEqual(unaccounted, set())

        self.assertGreaterEqual(len(MACOS_TERMINAL_BUNDLE_IDS), 10)


if __name__ == "__main__":
    unittest.main()
