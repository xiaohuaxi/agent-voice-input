[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

面向命令行 Coding Agent（如 **Codex**、**Claude Code**、**Antigravity CLI / `agy`**）的语境感知语音输入工具。

不同于缺乏代码上下文的通用语音输入法，`agent-voice-input` 采用「极速语音识别 + 结合当前活跃会话上下文的轻量精修」两阶段设计，在粘贴进终端前自动修准同音代码标识符、中英混说术语与口语中途改口。

> **早期版本说明（`v0.1.0`）**：当前已在 **Windows Terminal + WSL2（含 `tmux` 多分屏与远程桌面）** 与 **macOS + Ghostty（含 `tmux`）** 下打通日常语音输入工作流；更多终端适配以及更灵活的二阶段精修模型选择仍在持续推进中。

---

## 核心思路

1. **活跃会话上下文抽取**：自动定位前台终端中的主 Agent 会话，原生支持 `tmux` 分屏焦点窗格识别而无需修改 `~/.tmux.conf`（支持终端/窗格标题标签、运行时会话注册表、macOS Ghostty 分屏 `tty`/`cwd` 直连匹配，以及 Linux `/proc` / macOS `ps` 根会话扫描，严格排除后台 subagent 与相邻分屏干扰），增量提取近期对话与 `git status` 文件名作为上下文参考。
2. **一阶段语音识别（ASR）**：先在本地做 `-50 dBFS` 静音门控与 `-20 dBFS` 响度归一化，再调用云端语音识别（默认阿里百炼 `qwen3-asr-flash`，亦支持火山豆包、Gemini 或 OpenAI 兼容接口）。
3. **二阶段轻量语境精修**：结合抽取出的会话上下文修正同音标识符并理顺口语改口。目前暂时默认通过本地 Codex CLI 调用轻量模型（`codex-luna`）完成整理，也可在配置中切换为 OpenAI 兼容接口或直接关闭（`off`）。
4. **焦点安全粘贴**：Windows 与 macOS 均提供不抢焦点的顶部胶囊提示（录音、识别、精修阶段，默认随桌面 UI 语言自动切换中英文提示）；开始录音时锁定原终端窗口/分屏，上屏仅粘贴不自动按回车，中途切走焦点则仅写入剪贴板防串窗。
5. **依赖精简**：仅使用 Python 3.10+ 标准库与宿主机 `ffmpeg`（WSL2 复用系统自带 `powershell.exe`，macOS 首次启动使用 `/usr/bin/swiftc` 幂等编译缓存原生辅助程序 `mac_shim.swift`），无需额外安装第三方 Python 包。

---

## 快速开始

### 1. 前置条件

- **Python** `>= 3.10`
- **ffmpeg**：
  - Ubuntu / WSL2：`sudo apt install ffmpeg`
  - macOS：`brew install ffmpeg`
- **macOS 原生热键与胶囊辅助程序**（macOS 推荐）：安装 Xcode Command Line Tools（`xcode-select --install`）以提供 `/usr/bin/swiftc`。首次启动时自动幂等编译并缓存 `mac_shim.swift`；若未安装 `swiftc`，会自动降级为命令行 `toggle` / `once` 模式继续可用。

### 2. 安装

```bash
uv tool install agent-voice-input
# 或使用 pipx / pip：
# pipx install agent-voice-input
```

从源码本地软链开发：

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git ~/repos/agent-voice-input
mkdir -p ~/.local/bin
ln -s ~/repos/agent-voice-input/bin/agent-voice-input ~/.local/bin/agent-voice-input
```

### 3. 配置

在 `~/.config/agent-voice-input/agent-voice-input.env`（或直接通过环境变量）配置至少一项一阶段语音识别凭据即可启动：

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
ALI_API_KEY="<your-dashscope-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env
```

除 `ALI_API_KEY`（百炼）外，也支持 `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`（豆包）、`GEMINI_API_KEY` 或 `OPENAI_API_KEY`（详见 [`config.example.env`](config.example.env)）。如需自定义快捷键（单点切换 `tap_toggle` 或按住说话 `ptt`）、胶囊提示语言（`[ui] language = "auto" | "zh" | "en"`，或环境变量 `AGENT_VOICE_INPUT_UI_LANGUAGE`）、macOS 录音设备（`[audio] macos_device = "default"` 或环境变量 `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`）、模型或将二阶段精修改为 `openai-compatible` / 关闭（`off`），可将 [`config.example.toml`](config.example.toml) 保存为 `~/.config/agent-voice-input/config.toml` 按需修改。

### 4. 自检与运行

```bash
# 检查音频输入、mac_shim/win_shim 状态、凭据与会话识别
agent-voice-input doctor

# 启动后台守护进程（WSL2 与 macOS 下默认单点右 Ctrl 开始/结束录音）
agent-voice-input start

# 其它常用子命令
agent-voice-input status
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input stop
```

---

## macOS + Ghostty 开箱体验与权限说明

在 **macOS + Ghostty** 下无需配置终端钩子或高侵入全局权限即可开箱使用：

- **热键与顶部胶囊提示**：默认单点右 Ctrl（`RControl`）开始/结束录音，亦可在配置中设为 `trigger_mode = "ptt"` 按住说话；录音、识别、精修各阶段由顶部不抢焦点的悬浮胶囊实时提示。
- **Ghostty 原生分屏直投与防焦点漂移**：按下录音时记录当前聚焦的 Ghostty 分屏 surface，识别完成后通过原生脚本接口直接向该分屏注入 bracketed paste 文本（免辅助功能按键模拟）；若等待期间切到了别的分屏或窗口，仅写入剪贴板不误投。
- **分屏 TTY 免钩子会话自动关联**：自动采集聚焦 Ghostty 分屏的 `tty` 与 `cwd`，直接匹配该分屏内正在运行的 `codex`、`claude` 或 `agy` 活跃会话，多分屏并行开发互不串台。
- **录音设备指定**：默认 `[audio] macos_device = "default"` 跟随系统默认麦克风；也可在 `config.toml` 或环境变量 `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE` 中指定设备序号或名称（可用 `agent-voice-input doctor` 查看可用输入设备）。
- **macOS 系统权限说明（TCC）**：
  - **麦克风权限（Microphone）**：首次录音时授权给启动守护进程的终端/Python 环境，供 `ffmpeg`（`avfoundation`）采集音频。
  - **自动化权限（Automation → Ghostty）**：首次向 Ghostty 读取分屏或上屏时弹窗确认一次即可。
  - **辅助功能（Accessibility）与输入监控（Input Monitoring）**：默认 `RControl` + Ghostty 组合**不需要开启**；仅在向非 Ghostty 应用回退模拟 `Cmd+V` 粘贴（需辅助功能）或改用非修饰键热键 / 运行 `agent-voice-input key-test`（需输入监控 / 辅助功能）时按需授权。

### 可选：多窗口与 `tmux` 会话标题钩子

`tmux` 分屏下的焦点窗格识别开箱即用，无需改动 `~/.tmux.conf`。若常在同一项目目录下并行打开多个 Agent 终端窗口或分屏（如 WSL2、`tmux` 内或不支持暴露分屏 `tty`/`cwd` 的终端），可按 [`integrations/`](integrations/README.zh-CN.md) 为 Codex、Claude Code 或 Antigravity CLI 接入轻量终端/窗格标题标签。

---

## 开发与测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 致谢

本项目早期探索受到 [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) 的启发，尤其是将 `Qwen3-ASR` 与技术术语上下文偏置结合以改善中英混说识别，以及转写前的静音门控与响度归一化思路，特此致谢。

## License

[Apache-2.0 License](LICENSE)
