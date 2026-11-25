[English](README.md) | **简体中文**

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**专为 CLI Coding Agent 打造的会话感知语音输入——听得懂你的仓库、对话与远端 SSH。**  
支持 **Codex**、**Claude Code**、**Antigravity CLI (`agy`)**，同时开箱覆盖桌面 IDE 与日常桌面应用。

```text
🎙️ 你对着终端随口说：
   “呃帮我查一下 session router 点派里面 retry timeout 的默认值，
    从五秒改成十秒——呃不对算了改成十五秒，跑一下单测别接管道 head”

❌ 普通语音听写：
   呃帮我查一下 session router 点派里面 retry timeout 的默认值，从5秒改成10秒，呃不对算了改成15秒，跑一下单测别接管道head

✅ agent-voice-input（对齐当前仓库与活跃会话精修，只粘贴不自动回车）：
   帮我查一下 session_router.py 里面 retry_timeout_sec 的默认值，改成 15 秒，跑一下单测，别接管道 head。
```

---

## 核心技术亮点与实际收益

- **两段式 / 单跳双模式 + 50K 分块前缀缓存 → 高准确度、低延迟、省费用**：支持「一阶段专用转写 + 二阶段轻量精修」或「多模态单跳直出」；对话历史按固定 50K 只读块前置以稳定命中前缀缓存，连续说话后台并发精修、按顺序保序落字。
- **窗口焦点 + 终端会话 + SSH 远端防串台路由 → 纠偏精准、上下文绝不串台**：自动锁定当前终端窗口、Ghostty 分屏或 `tmux` 窗格的活跃会话与 `git status` 文件名；切到桌面 IDE 仅挂载本地工作区不泄终端对话，切到聊天/浏览器自动切通用润色；SSH 远端默认硬隔离防本地串台，加一行白名单即可经 `ssh -- <host> python3 -` 零远端安装异步预取远端上下文。
- **零第三方 Python 依赖纯标准库 + 声学静音门控 → 轻巧常驻、不浪费请求**：纯 Python `>= 3.10` 标准库搭配宿主机 `ffmpeg` 与原生轻量桥接（无 Electron）；本地静音门控与响度归一化拦截空触，支持默认最长 10 分钟口述（超 3 分钟按自然停顿自动切分并发转写），失败或取消在途任务的录音以 `0600` 权限暂存供 `retry` 恢复。
- **macOS Ghostty / `tmux` 零剪贴板直投 + 默认绝不自动回车 → 终端投递安全可控**：按下即锁起点窗口，处理期间切走焦点自动退回剪贴板防误贴；**macOS** 下 Ghostty 与 `tmux` 直接注入不碰系统剪贴板（WSL2 下即便在 `tmux` 内仍经 `win_shim` 走瞬态剪贴板快照 + 快捷键粘贴，其余窗口亦支持快照还原不污染原有剪贴板内容）；默认只粘贴绝不自动按回车，避免误执行命令。
- **100% 非交互式 CLI → 天然适合交给你的 AI Agent 一键安装与排障**：全部命令零 `y/n` 弹窗，自带 `doctor --ping --json` 与 `diagnose --json` 结构化自检，把一段提示词粘给 Codex / Claude Code / AGY 即可自动配好。

---

## 支持与验证状态矩阵

**状态图例**：
- `✅` **真机实测**：作者在真实设备上日常使用或端到端实测通过。
- `🧪` **代码与 CI 验证，待真机反馈**：已针对该环境或渠道做专属适配并通过离线单测或 CI 自动化实机自检（如真实 `tmux`、真实 `sshd`、`mac_shim`/`win_shim` 自检），尚未在该具体客户端或账号下完成真机实测，欢迎反馈点亮 `✅`。
- `⚪` **通用兜底**：默认 `hotkey.scope = "all"` 下可直接语音精修落字并带窗口级防串台，但无专属上下文或语气适配。

### 平台、终端、SSH 与重点桌面 App

