[English](guide.md) | **简体中文**

# 使用指南、兼容性矩阵、热词管理与常见问题

## 支持与验证状态矩阵

**状态图例**：
- `✅` **真机实测**：作者在真实设备上日常使用或端到端实测通过。
- `🧪` **代码与 CI 验证，待真机反馈**：已针对该环境或渠道做专属适配并通过离线单测或 CI 实机自检（真实 `tmux`、真实 `sshd`、`mac_shim`/`win_shim` 自检），欢迎提 Issue 反馈点亮 `✅`。
- `⚪` **通用兜底**：默认 `hotkey.scope = "all"` 下可直接语音精修落字并带窗口级防串台，无专属上下文适配。

| 类别 | 对象 | 状态 | 能力范围、投递方式与已知边界 |
| :--- | :--- | :---: | :--- |
| **宿主机** | **macOS** *（Apple Silicon / Intel）* | `✅` | 日常主力；原生 `mac_shim` 胶囊 + 系统麦克风 |
| **宿主机** | **Windows + WSL2** | `✅` | 日常主力；`win_shim` 原生 WASAPI 采集 + Per-Monitor V2 高分屏胶囊 |
| **宿主机** | **SSH 远端 Linux 主机** | `✅` | 零远端安装探针（仅需 Python `>= 3.10`）+ 本地防串台；Ubuntu CI 真实 `sshd` E2E 验证 |
| **宿主机** | **原生 Linux 桌面 / 纯原生 Windows（无 WSL2）** | `—` | 暂不支持守护进程、全局热键与 `toggle`；原生 Linux 当前支持单次文件转写（`once --file <wav> --dry-run`，无需本机热键或麦克风；仍调用已配置的云端 ASR/精修渠道，`--dry-run` 仅跳过粘贴）及作为 SSH 远端目标主机 |
| **终端** | **macOS Ghostty** | `✅` | AppleScript 零剪贴板直投 + 同分屏切 `tmux` pane 防串台 + 零钩子完整上下文 *（Codex / Claude Code / AGY CLI 真机实测）* |
| **终端** | **Windows Terminal (WSL2)** | `✅` | `win_shim` 瞬态剪贴板 + 快捷键粘贴（不污染原剪贴板）+ 同窗切 Tab 防串台 + 钩子/`tmux` 精准上下文 *（Codex / Claude Code / AGY CLI 真机实测）* |
| **终端** | **本地 & 远端 `tmux`** | `✅` | **macOS** 零剪贴板直投 / **WSL2** 走 `win_shim` 瞬态剪贴板粘贴；进程树按焦点窗格隔离；Linux & macOS CI 真实 `tmux` E2E 验证 |
| **终端** | **其他内置终端与专用 SSH 客户端** | `🧪` | 已内置粘贴键位路由、防串台与识别规则（名单见下方折叠区）；其他 CLI Agent（OpenCode、Aider 等）走工作区 `git`/文件名兜底 |
| **桌面 App** | **Antigravity / AGY 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | **Antigravity 2.0 独立桌面版** macOS 真机实测通过（只读 `conversation_summaries.db` + 标题匹配、工作区 `git`/文件名与 `brain/` 增量缓存及切会话防串台），兼容老版 VS Code Fork *（WSL2 仅扫 WSL 目录）* |
| **桌面 App** | **ChatGPT / Codex 统一桌面端** | `✅` *(macOS)* / `🧪` *(Windows)* | macOS 真机实测通过（`com.openai.codex`）：只读 `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl`，本地仓库任务自动升格 `agent` 模式，普通云端 ChatGPT 保持 `general` 模式，投递前复核同窗切会话；Windows 提供精修落字与锁窗防串台 |
| **桌面 App** | **Claude 桌面版** | `✅` *(macOS)* / `🧪` *(Windows)* | macOS 真机实测通过（`com.anthropic.claudefordesktop`）：**Code & Cowork** 本地会话绑定（有界只读 `AXWebArea` 探测、`Claude`/`Claude-3p` 扫描、`50K` 缓存与投递前复核），云端 Chat 保持 `general` 模式；Windows 提供精修落字与锁窗防串台 |
| **桌面 App** | **其他桌面 IDE** | `🧪` | IDE（**VS Code**、**Cursor**、**Windsurf**、**JetBrains**、**Zed** 等）走 `coding_gui` 挂载工作区 `git`/文件名；在 IDE 内置终端运行 CLI Agent 同样按 `coding_gui` 处理（跳过 CLI `sync_session` 与内置终端分屏区分） |
| **桌面 App** | **其他任意桌面 App** *（浏览器、IM、笔记、邮件等）* | `⚪` | 默认 `hotkey.scope = "all"` 走 `general` 通用润色模式（仅挂个人热词，精修落字 + 锁窗防串台） |

