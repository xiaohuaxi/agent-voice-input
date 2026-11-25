# 产品演进路线图（ROADMAP）

> **当前已发布基线**：`v0.15.0`（主干待发版增量归入 [CHANGELOG.md](CHANGELOG.md) 的 `[Unreleased]`；版本号与 PyPI / GitHub Release 实际对外发布严格 1:1 绑定，未发布能力按里程碑组织而不预锁小版本号）
>
> **工程规范与上下文路由架构**：[AGENTS.md](AGENTS.md) · [docs/context-routing.md](docs/context-routing.md)

本文档定义 `agent-voice-input` 的长期架构边界（Non-goals）、自动化验证分级体系，以及按能力里程碑组织的演进路线。

---

## 一、工程设计原则与不做边界

### 1.1 核心准则：重鲁棒性、反过度工程化

项目始终坚持**零第三方 Python 运行时依赖**（仅依赖 Python `>= 3.10` 标准库、宿主机 `ffmpeg` 及双平台系统原生桥接）与**规模匹配运行边界**，明确划定以下架构红线：

- **精修输出契约保持纯文本直出，不上 `json_schema` 结构化输出**：
  - 语音精修本质是高保真序列编辑而非结构化抽取。JSON 字符串转义会引入换行/引号/反斜杠失真与额外输出 Token 开销，且在 `/responses`、`:generateContent` 与 `/chat/completions` 多渠道间参数契约不一，亦无法阻止模型在字段内部直接回答用户提问的语义级劫持。
  - 坚持**纯文本输出 + `<asr_draft_to_clean>` / `<task_reminder>` 三明治指令边界**。
- **精修提示词治理：现阶段坚持维护一套统一提示词**：
  - 所有精修渠道与模型共用同一套提示词真身，避免多套提示词漂移带来的维护与回归成本；按单模型分叉专属提示词仅作备选预案，启用前须经维护者明确批准。
- **热词体系聚焦确定性提取，绝不让未确认的单次精修猜测直写持久化 `hotwords.txt`**：
  - 未经确认的模型猜词一旦误判写入持久化词表，会形成“脏词声学偏置 → 后续识别持续带偏”的自激污染闭环，并挤占豆包 ASR 等窄配额渠道预算（`30 词 / 180 字符`）。
  - 坚持通过确定性标识符提取、工作区源码/文件名兜底与低优先级内置词表覆盖高频术语。
- **桌面 GUI 适配抓大放小，不碰编辑器私有状态库与无障碍 DOM 抓取**：
  - 仅复用前台窗口进程/Bundle ID、窗口标题、macOS `AXDocument` 文档路径、同构明文会话日志及 AGY/Codex 只读会话索引获取上下文与热词。
  - **明确不做**：不读取 VS Code / Cursor 运行期持锁且表结构易变的内部 SQLite（`state.vscdb`），防锁竞争与版本升级断裂；不对桌面版 AI 聊天、IM 或浏览器遍历无障碍（Accessibility / UIAutomation）组件树抓取聊天正文，杜绝界面卡顿、改版脆弱性与隐私风险。
- **跨机 SSH 桥接坚持无状态探针，不在远端部署常驻守护进程或强制预装包**：
  - 跨机语音输入仅在本地宿主机运行守护进程与音频/密钥管线；远端 Linux 主机通过复用 SSH 连接执行轻量无状态只读探针拉取清洗后的上下文，**远端零常驻后台服务、零开放监听端口、零云厂商凭据落盘**。
- **单进程守护、被动旁路监听与默认绝不自动回车**：
  - 在途取消与有界并发精修保持本地单进程守护架构，**不拆独立通信 Worker 子进程**；
  - 双端键盘钩子保持纯被动旁路监听（不吞键、不改写系统事件流），跨端远程桌面让行统一在触发入口静默判定；
  - 无论本地终端、远程 SSH 还是桌面 GUI 应用，**默认只粘贴、绝不自动发送回车（`Enter`）**。