| 类别 | 对象 | 状态 | 能力范围、投递方式与已知边界 |
| :--- | :--- | :---: | :--- |
| **宿主机** | **macOS** *（Apple Silicon / Intel）* | `✅` | 日常主力；原生 `mac_shim` 胶囊 + 系统麦克风 |
| **宿主机** | **Windows + WSL2** | `✅` | 日常主力；`win_shim` 原生 WASAPI 采集 + Per-Monitor V2 高分屏胶囊 |
| **宿主机** | **SSH 远端 Linux 主机** | `✅` | 零远端安装探针（仅需 Python `>= 3.10`）+ 本地防串台；Ubuntu CI 真实 `sshd` E2E 验证 |
| **宿主机** | **原生 Linux 桌面 / 纯原生 Windows（无 WSL2）** | `—` | 暂不支持守护进程、全局热键与 `toggle`；原生 Linux 当前可用于离线文件转写（`once --file <wav> --dry-run`）及作为 SSH 远端目标主机 |
| **终端** | **macOS Ghostty** | `✅` | AppleScript 零剪贴板直投 + 同分屏切 `tmux` pane 防串台 + 零钩子完整上下文 *（Codex / Claude Code / AGY CLI 真机实测）* |
| **终端** | **Windows Terminal (WSL2)** | `✅` | `win_shim` 瞬态剪贴板 + 快捷键粘贴（不污染原剪贴板）+ 同窗切 Tab 防串台 + 挂钩子/`tmux` 精准上下文 *（Codex / Claude Code / AGY CLI 真机实测）* |
| **终端** | **本地 & 远端 `tmux`** | `✅` | **macOS** 零剪贴板直投 / **WSL2** 走 `win_shim` 瞬态剪贴板粘贴（不污染原剪贴板）；进程树按焦点窗格隔离；Linux & macOS CI 真实 `tmux` E2E 验证 |
| **终端** | **其他内置终端与专用 SSH 客户端** | `🧪` | 已内置粘贴键位路由、防串台与识别规则（完整名单见下方折叠区），待真机实测反馈 |
| **桌面 App** | **Antigravity / AGY 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | **Antigravity 2.0 独立桌面版**已在 macOS 真机实测通过（只读 `conversation_summaries.db` + 窗口标题精确/截断前缀匹配、工作区 `git`/文件名与 `brain/` 增量缓存及切会话防串台），同时兼容老版 VS Code Fork *（Windows 桌面版待真机反馈；WSL2 仅扫 WSL 内目录）* |
| **桌面 App** | **ChatGPT / Codex 统一桌面端** | `✅` *(macOS)* / `🧪` *(Windows)* | 已在 macOS 真机实测通过：统一桌面端（`com.openai.codex`）只读 `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl`，本地仓库任务自动升格为 `agent` 模式并挂载项目热词与增量缓存，普通云端 ChatGPT 聊天保持 `general + ai_chat` 零串台，同窗切会话在投递前复核防串台；独立旧版 ChatGPT 桌面端（`com.openai.chat`）保持 `general + ai_chat` 精修落字与锁窗防串台；Windows（`ChatGPT.exe` / `Codex.exe`）当前提供精修落字与锁窗防串台 *（Windows/WSL2 跨系统上下文绑定已列入路线图）* |
| **桌面 App** | **Claude 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | 已在 macOS 真机实测通过（`com.anthropic.claudefordesktop`）：**Code & Cowork** 本地会话上下文绑定（有界只读 `AXWebArea` 路由与标题采集、`Claude`/`Claude-3p` 双目录扫描、`50K` 增量缓存及投递前同窗切会话复核）以及精修落字与剪贴板自动恢复，云端 Chat 保持 `general + ai_chat` 零串台 *（Windows `Claude.exe` 待真机反馈）* |
| **桌面 App** | **其他桌面 IDE 与主流 IM** | `🧪` | IDE（**VS Code**、**Cursor**、**Windsurf**、**JetBrains**、**Zed** 等）走 `coding_gui` 并挂载宿主工作区 `git`/文件名；**在 IDE 内置终端运行 CLI Agent 时同样按 `coding_gui` 处理**（支持精修落字与工作区文件名参考，但**跳过 CLI `sync_session`**，不读内置终端里的 `codex`/`claude`/`agy` 对话历史，也不区分内置终端分屏）。IM（**微信**、**飞书**、**Slack**、**Discord** 等）走 `im_chat` |
| **桌面 App** | **其他任意桌面 App** *（浏览器、笔记、邮件等）* | `⚪` | 默认 `hotkey.scope = "all"` 可直接语音精修落字 + 锁窗防串台，无专属上下文或语气适配 |

<details>
<summary><b>其他内置终端与专用 SSH 客户端完整清单（<code>🧪</code>）</b></summary>

