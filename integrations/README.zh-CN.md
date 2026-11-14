[English](README.md) | [简体中文](README.zh-CN.md)

# Agent 终端标题与会话注册接入示例

`agent-voice-input` 支持在多个并行终端窗口中 **O(1) 精确锁定当前前台 Agent 会话**，抽取干净的最近对话与 `git status` 热词供语音识别和二阶段精修纠错，同时严格排除后台 subagent 或非交互子进程。

以下接入均为**可选增强**：
- 在 **macOS + Ghostty** 下，`agent-voice-input` 已可直接读取当前聚焦分屏的 `tty` 与 `cwd` 自动关联对应分屏内的 `codex`、`claude` 或 `agy` 会话，零钩子也不会串分屏（若在 `tmux` 或其他终端内使用，钩子在 macOS 下同样可通过 `ps` 祖先进程链找到 `/dev/ttys*` 正常工作）；
- 在 **Linux / WSL2** 下，未配置钩子时会自动回退到 `/proc` 前台根会话扫描与窗口标题 / 目录名消歧。

---

## 1. Codex CLI (`~/.codex/config.toml`)

将 [`codex/config.toml`](codex/config.toml) 中的 `terminal_title` 合并进 `~/.codex/config.toml` 的 `[tui]` 表：

```toml
[tui]
terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]
```

Codex 会在终端窗口标题末尾输出当前根会话的 `thread-id` UUID，`agent-voice-input` 可直接精确关联对应的 `rollout-*.jsonl`。

---

## 2. Claude Code (`~/.claude/settings.json`)

确保 `agent-voice-input` 已在 `PATH` 中（或替换为绝对路径，亦可参考 [`claude/voice-title.sh`](claude/voice-title.sh)），按 [`claude/settings.hooks.json`](claude/settings.hooks.json) 在 `~/.claude/settings.json` 中添加 `SessionStart` 与 `UserPromptSubmit` 钩子：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "agent-voice-input session-hook --cli claude"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "agent-voice-input session-hook --cli claude"
          }
        ]
      }
    ]
  }
}
```

钩子会：
1. 自动忽略带有 `agent_id` 或 `isSidechain` 的后台 subagent，并通过 `/proc`（Linux）或 `ps` 祖先链（macOS）拦截无头 `claude -p` / `--print` 调用；
2. 将主会话元数据（含规范化后的控制终端 `tty` 与根进程 `pid`）原子写入 `<runtime_dir>/sessions/claude-<id8>.json`（权限 `0600`）；
3. 沿父进程链定位控制终端字符设备（`/dev/pts/*` 或 `/dev/ttys*`）写入 OSC 2 标题 `claude · <short_cwd> [#<id8>]`，保持标准输出为空，绝不污染 Claude 上下文。

---

## 3. Antigravity CLI (`~/.gemini/hooks.json`)

参考 [`gemini/hooks.json`](gemini/hooks.json)，在 `~/.gemini/hooks.json` 中加入 `voice-session-title`：

```json
{
  "voice-session-title": {
    "SessionStart": [
      {
        "type": "command",
        "command": "agent-voice-input session-hook --cli agy",
        "timeout": 5
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "agent-voice-input session-hook --cli agy",
        "timeout": 5
      }
    ]
  }
}
```

钩子会结合 `presence/<cid>.lock` 文件锁、首行 JSONL 校验以及 `/proc`（Linux）或 `ps` 祖先进程链（macOS）严格拦截后台 subagent 与无头 `agy -p` 进程，将规范化 `tty` 记入 `<runtime_dir>/sessions/agy-<cid8>.json`，仅在前台交互式主会话中更新控制终端标题 `agy · <short_cwd> [#<cid8>]` 并返回标准空 JSON `{}`。