### 1.2 自动化验证分级：最大化自动闭环 + 薄人工真机卡点

1. **第一级：自动闭环门禁（主力签收基线）**
   - **离线单元测试套件**：`PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests -v`（全程离线可重入，覆盖协议解析、并发保序、在途取消、窗口/SSH 路由与剪贴板恢复状态机）。
   - **原生 Shim 离线自检**：macOS `mac_shim --self-check` 与 Windows `win_shim.ps1 -Mode self-check`，在无声卡、无全局钩子权限的 CI 环境中验证原生编译、按键状态机与粘贴键路由。
   - **隔离全链路自检与时间点回放基准**：通过 `.agents/skills/voice-eval/` 在独立运行目录验证真 ASR、前缀缓存、并发保序落字与 `0600` 隐私脱敏契约；回放评测产物严格限定在仓外缓存目录（`${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/`），严禁入库。
2. **第二级：薄人工真机卡点**
   - 仅当改动触及操作系统硬件链路、窗口焦点权限或跨机终端透传体感时，在自动门禁全绿后执行简短真机确认。

---

## 二、能力里程碑路线（Capability Milestones）

### 已发布基线（`已发布 · v0.1.0 – v0.8.0`）

- **双平台原生音频、热键与悬浮胶囊桥接**：Windows + WSL2 常驻 WASAPI 采集与 HiDPI 波形胶囊，macOS 原生 `mac_shim` 修饰键监听、Ghostty / `tmux` 零剪贴板直投及多终端 `Cmd+V` 安全粘贴。
- **共享 `50K` 封口前缀缓存与双模式管线**：两阶段转写+精修与单跳原生多模态音频统一共享 `50K` `<session_reference_log>` 前缀缓存块、自适应 `agent` / `general` 精修指令及全局桌面作用域 + 远程桌面自动让行（详见 [CHANGELOG.md](CHANGELOG.md)）。

---

### 最新发版里程碑（`已发布 · v0.15.0`）

以下 Claude 桌面版（macOS）本地会话绑定、剪贴板单所有权语音优先与防阻塞重构、桥接单写入/录音所有权收口以及核心模块与原生 Shim 多文件拆分已在 `v0.15.0` 完成落地并正式发布：

1. **Claude 桌面版（Code & Cowork）macOS 本地会话绑定与同窗防串台**：
   - 为 `com.anthropic.claudefordesktop` 落地有界只读 `AXWebArea` 路由与标题探测，同时兼容官方（`Claude`）与 3P（`Claude-3p`）元数据目录、`50K` 增量会话缓存、本地 Cowork 目录解析及落字前同窗线程复核；云端 Chat 继续严格隔离在 `general + ai_chat` 模式。
2. **单所有权语音优先剪贴板与 macOS 慢速粘贴板/无障碍解耦**：
   - 将双端剪贴板写入收口为原生桥接单一所有权，在瞬态冲突或超时下优先保障语音落字而非回滚旧剪贴板，并将焦点漂移或按住修饰键时的剪贴板回退 HUD 提示统一延长至 `5s`；
   - 将 macOS 粘贴板快照捕获隔离进可终止单槽位 worker 并将慢速无障碍探测解耦出热路径，彻底消除 Windows App / RDP 60s 剪贴板 Promise 对 HUD、热键与口述投递的卡顿阻塞。
3. **桥接单写入通道、录音所有权统一与零依赖多文件模块化**：
   - 将原生桥接 stdin 收敛为单写入通道、实现无锁非阻塞 HUD 派发并统一 macOS / WSL2 平台录音所有权，修复 WSL 停录/取消写超时状态恢复、提前释放槽位、控制套接字保序与管道关闭竞态；
   - 将守护进程拆分为聚焦模块（`control_ipc.py`、`daemon_pipeline.py`），并将单体原生桥接拆分为多文件源码（`mac_shim/*.swift`、`win_shim/*.cs`），同步打包元数据并保持零新增运行时依赖。

---

### 已归档里程碑（`已发布 · v0.14.0`）