- **macOS 终端（`🧪`）**：`Terminal.app`、`iTerm2`、`WezTerm`、`Kitty`、`Alacritty`、`Warp`、`Hyper`、`Rio`、`Tabby`
- **Windows 终端（`🧪`）**：`WezTerm`、`Alacritty`、`Ghostty`、`Tabby`、`Hyper`、`Rio`、`Warp`、`Mintty`、`PuTTY`、`KiTTY`
- **专用 SSH 客户端（`🧪`）**：`MobaXterm`、`Xshell`、`WindTerm`、`SecureCRT`、`Termius`、`Electerm`、`FinalShell`、`RoyalTS`

</details>

> 如果你在带 `🧪` 或 `⚪` 的环境里用起来顺畅（或遇到坑），欢迎提 Issue 告知「**系统 + 终端/App + 渠道**」，帮我们点亮更多 `✅`。

> [!TIP]
> **零 Hook、零 `tmux` 配置、全桌面开箱即用**：单窗口终端、macOS Ghostty 分屏、`tmux` 多窗格（无需改 `~/.tmux.conf`）、桌面 IDE 与日常桌面软件全部零配置即装即用。仅在「WSL2 不用 `tmux` 且同目录开多窗」或「需拉取 SSH 远端上下文」时才需按需配置可选钩子或一行白名单。

---

## 1 分钟快速上手

### 路径 A：让你的 AI Agent 帮你安装配置（推荐）

全命令行 **100% 非交互**（零 `y/n` 确认弹窗），把下面这段话直接发给 **Codex**、**Claude Code** 或 **AGY** 即可：

```text
请帮我安装并配置 agent-voice-input（https://github.com/xiaohuaxi/agent-voice-input）：
1. 检查 python3 (>=3.10) 与 ffmpeg（macOS 下顺带确认 Xcode Command Line Tools `/usr/bin/swiftc`），用 `uv tool install agent-voice-input` 安装（无 uv 则换用 pipx）；
2. 运行 `agent-voice-input init` 生成 0600 权限的 `~/.config/agent-voice-input/config.toml` 与 `agent-voice-input.env`；
3. 检查环境中是否已有至少一把一阶段语音识别凭据（如 DASHSCOPE_API_KEY、DOUBAO_SPEECH_API_KEY、OPENROUTER_API_KEY、GEMINI_API_KEY、OPENAI_API_KEY、DEEPSEEK_API_KEY、GROQ_API_KEY 或 SILICONFLOW_API_KEY；二阶段可选搭配 DEEPSEEK_API_KEY / ARK_API_KEY 或本机 codex/agy 登录，其中仅一把 DEEPSEEK_API_KEY 即可零配置覆盖完整两段式），没有就问我选哪家并写入 `agent-voice-input.env`；
4. 运行 `agent-voice-input doctor --ping` 确认通过后，执行 `agent-voice-input start` 启动后台守护进程。
```

### 路径 B：手动 3 步安装（`v0.14.0`）

```bash
# 1. 安装前置 ffmpeg 与 CLI（WSL2: `sudo apt install ffmpeg` | macOS: `brew install ffmpeg` + `xcode-select --install`）
uv tool install agent-voice-input          # 或 `pipx install agent-voice-input`

# 2. 初始化 0600 权限配置文件，并在 agent-voice-input.env 中填入至少一把一阶段语音 Key
agent-voice-input init

# 3. 探活体检并启动（轻按右 Ctrl <= 0.5s 开始/结束录音；长按 0.8s 即时取消）
agent-voice-input doctor --ping && agent-voice-input start
```

> MacBook 无右 Ctrl 键可在 `~/.config/agent-voice-input/config.toml` 的 `[hotkey]` 下改为 `key = "ROption"` 或 `"Fn"` 后执行 `agent-voice-input restart`。完整配置模板见 [`v0.14.0/config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.14.0/config.example.toml)（[`main`](config.example.toml)）与 [`v0.14.0/config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/v0.14.0/config.example.env)（[`main`](config.example.env)）。

### 场景化推荐供应商组合

