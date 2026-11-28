"""Lightweight console-script entrypoint with pre-import Python 3.10+ version guard.

Intentionally uses only Python 3.0-compatible syntax and ``sys`` so older
interpreters print a clear upgrade message instead of failing with a
SyntaxError or TypeError while importing package modules.
"""
import sys


def _format_python_version_error():
    ver = "{0}.{1}.{2}".format(
        sys.version_info[0], sys.version_info[1], sys.version_info[2]
    )
    return (
        "agent-voice-input requires Python 3.10+ (found {0} at {1}).\n"
        "Fix: install Python 3.10+ (macOS: 'brew install python'), "
        "or install with uv (auto-manages Python): 'uv tool install agent-voice-input'.\n".format(
            ver, sys.executable
        )
    )


def main(argv=None):
    if sys.version_info < (3, 10):
        sys.stderr.write(_format_python_version_error())
        return 1
    from agent_voice_input.cli import main as _cli_main

    return _cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
