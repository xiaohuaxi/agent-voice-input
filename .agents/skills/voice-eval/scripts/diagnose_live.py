#!/usr/bin/env python3
"""Compatibility wrapper delegating live diagnostics to ``agent_voice_input.diagnose``."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agent_voice_input.config import load_config  # noqa: E402
from agent_voice_input.diagnose import (  # noqa: E402
    diagnose_record_flags as _diagnose_record_flags,
    mask_session_key as _mask_session_key,
    probe_optional_legacy_codex_sqlite as _probe_optional_legacy_codex_sqlite,
    run_diagnose_command,
)

__all__ = [
    "_diagnose_record_flags",
    "_mask_session_key",
    "_probe_optional_legacy_codex_sqlite",
    "main",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sub-second read-only live voice-input diagnostics.")
    ap.add_argument("--config", help="Optional path to config.toml")
    ap.add_argument("--runtime-dir", default="", help="Override runtime_dir path")
    ap.add_argument("--window-title", default="", help="Optional window title hint for session matching check")
    ap.add_argument("--last", type=int, default=10, help="Number of recent utterances to inspect (default: 10)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable redacted JSON instead of Markdown")
    args = ap.parse_args(argv)
    cfg, env = load_config(config_path=args.config or None)
    return run_diagnose_command(
        cfg,
        env,
        runtime_dir=args.runtime_dir,
        window_title=args.window_title,
        last=args.last,
        as_json=bool(args.json),
    )


if __name__ == "__main__":
    raise SystemExit(main())
