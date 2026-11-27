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

## 1 分钟快速上手

### 路径 A：让你的 AI Agent 一键安装配置（推荐）

全命令行 **100% 非交互**（零 `y/n` 确认弹窗）并自带结构化 `--json` 自检。把下面这段话直接发给 **Codex**、**Claude Code** 或 **AGY**：

```text
请按 docs/agent-setup.md（https://github.com/xiaohuaxi/agent-voice-input/blob/main/docs/agent-setup.md）帮我安装并配置 agent-voice-input：
1. 按本机键盘类型设置热键（MacBook 内置键盘自动设为 RCommand；Mac mini 等台式 Mac 外接标准键盘以及 Windows/WSL2 保持 RControl）。
2. 仅在确实需要钩子的场景下（如 WSL2 非 tmux 同目录多开窗口）再执行 `agent-voice-input integrate`。
3. 跑通 `doctor --ping --json` 并 `start` 启动后，引导我做一次快速连通或 `--dry-run` 试录验收。
```

### 路径 B：纯手工 3 步上手

```bash
# 1. 安装前置与 CLI（需 Python >= 3.10；macOS: `brew install ffmpeg` + `xcode-select --install` | WSL2: `sudo apt install ffmpeg`）
uv tool install agent-voice-input          # 或 `pipx install agent-voice-input` / `pip install agent-voice-input`（需 Python >= 3.10）

# 2. 初始化 ~/.agent-voice-input/（0600 config.toml 与 .env + 默认全忽略 .gitignore；可选 `--asr` / `--refiner` / `--hotkey`）
agent-voice-input init

# 3. 探活体检并启动后台守护进程（首次运行需编译原生桥接：macOS 约 5–15 秒，WSL2 C# 编译 + Defender 扫描约 15–60 秒+）
agent-voice-input doctor --ping && agent-voice-input start
```

- **自动适配本机键盘（免 `init` 同样生效）与避坑**：无论是否运行过 `init`，MacBook 内置键盘在初始化与运行时均自动默认 `key = "RCommand"`（右 Command），台式 Mac（如 Mac mini 外接标准键盘）与 Windows/WSL2 默认保持 `key = "RControl"`；`config.toml` 中的显式 `key` 或 `--hotkey` 严格优先。避坑提示：`ROption` 易撞豆包输入法，`Fn` 撞 macOS 系统 🌐 键（如要用 `Fn` 需先在 系统设置 → 键盘 将「按下 🌐 键时」设为**不执行任何操作**），`RShift` 撞第三方中文输入法单按切中英文。轻按热键 `<= 0.5s` 开始/结束录音，长按 `0.8s` 即时取消。
- **macOS 权限归属启动终端速览**：**麦克风**（必选）、**辅助功能**（用于非 Ghostty/`tmux` 的自动粘贴及第三方终端/桌面端窗口标题与会话绑定）、**自动化**（用于 Ghostty / `Terminal.app` / `iTerm2`）均归属于执行 `agent-voice-input start` 的宿主终端；若换了其他终端 `restart`，需为新终端重新授权。
- **统一应用家目录（`~/.agent-voice-input/`）**：`config.toml`、`.env`、`hotwords/personal.txt` 与可选归档（`audio/`、`transcripts/`）均统一收口于 `~/.agent-voice-input/`（可通过 `AGENT_VOICE_INPUT_HOME` 覆盖根目录）；`init` 排他生成默认全忽略、仅放行 `hotwords/` 的 `.gitignore`，且 `init --force` 仅覆写 `config.toml`，绝不覆盖已有 `.env` 凭据与 `.gitignore`。
- **最简 1 把 Key 推荐**：`init` 会自动探测环境中已有的 API Key 及本机 `codex` 登录态，若已就绪直接提示开跑；若尚无一阶段语音 Key，只需在 `~/.agent-voice-input/.env` 中填入 **1 把 Key**：
  - **国内首选**：`DASHSCOPE_API_KEY` 或 `DEEPSEEK_API_KEY`
  - **海外首选**：`OPENAI_API_KEY` 或 `GEMINI_API_KEY`
- 完整配置模板与 Agent 接入契约：[`config.example.toml`](config.example.toml)、[`config.example.env`](config.example.env)、[`docs/agent-setup.md`](docs/agent-setup.md)（英文）。

> [!TIP]
> **约 80% 日常场景零 Hook 开箱即精准路由**：macOS Ghostty（`✅`）、`Terminal.app`（`🧪`）与 `iTerm2`（`🧪`，未授自动化权限时静默回退 PTY 扫描，需精准 `tty` 可在系统设置授予自动化权限）、任意平台本地或远端 `tmux`（无需改 `~/.tmux.conf`）、所有桌面 App，以及 WSL2 单窗口或不同目录多窗口均无需配置任何钩子。仅在 WSL2 Windows Terminal 不用 `tmux` 且**同一目录多开独立窗口**等场景下，才需按需执行一条 `agent-voice-input integrate`（详见 [`integrations/README.zh-CN.md`](integrations/README.zh-CN.md)）。

