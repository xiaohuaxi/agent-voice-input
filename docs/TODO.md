# 迭代追踪清单与技术备忘（TODO）

> **阶段全貌、不做边界与验证分级说明见**：[docs/ROADMAP.md](./ROADMAP.md)

本文件作为日常迭代的任务勾选面板，并保留跨模块特性的具体底层实现备忘。

---

## 一、阶段迭代追踪清单

### `v0.8.0`：核心日常体验跃升（已完成）
- [x] **`v0.8-1` WSL 蓝牙耳机充电盒双 HUD 胶囊竞态修复**：`win_shim.ps1` 补齐非 `loopback` 启动期 `850ms` 首帧门禁（`PeekMessage` 保活低级钩子）与中段重开有帧恢复确认。
- [x] **`v0.8-2` 精修评测回放、提示词/载荷重构与热词补齐**：
  - [x] 扩展 `voice-eval` 支持存量录音按会话时间点（Point-in-Time）回放评测（产出限定仓外缓存目录）；
  - [x] 重构 `REFINER_SYSTEM_PROMPT`（解开 Rule 2/Rule 3 折返与车轱辘话清理死锁、放开跨会话通用技术术语谐音纠偏、前置 `<workspace_git_status>`、锁定 `/responses` 历史块 `developer` 前缀缓存角色、自适应 `agent` / `general` 双子规则）；
  - [x] 补齐 Stage 1 热词提取（扩展 `_TECH_IDENT_RE` 覆盖 `PascalCase` / `kebab-case` / `.jsonl`、干净工作树 `git log` / `ls-files` 核心文件名兜底、内置低优通用 AI/CLI 词表并严守豆包等窄配额）。
- [x] **`v0.8-3` 全局通用语音输入、远程桌面让行与双端安全投递**：
  - [x] 新增 `hotkey.scope = "all" | "coding" | "terminals"`（默认 `"all"`，双向兼容 `only_in_terminals`）及 `hotkey.yield_remote_desktop = true` + `ignore_apps`（排除裸 `msrdc.exe` 防误伤 Win11 WSLg）；
  - [x] 窗口三分类（`terminal` / `coding_gui` / `general_gui`）：非终端窗口硬跳过 `SessionTracker.sync_session` 防后台 CLI 会话串台（`coding_gui` 走无历史 `agent` 模式、`general_gui` 走 `general` 模式），全场景保持不自动回车；
  - [x] 修复 WSL2/macOS 非终端投递门禁死锁与 `$TMUX` 跨窗误投、Windows 终端进程级粘贴键路由（`Ctrl+V` / `Ctrl+Shift+V` / `Shift+Insert` 含 `VK_INSERT` 扩展键标志）与 `[#id8]` 会话标记冲突检测、macOS GUI 动态标题放宽与 `mac_shim` 零 `Carbon` 原生 `CGEvent Cmd+V`、移除 Ghostty/`tmux` 直投写剪贴板（放弃存在后台 Tab 盲贴风险的 WSL2 `tmux` 直投；模拟粘贴剪贴板多格式备份恢复归入 `v0.9-2` 单进程状态机专项）。

### `v0.9.0`：2026 模型换代、原生音频单跳落定、操控流畅度与桌面感知
- [ ] **`v0.9-1` 单跳原生音频缓存对齐、2026 模型换代与双轨实测横评**：
  - [ ] 修复 `GeminiAudioProvider` / `ChatAudioAsrProvider` 滑动截尾零缓存问题（复用 50K 定界封口分块）、补齐单跳 Token/缓存指标与长音频整段/压缩上传策略；
  - [ ] 各渠道默认模型升级至 2026 下半年最新轻量长上下文代际，补齐混合推理模型显式关思考参数与连接预热；
  - [ ] 完成「双阶段文本精修 vs 原生音频一步到位」全量实测横评。
- [ ] **`v0.9-2` 单进程在途取消、连发口述 Stage 2 并发保序落字与 `win_shim` 编译缓存自愈**（实现细则见下方第二节）。
- [ ] **`v0.9-3` 桌面端 GUI 高性价比场景感知**：桌面版 Antigravity IDE 同构 `transcript_full.jsonl` 上下文复用、VS Code / Cursor / Zed 窗口标题与 `AXDocument` 工作区热词注入（跳过 WSL `/mnt/` 9P 路径）、桌面版 ChatGPT / 聊天应用末尾轻量场景提示。

### `v0.10.0`（及按需 `v0.11.x`）：开箱零门槛、运行期卫生与 CI 门禁
- [ ] **`v0.10-1` 开箱自适应与真体检**：默认 `refiner.provider = "auto"` 按可用凭据自动匹配公开模型；升级 `doctor` 覆盖权限、Shim 编译、WASAPI 流打开与连通性核查。
- [ ] **`v0.10-2` 运行期健壮性与双平台 CI 门禁**：守护进程 `flock` 单实例锁、修复 `retry` 粘贴与 stdout 双重输出、过期 `sessions/*.json` 清理、`win_shim.ps1 -Mode self-check` 与 GitHub Actions 三平台/打包门禁；（低优可选）基于用户已确认提交的会话输入提取个人高频热词建议。