<details>
<summary><b>其他内置终端与专用 SSH 客户端完整清单（<code>🧪</code>）</b></summary>

- **macOS 终端**：`Terminal.app`、`iTerm2`、`WezTerm`、`Kitty`、`Alacritty`、`Warp`、`Hyper`、`Rio`、`Tabby`
- **Windows 终端**：`WezTerm`、`Alacritty`、`Ghostty`、`Tabby`、`Hyper`、`Rio`、`Warp`、`Mintty`、`PuTTY`、`KiTTY`
- **专用 SSH 客户端**：`MobaXterm`、`Xshell`、`WindTerm`、`SecureCRT`、`Termius`、`Electerm`、`FinalShell`、`RoyalTS`

</details>

---

## 双平台机制细节（macOS TCC 与 WSL2 WASAPI）

### macOS（`Ghostty`、`tmux` 与其他终端）

- **原生 Retina 胶囊与投递**：首次启动经 `/usr/bin/swiftc` 编译缓存原生 `mac_shim`（约 5–15 秒），支持 `RCommand` / `RControl` / `ROption` / `Fn` 短按或 `ptt` 按住说话、远程桌面自动让行（`Windows App`、`屏幕共享`、Parsec、VMware/Parallels）并渲染 Retina 胶囊。**macOS Ghostty 与 `tmux`** 内直投不碰剪贴板；其他窗口锁定起点窗口 ID 后走原生 `CGEvent` `Cmd+V` 并带剪贴板快照还原。
- **零钩子分屏与 `tmux` 绑定**：读取 Ghostty 分屏 `tty`/`cwd`、`Terminal.app`（`🧪`）当前标签页 `tty`、`iTerm2`（`🧪`，零弹窗自动化预检并支持 PTY 回退）或终端子进程 PTY 树（含 `tmux` 焦点窗格）绑定 `codex`/`claude`/`agy` 会话；默认使用系统麦克风（`[audio] macos_device = "default"`）。
- **轻量 TCC 权限**：所有权限均归属于**执行 `agent-voice-input start` 的图形终端**（换终端重启需为新终端重新授权）。
  - **麦克风**（必选，供 `ffmpeg`）。
  - **自动化（Automation → Ghostty / Terminal / iTerm2）**（按所用终端授予一次，用于读取精确 `tty`）。
  - **辅助功能（Accessibility）**（向桌面 GUI 应用及非 `tmux` 下的非 Ghostty 终端发送 `Cmd+V`，以及第三方终端/桌面端读取窗口标题与会话绑定时必需；仅在 Ghostty 或 `tmux` 内直投时无需）。
  - **输入监控（Input Monitoring）**：默认修饰键热键（`RControl` / `RCommand` / `ROption` / `Fn`）**不需要**。

### Windows Terminal + WSL2（含远程桌面与 `tmux`）

