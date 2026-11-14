[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

面向命令行 Coding Agent（如 **Codex**、**Claude Code**、**Antigravity CLI / `agy`**）的语境感知语音输入工具。

不同于缺乏代码上下文的通用语音输入法，`agent-voice-input` 采用「极速语音识别 + 结合当前活跃会话上下文的轻量精修」两阶段设计，在粘贴进终端前自动修准同音代码标识符、中英混说术语与口语中途改口。

> **早期版本说明（`v0.1.0`）**：当前版本刚搭好基础框架并跑通核心特性，目前主要在 **Windows Terminal + WSL2（含远程桌面）** 下完成打磨与日常验证；更多终端支持、`tmux` 深度配合、macOS 常用终端真机完善以及更灵活的二阶段精修模型选择等工作仍在持续推进中。

---

## 核心思路

1. **活跃会话上下文抽取**：自动定位前台终端中的主 Agent 会话（支持可选终端标题标签，或通过 `/proc` 扫描并排除后台 subagent），增量提取近期对话与 `git status` 文件名作为上下文参考。
2. **一阶段语音识别（ASR）**：先在本地做 `-50 dBFS` 静音门控与 `-20 dBFS` 响度归一化，再调用云端语音识别（默认阿里百炼 `qwen3-asr-flash`，亦支持火山豆包、Gemini 或 OpenAI 兼容接口）。
3. **二阶段轻量语境精修**：结合抽取出的会话上下文修正同音标识符并理顺口语改口。目前暂时默认通过本地 Codex CLI 调用轻量模型（`codex-luna`）完成整理，也可在配置中切换为 OpenAI 兼容接口或直接关闭（`off`）。
4. **焦点安全粘贴**：Windows 下通过无焦点抢占的顶部胶囊显示录音、识别、精修阶段；上屏前核对终端焦点，仅粘贴不自动按回车，若中途切到其他窗口则只写入剪贴板。
5. **依赖精简**：当前仅使用 Python 3.10+ 标准库与宿主机 `ffmpeg`（WSL2 下复用系统自带 `powershell.exe`），无需额外安装第三方 Python 包。

---

## 快速开始

### 1. 前置条件

- **Python** `>= 3.10`
- **ffmpeg**（Ubuntu / WSL2 执行 `sudo apt install ffmpeg`）

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

除 `ALI_API_KEY`（百炼）外，也支持 `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`（豆包）、`GEMINI_API_KEY` 或 `OPENAI_API_KEY`（详见 [`config.example.env`](config.example.env)）。如需自定义快捷键、模型或未安装 Codex CLI 时改用 `openai-compatible` / 关闭二阶段精修（`off`），可将 [`config.example.toml`](config.example.toml) 保存为 `~/.config/agent-voice-input/config.toml` 按需修改。

### 4. 自检与运行

```bash
# 检查音频桥接、凭据与会话识别状态
agent-voice-input doctor

# 启动后台守护进程（WSL2 下默认点按右 Ctrl 开始/结束录音）
agent-voice-input start

# 其它常用子命令
agent-voice-input status
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input stop
```

### 可选：多窗口会话标题钩子

若常在同一项目目录下并行打开多个 Agent 终端窗口，可按 [`integrations/`](integrations/README.zh-CN.md) 为 Codex、Claude Code 或 Antigravity CLI 接入轻量终端标题标签。

---

## 开发与测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 致谢

本项目早期探索受到 [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) 的启发，尤其是将 `Qwen3-ASR` 与技术术语上下文偏置结合以改善中英混说识别，以及转写前的静音门控与响度归一化思路，特此致谢。

## License

[Apache-2.0 License](LICENSE)
