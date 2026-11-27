[English](README.md) | **简体中文**

# Agent 终端标题与会话注册接入指南

> [!TIP]
> **约 80% 日常场景零 Hook 即用**：macOS Ghostty（`✅`）、`Terminal.app`（`🧪`）与 `iTerm2`（`🧪`，未授自动化权限时静默回退 PTY 扫描，需精准 `tty` 可在系统设置授予自动化权限）、任意平台本地或远端 `tmux`（无需改动 `~/.tmux.conf`）、所有桌面 App，以及 WSL2 单窗口或不同目录多窗口均已开箱精准绑定，无需配置任何钩子。

以下可选钩子主要服务于**不用 `tmux` 且在同一项目目录多开独立终端窗口**的场景（例如 WSL2 Windows Terminal 同目录多窗，或 macOS 下无 `tty` 查询接口的 WezTerm / Kitty / Alacritty / Warp 多开窗口）。开启后，`agent-voice-input` 可在多窗并发下 **O(1) 精确锁定当前前台根会话**，同时严格排除后台 subagent 与无头子进程。

完整的本地终端、桌面 IDE 与 SSH 远端匹配优先级及防串台边界见 [`docs/context-routing.md`](../docs/context-routing.md)。

---

## 首推方式：官方安全一键接入（`agent-voice-input integrate`）

无需手工拼接 JSON/TOML（也无需让 AI Agent 直接手改配置），使用内置的非交互官方命令即可一步完成：

```bash
agent-voice-input integrate --status     # 只读检查：已安装的 CLI、当前钩子状态及本平台是否需要钩子
agent-voice-input integrate --dry-run    # 精确预览将做的改动，不落盘写入任何文件
agent-voice-input integrate              # 为本机检测到的 CLI 安全写入钩子
```

常用参数：
- `--cli all|claude|codex|agy`：仅针对指定 CLI 操作（默认 `all`）。
- `--uninstall`：幂等摘除 `agent-voice-input` 条目，不动任何其他用户钩子。
- `--json`：输出结构化 JSON，供 AI Agent 与脚本可靠解析。

**内置安全保障**：
1. **保护 dotfiles 软链**：若目标配置文件（如 `~/.claude/settings.json`）是指向 dotfiles 仓库的软链（GNU Stow、chezmoi 或自建软链），`integrate` 会自动解析真实路径并原子写回真身，绝不破坏软链。
2. **仓外安全备份**：首次修改前自动备份原文件至 `~/.cache/agent-voice-input/integrate-backups/`（`0700`/`0600` 权限），绝不在 dotfiles git 仓库里留下未追踪 `.bak` 文件污染 `git status`。
3. **幂等合并与保注释 TOML**：绝不覆盖用户既有的 `PreToolUse` / `Stop` 等钩子，也保留 Codex TOML 原有注释与结构（修改后经 `parse_toml()` 校验通过再原子替换）。
4. **免维护独立 `.sh` 文件、卸载零报错**：Claude Code 与 AGY 均使用内置容错内联命令（自动回退 `$HOME/.local/bin/agent-voice-input`），日后即使卸载 `agent-voice-input` 也绝不报错，Claude 标准输出严格为空，AGY 恒定输出 `{}`。

---

## 手动配置示例

若你希望手工管理配置（例如在共享 dotfiles 仓库中统一维护），下方列出与 `integrate` 等价的标准配置片段。

### 1. Codex CLI (`~/.codex/config.toml`)

将 [`codex/config.toml`](codex/config.toml) 中的 `terminal_title` 合并进 `~/.codex/config.toml` 的 `[tui]` 表（或直接执行 `agent-voice-input integrate --cli codex`）：

```toml
[tui]
terminal_title = ["spinner", "project-name", "thread-title", "thread-id"]
```

Codex 会在终端窗口标题末尾输出当前根会话的 `thread-id` UUID，`agent-voice-input` 可直接精确关联对应的 `rollout-*.jsonl`。

---

### 2. Claude Code (`~/.claude/settings.json`)

在 `~/.claude/settings.json` 中添加 `SessionStart` 与 `UserPromptSubmit` 钩子（或执行 `agent-voice-input integrate --cli claude`；另见 [`claude/settings.hooks.json`](claude/settings.hooks.json) 与 [`claude/voice-title.sh`](claude/voice-title.sh)）。推荐的内联命令自带 `PATH` 与 `$HOME/.local/bin` 兜底，卸载后静默退避不报错：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/usr/local/bin/agent-voice-input\"; [ -x \"$b\" ] && \"$b\" session-hook --cli claude --quiet >/dev/null 2>&1 || true'"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/usr/local/bin/agent-voice-input\"; [ -x \"$b\" ] && \"$b\" session-hook --cli claude --quiet >/dev/null 2>&1 || true'"
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

### 3. Antigravity CLI (`~/.gemini/config/hooks.json`)

在 `~/.gemini/config/hooks.json` 中加入 `voice-session-title`（或执行 `agent-voice-input integrate --cli agy`；另见 [`gemini/hooks.json`](gemini/hooks.json)，`doctor` 同时兼容旧路径 `~/.gemini/hooks.json` 与 `~/.gemini/settings.json`）。推荐的内联命令保证即便卸载工具也恒输出标准 `{}`：

```json
{
  "voice-session-title": {
    "SessionStart": [
      {
        "type": "command",
        "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/usr/local/bin/agent-voice-input\"; if [ -x \"$b\" ]; then \"$b\" session-hook --cli agy --quiet >/dev/null 2>&1 || true; fi; printf \"{}\\n\"'",
        "timeout": 5
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "sh -c 'b=$(command -v agent-voice-input 2>/dev/null || true); [ -x \"$b\" ] || b=\"$HOME/.local/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/opt/homebrew/bin/agent-voice-input\"; [ -x \"$b\" ] || b=\"/usr/local/bin/agent-voice-input\"; if [ -x \"$b\" ]; then \"$b\" session-hook --cli agy --quiet >/dev/null 2>&1 || true; fi; printf \"{}\\n\"'",
        "timeout": 5
      }
    ]
  }
}
```

钩子会结合 `presence/<cid>.lock` 文件锁、首行 JSONL 校验以及 `/proc`（Linux）或 `ps` 祖先进程链（macOS）严格拦截后台 subagent 与无头 `agy -p` 进程，将规范化 `tty`（及在 `tmux` 内时的 `tmux_pane` 元数据）记入 `<runtime_dir>/sessions/agy-<cid8>.json`，仅在前台交互式主会话中更新控制终端及窗格标题 `agy · <short_cwd> [#<cid8>]`（不改变分屏焦点）并恒返回标准空 JSON `{}`。

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

1. **在本地配置白名单**（`~/.agent-voice-input/config.toml`）：
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