- **全局作用域、远程桌面让行与高分屏胶囊**：默认单击右 Ctrl（`RControl`）在终端、IDE 与桌面应用中开/停录音（`hotkey.scope = "all"`，可选 `"coding"` / `"terminals"`；组合键如 `RControl+C` 原样放行），显示 Per-Monitor V2 高分屏胶囊。聚焦远程桌面/虚拟机客户端（`mstsc`、`vmconnect`、Parsec、RustDesk 等）时静默让行热键（`yield_remote_desktop = true`；默认放行裸 `msrdc` 保护 Win11 WSLg，非 WSLg 的 AVD/Windows 365 用户可加 `ignore_apps = ["msrdc"]`）。WSL2 下即便在 `tmux` 内也走 `win_shim` 瞬态剪贴板快照 + 按进程路由快捷键（`Ctrl+V`、`Ctrl+Shift+V` 或 `Shift+Insert`）且不污染原剪贴板。
- **原生 WASAPI 采集**：常驻 `win_shim` 调用 Windows WASAPI 16 kHz 采集，避开 WSLg `RDPSource` 休眠零包死锁；起止提示音默认关闭以免触发 RDP 音频唤醒延迟。首次 C# 编译 + Defender 扫描约需 15–60 秒+。
- **零配置 `tmux` 与可选钩子**：无需改 `~/.tmux.conf` 即追踪焦点 `tmux` 窗格。仅在不用 `tmux` 且**同一目录多开独立窗口**时才需按需执行 `agent-voice-input integrate`（见 [`integrations/README.zh-CN.md`](../integrations/README.zh-CN.md)）。

---

## 热词管理（支持让 Agent 帮你打理）

用 `~/.agent-voice-input/hotwords/personal.txt` 扁平纯文本统一管理人名、项目专名与顽固空耳（`agent-voice-input hotwords path` 查看真身路径；下次口述自动热加载；目录级 `flock` 写锁零 `.lock` 文件）：

- **初始开发者词表与 dotfiles 目录软链**：首次 `agent-voice-input init` 会写入约 18 个常见 AI/开发者初始词（可自由增删，`init --force` 绝不覆盖）。跨机同步时请**对 `hotwords/` 目录建软链**，不要对 `personal.txt` 建单文件软链（读写侧均会拒绝）：
  - **运行 `init` 之前**：`mkdir -p ~/.agent-voice-input && ln -s ~/.dotfiles/avi-hotwords ~/.agent-voice-input/hotwords`
  - **已运行过 `init`**：先把本地 `personal.txt` 备份/合并进 `~/.dotfiles/avi-hotwords/`，删除本地 `~/.agent-voice-input/hotwords` 目录后再 `ln -s ~/.dotfiles/avi-hotwords ~/.agent-voice-input/hotwords`（若本地仍是真实目录就直接 `ln -sfn` 会生成嵌套软链）。
- **置顶（`!`）与可选空耳（`<-`）**：核心词加 `!` 置顶（如 `!Antigravity` 或 `agent-voice-input hotwords add Antigravity --pin`）；顽固误听写 `规范词 <- 误识1, 误识2` 或 `agent-voice-input hotwords add Harness --alias 哈尼斯,哈尼思`（单条最多 5 个误识、拒单字、双字提醒、多词共享的歧义别名自动隐藏且保留两侧规范词）。一阶段 ASR 仅收规范词，二阶段/单跳用空耳纠偏。
- **双阶段配额预算**：
  - **一阶段 ASR**：按权重分层（`! 置顶` > `带空耳` > `普通词`，层内保留文件行序）。`general` 模式 100% 归个人词；`agent` 模式为仓库/会话动态词保底至少 30% 并自动回流空位（阿里百炼 **150 词 / 1,800 字**、豆包 **50 词 / 1,000 字**、OpenAI `keywords[]` **50 词**、OpenRouter `keyterms` **50 词**、Whisper **35 词 / 353 字**；`deepseek-asr` 由二阶段兜底）。
  - **二阶段 / 单跳块 0（`<vocabulary_hints>`）**：同样按优先级分层（`!` 置顶 → 有效 `<-` 别名 → 普通词，各层内保持文件行序）选取前 **200 条 / 3,000 字**（双模式统一生效）。随时用 `agent-voice-input hotwords list [--json]` 查看用量与告警。