以下 DeepSeek 一等官方 ASR、主流 AI 桌面端（Antigravity 2.0 / ChatGPT & Codex 统一桌面端）深度上下文绑定、性能与桥接防串台加固及双端对称回归验收体系已在 `v0.14.0` 完成落地并正式发布：

1. **DeepSeek 一等官方 ASR 与零推理延迟精修调优**：
   - 新增内置 `asr.provider = "deepseek"`（`POST https://api.deepseek.com/v1/tools/asr`，默认 `deepseek-asr`，支持 21 语言自动识别并纳入 `asr.provider = "auto"`），仅需一把 `DEEPSEEK_API_KEY` 即可零配置完成两段式口述（`deepseek-asr` + `deepseek-flash`），并统一自定义 `base_url` 与遥测字段契约；
   - 在二阶段精修系统提示词、`<task_reminder>` 及 `agy` 隔离规则中显式增加零反复推敲指令，降低精修输出延迟；在本地 `SessionTracker` 与 SSH `_remote_probe.py` 中共用字节级快速预过滤，跳过 Claude Code `2.1.330` 巨型非对话 JSONL 记录。
2. **Antigravity 2.0 与 ChatGPT / Codex 统一桌面端上下文绑定及同窗防串台**：
   - 支持 Antigravity 2.0 独立桌面版只读 `conversation_summaries.db` 精确/截断前缀标题匹配、工作区 URI 解析（含 WSL `vscode-remote://wsl+...`）、`annotations/*.pbtxt` 前台排序与 `brain/` `50K` 增量缓存，并兼容旧版 VS Code Fork；
   - 支持 ChatGPT / Codex 统一桌面端（`com.openai.codex` / `ChatGPT.exe` / `Codex.exe`）识别与只读 `~/.codex/sqlite/codex.db` + `~/.codex/sessions/*.jsonl` 绑定：本地仓库任务自动升格为 `agent` 模式并挂载项目热词与增量缓存，云端聊天严守 `general + ai_chat` 零串台，并在双端落字前复核同窗活跃会话防切换串台。
3. **双端桥接性能、兼容性透明矩阵与自动化真机回归体系**：
   - `mac_shim.swift` 将 CoreAudio 与无障碍查询卸载到后台串行队列、单屏 HUD 跳过 AX IPC 并引入剪贴板快照/投递截止预算；对齐 `platform_macos.py` 与 `platform_wsl.py` 修饰键与焦点守卫，并更新 `doctor` 支持 `~/.gemini/config/hooks.json` 与 dotfiles 软链；
   - 在双语 `README` 中新增 `✅` / `🧪` / `⚪` 兼容性与真机验证状态矩阵，明晰原生 Linux 边界、macOS/WSL2 `tmux` 投递差异及 IDE 内置终端边界；
   - 落地非阻塞 `control.sock` `probe_route` 与隔离的 `selftest_wav`，并在 `.agents/skills/voice-eval/` 中交付对称双端回归套件 `scripts/regress_matrix.py`（三层无头路由矩阵、守护进程健康探活与本机 CLI/DB 只读抽检）及桌面 GUI 端到端回归验收清单。

---

### 已归档里程碑（`已发布 · v0.13.0`）

以下默认模型升级、首屏开箱体验优化、并发与防串台深度加固及跨组件离线 E2E 能力已在 `v0.13.0` 完成落地并正式发布：

1. **主流默认模型升级与渠道连接池统一**：
   - OpenAI 默认升级为 `gpt-transcribe`（ASR）与 `gpt-6-luna`（Refiner），Gemini 公开 API 默认升级为 `gemini-3.7-flash`（AGY 订阅精修保持 `gemini-3.8-flash`），并将常驻 `agy acp` worker 空闲回收 TTL 由 `3` 分钟延至 `15` 分钟；
   - Codex / OpenAI Responses 代理连接池统一复用 `http_transport.py`（新增 `NO_PROXY` 的 `*.domain` 通配与不支持 scheme 的可读修复引导），并阻断单跳音频 `content_filter`/`safety` 截断及为 `gemini-flash-latest` 显式设置 `thinkingLevel="low"`。