---

## 核心技术亮点

- **两段式 / 单跳双模式 + 50K 分块前缀缓存**：支持「一阶段专用转写 + 二阶段轻量精修」或「多模态单跳直出」；对话历史按 50K 封口只读块前置以稳定命中前缀缓存，连续口述后台并发精修并严格按顺序保序落字。
- **窗口焦点 + 终端会话 + SSH 远端防串台路由**：自动锁定当前终端、Ghostty 分屏或 `tmux` 窗格的活跃会话与 `git status` 文件名；桌面 IDE 仅挂载本地工作区不泄终端对话，聊天/浏览器自动切通用润色；SSH 远端默认硬隔离防本地串台，加一行 `[remote] hosts` 白名单即可零远端安装异步预取远端上下文。
- **零第三方 Python 依赖纯标准库 + 声学静音门控与长停顿压缩**：纯 Python `>= 3.10` 标准库搭配宿主机 `ffmpeg` 与原生轻量桥接（无 Electron）；本地静音门控、自适应长停顿压缩（`[audio].silence_trim = "balanced" | "conservative" | "off"`）与响度归一化在上传前拦截空触并压缩长思考空白，支持最长 10 分钟口述（超 3 分钟按自然停顿自动切分），失败或取消在途任务的录音以 `0600` 权限暂存供 `retry` 恢复。
- **macOS Ghostty / `tmux` 零剪贴板直投 + 默认绝不自动回车**：按下即锁起点窗口，处理期间切走焦点自动退回剪贴板防误贴；**macOS** 下 Ghostty 与 `tmux` 直接注入不碰系统剪贴板（WSL2 下即便在 `tmux` 内及其余窗口也走瞬态剪贴板快照并自动还原）；默认只粘贴绝不自动按回车，避免误执行命令。
- **100% 非交互 CLI，人机皆可顺畅驾驭**：全部命令零 `y/n` 弹窗，自带 `status --json`、`doctor --ping --json`、`diagnose --json` 与 `integrate --status --json` 结构化输出，方便手工排查，也天然适合交给 AI Agent 一站式安装与排障。

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
| **终端** | **其他内置终端与专用 SSH 客户端** | `🧪` | 已内置粘贴键位路由、防串台与识别规则（完整名单见下方折叠区）；**其他 CLI Agent（如 OpenCode、Aider 等）**走工作区 `git`/文件名兜底，暂不读其对话历史；待真机实测反馈 |
| **桌面 App** | **Antigravity / AGY 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | **Antigravity 2.0 独立桌面版**已在 macOS 真机实测通过（只读 `conversation_summaries.db` + 窗口标题精确/截断前缀匹配、工作区 `git`/文件名与 `brain/` 增量缓存及切会话防串台），同时兼容老版 VS Code Fork *（Windows 桌面版待真机反馈；WSL2 仅扫 WSL 内目录）* |
| **桌面 App** | **ChatGPT / Codex 统一桌面端** | `✅` *(macOS)* / `🧪` *(Windows)* | 已在 macOS 真机实测通过：统一桌面端（`com.openai.codex`）只读 `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl`，本地仓库任务自动升格为 `agent` 模式并挂载项目热词与增量缓存，普通云端 ChatGPT 聊天保持 `general` 模式（仅挂载个人热词，零串台），同窗切会话在投递前复核防串台；Windows（`ChatGPT.exe` / `Codex.exe`）当前提供精修落字与锁窗防串台 *（Windows/WSL2 跨系统上下文绑定已列入路线图）* |
| **桌面 App** | **Claude 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | 已在 macOS 真机实测通过（`com.anthropic.claudefordesktop`）：**Code & Cowork** 本地会话上下文绑定（有界只读 `AXWebArea` 路由与标题采集、`Claude`/`Claude-3p` 双目录扫描、`50K` 增量缓存及投递前同窗切会话复核）以及精修落字与剪贴板自动恢复，云端 Chat 保持 `general` 模式（仅挂载个人热词，零串台）*（WSL2 暂不支持 Windows Claude Desktop 会话绑定；Windows `Claude.exe` 提供精修落字与锁窗防串台）* |
| **桌面 App** | **其他桌面 IDE** | `🧪` | IDE（**VS Code**、**Cursor**、**Windsurf**、**JetBrains**、**Zed** 等）走 `coding_gui` 并挂载宿主工作区 `git`/文件名；**在 IDE 内置终端运行 CLI Agent 时同样按 `coding_gui` 处理**（支持精修落字与工作区文件名参考，但**跳过 CLI `sync_session`**，不读内置终端里的 `codex`/`claude`/`agy` 对话历史，也不区分内置终端分屏） |
| **桌面 App** | **其他任意桌面 App** *（浏览器、IM、聊天、笔记、邮件等）* | `⚪` | 默认 `hotkey.scope = "all"` 统一走 `general` 通用润色模式（仅挂个人热词，精修落字 + 锁窗防串台，不再穷举聊天或浏览器窗口标题） |