- **两条 Agent 协作纪律**：**改 `personal.txt` 前必须先展示 diff 经你确认再落盘；不要凭空猜空耳，实战撞见真实错听后再补 `<-` 映射**。

```text
# 1. 首次冷启动建库（只提炼规范词，不猜空耳）
读我最近几个项目和 README/规范，帮我提炼一份去噪后的扁平个人与跨项目高频专名草案写入 ~/.agent-voice-input/hotwords/personal.txt（仅对最高频核心词加 ! 置顶），不要凭空猜空耳，先给我看 diff，确认后再落盘。

# 2. 遇错听秒补空耳映射
刚才语音把 "Harness" 听成了 "哈尼斯"，帮我用 `agent-voice-input hotwords add Harness --alias 哈尼斯` 补一条空耳映射（先给我看变动再执行）。

# 3. 定期体检瘦身与调优排序
运行 `agent-voice-input hotwords list --json` 检查是否临近一/二阶段预算或存在告警，把低频过时词裁掉、把跨项目高频词加 `!` 置顶，先出 diff 给我确认后再改。
```

---

## 进阶配置、本地归档与 CLI 参数

- **统一应用家目录（`~/.agent-voice-input/`）**：`config.toml`、`.env`、`hotwords/personal.txt` 与可选 `audio/`、`transcripts/` 均位于 `~/.agent-voice-input/`（受 `AGENT_VOICE_INPUT_HOME` 覆盖）。`init` 生成默认全忽略、仅放行 `hotwords/` 的 `.gitignore`；`init --force` 仅覆写 `config.toml`，绝不覆盖 `.env`、`personal.txt` 与 `.gitignore`。
- **渠道默认模型与界面语言**：`model = ""` 时自动匹配渠道默认模型；CLI 切换 `provider` 且未传 `--*-model` 时重置为新渠道默认。`ui.language` 支持 `"auto"`、`"zh"`、`"en"` 及 BCP 47 标签（如 `"zh-CN"` / `"en-US"`）。
- **两段式 vs 单跳协议（`asr.api`、`asr.doubao_resource_id`）**：`"auto"` 将专用 STT 路由至两段式，将原生 `gemini`、百炼 `qwen*-omni*`、官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini` 或显式 `asr.api = "chat-completions"` 路由至单跳（共享 50K `<session_reference_log>` 块、`>= 3s` 内存压 `64 kbps` MP3、上限 `610s`；默认跳过二阶段，除非 `refiner.force_on_single_hop = true`）。DeepSeek 官方 ASR 仅支持 `asr.api = "auto"`。
- **二阶段协议与思考档位（`refiner.api`、`effort`、`session_max_turns`）**：`refiner.api = "auto"` 将 `codex` 与官方 `openai` 路由至无状态 HTTP `/responses`（`store=false`、`stream=true`），`agy` 走 `agy acp` stdio JSON-RPC，`gemini` 走 `:generateContent`，其余走 `/chat/completions`。`asr.effort` / `refiner.effort` 默认留空 `""`（精修端 `codex`/`agy`/`openai`/`openrouter` 为 `"low"`，其余及一阶段单跳为 `"none"`），支持 `"default"` 或 `"none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`。`agy` 的 `refiner.session_max_turns = 0` 按 200K 淘汰或词表变化自然轮换。
- **附加偏好指令、前缀缓存与冷启动回退**：通过 `refiner.extra_instructions` 追加个人偏好（上限 20 条 / 2,000 字，两段式与单跳统一生效）。近期对话（`context_max_chars = 200000`）按 `50K` 封口块前置，末尾拼装 `<workspace_git_status>` 与 `<asr_draft_to_clean>`。新开空/极短会话时，`cold_start_fallback = "auto"`（或 `"hotwords"` / `"off"`）从同目录历史会话补热词并前置 1 块冻结的 `<= 25K` `<prior_session_reference>`。
- **静音压缩、同渠道重试与可选本地归档（`[audio]`、`max_retries`、`[archive]`）**：
  - `[audio].silence_trim = "balanced" | "conservative" | "off"` 上传前压缩长思考停顿；支持最长 10 分钟口述（超 3 分钟按停顿自动切分）；失败或取消在途任务的录音以 `0600` 暂存于 `failed/`（24h 内最多 10 条）供 `retry` 恢复。
  - 两阶段默认 `max_retries = 2`（`0..10`），受阶段 `timeout_sec` 与 9 秒建连/TLS 上限约束。
  - `[archive]` 默认全关（`0700` 目录 / `0600` 文件、受 `.gitignore` 拦截，在 `status`/`doctor`/`start` 中展示 `Archive:` 摘要）：`save_transcripts = true` 按月追加 **14 字段** 纯转录 JSONL（含 ASR 草稿、终稿及向后兼容的 `agent` / `agent_session_id`，在 `general` 模式或旧版 12 字段记录下缺省/`null`，绝不写上下文/热词/凭据）至 `~/.agent-voice-input/transcripts/YYYY-MM.jsonl`；`save_audio = true` 归档有效录音为 `16 kHz` `64 kbps` MP3 至 `~/.agent-voice-input/audio/<id>.mp3`（`audio_max_count = 1000`，`0` 不淘汰）。转录、音频与 `diag.jsonl` 共享短 ID（`YYYYMMDD-HHMMSS-<4hex>`）。