2. **双端 HUD 视觉、内置热词与公开用户开箱闭环**：
   - 精修双端连发 HUD 玻璃态 `+1`/`+2` 队列小胶囊，扩充 76 词程序员 / Agent / 模型默认内置热词；
   - 完成双语 README 首屏改版与无作者服务器/零中转隐私声明，补齐 `SECURITY.md` 与 `.github/ISSUE_TEMPLATE/`，并修复热键名白名单校验、WSL 冷编译超时与 WSLg 无麦体检、全注释 `config.example.env`、`doctor` AGY hook 检测与 `start` 缺 Key/`ffmpeg` 提示。
3. **跨窗口防串台、并发竞态与 3 套离线跨组件 E2E 门禁**：
   - 补齐 Ghostty 同 surface 切换 `tmux` pane 防串台（0ms 并发起点采样 + 空窗口安全降级）、Windows Terminal 带标记标签页切普通 shell 拦截与双端 13 组标题标记对齐；
   - 加固 WSL `rec_gen` discard-stop 与原生 `StopRecording` 错代保护、macOS PTT 崩溃 EOF 先停麦不泄线程、取消录音撞 `stop`/`restart` 恰好暂存一次及 SSH 远端预取/缓存单调代号防倒退，并新增 3 套离线跨组件 E2E 场景测试。

---

### 已归档里程碑（`已发布 · v0.12.0`）

以下本地 `agy` 订阅精修、代理连接池复用、录音投递可靠性与全平台 CI 加固能力已在 `v0.12.0` 完成落地并正式发布：

1. **显式 `refiner.provider = "agy"` 本地订阅精修渠道**：
   - 支持显式配置复用本机 Antigravity CLI（常驻、空闲自动回收的单 `agy acp` worker）执行无工具纯文本隔离精修与跨轮增量续接（不参与 `auto` 自动选路），并在 `SessionTracker` 中排除精修自身会话防上下文自污染；同步收紧精修提示词防多余注释改写。
2. **HTTP/HTTPS 代理 Keep-Alive 连接池复用**：
   - 在 `http_transport.py` 中实现 `CONNECT` HTTP/1.1 隧道复用与单层 TLS 代理连接池，剥离并脱敏调用方 `Proxy-Authorization`，对不安全/畸形代理配置 fail-closed，大幅降低跨境及代理链路单次往返耗时。
3. **录音/投递可靠性与跨平台细节加固**：
   - 补齐多块 ASR 静默丢块保护、Windows `rec_stop` 竞态防阻塞、`failed/` 24h TTL 主动清理、`hotwords.txt` 异常编码优雅降级与统一 4 态 HUD/遥测/诊断契约；
   - 新增 macOS 多屏聚焦窗口 HUD 锚定、非中文内建热词语言过滤、Eternal Terminal（`et`）远端识别、`DOUBAO_SPEECH_API_KEY` 统一单头凭据、豆包极速优先回退链与异步轮询退避优化及 POSIX `LC_ALL=C` 严格 ASCII locale 兼容；
   - CI 矩阵扩展至 Python 3.10–3.14、三端 clean wheel 冒烟与真实 `sshd`/多进程 daemon 环回 E2E。

---

### 已归档里程碑（`已发布 · v0.11.0`）

以下三项上下文冷启动、热词管理与跨周长稳自愈能力已在 `v0.11.0` 完成落地并正式发布：

1. **Agent Session 冷启动回退（`[refiner].cold_start_fallback`）**：
   - 新增 `"auto"` / `"hotwords"` / `"off"` 三层冷启动回退链路：Layer 0 在远程防串台门控下安全复用 `win.cwd` 工作区上下文，Layer 1 按同 `cwd` 跨 CLI 提取历史会话热词，Layer 2 注入可随当前对话自然驱逐的 `<=25K` 冻结前缀块 `<prior_session_reference>`；
   - 同步补齐 SSH `_remote_probe.py` 单 RTT 远端冷启动对齐、macOS `/var -> /private/var` 软链 Claude 项目目录匹配，以及 `diagnose` 的脱敏 `ctx_fallback` 字段观测。