<details>
<summary><b>其他内置终端与专用 SSH 客户端完整清单（<code>🧪</code>）</b></summary>

- **macOS 终端（`🧪`）**：`Terminal.app`、`iTerm2`、`WezTerm`、`Kitty`、`Alacritty`、`Warp`、`Hyper`、`Rio`、`Tabby`
- **Windows 终端（`🧪`）**：`WezTerm`、`Alacritty`、`Ghostty`、`Tabby`、`Hyper`、`Rio`、`Warp`、`Mintty`、`PuTTY`、`KiTTY`
- **专用 SSH 客户端（`🧪`）**：`MobaXterm`、`Xshell`、`WindTerm`、`SecureCRT`、`Termius`、`Electerm`、`FinalShell`、`RoyalTS`

</details>

> 如果你在带 `🧪` 或 `⚪` 的环境里用起来顺畅（或遇到坑），欢迎提 Issue 告知「**系统 + 终端/App + 渠道**」，帮我们点亮更多 `✅`。

---

## 场景化推荐供应商组合

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
2. **默认不落盘音频与原文**：运行状态、日志与本地诊断日志（`diag.jsonl`）均做凭据脱敏且绝不记录用户口述原文、会话上下文或热词正文；仅在识别/投递失败或主动取消在途任务时，将有效录音以 `0600` 权限暂存于 `failed/`（保留 24h 内最近 10 条）供 `retry` 恢复（录音中取消直接丢弃）。可选 `[archive]` 本地历史归档（`save_transcripts` → `transcripts/YYYY-MM.jsonl`、`save_audio` → `audio/<id>.mp3`）默认关闭，严格采用 `0700`/`0600` 权限且由 `.gitignore` 默认拦截。
3. **`0700`/`0600` 权限隔离与不越界抓取**：配置与运行目录严格私有权限；仅读取前台窗口名、macOS `AXDocument`、CLI Agent 明文日志及 AGY/Codex 只读会话索引——绝不读取 VS Code/Cursor 私有状态库（`state.vscdb`），也不抓取聊天软件或浏览器的无障碍 DOM；切到非终端应用硬跳过终端 CLI 会话，SSH 远端仅查显式白名单主机且零密钥落盘。
4. **仅传当前窗口对话与文件名（不传仓库全文）+ `auto` 凭据说明**：编码模式下仅发送当前活跃会话近期对话与 `git status`/浅层文件名，绝不上传仓库全文。同时透明提示：默认 `provider = "auto"` 会检测环境变量/`.env` 及本机已有 `codex` CLI 登录态（`agy` 需显式配置，绝不自动触发）；如需固定渠道请显式指定 `provider` 或设 `refiner.provider = "off"`。

---

## 日常运维与高频踩坑 FAQ

- **MacBook 没有右 Ctrl 键怎么办？**  
  无论是否运行过 `agent-voice-input init`，MacBook 内置键盘在初始化与运行时都会自动识别并默认采用 `key = "RCommand"`（右 Command），台式 Mac（如 Mac mini）外接标准键盘则保持 `key = "RControl"`；`config.toml` 中显式配置的 `key` 或命令行 `--hotkey` 始终优先。避坑提示：`ROption` 易撞豆包输入法，`Fn` 撞 macOS 系统 🌐 键（如要用 `Fn` 需在 系统设置 → 键盘 将「按下 🌐 键时」设为**不执行任何操作**），`RShift` 撞第三方中文输入法单按切中英文。随时可在 `~/.agent-voice-input/config.toml` 的 `[hotkey]` 中切换后执行 `agent-voice-input restart`，并用 `agent-voice-input key-test` 确认键码。
- **如何随终端自动启动、升级或彻底卸载？**
  - **随终端幂等自启**（在 `~/.zshrc` 或 `~/.bashrc` 加入一行）：
    ```bash
    [ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1
    ```
    > [!WARNING]
    > **macOS 切勿用 `launchd`（LaunchAgent）在后台拉起守护进程！** 经 `launchd` 启动会丢失 GUI 终端的麦克风与自动化 TCC 权限归属，导致录音静默全 0 静音；请务必在本机已授权的图形终端内启动。
  - **升级**：`uv tool upgrade agent-voice-input && agent-voice-input restart`（或 `pipx upgrade agent-voice-input && agent-voice-input restart`）。
  - **彻底卸载**：
    ```bash
    agent-voice-input stop
    agent-voice-input integrate --uninstall   # 可选：移除受管 CLI 钩子
    uv tool uninstall agent-voice-input       # 或 pipx / pip uninstall
    # 如需彻底清理数据，再按下方位置表删除配置、运行与缓存目录
    ```
  - **文件存放位置**：
    | 路径 | 用途 |
    | :--- | :--- |
    | `~/.agent-voice-input/` | 应用家目录（`config.toml`、`.env`、`hotwords/personal.txt`、可选 `audio/` 与 `transcripts/`；受 `AGENT_VOICE_INPUT_HOME` 覆盖） |
    | `~/.cache/agent-voice-input/` *（或 `$XDG_RUNTIME_DIR/agent-voice-input`）* | 运行目录（`control.sock`、`daemon.lock`、`daemon.log`、`diag.jsonl`、`failed/`、`sessions/`、`integrate-backups/`） |
    | `~/Library/Caches/agent-voice-input/` *（macOS）* | 编译后的 `mac_shim` 缓存目录 |