### 核心 CLI 覆盖参数

| CLI 参数 | 说明 |
| :--- | :--- |
| `--config PATH` | 指定 `config.toml` 或 `config.json` 路径（亦支持 `AGENT_VOICE_INPUT_CONFIG` 与 `AGENT_VOICE_INPUT_HOME`） |
| `--asr`, `--asr-provider` | 一阶段渠道（`auto`, `dashscope`, `doubao`, `deepseek`, `openai`, `gemini`, `groq`, `openrouter`, `siliconflow`, `openai-compatible`；别名 `volcengine`/`volc` → `doubao`、`bailian` → `dashscope`） |
| `--asr-model`, `--asr-api`, `--asr-effort` | 一阶段上游模型 ID、协议（`auto`, `audio-transcriptions`, `chat-completions`）与思考档位 |
| `--refiner`, `--refiner-provider` | 二阶段渠道（`auto`, `codex`, `agy`, `deepseek`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`；别名 `doubao`/`volc`/`ark` → `volcengine`、`bailian` → `dashscope`） |
| `--refiner-model`, `--refiner-api`, `--refiner-effort`, `--effort` | 二阶段上游模型 ID、协议（`auto`, `chat-completions`, `responses`）与思考档位 |

*（环境变量仅留给厂商凭据、引导路径 `AGENT_VOICE_INPUT_HOME` / `AGENT_VOICE_INPUT_CONFIG` / `AGENT_VOICE_INPUT_RUNTIME_DIR` 及底层运行期诊断项；常规参数统一放 `config.toml`。）*

---

## 日常运维与常见问题（FAQ）

### 随终端自启、升级、卸载与文件位置

- **随终端幂等自启**（在 `~/.zshrc` 或 `~/.bashrc` 加一行）：
  ```bash
  [ -z "$SSH_CONNECTION" ] && command -v agent-voice-input >/dev/null 2>&1 && agent-voice-input start >/dev/null 2>&1
  ```
  > **警告（macOS）**：切勿用 `launchd`（LaunchAgent）在后台拉起守护进程。经 `launchd` 启动会丢失 GUI 终端的麦克风与自动化 TCC 权限归属，导致录音静默全 0 静音；请务必在本机已授权的图形终端内启动。
- **升级**：`uv tool upgrade agent-voice-input && agent-voice-input restart`（或重跑 `install.sh`；源码直跑用户执行 `git pull && agent-voice-input restart`）。
- **彻底卸载**：
  ```bash
  agent-voice-input stop
  agent-voice-input integrate --uninstall   # 可选：移除受管 CLI 钩子
  uv tool uninstall agent-voice-input       # 或 rm ~/.local/bin/agent-voice-input
  ```
- **文件存放位置**：
  | 路径 | 用途 |
  | :--- | :--- |
  | `~/.agent-voice-input/` | 应用家目录（`config.toml`、`.env`、`hotwords/personal.txt`、可选 `audio/` 与 `transcripts/`） |
  | `~/.cache/agent-voice-input/` *（或 `$XDG_RUNTIME_DIR/agent-voice-input`）* | 运行目录（`control.sock`、`daemon.lock`、`daemon.log`、`diag.jsonl`、`failed/`、`sessions/`、`integrate-backups/`） |
  | `~/Library/Caches/agent-voice-input/` *（macOS）* | 编译后的 `mac_shim` 缓存目录 |

### 高频排错 FAQ

- **为什么不推荐直接裸跑 `pip install agent-voice-input`？**  
  macOS Homebrew Python 与较新 Debian/Ubuntu 系统 Python 默认启用 **PEP 668（`externally-managed-environment`）**，在系统环境裸跑 `pip install` 会报错拦截。请改用 `install.sh` 或 `uv tool install --python '>=3.10' agent-voice-input` 独立安装，或直接克隆仓库软链 `bin/agent-voice-input` 零依赖直跑。
- **MacBook 没有右 Ctrl 键怎么办？**  
  无论是否运行过 `init`，MacBook 内置键盘均自动默认 `key = "RCommand"`（右 Command），台式 Mac（Mac mini 等）与 Windows/WSL2 默认 `key = "RControl"`。避坑提示：`ROption` 撞豆包输入法，`Fn` 撞 macOS 系统 🌐 键（如要用 `Fn` 需在 系统设置 → 键盘 将「按下 🌐 键时」设为**不执行任何操作**），`RShift` 撞第三方中文输入法单按切中英文。随时可在 `config.toml` 修改 `[hotkey]` 后 `agent-voice-input restart`，并用 `agent-voice-input key-test` 确认键码。
- **为什么通过 SSH 在 macOS 上启动守护进程录不到声音？**  
  macOS 的麦克风 TCC 权限归属责任图形进程，经 `sshd`（或 `launchd`）拉起的守护进程会被系统静默喂全 0 静音帧。请务必在本机已授权的图形终端（如 Ghostty 或 `Terminal.app`）里执行 `agent-voice-input start` / `restart`。
- **为什么 WSL2 下无法自动粘贴到 Windows 管理员窗口？**  
  Windows UIPI 机制限制非提权 `win_shim` 向管理员窗口发送模拟粘贴按键；此时精修结果已安全留存在剪贴板，手动按 `Ctrl+V` 即可。
- **后台守护进程怎么走代理？**  
  在终端导出 `HTTPS_PROXY`（配 `NO_PROXY=127.0.0.1,localhost`）或写入 `~/.agent-voice-input/.env`（系统环境变量优先），再执行 `agent-voice-input restart`。因纯标准库 HTTP 客户端仅支持 `http://` 代理，若代理客户端仅设了 `ALL_PROXY=socks5://...`，请同时设置其 HTTP 混合端口（`HTTPS_PROXY=http://127.0.0.1:<port>`），并用 `agent-voice-input doctor --ping` 验证。
- **为什么默认只粘贴不自动按回车？**  
  终端与 SSH 下误听命令直接回车风险太高，粘贴后便于扫一眼确认；处理期间若焦点切走，结果自动退回剪贴板，绝不强贴错窗。