| 使用场景 | 推荐一阶段（语音识别） | 推荐二阶段（语境精修） | 所需凭据 / 配置 |
| :--- | :--- | :--- | :--- |
| **复用现有 Codex / AGY 订阅** *（二阶段免另购 API）* | `dashscope` `✅`、`doubao` `✅`、`openai` `✅`、`deepseek` `✅` 或 `groq` `🧪` | `codex` `✅` *（`auto` 自动复用，绝不开 Fast）* 或 `agy` `✅` *（显式配置启用）* | 1 把语音识别 Key + 本机 `codex` 或 `agy` 登录态 |
| **国内主力（中文 / 中英混排）** | `dashscope`（阿里百炼 `qwen3-asr-flash` `✅`）、`doubao`（豆包语音 `bigmodel` `✅`）或 `deepseek`（`deepseek-asr` `✅`） | `codex` `✅` / `agy` `✅` / `deepseek` `✅` / `dashscope` `✅` / `volcengine` `✅` | `DASHSCOPE_API_KEY`、`DOUBAO_SPEECH_API_KEY` *（单头 `X-Api-Key`，无需 `APP_ID`）* 或 `DEEPSEEK_API_KEY` + 可选精修 Key |
| **海外 / 纯英文主力** | `openai`（`gpt-transcribe` `✅`）或 `groq`（`whisper-large-v3-turbo` `🧪`） | `codex` `✅` / `agy` `✅` / `openai` `✅` / `gemini` `✅` / `openrouter` `✅` | `OPENAI_API_KEY` 或 `GROQ_API_KEY` + 可选精修 Key |
| **一把 Key 省心全包** | `deepseek` `✅`（高性价比两段式：`deepseek-asr` + `deepseek-flash`）、`dashscope` `✅`（国内两段式或 `qwen3.8-omni-flash` 单跳）、`gemini` `✅`（海外原生单跳）、`openrouter` `✅`（两段式/单跳均可）或 `openai` `✅`（两段式或 `gpt-audio-1.5` / `gpt-audio-mini` 单跳） | 两段式用同渠道内置默认，或单跳跳过二阶段 | 仅需 1 把 Key：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`GEMINI_API_KEY`、`OPENROUTER_API_KEY` 或 `OPENAI_API_KEY` |

> **关于本地 / 私有部署模型**：当前版本重点聚焦并充分实测了主流云端 API 与现有 CLI 登录（Codex / AGY）。虽然底层已提供通用的 `openai-compatible` 自定义 `base_url` 接口（`🧪`），但经过完整实测的**本地/企业私有部署模型（自部署小型 ASR + 精修）一等公民支持**规划在后续版本（**`v1.1+`**）推进。

---

## 隐私与数据流向硬承诺

1. **无作者服务器、零中转、完全由你掌控**：`agent-voice-input` **没有任何作者托管的服务器或中转服务**，也没有任何隐藏遥测；全部语音与上下文只从本机直连你自己在配置中选定的模型渠道，所有模型与凭据完全由用户自主选择和控制（Apache-2.0 全量开源）。
2. **默认不落盘音频与原文**：运行状态、日志与遥测均做凭据脱敏且不记录用户口述原文；仅在识别/投递失败或主动取消在途任务时，将有效录音以 `0600` 权限暂存于 `failed/`（保留 24h 内最近 10 条）供 `retry` 恢复（录音中取消直接丢弃；可选 MP3 归档默认关闭）。
3. **`0700`/`0600` 权限隔离与不越界抓取**：配置与运行目录严格私有权限；仅读取前台窗口名、macOS `AXDocument`、CLI Agent 明文日志及 AGY/Codex 只读会话索引——绝不读取 VS Code/Cursor 私有状态库（`state.vscdb`），也不抓取聊天软件或浏览器的无障碍 DOM；切到非终端应用硬跳过终端 CLI 会话，SSH 远端仅查显式白名单主机且零密钥落盘。
4. **仅传当前窗口对话与文件名（不传仓库全文）+ `auto` 凭据说明**：编码模式下仅发送当前活跃会话近期对话与 `git status`/浅层文件名，绝不上传仓库全文。同时透明提示：默认 `provider = "auto"` 会检测环境变量/`.env` 及本机已有 `codex` CLI 登录态（`agy` 需显式配置，绝不自动触发）；如需固定渠道请显式指定 `provider` 或设 `refiner.provider = "off"`。

---

## 高频踩坑 FAQ

- **MacBook 没有右 Ctrl 键怎么办？**  
  在 `~/.config/agent-voice-input/config.toml` 的 `[hotkey]` 中改为 `key = "ROption"`（右 Option）或 `"Fn"`，运行 `agent-voice-input restart` 生效；可用 `agent-voice-input key-test` 确认键码。