2. **一等热词管理（`agent-voice-input hotwords`）**：
   - 新增独立模块 `hotwords.py` 与 `hotwords list|add|remove [--scope global|chat|coding]` 子命令，支持 `[global]`/`[chat]`/`[coding]` 分段 `hotwords.txt`（保留 BOM/CRLF 与注释，经 `flock` + `0600` 原子写回），运行期免重启实时热加载；
   - 落地四级优先级合并与跨 Doubao / Whisper / DashScope / Gemini / OpenRouter 的统一预算裁剪（`budget_hotwords_for_provider`）及配额可视化。
3. **跨周休眠 / 切网长稳自愈**：
   - 强化连接与解析自愈：`_KeepAlivePool` 双时钟休眠/漂移逐出、`0ms` 过期探测、代理零 DNS 直连与可取消 `_BoundedDnsResolver`；
   - 强化双端唤醒与长跑稳定性：Windows `PowerBroadcastWindow`（`WM_POWERBROADCAST`）钩子原子重挂、macOS `didWakeNotification` 唤醒恢复与 PTT 安全收尾、O(1) 五元组 `verified_stat` 会话短路与 64 项有界 ETag LRU 缓存，以及 `2 MiB` `daemon.log` 原子轮转（`daemon.log.1`）与 `diagnose` 尾部回退读取。

---

### 已归档里程碑（`已发布 · v0.10.0`）

以下四条日常可用性与操控闭环已在 `v0.10.0` 完成落地与全平台 CI 签收并正式发布：

1. **极简幂等运维与官方诊断闭环**：
   - `start` 幂等启动并在运行期代码/版本漂移时明确提示，`restart` 无条件重建进程，`status` 区分 `stopped` / `unresponsive` / `error` 并展示当前生效管线与历史记录；全量子命令补齐 `--help`；
   - 新增官方 `agent-voice-input diagnose [--last N] [--json]` 只读诊断与豆包 ASR 1.0 `--ping` 连通性探活。
2. **零误扣费透明 `auto` 与跨阶段渠道别名**：
   - 统一 Codex 订阅登录与 API Key 认证模式判定及基础 URL 净化，`refiner.provider = "auto"` 仅复用已确认的本机 ChatGPT 订阅态、杜绝意外 API 扣费；
   - 支持跨阶段渠道别名（`doubao` ↔ `volcengine`/`volc`/`ark`、`bailian` → `dashscope`）无缝互认。
3. **三段式即时取消热键状态机与连发 HUD 自检**：
   - `tap_toggle` 升级为三段式实时状态机（`<= 500ms` 短按、`500–800ms` 犹豫死区、满 `800ms` 不等松手即时触发 `hold_cancel` 且吞掉后续松手），解除 `only_inflight=True` 实现当前录音与在途处理分层取消，按住 `250ms` 起原地轻提示，并平滑兼容旧 `tap_max_ms` 配置；双端补齐 Windows 陈旧 `_otherDown` 自愈与 macOS 定时组合键复查；
   - 三端同步微调录音文案（`"按右 Ctrl 结束"` / `"松开右 Ctrl 结束"` 及英文版）、新增连发后台在途角标 `+1`/`+2`，并落地 Windows（216 组）与 macOS Retina 双语离屏像素布局自检及 `e2e_system.py`（`C7`）接入。
4. **全平台 CI 零静默跳过与评测缓存隔离**：
   - GitHub Actions 全平台 runner 安装 `ffmpeg`/`tmux` 并禁止 CI 静默跳过单测；`eval_recordings.py` 隔离每槽位 ASR 缓存键并修复 `--manifest-from` 分母统计。

---

