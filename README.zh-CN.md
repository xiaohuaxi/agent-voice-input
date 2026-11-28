[English](https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.md) | **简体中文**

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)

**专为 CLI Coding Agent 打造的会话感知语音输入——听得懂你的仓库、对话与远端 SSH。**  
支持 **macOS** 与 **Windows + WSL2**，深度适配 **Codex**、**Claude Code** 与 **Antigravity CLI (`agy`)**，同时开箱覆盖桌面 IDE 与日常桌面应用。

```text
🎙️ 你对着终端随口说：
   “呃帮我查一下 session router 点派里面 retry timeout 的默认值，
    从五秒改成十秒——呃不对算了改成十五秒，跑一下单测别接管道 head”

❌ 普通语音听写：
   呃帮我查一下 session router 点派里面 retry timeout 的默认值，从5秒改成10秒，呃不对算了改成15秒，跑一下单测别接管道head

✅ agent-voice-input（对齐当前仓库与活跃会话精修，只粘贴不自动回车）：
   帮我查一下 session_router.py 里面 retry_timeout_sec 的默认值，改成 15 秒，跑一下单测，别接管道 head。
```

- **听得懂当前仓库与对话**：自动锁定焦点终端、Ghostty 分屏、`tmux` 窗格、桌面 IDE 工作区或白名单 SSH 主机，结合 50K 分块前缀缓存精准还原文件名、标识符并消解口语改口。
- **窗口防串台 + 只粘贴不回车**：按下即锁起点窗口，macOS Ghostty/`tmux` 零剪贴板直投（其余窗口快照还原不污染剪贴板），焦点切走自动退回剪贴板，默认绝不自动回车。
- **纯标准库零第三方依赖**：纯 Python `>= 3.10` 标准库 + 宿主机 `ffmpeg` + 轻量原生桥接（无 Electron），内置静音门控、自适应长停顿压缩与失败录音 `retry` 恢复。
- **100% 非交互 CLI**：全命令零 `y/n` 弹窗并支持 `--json` 结构化自检，人机皆可顺畅驾驭。

---

## 快速上手

### 方式 A：交给你的 Coding Agent 一键装好配好（推荐）

把下面这一句话直接复制发给 **Claude Code**、**Codex**、**Antigravity (`agy`)** 或 **Cursor**：

```text
请按 https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md 帮我在这台电脑上安装并配置 agent-voice-input（缺 uv 或 ffmpeg 时一并检查指引），配好后带我完成第一次口述测试。
```

### 方式 B：手动安装

**1. 一行安装** *（经 `uv` 自动隔离安装并解决 Python `>= 3.10` 与 PEP 668；不自动跑 `sudo`，不偷改 shell 配置）*：

```bash
(tmp=$(mktemp) && trap 'rm -f "$tmp"' EXIT && curl -fsSL https://raw.githubusercontent.com/xiaohuaxi/agent-voice-input/main/install.sh -o "$tmp" && sh "$tmp")
```

*（本机已有 `uv` 可直接运行 `uv tool install --python '>=3.10' agent-voice-input`。若缺 `ffmpeg`：macOS 执行 `brew install ffmpeg` + `xcode-select --install`；WSL2 执行 `sudo apt update && sudo apt install -y ffmpeg`。）*

<details>
<summary>克隆仓库零依赖直跑（本机已有 Python &gt;= 3.10）</summary>

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git && mkdir -p ~/.local/bin
ln -s "$PWD/agent-voice-input/bin/agent-voice-input" ~/.local/bin/agent-voice-input
```

</details>

**2. 3 步开说** *（编辑 `.env` 时务必去掉行首 `#` 注释符）*：

```bash
# 1) 初始化 ~/.agent-voice-input/（config.toml、.env 与初始热词 hotwords/personal.txt）
agent-voice-input init

# 2) 在 ~/.agent-voice-input/.env 取消注释并填入任意 1 把语音识别 API Key
#    国内首选：DASHSCOPE_API_KEY 或 DEEPSEEK_API_KEY
#    海外首选：OPENROUTER_API_KEY、OPENAI_API_KEY 或 GEMINI_API_KEY
#    （本机已有 Codex ChatGPT 订阅登录时 `auto` 默认最先复用，绝不开 Fast；
#     若要固定走 Codex（含 API Key 模式），可显式设 `refiner.provider = "codex"`。）

# 3) 连通性体检并启动后台服务（首次启动需编译原生桥接）
agent-voice-input doctor --ping && agent-voice-input start
```

