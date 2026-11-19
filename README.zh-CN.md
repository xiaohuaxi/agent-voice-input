[English](https://github.com/xiaohuaxi/agent-voice-input/blob/main/README.md) | **简体中文**

# agent-voice-input

[![PyPI version](https://img.shields.io/pypi/v/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/agent-voice-input.svg)](https://pypi.org/project/agent-voice-input/)
[![CI](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaohuaxi/agent-voice-input/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)

专为命令行 Coding Agent（**Codex**、**Claude Code**、**Antigravity CLI / `agy`**）打造的**会话感知（Context-Aware）语音输入工具**。

通用语音输入法听不懂你项目里的文件名和变量名；`agent-voice-input` 会自动锁定当前终端窗口/分屏里正在工作的 Agent 会话，结合近期对话与 `git status` 真实上下文对语音转写结果做轻量精修，在粘贴进终端前把同音标识符、中英混说术语与口语中途改口一次修准：

```text
🎙️ 你对着终端随口说：
   “帮我看一下那个平台微埃斯艾尔点派里面 max duration 秒数的默认值，
    把三分钟改成五分钟，呃不对算了改成十分钟，顺便跑一下单测别接管道 head”

❌ 普通语音听写：
   帮我看一下那个平台WSL点派里面max duration秒数的默认值，把3分钟改成5分钟，呃不对算了改成10分钟，顺便跑一下单测别接管道head

✅ agent-voice-input（结合当前 Agent 会话与代码仓上下文精修后粘贴，不自动按回车）：
   帮我看一下 platform_wsl.py 里面 max_duration_sec 的默认值，改成 10 分钟，顺便跑一下单测，别接管道 head。
```

> 已验证支持 **Windows Terminal + WSL2（含远程桌面与 `tmux`）** 与 **macOS（`Ghostty`、系统自带 `Terminal.app` /「终端」及 `tmux`）**；其他常用 macOS 终端（`iTerm2`、`WezTerm`、`Alacritty`、`Kitty`、`Warp` 等）已内置窗口级防串窗、子进程 PTY 会话发现与 `Cmd+V` / `tmux` 粘贴基线支持，欢迎在对应环境实测并提交 PR。

---

## 核心亮点

- **精准绑定活跃 Agent 上下文，同时开箱通用全桌面**：在终端内自动识别前台窗口、Ghostty 分屏或 `tmux` 焦点窗格（无需修改 `~/.tmux.conf`）中的主 Agent 会话，增量抽取最近对话与 `git status` 变更文件名（非 Git 普通项目/文稿目录自动提取浅层文件名）纠偏，严格隔离后台 subagent 与相邻分屏；同时默认支持任意桌面窗口语音输入（`hotkey.scope = "all"`，亦可设为 `"coding"` 或 `"terminals"`）：主流桌面 IDE（`VS Code`、`Cursor`、`Windsurf`、`Zed`、`JetBrains`、`Xcode` 等）自动定位当前本地工作区（兼容 Git 仓库与普通文件夹）挂载文件拼写参考且硬隔离后台终端会话，桌面版 **Antigravity IDE** 更可关联该工作区最近活跃的同构 `brain/` 主会话；AI 聊天桌面端、即时通讯与日常窗口自动切换自适应通用模式，聚焦远程桌面/虚拟机客户端时自动静默让行热键（`yield_remote_desktop = true`）。
- **按渠道即插即用的「识别 + 精修」双引擎**：默认采用「极速语音转写 + 复用本机 `codex` 登录态的无状态 Keep-Alive 精修」，也支持切换任意内置云端/本地 API 渠道，或使用百炼 `qwen3.8-omni-flash` / Gemini / OpenRouter 多模态单跳共享 50K 缓存直出；日常只需指定 `provider`，自动匹配对应渠道的默认模型。
- **面向长口述与连续口述的可靠录音链**：支持在上一句转写/精修期间直接开始下一段录音——二阶段无状态精修自动并发重叠、上屏严格按 `seq_id` 先后保序落字（切走窗口回退剪贴板的多段结果自动空行拼接、不覆盖前文）；支持随时用 `agent-voice-input cancel`（或在 `tap_toggle` 非录音态下单独长按热键 `1.2s–8s`、即超出 `hotkey.tap_max_ms` 后松开）即时取消当前录音或中断后台在途识别/精修；内置静音门控与响度归一化拦截空触；单次默认最长 10 分钟（最后 15 秒胶囊倒计时提示），超过 3 分钟的长语音自动挑自然停顿处切分并并发转写；无麦克风或中途掉麦即时提醒；转写/上屏失败或主动取消的有效语音均自动以 `0600` 权限暂存，随时用 `agent-voice-input retry` 恢复，绝不丢话。
- **原生高分屏波形胶囊与防串窗安全粘贴**：Windows（Per-Monitor V2 HiDPI）与 macOS（Retina 原生面板）均提供不抢焦点的悬浮胶囊（计时、实时声波与阶段进度自动跟随系统中英文）；开始录音即锁定目标窗口/分屏，按进程自动路由粘贴快捷键（Windows 区分 `Ctrl+V` / `Ctrl+Shift+V` / `Shift+Insert`；macOS 在 Ghostty 与 `tmux` 内零触碰系统剪贴板直投，其余窗口走原生 `CGEvent` `Cmd+V`），只粘贴不自动回车，中途切走焦点则自动退回剪贴板防误投。
- **零第三方 Python 依赖**：仅使用 Python `>= 3.10` 标准库与宿主机 `ffmpeg`（WSL2 复用系统内置 `powershell.exe`，macOS 首次启动自动编译缓存原生 `mac_shim`）。

---

## 快速开始

### 方式一：直接让你的 Coding Agent 帮你安装配置（推荐）

把下面这段话直接发给终端里的 **Codex**、**Claude Code** 或 **AGY** 即可完成安装、配妥凭据并后台启动：

```text
请帮我安装并配置 agent-voice-input（https://github.com/xiaohuaxi/agent-voice-input）：
1. 检查 python3 (>=3.10) 与 ffmpeg（macOS 下顺带确认 Xcode Command Line Tools `/usr/bin/swiftc`），用 `uv tool install agent-voice-input`（无 uv 则用 pipx）安装；
2. 检查我当前环境中是否已有可用凭据（如 DASHSCOPE_API_KEY、OPENROUTER_API_KEY、GEMINI_API_KEY、OPENAI_API_KEY、GROQ_API_KEY 等），没有就问我用哪家，写入 `~/.config/agent-voice-input/agent-voice-input.env`（权限 600）；
3. 若本机未安装或未登录 `codex` CLI，请在 `~/.config/agent-voice-input/config.toml` 将 `[refiner]` 的 `provider` 设为与该凭据匹配的渠道（无需手写 model）；
4. 运行 `agent-voice-input doctor` 确认各项检查通过后，执行 `agent-voice-input start` 启动后台守护进程。
```

### 方式二：手动三步安装

**1. 准备前置工具**

- **Python** `>= 3.10`
- **ffmpeg**：Ubuntu / WSL2 执行 `sudo apt install ffmpeg`；macOS 执行 `brew install ffmpeg`
- **Xcode Command Line Tools**（macOS 推荐）：执行 `xcode-select --install` 提供 `/usr/bin/swiftc`，首次启动会自动编译缓存原生热键与胶囊辅助程序（未安装时仍可用命令行 `toggle` / `once` 模式）。

**2. 安装 CLI**

```bash
uv tool install agent-voice-input
# 或使用 pipx：pipx install agent-voice-input
```

**3. 配置一把 API Key 并启动**

在 `~/.config/agent-voice-input/agent-voice-input.env` 填入任一支持的凭据（例如阿里百炼 `DASHSCOPE_API_KEY`，或一把 `OPENROUTER_API_KEY` / `GEMINI_API_KEY`）：

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
DASHSCOPE_API_KEY="<your-dashscope-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env

# 自检环境、渠道解析与当前会话识别，然后后台启动（默认单击右 Ctrl 开始/完成录音；识别/精修期间单独长按右 Ctrl 1.2s–8s 松开可取消）
agent-voice-input doctor
agent-voice-input start
```

> 如需调整快捷键（如改为按住说话 `trigger_mode = "ptt"`）、胶囊位置（`bottom` / `top`）、录音时长上限或切换渠道，将 [`config.example.toml`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.toml) 复制到 `~/.config/agent-voice-input/config.toml` 按需修改即可（完整凭据变量见 [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)）。

---

## 双平台体验说明

### Windows Terminal + WSL2（含远程桌面与 `tmux`）

- **全局热键作用域、远程桌面让行与高分屏胶囊**：默认单击右 Ctrl（`RControl`）即可在终端、IDE 与日常桌面应用中开始/完成录音（`hotkey.scope = "all"`，可选 `"coding"` / `"terminals"`；组合键如 `RControl+C` 原样放行，录音中途切到别的应用仍可正常结束），屏幕底部（或顶部）显示原生 Per-Monitor V2 高分屏抗锯齿波形胶囊。聚焦远程桌面/虚拟机客户端（`mstsc`、`vmconnect`、Parsec、RustDesk 等）时自动静默让行新热键触发（`yield_remote_desktop = true`；默认不拦截裸 `msrdc` 以保护 Win11 本地 WSLg 应用，不使用 WSLg 的 Azure Virtual Desktop / Windows 365 用户可在配置中添加 `ignore_apps = ["msrdc"]`），并按终端进程自动路由 `Ctrl+V`、`Ctrl+Shift+V`（`WezTerm`、`Alacritty`、`Tabby`、`Hyper` 等）与 `Shift+Insert`（`mintty`、`PuTTY`、`KiTTY`）。
- **原生 WASAPI 采集，告别 WSLg 与远程桌面卡顿**：默认通过常驻 `win_shim` 直接调用 Windows 原生 WASAPI 16 kHz 采集音频，彻底避开 WSLg `RDPSource` 休眠零包死锁；默认关闭起止提示音以避免远程桌面（RDP）1–2 秒音频通道唤醒延迟。
- **`tmux` 分屏开箱即用**：无需改动 `~/.tmux.conf`，自动追踪当前 attached 客户端的焦点窗格并隔离相邻后台分屏。

### macOS（`Ghostty`、系统自带 `Terminal.app` 与 `tmux`）

- **原生胶囊与跨场景安全投递**：通过缓存的原生 `mac_shim` 监听右 Ctrl（或 `ptt` 按住说话）、在远程桌面/虚拟机窗口（`Windows App`、`屏幕共享`、Parsec、VMware/Parallels 等）静默让行热键并渲染 Retina 悬浮波形胶囊。在 **Ghostty** 与 **`tmux`** 中直接注入文本且零触碰系统剪贴板；在其他终端、IDE 及桌面 GUI 应用中锁定起点窗口 ID 并通过原生 `CGEvent` `Cmd+V` 粘贴，中途切换窗口或标签页均自动退回剪贴板防误投。
- **零钩子标签页/分屏与 `tmux` 会话关联**：直接读取 Ghostty 分屏 `tty`/`cwd`、`Terminal.app` 当前选中标签页 `tty` 或终端窗口子进程 PTY 树（连接 `tmux` 时进一步定位对应焦点窗格），精准匹配当前标签页内的 `codex`、`claude` 或 `agy` 活跃会话，并在非终端 GUI 窗口中硬隔离后台 CLI 会话防跨仓串台；录音设备默认跟随系统麦克风（可通过 `[audio] macos_device` 指定）。
- **轻量系统权限（TCC）**：需授予**麦克风权限**（供 `ffmpeg` 录音），以及按所用终端授予一次**自动化（Automation → Ghostty 或 Terminal）**确认；默认 `RControl` 修饰键热键**不需要**开启「输入监控（Input Monitoring）」，向桌面 GUI 应用及非 `tmux` 下的非 Ghostty 终端发送原生 `Cmd+V` 粘贴时需为宿主终端开启「辅助功能（Accessibility）」（仅在 Ghostty 或 `tmux` 内直投时无需该权限）。

---

## 配置与渠道参考

配置由两个文件组成，覆盖优先级为 `CLI 参数 > config.toml > 内置默认值`：

- **`~/.config/agent-voice-input/config.toml`**：结构化配置 `[hotkey]`、`[ui]`、`[audio]`、`[asr]` 与 `[refiner]`。
- **`~/.config/agent-voice-input/agent-voice-input.env`**：存放 API Key（兼容前缀别名，见 [`config.example.env`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/config.example.env)）。

### `provider`、`model` 与工作模式约定

- **按 Agent / `provider` 选择即可，`model` 默认留空自动匹配**：`[asr]` 与 `[refiner]` 中的 `model` 留空（`""` 或不填）时，自动采用所选 `provider` 的内置默认模型；通过 CLI 切换 `provider` 且未显式指定 `model` 时，也会自动切回新渠道的默认模型，不会把旧渠道的模型名串过去。如需指定具体模型，原样填写该渠道的上游模型 ID 即可。
- **两阶段模式 vs 多模态单跳模式（`asr.api`）**：
  - 默认 `asr.api = "auto"` 会自动按渠道与模型选择协议：`dashscope`（默认 `qwen3-asr-flash`）、`doubao` 与专用 STT 模型（如 `qwen/qwen3-asr-1.7b`、`whisper-*`、`*-transcribe`、`SenseVoiceSmall`）走一阶段纯转写 + 二阶段语境精修；
  - 原生 `gemini`、百炼 `qwen*-omni*`（如 `qwen3.8-omni-flash`）以及 OpenRouter / OpenAI 兼容渠道下的多模态音频理解模型（如 `gemini-3.5-flash-lite`、`google/gemini-3.5-flash-lite`、`qwen/qwen3.8-omni-flash`、`gpt-audio*`）自动共享同一套 50K 封口 `<session_reference_log>` 前缀缓存块（`agent` 模式下会将最多 `refiner.context_max_chars` 会话历史发往所选多模态 ASR 渠道）、对 `>= 3s` 语音内存压缩为 `64 kbps` MP3、以 `610s` 为整段上限一步完成转写与精修，并默认跳过二阶段（除非设置 `refiner.force_on_single_hop = true`）。在 `openrouter` 下使用未内置收录的新模型时，请显式指定 `asr.api = "audio-transcriptions"` 或 `"chat-completions"`。
- **二阶段协议选择（`refiner.api`）**：支持 `"auto" | "chat-completions" | "responses"`。默认 `"auto"` 将 `codex` 路由至纯无状态 HTTP `/responses`（`store=false`、`stream=true`），将 `gemini` 路由至原生 `:generateContent`，其余兼容渠道保持 `/chat/completions`；在 `openai`、`openrouter` 或 `openai-compatible` 下亦可显式设 `refiner.api = "responses"` 启用无状态 `/responses` 引擎。
- **思考档位控制（`effort`）**：支持 `"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`。`asr.effort` 默认 `"auto"`；`refiner.effort` 按渠道自动默认（`codex` / `openai` 默认 `"low"`，其余渠道默认 `"auto"` 自动映射对应渠道的零思考极速参数，如 Gemini 3+ `minimal`、百炼 `enable_thinking=false`、火山方舟/官方 DeepSeek `thinking.type="disabled"`）。
- **精修提示词与前缀缓存组织（`refiner.prompt`、`include_git_status`、`context_max_chars`）**：`prompt` 默认留空 `""` 时使用内置自适应英文语音听写编辑提示词（`agent` 模式结合只读 `<session_reference_log>`、`<workspace_git_status>` 与常见研发/AI 术语修准同音错词并消解口语中途改口，`general` 模式面向日常口述自然润色且不掺入代码语境；显式配置非空 `prompt = '''...'''` 则覆盖两模式默认提示词）。终端历史对话（默认上限 `context_max_chars = 200000` 字符，满窗按四分之一即 `50K` 整块滑出）按固定只读分块置于前部（`/responses` 下作 `developer` 消息块，`/chat/completions` 下置于单条 `user` 消息前部），末尾 `user` 块依次拼装 `<workspace_git_status>`（`include_git_status = true`，工作树干净时自动补充仓库核心追踪文件名作拼写参考）、`<asr_draft_to_clean>` 与 `<task_reminder>`，稳定命中前缀缓存（`codex` 配合确定性会话 `prompt_cache_key` 与负载均衡亲和性 Cookie 实现高缓存命中且零历史草稿残留；一阶段在 `agent` 模式下亦会在用户 `hotwords.txt` 与实时变更/对话标识符之后自动追加低优 AI/CLI 兜底热词）。
- **同渠道重试与建连超时（`max_retries`）**：`[asr] max_retries` 与 `[refiner] max_retries` 默认 `2`（`0..10`，指首次请求之外的额外重试次数，`0` 关闭重试），在阶段 `timeout_sec` 总预算与独立的 9 秒建连/TLS 握手超时上限内，区分频率限流（`HTTP 429`、`Retry-After`、厂商 QPS 码）与瞬态通信/5xx 错误分别退避重试。
- **可选本地 MP3 录音环形归档（`audio.save_recordings`）**：默认关闭（`false`）。显式设为 `true` 时，后台异步通过 `ffmpeg` 将有效非静音录音压缩为 `16 kHz` 单声道 `64 kbps` MP3 以 `0600` 权限保存至 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/`，并按修改时间保留最新 `save_recordings_max_count`（默认 `300`，范围 `1..5000`）条，不阻塞实时识别与上屏耗时。

### 内置渠道一览

| `provider` | 可用阶段 | 内置默认 `asr.model` | 内置默认 `refiner.model` | 凭据来源 / 环境变量 |
| :--- | :--- | :--- | :--- | :--- |
| **`codex`** | `refiner` | — | `gpt-6-luna` | 复用本机 `codex` CLI 登录态 *(无状态 HTTP `/responses` 直连 + 30 分钟提前安全刷新，绝不开启 Fast 模式)* |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` *(或单跳 `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(兼容 `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` *(自动探测可用槽位)* |
| **`volcengine`** | `refiner` | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(兼容 `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-3.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-3.5-flash-lite` *(单跳)* | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(需配 `base_url`)* | *(需显式配 `base_url` + `model`，如官方 DeepSeek `deepseek-flash`)* | `api_key_env` 或 `OPENAI_API_KEY` *(本机回环地址免 Key)* |

- `asr.provider = "auto"`（默认）：按 `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow` 顺序自动选用首个已配置凭据的渠道。
- `refiner.provider = "off"`：关闭二阶段语境精修。

### 常见 `config.toml` 配方

```toml
# 配方 1：国内低延迟主力（百炼 ASR + 本机无状态 Codex 精修；未装 Codex 可将 refiner.provider 改为 "dashscope" 或 "volcengine"）
[asr]
provider = "dashscope"

[refiner]
provider = "codex"
```

```toml
# 配方 2：OpenRouter 一把 Key · 两阶段专用 STT + 轻量精修（自动使用各自默认模型）
[asr]
provider = "openrouter"

[refiner]
provider = "openrouter"
```

```toml
# 配方 3：多模态单跳音频 + 50K 缓存上下文直出（百炼 Qwen-Omni、OpenRouter 或原生 Gemini，自动跳过二阶段）
[asr]
provider = "dashscope"           # 亦可设为 provider = "openrouter" 搭配 model = "google/gemini-3.5-flash-lite"，或 provider = "gemini"
model = "qwen3.8-omni-flash"
effort = "auto"
```

---

## 常用命令与覆盖参数

### 子命令速查

```bash
agent-voice-input start                  # 后台启动守护进程
agent-voice-input restart                # 重启守护进程（修改配置后常用）
agent-voice-input status                 # 查看运行状态、最近一次耗时与错误摘要
agent-voice-input doctor                 # 全面自检音频链路、原生辅助层、凭据与当前会话匹配
agent-voice-input retry --list           # 查看暂存的失败录音（用 `agent-voice-input retry` 重新转写最近一条）
agent-voice-input once --duration 4 --dry-run  # 单次录音验证流水线（不粘贴上屏）
agent-voice-input toggle                 # 命令行触发开始/结束录音
agent-voice-input cancel                 # 取消当前录音，或中断后台在途识别/精修（可用 `--seq N` 指定单条）
agent-voice-input stop                   # 停止后台守护进程
agent-voice-input key-test               # 测试按键扫描码与修饰键捕获
```

### 核心 CLI 参数

| CLI 参数 | 说明 |
| :--- | :--- |
| `--config PATH` | 指定 `config.toml` 或 `config.json` 路径（亦可通过 `AGENT_VOICE_INPUT_CONFIG` 指定） |
| `--asr`, `--asr-provider` | 一阶段渠道（`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`） |
| `--asr-model` | 一阶段上游模型 ID |
| `--asr-api` | 一阶段协议（`auto`, `audio-transcriptions`, `chat-completions`） |
| `--asr-effort` | 一阶段思考档位 |
| `--refiner`, `--refiner-provider` | 二阶段渠道（`codex`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`） |
| `--refiner-model` | 二阶段上游模型 ID |
| `--refiner-api` | 二阶段协议（`auto`, `chat-completions`, `responses`） |
| `--refiner-effort`, `--effort` | 二阶段思考档位 |

*（环境变量用于存放各厂商凭据、引导路径 `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR` 及底层运行期诊断项；常规参数统一在 `config.toml` 中配置。）*

### 可选：多窗口并发会话标题钩子

在单项目目录单窗口、macOS + Ghostty 多分屏或 `tmux` 分屏下均无需额外配置。若常在**同一项目目录**并行开启多个独立 WSL2 终端窗口或非 Ghostty 终端分屏，可参考 [`integrations/`](https://github.com/xiaohuaxi/agent-voice-input/blob/main/integrations/README.zh-CN.md) 为 Codex、Claude Code 或 Antigravity CLI 接入轻量会话标题标签（`agent-voice-input session-hook`）。

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

[Apache-2.0 License](https://github.com/xiaohuaxi/agent-voice-input/blob/main/LICENSE)
