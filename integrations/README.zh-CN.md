[English](README.md) | [简体中文](README.zh-CN.md)

# Agent 终端标题与会话注册接入示例

`agent-voice-input` 支持在多个并行终端窗口中 **O(1) 精确锁定当前前台 Agent 会话**，抽取干净的最近对话与 `git status` 热词供语音识别和二阶段精修纠错，同时严格排除后台 subagent 或非交互子进程。

以下接入均为**可选增强**：
- **在 `tmux` 分屏内**，Linux/WSL2 与 macOS 下无需修改 `~/.tmux.conf` 即可开箱自动锁定当前活跃客户端的焦点窗格（隔离隔壁后台分屏干扰）；
- 在 **macOS + Ghostty** 下，`agent-voice-input` 已可直接读取当前聚焦分屏的 `tty` 与 `cwd` 自动关联对应分屏内的 `codex`、`claude` 或 `agy` 会话，零钩子也不会串分屏（含 Ghostty 连接 `tmux` 的场景）；
- 在 **Linux / WSL2** 下，未配置钩子时会自动回退到 `/proc` 前台根会话扫描与窗口标题 / 目录名消歧。
- 完整的本地终端、桌面 IDE 与 SSH 远端匹配优先级及防串台边界见 [`docs/context-routing.md`](../docs/context-routing.md)。

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
2. 将主会话元数据（含 `agent_pid`/`pid`、规范化后的控制终端 `tty`，在 `tmux` 内时一并记录 `tmux_pane` 与 `tmux_socket`）原子写入 `<runtime_dir>/sessions/claude-<id8>.json`（权限 `0600`）；
3. 沿父进程链定位控制终端字符设备（`/dev/pts/*` 或 `/dev/ttys*`）写入 OSC 2 标题 `claude · <short_cwd> [#<id8>]`（在 `tmux` 分屏内同时同步该窗格的 `#{pane_title}`，不改变当前焦点窗格），保持标准输出为空，绝不污染 Claude 上下文。

---

## 3. Antigravity CLI (`~/.gemini/config/hooks.json`)

参考 [`gemini/hooks.json`](gemini/hooks.json)，在 `~/.gemini/config/hooks.json` 中加入 `voice-session-title`（`doctor` 同时兼容旧路径 `~/.gemini/hooks.json` 与 `~/.gemini/settings.json`）：

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

钩子会结合 `presence/<cid>.lock` 文件锁、首行 JSONL 校验以及 `/proc`（Linux）或 `ps` 祖先进程链（macOS）严格拦截后台 subagent 与无头 `agy -p` 进程，将规范化 `tty`（及在 `tmux` 内时的 `tmux_pane` 元数据）记入 `<runtime_dir>/sessions/agy-<cid8>.json`，仅在前台交互式主会话中更新控制终端及窗格标题 `agy · <short_cwd> [#<cid8>]`（不改变分屏焦点）并返回标准空 JSON `{}`。

---

## 4. `tmux` 分屏支持（无需修改 `~/.tmux.conf`）

`agent-voice-input` 原生支持在 `tmux` 多分屏下并发运行多个 Agent，无需开启 `set-titles on` 或改动 `~/.tmux.conf`：

- **自动锁定焦点窗格**：即使外层终端窗口标题未透传 `[#tag]`，守护进程也会自动查询当前活跃 attached `tmux` 客户端的焦点窗格（macOS + Ghostty 下还可直接按外层 `window_tty`/`window_pid` 精确锁定对应客户端），并在该窗格范围内做两级解析：
  1. **已配钩子或 Codex 标题 UUID**：优先匹配焦点窗格 `#{pane_title}` 中的 `[#tag]` / UUID 或注册表中绑定的 `tmux_pane`，同时核验记录的 `agent_pid` 与窗格内存活进程，确保旧会话退出或重开新会话时不命中过期记录。
  2. **未配钩子**：将交互式根会话扫描严格收敛到当前焦点窗格内（Linux/WSL2 走 `/proc` 后代进程树，macOS 走 `ps`/`lsof` 控制终端扫描），隔壁后台 teammate 分屏刷屏不会串台，切到空 shell 分屏时也不会误抓相邻分屏上下文。
- **多标签页/分屏混用防误伤**：当一个终端标签页或分屏挂着 attached `tmux`、同时切到另一个普通非 `tmux` 标签页/分屏运行 Agent 时，守护进程会综合外层 `window_tty`（Ghostty）、`[#tag]`、CLI / 动画标记 / 目录名标题与客户端交互时间区分当前前台目标，避免后台 `tmux` 抢走上下文。

---

## 5. SSH 远端 Linux 主机（零安装直连 + 可选单文件标题钩子）

当你在本地 macOS 或 Windows+WSL2 上按热键说话、向远端无头 Linux 服务器上的 CLI Agent 输入时：

1. **在本地配置白名单**（`~/.config/agent-voice-input/config.toml`）：
   ```toml
   [remote]
   hosts = ["devbox"]
   ```
2. **远端零安装开箱即用（默认）**：远端无需安装 `agent-voice-input`、无需 `ffmpeg` 或任何 API Key（仅需系统自带 Python 3.10+ 标准库）。本地守护进程在按下热键录音期间通过 `ssh -- <target> python3 -` 异步预取远端活跃会话（自动匹配 `user@devbox: ~/repos/my-app` 目录、远端活跃 `tmux` 焦点窗格及远端 `/proc` 交互根会话，支持非 Git 普通目录浅层文件名提取）；未列入 `[remote]` 白名单的 SSH 终端与 Remote-SSH IDE 窗口也会触发本地硬否决闸门，彻底杜绝本地后台会话串台。
3. **远端 `tmux` 标题透传（多远端标签页推荐，2 行配置）**：在远端 `~/.tmux.conf` 加入以下两行，使外层本地终端自动收到窗格标题与 `[@<短主机名>]` 标记：
   ```tmux
   set -g set-titles on
   set -g set-titles-string '#T [@#h]'
   ```
4. **可选标题指纹同机多会话精准消歧**：
   - 若远端已装有本仓库，`agent-voice-input session-hook` 在检测到 `SSH_CONNECTION` 时会自动追加 `[@<short_hostname>]`（亦可通过环境变量 `AGENT_VOICE_INPUT_HOST_TAG` 指定别名）。
   - 若远端保持零安装，只需将单文件脚本 [`remote/remote-title-hook.sh`](remote/remote-title-hook.sh) 拷至远端 `~/.local/bin/remote-title-hook.sh`（`chmod +x`），在 Claude Code（`~/.local/bin/remote-title-hook.sh claude`）或 Antigravity CLI（`~/.local/bin/remote-title-hook.sh agy`）钩子中调用即可。
