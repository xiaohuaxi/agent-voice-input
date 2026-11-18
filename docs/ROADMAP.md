# 产品演进路线图（ROADMAP）

> **当前基线**：`v0.7.1`  
> **配套追踪清单**：[docs/TODO.md](./TODO.md) ｜ **项目工程规范**：[AGENTS.md](../AGENTS.md)

本文档定义 `agent-voice-input` 从“面向终端 CLI Agent 的可用工具”迈向“跨终端与桌面场景、外部用户零门槛开箱、高频日常长稳运行”的阶段性演进规划与工程边界。

---

## 一、工程设计原则与验证分级体系

### 1.1 核心准则：重鲁棒性、反过度工程化

项目始终坚持**零第三方 Python 运行时依赖**（仅依赖 Python `>= 3.10` 标准库、宿主机 `ffmpeg` 及双平台系统原生桥接）与**规模匹配运行边界**。在推进后续迭代时，明确划定以下“有所为、有所不为”的架构红线：

- **精修输出契约保持纯文本直出，不上 `json_schema` 结构化输出**：
  - 语音精修本质是高保真序列编辑而非多步结构化抽取。将多段长口述、引号、CLI 参数与反斜杠路径塞入 JSON 字符串，会引入 `1%～4%` 的内部转义失真（吞段落换行、偷换引号、反斜杠错乱）与 `5%～12%` 的额外输出 Token 开销；同时 `json_schema` 在 `/responses`、`:generateContent` 与 `/chat/completions` 多渠道间参数契约碎裂（部分高性价比节点直接返回不可重试的 HTTP 400 触发静默降级），且无法阻止“在 JSON 字段内部回答用户提问”的语义级劫持。
  - 坚持**纯文本输出 + `<asr_draft_to_clean>` / `<task_reminder>` 三明治指令边界**（若个别轻量模型偶发引导语，仅用约十行标准库代码做单对轻量 XML 定界标签无损剥离，漏标签原样回退）。
- **热词体系优先补齐确定性提取，绝不让未确认的单次精修猜测直写持久化 `hotwords.txt`**：
  - Stage 1 专有名词漏识的主因是确定性正则覆盖不全与干净工作树文件词饥饿，而非缺少动态猜词。若将轻量精修模型的单次猜测自动写入持久化 `hotwords.txt`，一旦偶发误修就会形成“脏词声学偏置 → 后续识别持续带偏”的自激污染闭环，并迅速挤爆豆包 ASR 等渠道极窄的热词配额（`30 词 / 180 字符`）。
  - 优先通过确定性正则扩展、干净工作树 `git ls-files` 兜底与低优先级内置基础词表解决 90%+ 痛点；任何动态词频建议仅可基于**用户已回车确认提交的会话输入**离线生成。
- **桌面 GUI 适配抓大放小，不碰编辑器私有数据库与无障碍 DOM 抓取**：
  - 复用已有的前台窗口进程/Bundle ID、窗口标题、macOS `AXDocument` 文档路径及同构明文日志（如桌面版 Antigravity IDE 的 `transcript_full.jsonl`）即可低成本获得高价值上下文与热词。
  - **明确不做**：不逆向读取 VS Code / Cursor 运行期持锁且表结构易变的内部 SQLite（`state.vscdb`），防 `SQLITE_BUSY` 锁竞争与版本升级断裂；不对桌面版 ChatGPT、IM 或浏览器遍历无障碍（Accessibility / UIAutomation）组件树抓取聊天内容，杜绝长帖卡顿、UI 改版脆弱性与用户隐私信任风险。
- **单进程守护、被动旁路监听与默认绝不自动回车**：
  - 在途取消与并发精修保持现有单进程守护架构，**不拆独立通信 Worker 子进程**；
  - 双端键盘钩子保持纯被动旁路监听（不吞键、不改写系统事件流），跨端远程桌面让行统一在守护进程触发入口静默判定；
  - 无论终端还是桌面 GUI 应用，**默认只粘贴、绝不自动发送回车（`Enter`）**，防止终端误执行命令或聊天窗口直接发出未核对消息。