- **为什么通过 SSH 在 macOS 上启动守护进程录不到声音（全静音）？**  
  macOS 的麦克风 TCC 权限归属责任图形进程，经 `sshd`（或 `launchd`）拉起的守护进程会被系统静默喂全 0 静音帧。请务必在本机已获麦克风授权的图形终端（如 Ghostty 或 `Terminal.app`）里执行 `agent-voice-input start` 或 `restart`。
- **为什么 WSL2 下无法自动粘贴到 Windows 管理员提权窗口？**  
  受 Windows UIPI（用户界面特权隔离）机制限制，非提权的 `win_shim` 无法向管理员权限窗口发送模拟粘贴按键；此时精修结果已安全留存在剪贴板，手动按 `Ctrl+V` 即可粘贴。
- **后台守护进程怎么走代理？**  
  可在终端导出 `HTTPS_PROXY` / `HTTP_PROXY`（以及本机回环排除 `NO_PROXY=127.0.0.1,localhost`），或写入 `~/.agent-voice-input/.env`（系统环境变量优先），然后执行 `agent-voice-input restart`。由于纯标准库 HTTP 客户端仅支持 HTTP/HTTPS 代理、不支持 SOCKS5，如果代理客户端只设置了 `ALL_PROXY=socks5://...`，请同时设置对应的 HTTP 混合端口（`HTTPS_PROXY=http://127.0.0.1:<port>`），并用 `agent-voice-input doctor --ping` 验证连通。
- **为什么默认只粘贴不自动按回车？**  
  终端与 SSH 下误听一条命令直接回车风险太高，因此默认只粘贴不发送 `Enter`，方便扫一眼再确认；处理期间若切走焦点，也会自动退回剪贴板而不强贴到别的窗口。