- **默认热键**：MacBook 内置键盘自动默认 **右 `Command`（`RCommand`）**，台式 Mac（如 Mac mini）与 Windows/WSL2 默认 **右 `Ctrl`（`RControl`）**；短按 `<= 0.5s` 开/停录音，长按 `0.8s` 即时取消。
- **macOS 提示**：请务必在**本机图形终端**（如 Ghostty、`Terminal.app`）里执行 `agent-voice-input start`，切勿经 SSH 或 `launchd` 启动，并按提示为该终端授予麦克风（及按需的自动化/辅助功能）权限。

---

## 支持场景与 1 把 Key 速选

- **约 80% 日常场景零钩子开箱精准路由**：macOS Ghostty（`✅`）、`Terminal.app`（`🧪`）、`iTerm2`（`🧪`）、本地/远端 `tmux`（`✅`）、Antigravity 2.0 / ChatGPT Codex / Claude 桌面端（macOS `✅`）、主流桌面 IDE（`🧪`）、WSL2 单/不同目录窗口（`✅`）及任意日常桌面应用（`⚪`）均可直接使用。完整矩阵与权限说明见[使用指南与兼容性矩阵](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/guide.zh-CN.md)。

| 使用场景 | 推荐一阶段（语音识别） | 推荐二阶段（语境精修） | 所需 Key / 登录态 |
| :--- | :--- | :--- | :--- |
| **复用本机 Codex / AGY** *（高质量首选）* | `dashscope`、`openrouter`、`doubao`、`openai` 或 `deepseek` | `codex` *（本机已有 ChatGPT 订阅登录时 `auto` 默认最先复用，绝不开 Fast；若要固定走 Codex 含 API Key 模式，可显式设 `refiner.provider = "codex"`）* 或 `agy` *（显式启用）* | 1 把一阶段 Key + 本机 `codex`/`agy` 登录 |
| **国内主力（中文 / 中英混排）** | `dashscope`（`qwen3-asr-flash`，150 词 / 1,800 字）或 `doubao`（`bigmodel`，50 / 1,000） | `codex` / `agy` / `openrouter` *（或省心 `deepseek` / `dashscope`）* | `DASHSCOPE_API_KEY`，或 `DOUBAO_SPEECH_API_KEY` *（豆包二阶段精修需再配一个 LLM Key 或本机 `codex`/`agy` 登录）* |
| **海外 / 聚合一把 Key（高质量两段式）** | `openrouter`（`microsoft/mai-transcribe-2`，约 `$0.10/小时`）或 `openai`（`gpt-transcribe`） | `openrouter`（`google/gemini-3.8-flash`）或 `openai`（`gpt-6-luna`） | `OPENROUTER_API_KEY` 或 `OPENAI_API_KEY` |
| **极简一把 Key（经济两段式 / 单跳）** | `deepseek` *（两段式）*、`dashscope` *（两段式或 `qwen3.8-omni-flash`）* 或 `gemini` *（单跳）* | 同渠道内置默认，或单跳跳过二阶段 | `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` 或 `GEMINI_API_KEY` |

完整 13 渠道对照表、`auto` 选路规则与 `config.toml` 配方见[模型选型与渠道指南](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/providers.zh-CN.md)。

---

## 隐私与数据流向硬承诺