### 1.2 自动化验证分级：最大化自动闭环 + 薄人工真机卡点

为保障每次迭代可快速回归且不引入跨平台隐患，所有改动按两级验证体系签收：

1. **第一级：100% 自动闭环验证（主力门禁，零人工介入即可签收）**
   - **离线单元测试套件**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（全程离线可重入，利用假 Shim 子进程、回环慢速 SSE 服务、本地隔离 `tmux -L` 服务及模拟窗口/工作区目录树覆盖协议、并发、路由与异常分支）。
   - **原生 Shim 离线自检**：macOS `mac_shim --self-check` 与新增 Windows `win_shim.ps1 -Mode self-check`，在无声卡、无全局钩子权限的 CI 环境中直接验证原生编译、按键状态机、粘贴路由表与离屏 HUD 位图渲染。
   - **隔离全链路系统自检**：运行 [`.agents/skills/voice-eval/scripts/e2e_system.py`](../.agents/skills/voice-eval/scripts/e2e_system.py)，在独立运行目录与隔离 `tmux` 会话中端到端验证守护进程、真 ASR、精修缓存、串行/并发落字及 `0600` 隐私脱敏契约。
   - **真实录音时间点回放基准**：基于 [`.agents/skills/voice-eval/`](../.agents/skills/voice-eval/SKILL.md) 回放本地归档录音，按口述发生当刻的时间戳截断还原历史会话上下文（防未来轮次信息泄露），复用本地固定 ASR 初稿缓存执行确定性规则硬断言与多维盲评对比。所有录音索引与评测中间产物严格限定在仓外缓存目录（`${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/`，权限 `0700`/`0600`），严禁进入版本库。
2. **第二级：薄人工真机验收卡点（压缩至最薄一环）**
   - 仅当改动触及操作系统底层硬件链路、窗口焦点权限或主观交互分寸时（如蓝牙耳机盒内假在线状态、跨终端/桌面 GUI 真实焦点粘贴与剪贴板恢复时序、macOS 辅助功能授权弹窗、远程桌面按键透传、HUD 局部按钮防抢焦点），在自动门禁全绿后执行一次 3～5 分钟的真机确认。

---

## 二、阶段版本演进规划

版本路线按 **`v0.8.0` -> `v0.9.0` -> `v0.10.0`（及按需 `v0.11.x`）** 分阶段推进。

> **关于 `v1.0.0` 的发布原则**：`v1.0.0` **不预设赶工时间节点**，仅在全部约定功能范围高度可用、双平台自动化门禁完备，并经过连续日常高频长稳检验合格后，再正式打标发布。

---

### `v0.8.0`：核心日常体验跃升（精修重构、全局输入与远程桌面让行、WSL 蓝牙双胶囊修复）

聚焦解决日常最高频的三大痛点：长口述精修质量与跨会话术语纠偏、任意桌面窗口语音输入与跨端远程桌面冲突、以及蓝牙耳机盒内假在线引发的 HUD 异常。

#### `v0.8-1`：修复 WSL 端蓝牙耳机充电盒假在线触发双 HUD 胶囊竞态
- **背景与方案**：Windows 下蓝牙耳机置于充电盒（或经远程音频重定向）时，WASAPI 端点仍保持 `ACTIVE` 且 `IAudioClient.Start()` 立即返回 `S_OK`，但持续产出零数据包；现有逻辑未验首帧即弹出主录音胶囊，满 `900ms` 断流重开后又未验真帧即谎报 `rec_recovered` 叠出 `"已自动恢复"` Toast，最终在配额耗尽后双双消失。对齐 macOS 捕获不变量修复 [`src/agent_voice_input/win_shim.ps1`](../src/agent_voice_input/win_shim.ps1)：
  - 非 `loopback` 启动期引入最多 `850ms` 首帧门禁（主线程等待期间以 `25ms` 步进调 `PeekMessage(..., PM_NOREMOVE)` 保活低级键盘钩子防卡键；超时零帧返回 `mic_busy:wasapi_startup_no_frames`，仅弹单条麦克风暂忙 Toast，不进假录音态）；
  - 中断重开仅在本次会话曾收到真实音频帧（`everGotFrames == true`）时启用，且重开后须实际读到新音频包才补齐静音并发送 `rec_recovered`（`500ms` 内仍无帧则直接转 `rec_lost` 收敛）。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（覆盖 `WslPlatform._classify_wasapi_start_error` 对 `mic_busy:wasapi_startup_no_frames` 的异常分类、不触发 `recording` 主胶囊及断流收敛单测）。
  - **真机薄验收**：Windows/WSL2 真机将已连接蓝牙耳机放入充电盒按热键，确认约 `0.85s` 内仅弹单条暂忙提示、无双胶囊同现；正常佩戴耳机确认 `10～20ms` 零感知进入录音。