### 已归档里程碑（`已发布 · v0.9.0 – v0.9.1`）

以下核心能力与架构收敛已在 `v0.9.0`（及 `v0.9.1` 模块解耦、Windows `warp` 终端路由、100 MiB 剪贴板快照上限、WASAPI 零帧提示与架构/微秒并发/分层覆盖率门禁加固）完成发布：

1. **低延迟操控韧性与双端桥接 / 剪贴板自愈**：
   - 支持 `agent-voice-input cancel [--seq N]` 与长按热键即时中断在途识别/精修（`CancelToken` + `shutdown(SHUT_RDWR)` 唤醒阻塞连接，已过门控语音自动 `0600` 暂存 `failed/` 供 `retry` 恢复）；
   - Stage 1 保持串行防限流、Stage 2 无状态精修并发重叠，严格按 `seq_id` 先后保序上屏；
   - `win_shim.ps1` 编译 DLL 磁盘缓存（按 SHA-256 索引）消除冷启动抖动并支持辅助进程异常自愈；Windows 与 macOS 实现单槽位防连发竞态的多格式图文剪贴板备份还原，并对密码管理器敏感项到期清空而不重发。
2. **全桌面 GUI 场景感知与非 Git 目录支持**：
   - 主流桌面 IDE（VS Code、Cursor、Windsurf、Antigravity IDE、Zed、JetBrains、Xcode、Sublime Text）非递归定位本地工作区（含 WSL2 `/mnt/` 零慢盘 I/O 保护），硬隔离后台终端会话；桌面版 Antigravity IDE 通过三层路径证据绑定该工作区最新活跃的 `brain/<cid>` 主会话；
   - 跨本地 CLI、桌面 IDE 与远端目录统一支持**非 Git 普通文件夹**有界浅层文件名扫描兜底（严格排除 `~`、`/`、`/tmp` 等隐私根目录），并为桌面 AI 聊天与 IM 应用注入轻量 `<target_app_hint>` 场景微调。
3. **零门槛开箱、运行期自愈与三平台 CI 门禁**：
   - 默认 `refiner.provider = "auto"`（优先复用本地 `codex` 登录并支持公开轻量模型自动回退，继而按序匹配 API Key 或优雅旁路 Stage 2），新增一等公民 `deepseek`（`deepseek-flash`）渠道；
   - 提供 `agent-voice-input init [--force]` 与升级版 `doctor [--ping] [--remote] [--json]`（含可读修复建议与 5s 连通性探活），`retry` 自动粘贴免重复输出 `stdout`（配 `--print`）；
   - 引入持久化 `0600` `daemon.lock`（`O_CLOEXEC` `flock`）单实例互斥、过期死进程 `sessions/*.json` 节流清理、长静音稀疏叹气/敲击抗幻觉滑窗门控，以及 GitHub Actions `windows-latest` 无头 `win_shim` CI 自检。
4. **SSH 远端 Linux 零安装跨机上下文感知**：
   - **Phase 0 四路远程防串台硬否决**：零配置阻断本地后台会话误串入专用 SSH 客户端、远端窗口标题、前台 `ssh` 终端或 Remote-SSH IDE（含 macOS `iTerm2` 零弹窗 TCC 预检）；
   - **白名单无状态只读探针**：配置 `[remote] hosts` 后通过 `ssh -- <target> python3 -` 注入纯标准库探针，支持 ETag 未变短路、正向 `50K` 整块对齐裁剪（与本地增量分块保持逐字节前缀一致，高效复用上游前缀缓存）、热键按下后台异步预取、默认 `400ms` 松键剩余等待预算与主机级熔断器（完整匹配机制见 [docs/context-routing.md](docs/context-routing.md)）。

---

### 后续演进里程碑

#### 后续演进方向（`演进中`）

1. **本地 CLI 订阅精修渠道**：
   - `refiner.provider = "claude"`（`规划中`）：待本机 Claude Code 环境就绪后实测落地无工具精修渠道（`agy` 已于 `v0.12.0` 正式发布）。