- **两段式（STT + 精修）和一阶段单跳多模态怎么选？**  
  **两段式**组合最灵活（含仅一把 `DEEPSEEK_API_KEY` 的完整两段式）、可免费复用现有 Codex/AGY CLI 订阅，且长句与复杂术语纠偏最稳；**单跳多模态**（如原生 `gemini`、百炼 `qwen3.8-omni-flash`、官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini` 或 OpenRouter 多模态模型）只需一把 Key 和一次请求，但会把音频与会话上下文一并直接发给该多模态渠道。

---

## 常用命令与可选增强

```bash
agent-voice-input init [--asr P] [--refiner P] [--hotkey K] [--force]  # 智能初始化 ~/.agent-voice-input/（0600 config.toml 与 .env + .gitignore）
agent-voice-input start | restart | stop                # 启停或重启后台守护进程（start 幂等，代码/版本漂移时提示 restart）
agent-voice-input status [--history N] [--json]         # 查看运行状态、当前生效管线、Archive: 归档状态、热词计数与漂移信息
agent-voice-input doctor [--ping] [--remote] [--json]   # 全面自检音频、原生桥接、凭据、热词、钩子、SSH 就绪度与端点探活
agent-voice-input integrate [--status] [--dry-run] [--cli claude|codex|agy] [--uninstall] [--json]  # 安全一键接入可选 CLI 钩子
agent-voice-input diagnose [--last N] [--json]          # 本地只读诊断：延迟 p50、缓存命中率、失败录音数与脱敏日志尾部
agent-voice-input retry [--list] [--print]              # 查看或重跑暂存的失败录音
agent-voice-input hotwords list|add|remove|path         # 管理扁平个人热词 personal.txt、! 置顶与 <- 空耳映射（--pin、--alias、--json）
agent-voice-input once --duration 4 --dry-run           # 单次录音验证流水线（不粘贴上屏；Linux 可用 `--file <wav> --dry-run` 离线转写）
agent-voice-input toggle | cancel [--seq N] [--json]    # 命令行触发起止录音或取消当前/后台在途任务
agent-voice-input key-test                              # 测试按键扫描码与修饰键捕获
```

- **AI Agent 一站式安装与排障契约**：[`docs/agent-setup.md`](docs/agent-setup.md)（英文）
- **可选多窗口钩子与 SSH 远端 Linux（`[remote]`）**：单窗口终端、Ghostty 分屏与 `tmux` 分屏零钩子即可用。若常在 WSL2 **同一项目目录**并行开多个独立终端窗口（推荐使用 `agent-voice-input integrate`），或需拉取远端 Linux 服务器上的活跃 Agent 上下文（在 `config.toml` 配置 `[remote] hosts = ["devbox"]` 并运行 `agent-voice-input doctor --remote`），详见 [`integrations/README.zh-CN.md`](integrations/README.zh-CN.md)（SSH 章节：[SSH 远端 Linux 主机](integrations/README.zh-CN.md#5-ssh-远端-linux-主机零安装直连--可选单文件标题钩子)）与 [`docs/context-routing.md`](docs/context-routing.md)。

---

## 热词管理（支持让你的 Agent 帮你打理）

用一份扁平纯文本词表统一管理人名、项目专名与顽固空耳，并可直接交给 **Claude Code**、**Codex** 或 **AGY** 冷启动建库与定期瘦身：

- **扁平单文件与 dotfiles 目录软链**：`~/.agent-voice-input/hotwords/personal.txt`（可通过 `agent-voice-input hotwords path` 查看路径与软链真身；修改后下次口述自动热加载；目录级 `flock` 写锁零 `.lock` 脏文件）。如需跨机同步，推荐**对 `hotwords/` 目录建软链**（如 `ln -sfn ~/.dotfiles/avi-hotwords ~/.agent-voice-input/hotwords`），不要对 `personal.txt` 建单文件软链（读写侧均会拒绝；注：`~/.agent-voice-input/.gitignore` 默认只放行 `hotwords/`，Git 不会跨目录软链追踪目标目录本身，符合预期）。
- **置顶（`!`）与可选空耳语法（`<-`）**：高频核心词可加 `!` 前缀置顶（如 `!Antigravity` 或 `agent-voice-input hotwords add Antigravity --pin`）；顽固误听可写 `规范词 <- 误识1, 误识2` 或执行 `agent-voice-input hotwords add Harness --alias 哈尼斯,哈尼思`（单条最多 5 个误识、拒单字误识、双字给出提醒、禁与规范词冲突；多个词共享的歧义别名在运行期自动隐藏但保留两侧规范词）。一阶段 ASR 仅收规范词，二阶段/单跳用空耳精准纠偏。
- **双阶段投影与配额预算**：
  - **一阶段 ASR**：按权重分层（`! 置顶` > `带空耳` > `普通词`，层内保留文件行序防中文专名被字母序挤出窄窗）。`general` 模式 100% 配额归个人词；`agent` 模式为仓库/会话动态词保底至少 30% 名额并在有空位时自动回流（阿里百炼 **150 词 / 1,800 字**、豆包 **50 词 / 500 字**、OpenAI `keywords[]` **50 词**、OpenRouter `keyterms` **50 词**、Whisper **35 词 / 353 字**；`deepseek-asr` 由二阶段兜底）。
  - **二阶段 / 单跳块 0（`<vocabulary_hints>`）**：按 `(权重, term.lower(), term)` 确定性排序，改注释、空行或调整同权重行序不影响块 0 字节、稳保 85%–99% 前缀缓存；`agent` 与 `general` 模式统一提供 **200 条 / 3,000 字** 宽口词表预算，随时运行 `agent-voice-input hotwords list [--json]` 查看用量与诊断告警。
- **两条 Agent 协作纪律**：**改动 `personal.txt` 前必须先展示 diff 经用户确认再落盘；不要凭空瞎猜空耳，等实战真正撞见错听后再补 `<-` 映射**。

可直接复制发给你的 Coding Agent：

```text
# 1. 首次冷启动建库（只提炼规范词，不猜空耳）
读我最近几个项目和 README/规范，帮我提炼一份去噪后的扁平个人与跨项目高频专名草案写入 ~/.agent-voice-input/hotwords/personal.txt（仅对最高频核心词加 ! 置顶），不要凭空猜空耳，先给我看 diff，确认后再落盘。

# 2. 遇错听秒补空耳映射
刚才语音把 "Harness" 听成了 "哈尼斯"，帮我用 `agent-voice-input hotwords add Harness --alias 哈尼斯` 补一条空耳映射（先给我看变动再执行）。

