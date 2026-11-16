# 待办事项（TODO）

## 通信在途取消机制与交互方案

识别（Stage 1 ASR）或精修（Stage 2 Refiner）遇网络抖动、上游排队或退避重试时，当前尚无用户侧主动取消入口。后续推进时遵循以下已拍板边界：

### 架构边界：保持单进程

- 保持现有单进程守护架构，**不拆独立通信 Worker 子进程**，避免引入跨进程世代隔离与常驻状态重建复杂度。
- 接受标准库同步线程模型下的极少数内核调用边界（如同步 DNS `getaddrinfo` 无法被用户态信号即时打断），确保取消后立即阻断结果上屏并尽快中断已建连收发即可。

### 用户侧取消交互备选（无需新增 CLI 子命令）

1. **识别/精修期间再次按录音热键取消**：录音结束后，若处于 `recognizing` 或 `refining` 状态时再次按下对应录音热键，取消当前在途识别/精修任务（而非直接开启下一段录音）。
2. **HUD 胶囊局部取消按钮**：后续在两端 HUD 胶囊上增加局部可点击、不激活窗口、不抢终端焦点的 `×` 按钮（绑定展示时的任务序号防迟到误触；不监听全局 `Esc`，以免吞键干扰 Vim/Agent 终端交互）。

### 单进程底层收敛要点

- **取消令牌贯穿**：每轮任务持独立 `CancelToken`（区分用户主动取消、新轮覆盖、进程退出），贯穿分片调度、退避等待、豆包轮询与二阶段精修；用户取消须穿透 Refiner 原稿兜底，绝不退回初稿继续粘贴。
- **在途 Socket 快速唤醒**：HTTP 传输层登记当前活动 socket（含 TLS 握手前后的 `SSLSocket` 切换，以及归还 Keep-Alive 连接池前先注销），取消时调用 `shutdown(SHUT_RDWR)` 立即唤醒阻塞读写。
- **Codex 精修取消与进程隔离**：`CodexRefiner` 已改为无全局串行锁的多路复用单轮纯 Chat 架构；接入主动取消时仅终止受影响在途轮次或对应 `codex exec` 子进程组，避免误伤同客户端上并行的其他精修请求。
- **投递前互斥门禁**：在最终粘贴/写剪贴板前设短临界区门禁，消除“刚检查完未取消、紧接着触发取消却仍上屏”的竞态。
- **有效录音留存防白说**：`recording` 阶段取消直接丢弃；进入 `recognizing` / `refining` 后主动取消的，将已通过门控的有效 WAV 复用 `save_failed_audio()`（`0600` 原子写、保留 24 小时内最近 10 条）暂存至 `failed/` 供 `retry` 恢复（若来源本身已是 `retry` 则保留原文件不重复落盘），实施时同步更新 `AGENTS.md` 状态隐私说明。

## 会话压缩节点（`/compact`）后的精修上下文截取

当前 `session_ctx.py` 从会话 JSONL 文件头按字节偏移增量提取全部历史用户消息与 Agent 回复（受 `refiner.context_max_chars = 200000` 上限约束）。当长会话执行过多次上下文压缩（如 `/compact`）且底层仍追加在同一个 JSONL 文件中时，把压缩节点之前的陈旧对话一并塞进精修上下文既冗余又易冲掉前缀缓存。后续按各 CLI 实际落盘契约分别适配：

- **`agy`（Antigravity CLI）**：同一会话的压缩前后记录均保存在同一个 `transcript_full.jsonl` / `transcript.jsonl` 内，每次 `/compact` 会写入一条 `{"source": "SYSTEM", "type": "CHECKPOINT", ...}` 记录（`content` 含 `<CONTEXT_SUMMARY>`）。增量解析遇到新的 `CHECKPOINT` 节点时，可丢弃此前累积的 `clean_turns`，改为以最新压缩摘要（或自该节点起的新对话）作为精修上下文起点。
- **`codex` 与 `claude`（Claude Code）**：两者的会话压缩记录格式与单文件/切文件行为与 `agy` 不同（实施前先在本地实测核对 Codex rollout 中的 `compacted` / 摘要事件及 Claude Code JSONL 中的 `compact_boundary` / summary 记录结构，不凭猜测硬编码），按各自确认后的压缩边界统一收敛截取逻辑。

## 本地真实渠道系统测试与精修缓存命中率可观测性

1. **本地真实凭据系统测试模块（不进 CI）**：
   - 在现有 100% 离线单测套件（`tests/`）之外，单独提供按需手动触发的端到端系统测试入口（绝不接入 GitHub Actions CI）。
   - 直接复用本机已配置的 API Key（如百炼、OpenRouter、Gemini 等）与本机 `codex` 订阅登录态，覆盖真实一阶段转写、二阶段语境精修保真度（错词纠正、句式保留、不误答提问）以及多轮连续请求的延迟与缓存命中率实测。
2. **精修前缀缓存命中采集与用户侧提示**：
   - 采集各精修渠道返回的 token 与缓存命中指标（OpenAI 兼容接口的 `usage.prompt_tokens_details.cached_tokens`、DeepSeek 的 `usage.prompt_cache_hit_tokens`、Gemini 原生接口的 `usageMetadata.cachedContentTokenCount`、Codex `app-server` 的 `thread/tokenUsage/updated` 中 `cachedInputTokens`）。
   - 将本轮精修是否命中缓存及命中比例纳入 `status` 运行快照与可选诊断/状态提示，方便直观确认缓存是否生效。