2. **真录音模型横评与原生 Gemini 单跳定向补测**：依托仓外隔离时间点回放基线，系统开展主流 ASR / 精修组合的真录音横向对比评测与原生 Gemini 单跳路径定向补测。
3. **桌面版全新空会话首轮上下文关联**：在 Antigravity 2.0 与 ChatGPT/Codex 统一桌面端中，新建空对话在首句生成标题前尚无窗口级目录线索，当前保持宁缺毋滥防跨仓串台；后续视桌面端暴露活跃工作区/会话 ID 机制再评估零猜测首轮绑定。
4. **桌面端后续扩展**：跟进 WSL2 跨系统读取 Windows 宿主机桌面 App 上下文（macOS Claude 桌面版 Code / Cowork 本地会话绑定已于 `v0.15.0` 正式发布）。
5. **脱离 WSL 的原生 Windows 独立运行**：覆盖未安装 WSL 的纯 Windows 桌面端用户，原生本地直读 Windows 宿主机 `.codex/`、`.gemini/` 与 Claude 会话目录并复用原生 WASAPI 采集与热键/HUD 桥接；针对同时使用 WSL 终端与 Windows 桌面 App 的混合用户，优先评估「原生 Windows 单守护进程 + 轻量 `wsl.exe` 探针（复用现有 SSH 远端探针模型）」的单热键共存架构，避免双系统热键状态机跳跃冲突。

#### 近期架构复评与技术债跟踪

- **平台录音生命周期异步交接复评（Stage 3A 遗留警觉点）**：本轮平台桥接重构中，`MacOSPlatform`（`15s` 首帧授权等待、`0.45s` 尾音 `tail_drain`、麦克风热插拔自动恢复与 `close()` 收敛）与 `WslPlatform`（录音取消/停录代次交接）历经多轮独立冷审才将 P0 清零。当前实现已闭环可发布，但两处仍属于结构偏紧的高异步热点；发版后专门排期做架构简化评估，防止后续迭代再次滑向复杂失控。

#### `v1.0` 正式版日常长稳签收（Production Readiness Gate）

`v1.0` 不预设硬性时间表，待 `v0.10.0`–`v0.15.0` 已落地核心能力在真实日常高频口述中完成长稳验证、满足以下四项准入标准后正式打标：

1. **外部用户零门槛开箱**：仅配置任意单一支持语音转写的主流渠道 API Key（可自动搭配本地 CLI 登录态或独立二阶段凭据），即可通过 `init` 与 `doctor --ping` 完成体检并稳定投入日常使用。
2. **全场景安全投递与零串台**：本地终端、跨机 SSH 远端 Linux、主流桌面 IDE、普通桌面 GUI 应用与远程桌面自动让行均长期稳定运行；跨场景上下文严格隔离，系统剪贴板零意外污染，全链路严守不自动发送回车。
3. **高保真精修与操控韧性**：跨句中途改口消解、中英技术术语还原、长静音稀疏底噪抗幻觉与抗指令劫持稳定达标；长会话前缀缓存高效命中；支持在途即时取消、连发并发保序落字与失败/取消录音无损 `retry`。
4. **双平台原生桥接与三端 CI 长稳无回归**：`mac_shim.swift` 与 `win_shim.ps1` 自检及打包完整性校验持续通过 Linux / macOS / Windows CI 门禁，单实例锁、异常自愈与 `0600` 隐私脱敏契约经连续日常长稳运行零回归。

#### 远期可选扩展赛道（按实际需求演进）

- **原生 Linux 桌面（Wayland / X11）交互桥接**：面向不使用 macOS 或 Windows+WSL2、直接在原生 Linux 图形桌面工作的开发者，评估轻量全局热键、悬浮状态提示与活动窗口识别桥接。
- **更多生态工具上下文接入**：在保持零第三方依赖与只读明文解析边界的前提下，按需跟进新兴主流 CLI / 桌面 Agent 的会话日志结构与标题协议。