# 3. 定期体检瘦身与调优排序
运行 `agent-voice-input hotwords list --json` 检查是否临近一/二阶段预算或存在告警，把低频过时词裁掉、把跨项目高频词加 `!` 置顶，先出 diff 给我确认后再改。
```

---

## 详细参考

<details open>
<summary><b>内置渠道与验证状态矩阵及常见 <code>config.toml</code> 配方</b></summary>

配置覆盖优先级：`CLI 参数 > ~/.agent-voice-input/config.toml > 内置默认值`（根目录可通过 `AGENT_VOICE_INPUT_HOME` 覆盖）。凭据存放于 `~/.agent-voice-input/.env`（见 [`config.example.env`](config.example.env)）。

| `provider` | 可用阶段 | 验证状态 | 内置默认 `asr.model` | 内置默认 `refiner.model` | 凭据来源 / 环境变量 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **`codex`** | `refiner` | `✅` | — | `gpt-6-luna` *(账号未启用默认模型时自动回退 `gpt-5.4-mini` / `gpt-5-codex-mini`)* | 复用本机 `codex` CLI 登录态 *(无状态 HTTP `/responses` 直连 + 30 分钟提前安全刷新，绝不开启 Fast 模式)* |
| **`agy`** | `refiner` | `✅` | — | `gemini-3.8-flash` *(默认 `effort = "low"`；需显式启用，不参与 `auto` 自动选路)* | 复用本机已登录的 `agy` CLI *(零工具 `agy acp` stdio JSON-RPC + 增量续接前缀缓存)* |
| **`deepseek`** | `asr`, `refiner` | `✅` *(双段实测)* | `deepseek-asr` *(官方 `POST /v1/tools/asr`；仅支持 `asr.api = "auto"`；自动检测 21 种语言，不支持热词与语言参数)* | `deepseek-flash` | `DEEPSEEK_API_KEY` *(兼容 `API_KEY_DEEPSEEK`；一把 Key 零配置跑通完整两段式)* |
| **`dashscope`** | `asr`, `refiner` | `✅` *(转写/单跳/精修均实测)* | `qwen3-asr-flash` *(或单跳 `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(兼容 `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `✅` *(1.0 & 2.0)* | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *（单头 `X-Api-Key`，无需 `APP_ID`；兼容旧 `DOUBAO_ASR_API_KEY` 或双头 `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`；`asr.doubao_resource_id = "auto"` 优先同步 `volc.bigasr.auc_turbo`，未开通时自动回退并缓存 `volc.seedasr.auc` / `volc.bigasr.auc`）* |
| **`volcengine`** | `refiner` | `✅` *(`doctor --ping` & 精修)* | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(兼容 `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `✅` *(转写/单跳/精修)* | `qwen/qwen3-asr-1.7b` | `google/gemini-3.8-flash` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `✅` *(单跳 & 精修)* | `gemini-3.7-flash` *(单跳)* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `✅` *(转写/单跳/精修)* | `gpt-transcribe` *(或单跳 `gpt-audio-1.5` / `gpt-audio-mini`)* | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `🧪` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `🧪` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `🧪` | `gpt-4o-mini-transcribe` *(需配 `base_url`)* | *(需显式配 `base_url` + `model`)* | `api_key_env` 或 `OPENAI_API_KEY` *(本机回环地址免 Key)* |
| **`off`** | `refiner` | `✅` | — | *(关闭二阶段，直出一阶段原文)* | 无需凭据 |

- `asr.provider = "auto"`（默认）：按 `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow` 顺序选用首个已配置凭据的渠道。
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
provider = "dashscope"           # 亦可设为 "gemini"（免填 model）、"openai" + model = "gpt-audio-1.5" / "gpt-audio-mini"，或 "openrouter" + model = "google/gemini-3.8-flash"
model = "qwen3.8-omni-flash"
```

</details>

<details>
<summary><b>双平台机制细节（WSL2 WASAPI / macOS TCC 权限与投递）</b></summary>

### Windows Terminal + WSL2（含远程桌面与 `tmux`）
- **全局作用域、远程桌面让行与高分屏胶囊**：默认单击右 Ctrl（`RControl`）即可在终端、IDE 与日常桌面应用中开始/结束录音（`hotkey.scope = "all"`，可选 `"coding"` / `"terminals"`；组合键如 `RControl+C` 原样放行），显示原生 Per-Monitor V2 抗锯齿波形胶囊。聚焦远程桌面/虚拟机客户端（`mstsc`、`vmconnect`、Parsec、RustDesk 等）时静默让行热键（`yield_remote_desktop = true`；默认放行裸 `msrdc` 以保护 Win11 WSLg，非 WSLg 的 AVD/Windows 365 用户可加 `ignore_apps = ["msrdc"]`）。WSL2 下即便在 `tmux` 内，投递仍走 `win_shim` 瞬态剪贴板快照 + 按进程路由粘贴快捷键（`Ctrl+V`、`Ctrl+Shift+V` 用于 `WezTerm`/`Alacritty`/`Ghostty`/`Tabby`/`Hyper`/`Rio`/`Warp`/`WindTerm`/`SecureCRT`/`Termius`/`Electerm`，`Shift+Insert` 用于 `mintty`/`PuTTY`/`KiTTY`/`MobaXterm`/`Xshell`），粘贴后还原且不污染原剪贴板内容。
- **原生 WASAPI 采集**：常驻 `win_shim` 直接调用 Windows WASAPI 16 kHz 采集，避开 WSLg `RDPSource` 休眠零包死锁；起止提示音默认关闭以免触发 RDP 音频通道唤醒延迟。
- **零配置 `tmux` 分屏追踪**：无需改动 `~/.tmux.conf`，自动锁定当前 attached 客户端的焦点窗格。

### macOS（`Ghostty`、`tmux` 与其他终端）
- **原生 Retina 胶囊与安全投递**：首次启动经 `/usr/bin/swiftc` 编译缓存原生 `mac_shim`，监听 `RControl` / `RCommand` / `ROption` / `Fn` 短按或 `ptt` 按住说话、远程桌面自动让行（`Windows App`、`屏幕共享`、Parsec、VMware/Parallels 等）并渲染 Retina 胶囊。仅在 **macOS 的 Ghostty 与 `tmux`** 内直投不碰系统剪贴板；其他窗口锁定起点窗口 ID 后走原生 `CGEvent` `Cmd+V` 并带剪贴板快照还原。
- **零钩子分屏与 `tmux` 绑定**：读取 Ghostty 分屏 `tty`/`cwd`、`Terminal.app`（`🧪`）当前标签页 `tty`、`iTerm2`（`🧪`，零弹窗自动化预检并支持 PTY 回退）或终端子进程 PTY 树（含 `tmux` 焦点窗格）绑定 `codex`/`claude`/`agy` 会话，并在非终端 GUI 窗口（含 IDE 内置终端）硬跳过 CLI 会话查询；默认使用系统麦克风（`[audio] macos_device = "default"`）。
- **轻量 TCC 权限**：所有权限均归属于**执行 `start` 的宿主终端**（换终端重启需重新授权）。需授予**麦克风权限**（必选，供 `ffmpeg`）以及按所用终端授予一次**自动化（Automation → Ghostty / Terminal / iTerm2）**；默认修饰键热键（`RControl` / `RCommand` / `ROption` / `Fn`）**不需要**「输入监控（Input Monitoring）」；向桌面 GUI 应用与非 `tmux` 下的非 Ghostty 终端发送 `Cmd+V` 以及在第三方终端/桌面端读取窗口标题与会话绑定时，需为宿主终端开启「辅助功能（Accessibility）」（仅在 Ghostty 或 `tmux` 内直投时无需该权限）。

</details>

<details>
<summary><b>进阶配置说明（协议路由、前缀缓存、热词、重试与本地归档）</b></summary>

- **按 `provider` 选渠道与界面语言（`model`、`ui.language`）**：`[asr]` 与 `[refiner]` 的 `model` 留空时自动匹配内置默认模型；通过 CLI 切换 `provider` 且未传 `--*-model` 时也会重置为新渠道默认模型。显式指定 `model` 时原样透传上游 ID。`ui.language` 支持 `"auto"`、`"zh"`、`"en"`，同时兼容标准 BCP 47 标签（如 `"zh-CN"` / `"en-US"`）。
- **两阶段 vs 单跳协议（`asr.api`、`asr.doubao_resource_id`）**：`"auto"` 将专用 STT（`qwen3-asr-flash`、`doubao`、`deepseek-asr`、`whisper-*`、`*-transcribe`、`SenseVoiceSmall`）路由至一阶段 + 二阶段，将原生 `gemini`、百炼 `qwen*-omni*`、官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini`（或显式 `asr.api = "chat-completions"`）与 OpenRouter/OpenAI 兼容多模态模型路由至单跳（共享 50K `<session_reference_log>` 前缀块、`>= 3s` 内存压缩 `64 kbps` MP3、上限 `610s`；默认跳过二阶段，除非 `refiner.force_on_single_hop = true`）。DeepSeek 官方 ASR（`POST https://api.deepseek.com/v1/tools/asr`）仅支持 `asr.api = "auto"`（支持 21 种语言自动检测，不支持热词与语言参数）。豆包槽位通过 `asr.doubao_resource_id = "auto"`（或显式槽位 ID）与 `asr.doubao_mode` 控制。在 `openrouter` 使用未收录新模型时请显式指定 `asr.api = "audio-transcriptions"` 或 `"chat-completions"`。
- **二阶段协议、思考档位与会话轮次（`refiner.api`、`effort`、`session_max_turns`）**：`refiner.api = "auto"` 将 `codex` 与官方 `openai`（`api.openai.com`）路由至无状态 HTTP `/responses`（`store=false`、`stream=true`），`agy` 路由至本机 `agy acp` stdio JSON-RPC，`gemini` 路由至 `:generateContent`，自定义第三方 `openai` `base_url` 及其他渠道保守默认走 `/chat/completions`（显式配置 `"responses"` / `"chat-completions"` 始终优先）。`asr.effort` 与 `refiner.effort` 默认留空 `""`（使用工具为当前渠道调优的推荐默认：精修端 `codex`/`agy`/`openai` 为 `"low"`，其余精修渠道及一阶段单跳音频为 `"none"`），支持 `"default"`（不传思考参数，听上游默认）或 `"none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`（`"none"` 尽力关闭思考，强制思考模型自动落到最低合法档位，非思考模型自动忽略）。针对 `agy`，`refiner.session_max_turns = 0`（默认）不设固定轮次上限，随 200K 淘汰或词表变化自然轮换。
- **附加偏好指令、前缀缓存与冷启动回退（`refiner.extra_instructions`、`include_git_status`、`context_max_chars`、`cold_start_fallback`）**：默认始终启用内置自适应 `agent` / `general` 精修提示词，可通过 `refiner.extra_instructions` 在内置规则之后安全追加个人偏好（支持字符串数组、单行或多行文本，上限 20 条 / 2,000 字，两段式与单跳统一生效；留空时与默认提示词逐字节一致）。近期对话（上限 `context_max_chars = 200000`）按 `50K` 封口块前置，末尾拼装 `<workspace_git_status>`（工作树干净时补充核心追踪文件名）与 `<asr_draft_to_clean>`。新开空/极短会话时，`cold_start_fallback = "auto"`（可选 `"hotwords"` / `"off"`）自动从同目录近期历史会话补充热词并前置 1 块冻结的 `<= 25K` `<prior_session_reference>`。
- **扁平个人热词表（`hotwords/personal.txt` 与 `hotwords` 子命令）**：直接编辑 `~/.agent-voice-input/hotwords/personal.txt` 或运行 `agent-voice-input hotwords list|add [--pin] [--alias]|remove|path`，下一次口述自动热加载生效（目录级 `flock` 写锁零 `.lock` 脏文件）。一阶段按权重分层并层内保行序，`agent` 模式为动态代码词保底 30% 并空位回流（`general` 模式 100% 归个人词；百炼 `150 词 / 1,800 字`、豆包 `50 / 500`、OpenAI `50`、Whisper `35 / 353`）；二阶段/单跳块 0 按确定性顺序渲染（`200 条 / 3,000 字`）以稳定保住前缀缓存。
- **同渠道重试与可选本地归档（`max_retries`、`[archive]`）**：两阶段默认 `max_retries = 2`（`0..10`），在阶段 `timeout_sec` 与 9 秒建连/TLS 上限内分别对 `429`/QPS 限流与瞬态网络/5xx 退避重试。`[archive]` 默认全关（`0700` 目录 / `0600` 文件、受 `.gitignore` 默认拦截，并在 `status`/`doctor`/`start` 中以 `Archive:` 摘要展示）：开启 `save_transcripts = true` 后按月追加 12 字段纯转录 JSONL（仅含 ASR 草稿、精修终稿与扁平元数据，绝不写入上下文/热词/凭据）至 `~/.agent-voice-input/transcripts/YYYY-MM.jsonl`；开启 `save_audio = true` 后异步将有效录音压缩为 `16 kHz` `64 kbps` MP3 存入 `~/.agent-voice-input/audio/<id>.mp3`（按 `audio_max_count = 1000` 环形裁剪，`0` 表示不淘汰）。转录记录、音频文件与 `diag.jsonl` 共享同一短 ID（`YYYYMMDD-HHMMSS-<4hex>`）实现 1:1 关联。

</details>

<details>
<summary><b>核心 CLI 覆盖参数</b></summary>

| CLI 参数 | 说明 |
| :--- | :--- |
| `--config PATH` | 指定 `config.toml` 或 `config.json` 路径（亦支持 `AGENT_VOICE_INPUT_CONFIG` 与 `AGENT_VOICE_INPUT_HOME`） |
| `--asr`, `--asr-provider` | 一阶段渠道（`auto`, `dashscope`, `doubao`, `deepseek`, `openai`, `gemini`, `groq`, `openrouter`, `siliconflow`, `openai-compatible`；别名 `volcengine`/`volc` → `doubao`、`bailian` → `dashscope`） |
| `--asr-model` | 一阶段上游模型 ID |
| `--asr-api` | 一阶段协议（`auto`, `audio-transcriptions`, `chat-completions`） |
| `--asr-effort` | 一阶段思考档位 |
| `--refiner`, `--refiner-provider` | 二阶段渠道（`auto`, `codex`, `agy`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`；别名 `doubao`/`volc`/`ark` → `volcengine`、`bailian` → `dashscope`） |
| `--refiner-model` | 二阶段上游模型 ID |
| `--refiner-api` | 二阶段协议（`auto`, `chat-completions`, `responses`） |
| `--refiner-effort`, `--effort` | 二阶段思考档位 |

*（环境变量用于厂商凭据、引导路径 `AGENT_VOICE_INPUT_HOME` / `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR` 及底层运行期诊断项；常规参数统一放 `config.toml`。）*

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