1. **无作者服务器、零中转、完全由你掌控**：没有任何作者托管的服务器、中转服务或隐藏遥测；全部语音与上下文只从本机直连你显式配置的模型渠道（Apache-2.0 全量开源）。
2. **默认不落盘音频与原文**：运行状态、日志与 `diag.jsonl` 均做凭据脱敏且绝不写口述原文、上下文或热词；仅识别/投递失败或取消在途任务时以 `0600` 暂存 `failed/`（24h 内最多 10 条）供 `retry`。可选 `[archive]`（`save_transcripts` 14 字段 JSONL 含 `agent`/`agent_session_id`、`save_audio` MP3）默认关闭、`0700`/`0600` 权限且受 `.gitignore` 拦截。
3. **`0700`/`0600` 权限隔离与不越界抓取**：仅读取前台窗口元数据、CLI Agent 明文日志及只读 AGY/Codex/Claude 本地会话索引，绝不读 VS Code/Cursor `state.vscdb` 或聊天/浏览器 DOM；SSH 仅查显式白名单主机且零远端密钥。
4. **仅传当前窗口对话与文件名 + `auto` 透明说明**：编码模式仅发近期活跃对话与 `git status`/浅层文件名，绝不上传仓库全文。默认 `provider = "auto"` 会检测环境/`.env` 凭据，且本机已有 Codex ChatGPT 订阅登录时二阶段默认最先复用（`agy` 仅显式配置启用）；若要固定走 Codex（含 API Key 模式）或其他渠道，请显式指定 `provider`（如 `refiner.provider = "codex"` 或 `"off"`）。

---

## 日常命令速查

```bash
agent-voice-input init [--asr P] [--refiner P] [--hotkey K]   # 初始化 ~/.agent-voice-input/
agent-voice-input start | restart | stop                      # 启停或重启后台守护进程
agent-voice-input status [--history N] [--json]               # 运行状态、生效管线、归档与热词摘要
agent-voice-input doctor [--ping] [--remote] [--json]         # 音频、原生桥接、凭据、钩子与端点探活
agent-voice-input diagnose [--last N] [--json]                # 延迟 p50、缓存命中率与脱敏日志尾部
agent-voice-input retry [--list] [--print]                    # 查看或重跑暂存的失败录音
agent-voice-input hotwords list|add|remove|path               # 管理个人热词 personal.txt（! 置顶与 <- 空耳）
agent-voice-input once --duration 4 --dry-run                 # 单次录音验证全管线（不上屏粘贴）
agent-voice-input integrate [--status] [--dry-run] [--json]   # 按需安全接入多窗口 CLI 钩子
agent-voice-input key-test                                    # 测试按键扫描码与修饰键捕获
```

- **个人热词**：编辑 `~/.agent-voice-input/hotwords/personal.txt`（或运行 `agent-voice-input hotwords add 词条 --pin --alias ...`）即可置顶专名并修正顽固空耳，下一次口述自动热加载。
- **升级 / 卸载**：升级执行 `uv tool upgrade agent-voice-input && agent-voice-input restart`（或重跑 `install.sh`）；卸载执行 `agent-voice-input stop && uv tool uninstall agent-voice-input`。
- **随终端自启** *（macOS 切勿用 `launchd`）*：在 `~/.zshrc` 或 `~/.bashrc` 加入 `[ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1`。

---

## 文档导航

- [AI Agent 一站式安装与排障契约（英文）](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md) — 供 Claude Code / Codex / AGY 执行的确定性接入指南
- [模型选型、渠道矩阵与配置配方](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/providers.zh-CN.md) — 13 个内置渠道、两段式 vs 单跳与常用配方
- [使用指南、兼容性矩阵、热词管理与 FAQ](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/guide.zh-CN.md) — macOS TCC、WSL2 WASAPI、热词、14 字段归档、CLI 参数、PEP 668 与代理排错
- [可选多窗口钩子](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.zh-CN.md) · [SSH 远端接入](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.zh-CN.md#5-ssh-远端-linux-主机零安装直连--可选单文件标题钩子) · [会话路由与防串台设计](https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/context-routing.md)
- 配置模板与安装脚本：[`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml) · [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env) · [`install.sh`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/install.sh)

## 安全、致谢与许可

- **安全策略**：漏洞上报与信任边界详见 [SECURITY.md](https://github.com/xiaohuaxi/agent-voice-input/blob/main/SECURITY.md)。
- **致谢**：早期探索受 [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) 启发（`Qwen3-ASR` 结合技术术语上下文、静音门控与响度归一化思路）。
- **开源许可**：[Apache-2.0 License](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)
- **源码离线单测**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`
