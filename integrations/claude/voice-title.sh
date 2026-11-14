#!/usr/bin/env bash
# Optional standalone wrapper for Claude Code hooks if agent-voice-input is in ~/.local/bin
set -u
export PATH="$HOME/.local/bin:$PATH"
if command -v agent-voice-input >/dev/null 2>&1; then
    exec agent-voice-input session-hook --cli claude
fi
exit 0