- **为什么通过 SSH 在 macOS 上启动守护进程录不到声音（全静音）？**  
  macOS 的麦克风 TCC 权限归属责任图形进程，经 `sshd` 拉起的守护进程会被系统静默喂全 0 静音帧。请务必在本机已获麦克风授权的图形终端（如 Ghostty 或 `Terminal.app`）里执行 `agent-voice-input start` 或 `restart`。
- **后台守护进程怎么走代理？**  
  守护进程在启动时继承当前 shell 环境变量：请先在终端导出 `HTTPS_PROXY`（以及本机回环排除 `NO_PROXY=127.0.0.1,localhost`），再执行 `agent-voice-input restart`。由于纯标准库 HTTP 客户端仅支持 `http://` 代理，如果代理客户端只设置了 `ALL_PROXY=socks5://...`，请同时设置对应的 HTTP 混合端口（`HTTPS_PROXY=http://127.0.0.1:<port>`），并用 `agent-voice-input doctor --ping` 验证连通。
- **为什么默认只粘贴不自动按回车？**  
  终端与 SSH 下误听一条命令直接回车风险太高，因此默认只粘贴不发送 `Enter`，方便扫一眼再确认；处理期间若切走焦点，也会自动退回剪贴板而不强贴到别的窗口。
- **两段式（STT + 精修）和一阶段单跳多模态怎么选？**  
  **两段式**组合最灵活（含仅一把 `DEEPSEEK_API_KEY` 的完整两段式）、可免费复用现有 Codex/AGY CLI 订阅，且长句与复杂术语纠偏最稳；**单跳多模态**（如原生 `gemini`、百炼 `qwen3.8-omni-flash`、官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini` 或 OpenRouter 多模态模型）只需一把 Key 和一次请求，但会把音频与会话上下文一并直接发给该多模态渠道。

---

## 常用命令与可选增强

```bash
agent-voice-input init                   # 初始化 0600 权限 config.toml 与全注释 agent-voice-input.env（支持 --force）
agent-voice-input start | restart | stop # 启停或重启后台守护进程（start 幂等，代码/版本漂移时提示 restart）
agent-voice-input status                 # 查看运行状态、当前生效管线与漂移信息（支持 --history N）
agent-voice-input doctor [--ping] [--remote] [--json]   # 全面自检音频、原生桥接、凭据、SSH 就绪度与端点探活
agent-voice-input diagnose [--last N] [--json]          # 本地只读诊断：延迟 p50、缓存命中率、失败录音数与脱敏日志尾部
agent-voice-input retry [--list] [--print]              # 查看或重跑暂存的失败录音
agent-voice-input hotwords list|add|remove              # 管理 [global]/[chat]/[coding] 分层热词并查各渠道配额（--scope、--json）
agent-voice-input once --duration 4 --dry-run           # 单次录音验证流水线（不粘贴上屏；Linux 可用 `--file <wav> --dry-run` 离线转写）
agent-voice-input toggle | cancel        # 命令行触发起止录音或取消当前/后台在途任务（--seq N）
agent-voice-input key-test               # 测试按键扫描码与修饰键捕获
```

- **可选多窗口钩子与 SSH 远端 Linux（`[remote]`）**：单窗口终端、Ghostty 分屏与 `tmux` 分屏零钩子即可用。若常在 WSL2 **同一项目目录**并行开多个独立终端窗口，或需拉取远端 Linux 服务器上的活跃 Agent 上下文（在 `config.toml` 配置 `[remote] hosts = ["devbox"]` 并运行 `agent-voice-input doctor --remote`），详见 [`integrations/README.zh-CN.md`](integrations/README.zh-CN.md) 与 [`docs/context-routing.md`](docs/context-routing.md)。

---

## 详细参考

<details open>
<summary><b>内置渠道与验证状态矩阵及常见 <code>config.toml</code> 配方</b></summary>

配置覆盖优先级：`CLI 参数 > ~/.config/agent-voice-input/config.toml > 内置默认值`。凭据存放于 `~/.config/agent-voice-input/agent-voice-input.env`（见 [`config.example.env`](config.example.env)）。

| `provider` | 可用阶段 | 验证状态 | 内置默认 `asr.model` | 内置默认 `refiner.model` | 凭据来源 / 环境变量 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **`codex`** | `refiner` | `✅` | — | `gpt-6-luna` *(公开账号自动回退 `gpt-5.4-mini` / `gpt-5-codex-mini`)* | 复用本机 `codex` CLI 登录态 *(无状态 HTTP `/responses` 直连 + 30 分钟提前安全刷新，绝不开启 Fast 模式)* |
| **`agy`** | `refiner` | `✅` | — | `gemini-3.8-flash` *(默认 `effort = "low"`；需显式启用，不参与 `auto` 自动选路)* | 复用本机已登录的 `agy` CLI *(零工具 `agy acp` stdio JSON-RPC + 增量续接前缀缓存)* |
| **`deepseek`** | `asr`, `refiner` | `✅` *(双段实测)* | `deepseek-asr` *(官方 `POST /v1/tools/asr`；仅支持 `asr.api = "auto"`；自动检测 21 种语言，不支持热词与语言参数)* | `deepseek-flash` | `DEEPSEEK_API_KEY` *(兼容 `API_KEY_DEEPSEEK`；一把 Key 零配置跑通完整两段式)* |
| **`dashscope`** | `asr`, `refiner` | `✅` *(转写/单跳/精修均实测)* | `qwen3-asr-flash` *(或单跳 `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(兼容 `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `✅` *(1.0 & 2.0)* | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *（单头 `X-Api-Key`，无需 `APP_ID`；兼容旧 `DOUBAO_ASR_API_KEY` 或双头 `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`；`auto` 优先同步 `volc.bigasr.auc_turbo`，未开通时自动回退并缓存 `volc.seedasr.auc` / `volc.bigasr.auc`）* |
| **`volcengine`** | `refiner` | `✅` *(`doctor --ping` & 精修)* | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(兼容 `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `✅` *(转写/单跳/精修)* | `qwen/qwen3-asr-1.7b` | `google/gemini-3.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `✅` *(单跳 & 精修)* | `gemini-3.7-flash` *(单跳)* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `✅` *(转写/单跳/精修)* | `gpt-transcribe` *(或单跳 `gpt-audio-1.5` / `gpt-audio-mini`)* | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `🧪` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `🧪` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `🧪` | `gpt-4o-mini-transcribe` *(需配 `base_url`)* | *(需显式配 `base_url` + `model`)* | `api_key_env` 或 `OPENAI_API_KEY` *(本机回环地址免 Key)* |
| **`off`** | `refiner` | `✅` | — | *(关闭二阶段，直出一阶段原文)* | 无需凭据 |

- `asr.provider = "auto"`（默认）：按 `dashscope -> doubao -> openrouter -> gemini -> openai -> deepseek -> groq -> siliconflow` 顺序选用首个已配置凭据的渠道。
- `refiner.provider = "auto"`（默认）：优先复用本机已登录的 `codex` CLI，否则按 `deepseek -> dashscope -> gemini -> openrouter -> volcengine -> openai -> groq -> siliconflow` 选用，无凭据时降级为 `"off"`。（`agy` 仅显式配置启用。）

```toml
# 配方 1：国内低延迟主力（百炼 / 豆包 / DeepSeek ASR + 复用本机 Codex 或 AGY 订阅精修）
[asr]
provider = "dashscope"           # 亦可改为 "doubao" / "deepseek" / "openai"

[refiner]
provider = "codex"               # 或显式设为 "agy" 复用 Antigravity CLI，无订阅可改为 "deepseek" / "dashscope"
```

```toml
# 配方 2：一把 Key 两段式（DeepSeek 或 OpenRouter，直接用内置默认模型）
[asr]
provider = "deepseek"            # 亦可改为 "openrouter"（仅配 DEEPSEEK_API_KEY 时，`auto` 即可零配置直出 DeepSeek 两段式）

[refiner]
provider = "deepseek"            # 亦可改为 "openrouter"
```

```toml
# 配方 3：多模态单跳音频 + 50K 缓存上下文直出（自动跳过二阶段）
[asr]
provider = "dashscope"           # 亦可设为 "gemini"（免填 model）、"openai" + model = "gpt-audio-1.5" / "gpt-audio-mini"，或 "openrouter" + model = "google/gemini-3.5-flash-lite"
model = "qwen3.8-omni-flash"
effort = "auto"
```

</details>

<details>
<summary><b>双平台机制细节（WSL2 WASAPI / macOS TCC 权限与投递）</b></summary>

### Windows Terminal + WSL2（含远程桌面与 `tmux`）
- **全局作用域、远程桌面让行与高分屏胶囊**：默认单击右 Ctrl（`RControl`）即可在终端、IDE 与日常桌面应用中开始/结束录音（`hotkey.scope = "all"`，可选 `"coding"` / `"terminals"`；组合键如 `RControl+C` 原样放行），显示原生 Per-Monitor V2 抗锯齿波形胶囊。聚焦远程桌面/虚拟机客户端（`mstsc`、`vmconnect`、Parsec、RustDesk 等）时静默让行热键（`yield_remote_desktop = true`；默认放行裸 `msrdc` 以保护 Win11 WSLg，非 WSLg 的 AVD/Windows 365 用户可加 `ignore_apps = ["msrdc"]`）。WSL2 下即便在 `tmux` 内，投递仍走 `win_shim` 瞬态剪贴板快照 + 按进程路由粘贴快捷键（`Ctrl+V`、`Ctrl+Shift+V` 用于 `WezTerm`/`Alacritty`/`Ghostty`/`Tabby`/`Hyper`/`Rio`/`Warp`/`WindTerm`/`SecureCRT`/`Termius`/`Electerm`，`Shift+Insert` 用于 `mintty`/`PuTTY`/`KiTTY`/`MobaXterm`/`Xshell`），粘贴后还原且不污染原剪贴板内容。
- **原生 WASAPI 采集**：常驻 `win_shim` 直接调用 Windows WASAPI 16 kHz 采集，避开 WSLg `RDPSource` 休眠零包死锁；起止提示音默认关闭以免触发 RDP 音频通道唤醒延迟。
- **零配置 `tmux` 分屏追踪**：无需改动 `~/.tmux.conf`，自动锁定当前 attached 客户端的焦点窗格。

### macOS（`Ghostty`、`tmux` 与其他终端）
- **原生 Retina 胶囊与安全投递**：首次启动经 `/usr/bin/swiftc` 编译缓存原生 `mac_shim`，监听 `RControl` / `ROption` / `Fn` 短按或 `ptt` 按住说话、远程桌面自动让行（`Windows App`、`屏幕共享`、Parsec、VMware/Parallels 等）并渲染 Retina 胶囊。仅在 **macOS 的 Ghostty 与 `tmux`** 内直投不碰系统剪贴板；其他窗口锁定起点窗口 ID 后走原生 `CGEvent` `Cmd+V` 并带剪贴板快照还原。
- **零钩子分屏与 `tmux` 绑定**：读取 Ghostty 分屏 `tty`/`cwd`、`Terminal.app` 当前标签页 `tty` 或终端子进程 PTY 树（含 `tmux` 焦点窗格）绑定 `codex`/`claude`/`agy` 会话，并在非终端 GUI 窗口（含 IDE 内置终端）硬跳过 CLI 会话查询；默认使用系统麦克风（`[audio] macos_device = "default"`）。
- **轻量 TCC 权限**：需授予**麦克风权限**（供 `ffmpeg`）以及按所用终端授予一次**自动化（Automation → Ghostty 或 Terminal）**；默认修饰键热键**不需要**「输入监控（Input Monitoring）」；仅在向桌面 GUI 应用及非 `tmux` 下的非 Ghostty 终端发送 `Cmd+V` 时需为宿主终端开启「辅助功能（Accessibility）」（仅在 Ghostty 或 `tmux` 内直投时无需该权限）。

</details>

<details>
<summary><b>进阶配置说明（协议路由、前缀缓存、热词、重试与 MP3 归档）</b></summary>

- **按 `provider` 选渠道，`model` 默认留空自动匹配**：`[asr]` 与 `[refiner]` 的 `model` 留空时自动匹配内置默认模型；通过 CLI 切换 `provider` 且未传 `--*-model` 时也会重置为新渠道默认模型。显式指定 `model` 时原样透传上游 ID。
- **两阶段 vs 单跳协议（`asr.api`）**：`"auto"` 将专用 STT（`qwen3-asr-flash`、`doubao`、`deepseek-asr`、`whisper-*`、`*-transcribe`、`SenseVoiceSmall`）路由至一阶段 + 二阶段，将原生 `gemini`、百炼 `qwen*-omni*`、官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini`（或显式 `asr.api = "chat-completions"`）与 OpenRouter/OpenAI 兼容多模态模型路由至单跳（共享 50K `<session_reference_log>` 前缀块、`>= 3s` 内存压缩 `64 kbps` MP3、上限 `610s`；默认跳过二阶段，除非 `refiner.force_on_single_hop = true`）。DeepSeek 官方 ASR（`POST https://api.deepseek.com/v1/tools/asr`）仅支持 `asr.api = "auto"`（支持 21 种语言自动检测，不支持热词与语言参数）。在 `openrouter` 使用未收录新模型时请显式指定 `asr.api = "audio-transcriptions"` 或 `"chat-completions"`。
- **二阶段协议与思考档位（`refiner.api`、`effort`）**：`refiner.api = "auto"` 将 `codex` 路由至无状态 HTTP `/responses`（`store=false`、`stream=true`），`agy` 路由至本机 `agy acp` stdio JSON-RPC，`gemini` 路由至 `:generateContent`，其余走 `/chat/completions`（`openai`/`openrouter`/`openai-compatible` 可显式启用 `"responses"`）。`effort` 支持 `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`（`codex`/`agy`/`openai` 默认 `"low"`，其余渠道默认 `"auto"` 零思考极速档）。
- **精修提示词、前缀缓存与冷启动回退（`refiner.prompt`、`include_git_status`、`context_max_chars`、`cold_start_fallback`）**：`prompt` 留空时使用内置自适应 `agent` / `general` 提示词（非空则覆盖两模式）。近期对话（上限 `context_max_chars = 200000`）按 `50K` 封口块前置，末尾拼装 `<workspace_git_status>`（工作树干净时补充核心追踪文件名）与 `<asr_draft_to_clean>`。新开空/极短会话时，`cold_start_fallback = "auto"`（可选 `"hotwords"` / `"off"`）自动从同目录近期历史会话补充热词并前置 1 块冻结的 `<= 25K` `<prior_session_reference>`。
- **分层纯文本热词表（`asr.hotwords_file` 与 `hotwords` 子命令）**：直接编辑 `~/.config/agent-voice-input/hotwords.txt` 或运行 `agent-voice-input hotwords list|add|remove`，下一次口述自动热加载生效。无分组文件归入 `[global]`，可选 `[chat]` 与 `[coding]` 按前台窗口自动挂载，优先级高于动态仓库/会话词与内置程序员热词，并内置 `doubao`/`whisper`/`dashscope`/`gemini` 配额预算检查。
- **同渠道重试与可选本地 MP3 归档（`max_retries`、`audio.save_recordings`）**：两阶段默认 `max_retries = 2`（`0..10`），在阶段 `timeout_sec` 与 9 秒建连/TLS 上限内分别对 `429`/QPS 限流与瞬态网络/5xx 退避重试。开启 `audio.save_recordings = true` 后，后台异步将有效录音压缩为 `0600` 权限 `16 kHz` `64 kbps` MP3 存入 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/`（按修改时间环形保留最新 `save_recordings_max_count = 300` 条）。

</details>

<details>
<summary><b>核心 CLI 覆盖参数</b></summary>

| CLI 参数 | 说明 |
| :--- | :--- |
| `--config PATH` | 指定 `config.toml` 或 `config.json` 路径（亦支持 `AGENT_VOICE_INPUT_CONFIG` 与 `XDG_CONFIG_HOME`） |
| `--asr`, `--asr-provider` | 一阶段渠道（`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `deepseek`, `groq`, `siliconflow`, `openai-compatible`；别名 `volcengine`/`volc` → `doubao`、`bailian` → `dashscope`） |
| `--asr-model` | 一阶段上游模型 ID |
| `--asr-api` | 一阶段协议（`auto`, `audio-transcriptions`, `chat-completions`） |
| `--asr-effort` | 一阶段思考档位 |
| `--refiner`, `--refiner-provider` | 二阶段渠道（`auto`, `codex`, `agy`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`；别名 `doubao`/`volc`/`ark` → `volcengine`、`bailian` → `dashscope`） |
| `--refiner-model` | 二阶段上游模型 ID |
| `--refiner-api` | 二阶段协议（`auto`, `chat-completions`, `responses`） |
| `--refiner-effort`, `--effort` | 二阶段思考档位 |

*（环境变量用于厂商凭据、引导路径 `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR` 及底层运行期诊断项；常规参数统一放 `config.toml`。）*

</details>

---

## 本地源码开发与测试

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git ~/repos/agent-voice-input
ln -sf ~/repos/agent-voice-input/bin/agent-voice-input ~/.local/bin/agent-voice-input
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 致谢

本项目早期探索受到 [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) 的启发，尤其是将 `Qwen3-ASR` 与技术术语上下文结合以改善中英混说识别，以及转写前的静音门控与响度归一化思路，特此致谢。

## License

[Apache-2.0 License](LICENSE)