---

## 二、`v0.9-2` 底层实现设计备忘（已拍板边界）

### 2.1 通信在途取消机制与交互方案

识别（Stage 1 ASR）或精修（Stage 2 Refiner）遇网络抖动、上游排队或退避重试时，当前尚无用户侧主动取消入口。推进 `v0.9-2` 时遵循以下已拍板边界：

#### 架构边界：保持单进程

- 保持现有单进程守护架构，**不拆独立通信 Worker 子进程**，避免引入跨进程世代隔离与常驻状态重建复杂度。
- 接受标准库同步线程模型下的极少数内核调用边界（如同步 DNS `getaddrinfo` 无法被用户态信号即时打断），确保取消后立即阻断结果上屏并尽快中断已建连收发即可。

#### 用户侧取消交互备选（无需新增 CLI 子命令）

1. **识别/精修期间再次按录音热键取消**：录音结束后，若处于 `recognizing` 或 `refining` 状态时再次按下对应录音热键，取消当前在途识别/精修任务（而非直接开启下一段录音）。
2. **HUD 胶囊局部取消按钮**：后续在两端 HUD 胶囊上增加局部可点击、不激活窗口、不抢终端焦点的 `×` 按钮（绑定展示时的任务序号防迟到误触；不监听全局 `Esc`，以免吞键干扰 Vim/Agent 终端交互）。

#### 单进程底层收敛要点

- **取消令牌贯穿**：每轮任务持独立 `CancelToken`（区分用户主动取消、新轮覆盖、进程退出），贯穿分片调度、退避等待、豆包轮询与二阶段精修；用户取消须穿透 Refiner 原稿兜底，绝不退回初稿继续粘贴。
- **在途 Socket 快速唤醒**：HTTP 传输层登记当前活动 socket（含 TLS 握手前后的 `SSLSocket` 切换，以及归还 Keep-Alive 连接池前先注销），取消时调用 `shutdown(SHUT_RDWR)` 立即唤醒阻塞读写。
- **Codex 精修取消与连接收敛**：`CodexRefiner` 已切换为纯无状态 `http.client` Keep-Alive `/responses` 直连（仅在凭据距过期不足 30 分钟或 Keyring 无文件模式下短调一次 `codex app-server` `getAuthStatus`）；接入主动取消时直接关闭当前在途 HTTP socket（若正处于凭据刷新则终止对应短生命周期子进程），不影响后续请求。
- **投递前互斥门禁**：在最终粘贴/写剪贴板前设短临界区门禁，消除“刚检查完未取消、紧接着触发取消却仍上屏”的竞态。
- **有效录音留存防白说**：`recording` 阶段取消直接丢弃；进入 `recognizing` / `refining` 后主动取消的，将已通过门控的有效 WAV 复用 `save_failed_audio()`（`0600` 原子写、保留 24 小时内最近 10 条）暂存至 `failed/` 供 `retry` 恢复（若来源本身已是 `retry` 则保留原文件不重复落盘），实施时同步更新 [`AGENTS.md`](../AGENTS.md) 状态隐私说明。

### 2.2 连发口述优化：ASR 保持串行 + 无状态精修并发重叠（保序落字）

连续快速录入多条语音时，当前单线程 `ASR -> Refine -> Deliver` 全串行队列会产生队头阻塞（后一条需等前一条精修与粘贴全部完成才开始识别）。推进 `v0.9-2` 时遵循以下优化边界：

- **Stage 1 ASR 保持串行**：部分语音识别渠道（如豆包试用期 QPS / 并发限制较严）在突发并发下易触发限流报错，且 ASR 本身热态耗时较短（通常 `<1s`），因此第一阶段继续单路串行消费录音队列。
- **Stage 2 精修并发重叠 + 严格保序投递**：`OpenAIResponsesRefiner` / `CodexRefiner` 及各 HTTP 精修器现均为纯无状态请求，上游对并发承受度充裕且占用了主要耗时（`1.5s～3.5s`）。下一条语音跑完串行 ASR 后无须等待上一条精修结束即可并发发起精修请求，最终在终端粘贴投递环节严格按 `seq_id` 顺序提交上屏。
- **补充排队可观测性与去锁争用**：在 `_QueuedUtterance` 与 `telemetry.jsonl` 中补记 `queue_wait_ms`（区分队列等待与实际处理耗时），并避免后台管线阶段回调在录音启停期间争抢 `_trigger_lock`。