#### `v0.8-2`：存量录音时间点回放评测、精修提示词与载荷重构、Stage 1 热词提取补齐
- **背景与方案**：
  1. **存量录音时间点回放评测基建**：扩展 [`.agents/skills/voice-eval/`](../.agents/skills/voice-eval/SKILL.md)，将本地环形归档录音与根会话 JSONL 按时间戳精确对齐，截取口述当刻之前的历史上下文回放，缓存固定 Stage 1 ASR 初稿，跑通“确定性格式/抗劫持硬断言 + 参考锚定多维盲评”，评测产出全部落盘仓外 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/`。
  2. **精修提示词与载荷顺序重构**（[`src/agent_voice_input/refiner.py`](../src/agent_voice_input/refiner.py)）：
     - 解开现行系统提示词中 Rule 2（清理口语填充与重复）与 Rule 3（禁删任何子句或尾句）的正面冲突：明确授权消解**跨分句思维折返（前说后否）**、**车轱辘话同义反复**与**无信息量口头禅尾句**，支持长口述在显式口述枚举（“第一点 / 第二点”）时合理分段，同时严禁强加因果连接词或脑补未提及的技术细节；
     - 放开纠错词源限制，允许结合上下文语境纠正不在历史日志中的通用 AI / 编程中英术语谐音（如 `Codex`、`Luna`、`Gemini Flash-Lite` 等）；
     - 在尾部动态块 `final_block` 中将 `<workspace_git_status>` 前置于 `<asr_draft_to_clean>` 之前，顺应自回归模型前向因果注意力且零损前缀缓存；核验并优化 `/responses` 历史分块消息角色权重，降低海量只读日志对短口述的文风同化；
     - 构建同源基座下的 **`agent` / `general` 自适应双子规则**，兼顾编程指令精确度与日常通用输入自然度。
  3. **Stage 1 确定性热词提取补齐**（[`src/agent_voice_input/session_ctx.py`](../src/agent_voice_input/session_ctx.py)、[`src/agent_voice_input/asr_providers.py`](../src/agent_voice_input/asr_providers.py)）：
     - 扩展 `_TECH_IDENT_RE` 覆盖 `PascalCase` 类名（如 `SessionTracker`）、`kebab-case` 模型名/参数及 `.jsonl` 等常见后缀；
     - 当工作树干净（`git status --short` 为空）时，以带 TTL 缓存的轻量 `git ls-files` 提取核心源码文件名兜底；
     - 内置低优先级精选通用 AI/CLI 基础词表（排在用户 `hotwords.txt` 与当前仓库动态词之后，严守豆包 ASR `30 词 / 180 字符` 等渠道配额上限）。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（覆盖新正则提取、干净工作树兜底、渠道配额截断、载荷块顺序与双模式切换单测）+ `bench_refiner.py`（6 轮探针缓存命中与抗劫持全绿）+ `voice-eval` 存量真实录音时间点回放基准量化对比。
  - **真机薄验收**：抽检本地仓外回放生成的 3～5 条长口述脱敏前后对比，确认跨句折返清理与分段自然、零原意篡改。

#### `v0.8-3`：解锁全局通用语音输入、远程桌面自动让行、上下文硬隔离与双端安全投递
- **背景与方案**：
  1. **全局作用域配置与远程桌面静默让行**（[`src/agent_voice_input/config.py`](../src/agent_voice_input/config.py)、[`src/agent_voice_input/daemon.py`](../src/agent_voice_input/daemon.py)）：
     - 新增 `hotkey.scope = "all" | "coding" | "terminals"`（默认 `"all"`，向后兼容旧字段 `only_in_terminals`）；
     - 新增 `hotkey.yield_remote_desktop = true`（默认开启）与 `hotkey.ignore_apps` 自定义扩展列表：当聚焦双端远程桌面/串流/虚拟机客户端（如 macOS `Windows App`、`Screen Sharing`、`Parsec`、`RustDesk`、`VMware`、`Parallels`；Windows `mstsc`、`vmconnect` 等，**刻意排除裸 `msrdc.exe` 以免误伤 Win11 本地 WSLg Linux GUI 窗口**）且本地未处于录音态时，热键启动入口 100% 静默让行给远端响应；若本地已在录音中切入远程窗口，仍允许正常按键停止收尾。
  2. **非终端/非 Agent 窗口上下文硬隔离**：当前台窗口属于普通非 Agent 应用（`general` 模式）时，强制跳过 `SessionTracker.sync_session`，彻底杜绝 WSL2 `/proc` 全局扫描、后台挂接 `tmux` 或 macOS 磁盘历史会话回退把无关代码仓 200K 上下文串台注入日常聊天/写作请求，并严格保持不自动回车。
  3. **修复双端非终端投递死锁、粘贴键路由与剪贴板免污染保护**（[`src/agent_voice_input/platform_wsl.py`](../src/agent_voice_input/platform_wsl.py)、[`src/agent_voice_input/platform_macos.py`](../src/agent_voice_input/platform_macos.py)）：
     - 解除 WSL2 `deliver_text` 硬编码 `"expect_terminal": True` 与 macOS `deliver_text` 硬编码 `not self.is_allowed_terminal(fg)` 导致的非终端永不粘贴 Bug，改为锁定录音起点窗口句柄与进程/Bundle ID；
     - WSL2 新增活跃 `tmux` 窗格 `paste-buffer -d -p` 免剪贴板直投；`win_shim.ps1` 建立进程级粘贴组合键路由（GUI / IDE / Windows Terminal 走 `Ctrl+V`；`alacritty` / `wezterm` / `kitty` / `rio` / `ghostty` 走 `Ctrl+Shift+V`；`mintty` / `putty` 走 `Shift+Insert`）及同窗口切 Tab 标题漂移核对；
     - macOS 对同 `hwnd` + 同 `bundle_id` 的非终端 GUI 窗口放宽动态标题漂移判定（防流式对话标题或编辑器未保存标记中断粘贴），并将慢速 `osascript` `Cmd+V` 下沉至 `mac_shim.swift` 原生 `CGEvent` 虚拟键注入；
     - 移除 macOS Ghostty / `tmux` 直投成功路径多余的写剪贴板调用，并为模拟按键粘贴路径增加纯文本剪贴板快照备份与延迟恢复（仅焦点切走退回剪贴板保底时保留识别文本）。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（覆盖 `scope` 门禁、远程客户端静默让行与 WSLg `msrdc` 豁免、非终端 `sync_session` 硬隔离、进程粘贴键路由纯函数、直投零剪贴板触碰与快照恢复单测）+ `mac_shim --self-check` + `e2e_system.py`（同步更新 `config.example.toml`、`README.md` 与 `README.zh-CN.md`）。
  - **真机薄验收**：① Mac 全屏 `Windows App` 连远端 Windows/WSL2 按热键验证本地零弹窗零抢麦、远端正常触发；② 双端在浏览器、聊天应用、IDE 与跨平台终端（WezTerm / Mintty）验证原地落字及原剪贴板内容恢复；③ macOS 非 Ghostty GUI 首次粘贴验证辅助功能（Accessibility）提示与剪贴板回退兜底。

---

### `v0.9.0`：2026 模型换代、原生音频单跳实证落定、操控流畅度与桌面感知

聚焦底层模型代际升级、单跳原生音频与双阶段路线实证统一、连续/在途操控体验升级，以及高性价比桌面 IDE 场景感知。

#### `v0.9-1`：单跳原生音频前缀缓存修复、2026 模型换代与双轨实测横评
- **背景与方案**：
  1. **修复单跳多模态前缀缓存归零缺陷与长音频策略**：当前 `GeminiAudioProvider` 与 `ChatAudioAsrProvider` 使用 16K 滑动截尾 `_compose_context_text` 且前置易变 `git_status`，导致每轮前缀偏移、缓存命中率恒为 `0%`。改为统一复用 Stage 2 的 **50K 定界封口 `<session_reference_log>` 分块结构**（静态指令与固定历史块在前，音频与动态工作区状态殿后）；为 `AsrResult` 补齐 `input_tokens` / `cached_input_tokens` / `audio_tokens` / `reasoning_tokens` 遥测提取；对支持长音频的单跳模型放宽整段直传切块阈值并支持轻量 `64 kbps MP3` 编码上传以降低跨境代理带宽耗时。
  2. **2026 下半年模型代际升级与思考开关适配**：将各渠道残留的 2024–2025 旧默认模型统一升级至 2026 下半年最新轻量长上下文（`>=250K～1M`）主力模型（如 `Codex` 保持 `Luna` 档，`Gemini` 升级至 `gemini-3.5-flash-lite`，`DashScope` 升级至 `qwen3.8-flash` 及单跳 `qwen3.8-omni-flash`，`Volcengine` 升级至 `doubao-seed-2-0-mini` / `2-0-lite` 等）；在 `apply_chat_effort_params` 与 `OpenAICompatibleRefiner` 补齐各厂商混合推理模型的显式关思考参数（如 `enable_thinking: false`、`thinking: {"type": "disabled"}`），避免隐式思维链拖慢首字延迟；补齐 `GeminiRefiner` / `OpenAICompatibleRefiner` 的 `prepare_session()` 连接预热。
  3. **「双阶段文本精修 vs 原生音频一步到位」实测横评**：基于存量真实录音集与 6 类确定性对抗探针（反问句、列清单劫持、中途改口、生僻代码符号中英混读、首尾底噪抗幻觉、长口述），横向对比端到端延迟、缓存命中率、中英术语准确率与异常降级韧性，确立双轨并存推荐配置（双阶段高韧性默认路线 + 单跳极速可选路线）。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（覆盖单跳 50K 分块字节稳定性、音频/缓存 Token 解析、各渠道关思考请求参数断言）+ `voice-eval` 多模态与双阶段横向基准脚本全自动跑分。
  - **真机薄验收**：使用更新后的默认模型与单跳极速模式各进行数轮日常长短口述，确认端到端体感延迟与环境底噪零幻觉表现。

#### `v0.9-2`：单进程在途语音处理取消、连发口述 Stage 2 并发保序落字与 `win_shim` 编译缓存自愈
- **背景与方案**（具体底层实现要点详见 [docs/TODO.md](./TODO.md) 技术备忘）：
  1. **单进程在途取消机制**：保持单进程架构，支持在 `recognizing` / `refining` 期间再次按录音热键主动取消（及后续可选的不抢焦点 HUD 胶囊局部 `×` 按钮）；每轮持独立 `CancelToken` 穿透重试与原稿兜底，登记活动 HTTP/TLS Socket 调 `shutdown(SHUT_RDWR)` 立即唤醒阻塞读写，加投递前互斥临界区防迟到上屏，并将已过门控的有效 WAV 复用 `save_failed_audio()` 存入 `failed/` 供 `retry` 找回。
  2. **连发口述 Stage 2 并发精修与保序落字**：Stage 1 ASR 保持串行消费以防上游 QPS 限流；Stage 2 无状态精修改为前句未结束即可并发发起下一句请求，最终严格按 `seq_id` 顺序提交上屏；补记 `queue_wait_ms` 遥测并解除阶段回调对 `_trigger_lock` 的争用。
  3. **WSL2 `win_shim.ps1` C# DLL 缓存加速与未就绪自愈**：按脚本 SHA-256 将编译后的程序集缓存在 Windows `%LOCALAPPDATA%\agent-voice-input\`，消除每次启动现场编译受安全软件扫描拖慢数秒的抖动；修复 `WslPlatform._ensure_shim` 对 `ready` 前超时退出子进程的检活与自动重拉自愈。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（本地回环慢速 SSE 服务模拟 `<100ms` 取消断连、零上屏与 `0600` `failed/*.wav` 暂存；多句乱序返回下按 `seq_id` 严格保序投递；假挂死 Shim 自愈重拉单测）+ `e2e_system.py`。
  - **真机薄验收**：WSL2 真机验证热启动秒级就绪、识别中按热键即时取消（若启用 HUD `×` 按钮，在 Windows 多显示器高分屏与 macOS 全屏 Space 下确认点击绝不抢走下层输入框焦点）、连续三句快速口述保序落字。

#### `v0.9-3`：桌面端 GUI 高性价比场景感知（macOS + Windows）
- **背景与方案**：严守“不读私有 SQLite、不抓无障碍 DOM”边界，落地三项高投入产出比增强：
  1. **桌面版 Antigravity IDE 同构会话复用**：识别前台桌面版 Antigravity IDE 窗口并解析当前工作区路径，自动匹配同构明文 `brain/<cid>/.system_generated/logs/transcript_full.jsonl` 根会话，无缝接入现有增量读取与 50K 分块前缀缓存。
  2. **VS Code / Cursor / Zed 工作区热词注入与内置终端关联**：通过窗口标题（`<file> — <workspace> — <IDE>`）与 macOS `AXDocument` 属性反解项目根目录 `cwd`，复用 `_get_git_info` 注入变更文件与模块名热词（**WSL2 红线**：识别为 `/mnt/c/...` 等 9P 跨盘路径时严禁执行 `git status` 防大仓阻塞），并打通 IDE 内置终端中存活 CLI Agent 会话的自动匹配。
  3. **桌面版 ChatGPT / 聊天应用末尾场景轻提示**：绝不改动静态 `REFINER_SYSTEM_PROMPT`（防击穿前缀缓存），仅在动态 `final_block` 末尾附单行只读 `<target_app_hint category="..." />` 区分编程 IDE 与通用对话倾向。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（模拟双平台 IDE/聊天窗口元数据、`AXDocument` 路径解析、WSL `/mnt/` 跨盘跳过门禁、同构 `transcript_full.jsonl` 关联及静态系统前缀零变动单测）。
  - **真机薄验收**：在 macOS 与 Windows 的桌面版 Antigravity IDE、Cursor / VS Code 及桌面版 ChatGPT 中各做一次含仓内文件名口述的端到端验收。

---

### `v0.10.0`（及按需 `v0.11.x`）：外部开箱零门槛、运行期健壮性与 CI 门禁

聚焦消除外部新用户首次安装配置摩擦、补齐诊断真体检、加固长期常驻卫生与双平台 CI 编译门禁。

#### `v0.10-1`：`refiner.provider = "auto"` 开箱自适应与 `doctor` 全链路真体检
- **背景与方案**：
  1. **默认 `refiner.provider = "auto"`**：解决当前默认强绑 `codex` 导致纯第三方 API Key 用户开箱 `doctor` 报错或公开账号模型名不匹配回退的问题——优先检测本机已登录 `codex` 并校验匹配可用模型，否则按已配置环境变量凭据自动对齐至 `openrouter` / `dashscope` / `gemini` / `openai` / `volcengine` / `groq` / `siliconflow`。
  2. **升级 `doctor` 消除假绿（False Positive）**：补齐 macOS `swiftc` 与 `mac_shim` 编译校验、麦克风授权状态、非 Ghostty 粘贴所需的辅助功能（Accessibility）及功能键所需的输入监控（Input Monitoring）权限检查；补齐 WSL2 WASAPI 实际音频流打开探针（检出系统麦克风隐私开关拦截）；在不支持的原生 Linux 桌面给出清晰环境提示而非绿灯放行；提供轻量可选的上游 API 连通性验证。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（构造隔离 `HOME`、多渠道凭据矩阵与模拟权限状态，全覆盖断言 `auto` 推导结果、`doctor` 退出码与 `redact_secrets` 脱敏输出）。
  - **真机薄验收**：在仅配置单把第三方 API Key（无 `codex`）及手动关闭系统麦克风/辅助功能权限的环境下核对 `doctor` 引导提示。

#### `v0.10-2`：守护进程运行期卫生、双平台 Shim CI 门禁与可选热词学习
- **背景与方案**：
  1. **单实例锁、`retry` 交互修正与过期会话清理**：在 `runtime_dir / "daemon.lock"` 引入 `fcntl.flock(LOCK_EX | LOCK_NB)` 防止并发启动双守护进程争抢热键钩子；修复 `retry` 子命令既对当前 shell 模拟粘贴又向 stdout 打印的双重输出问题（改为默认写剪贴板 + 打印 stdout，显式传 `--paste` 才按键粘贴），并在转写失败且已暂存音频时于 HUD Toast 提示可用 `retry` 恢复；为持久化缓存目录下的 `sessions/*.json` 增加过期死 PID 惰性清理。
  2. **`win_shim.ps1 -Mode self-check` 与 GitHub Actions 门禁补齐**：对标 `mac_shim --self-check` 为 `win_shim.ps1` 增加离线 `-Mode self-check`；在 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) 新增 `windows-latest` 自检任务，并在 `ci.yml` / `publish.yml` 的构建产物断言中补检 `mac_shim.swift` 与 `macos-latest` 发版门禁。
  3. **（低优可选，可归入 `v0.11.x`）基于已确认会话输入的个人高频热词建议**：按需从根会话 JSONL 中用户已实际回车提交的 `USER_INPUT` 轮次统计高频 ASCII 技术标识符，供用户审阅确认后追加至 `hotwords.txt`（零热路径延迟、绝不自动盲写污染）。
- **验证方式**：
  - **自动闭环**：`PYTHONPATH=src python3 -m unittest discover -s tests -v`（多进程抢锁、`retry` 输出模式、过期会话注册表清理、确认词频提取过滤单测）+ `powershell -NoProfile -File src/agent_voice_input/win_shim.ps1 -Mode self-check` + GitHub Actions `ubuntu` / `macos` / `windows` 矩阵及 wheel/sdist 打包检查全绿。
  - **真机薄验收**：若配套提供可选开机自启配置模板（macOS `launchd` / WSL `systemd --user`），在真机重登录时确认一次守护进程自启与麦克风 TCC 权限归属正常。

---

### `v1.0.0`：正式版准入标准（Quality Gate）

`v1.0.0` 仅在以下四项准入条件全部达成并通过日常高频长稳检验后发布：

1. **外部用户零门槛开箱**：仅配置任意单一主流渠道凭据或本地 CLI 登录态，无需手动覆盖内部模型名即可通过 `doctor` 真体检并稳定完成识别与精修。
2. **全场景安全投递与零串台**：终端 CLI Agent、主流桌面 IDE、普通桌面 GUI 应用与跨端远程桌面让行均可靠运作；非 Agent 场景 100% 隔离后台代码仓上下文，系统剪贴板零意外污染，全链路严守不自动发回车。
3. **高保真精修与操控韧性**：在真实长短口述回归集上，跨句折返消解、中英技术术语还原与抗指令劫持指标稳定达标，长上下文前缀缓存高效命中；支持在途即时取消与失败录音无损 `retry`。
4. **双平台原生桥接与工程门禁闭环**：`mac_shim.swift` 与 `win_shim.ps1` 均具备离线自检并纳入 CI 三平台必跑门禁；守护进程单实例互斥、异常自愈、缓存清理与 `0600` 隐私脱敏契约经连续数周高频日常使用无严重回归。
