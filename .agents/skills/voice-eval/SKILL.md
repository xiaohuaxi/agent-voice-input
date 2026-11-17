---
name: voice-eval
description: 在本机真实长会话或合成样本上实测语音输入精修器（Refiner）与零打扰全链路系统自检（真 ASR、精修缓存、守护进程串行队列、隔离 tmux 落字），或只读诊断本机守护进程近期口述变慢、降级与缓存未命中问题。改动精修提示词、上下文分块、缓存复用、渠道实现、守护进程管线，需要跑真实基准或端到端真机回归，或排查实机口述延迟与润色异常时加载；日常纯离线单元测试、热键或 HUD 界面样式调整及常规文档修改不加载。
---

# 语音链路实测、全链路自检与线上诊断（voice-eval）

覆盖三类本机实证验证任务：
1. 真实长会话精修基准（`scripts/bench_refiner.py`）：改动 Stage 2 精修器、提示词、`<session_reference_log>` 分块或前缀缓存机制后，用本机真实长会话与真实凭据跑连续多轮冷/热基准。
2. 零打扰全链路系统自检（`scripts/e2e_system.py`）：功能新增或重构后，在不触碰桌面热键、HUD、剪贴板与主守护进程的前提下，自动跑通「真实人声 WAV → 真 ASR → 真多块上下文 → 真无状态精修 → 真 `VoiceInputDaemon` 串行队列 → 隔离 `tmux` 粘贴读回 → 遥测与故障恢复」全链路。
3. 本机运行期只读诊断（`scripts/diagnose_live.py`）：用户反馈近期口述变慢、缓存未命中、专有名词未修对或偶发降级时，秒级只读定位根因。

## 安全与隐私红线

- 红线：调用 `CodexRefiner` 或任何 `codex` 子进程做实测时，严禁通过参数、配置或环境变量开启 Fast 模式。
- 红线：严禁将任何真实会话内容、语音样本、`telemetry.jsonl` / `daemon.log` 运行流水、宿主机隐私路径或未脱敏凭据写入版本库。
- 本地输出脱敏与隔离边界：终端报告与仓外临时输出统一经 `redact_secrets` 脱敏。`diagnose_live.py` 不输出任何会话或口述正文（`--json` 将 `saved_audio` 转为布尔字段 `has_saved_audio`，不暴露宿主机路径）；`e2e_system.py` 对宿主机 `recordings/` 真实历史录音回放仅报告音频时长、分段耗时、token/缓存命中数与落字字符长度，绝不打印真实用户口述原文或隐私路径；`bench_refiner.py` 仅对合成测试句及最多 2 个从会话提取的安全代码标识符展示精修对照（超长异常输出自动截断）；`bench_refiner.py` 与 `e2e_system.py` 全程在临时 `0700` `runtime_dir`（及隔离 `XDG_CACHE_HOME`）运行并自动清理，`e2e_system.py` 合成的测试 WAV 仅缓存于仓外 `~/.cache/agent-voice-input/eval-wavs/`（权限 `0700`）。

## 场景 1：真实长会话精修基准实测（`scripts/bench_refiner.py`）

从仓库根目录运行：

```bash
# 自动挑选本机长根会话，跑默认精修渠道连续 6 轮冷/热基准
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/bench_refiner.py

# 指定轮数与首轮预热提前量，或多渠道同台横评（--model 仅支持单 provider）
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/bench_refiner.py --turns 3 --prewarm-ms 600
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/bench_refiner.py \
  --providers codex,dashscope,gemini --out /tmp/avi-bench.md

# 指定会话文件、纯合成长会话样本，或附加本地 WAV 串跑 Stage 1 + Stage 2
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/bench_refiner.py --session-file /path/to/transcript.jsonl
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/bench_refiner.py --synthetic --wav /tmp/sample.wav
```

关键参数与探针设计：
- 会话选样：默认按最近修改时间扫描 `~/.gemini/antigravity-cli/brain/`、`~/.codex/sessions/`、`~/.claude/projects/` 中 `>= 4 KB` 的非 subagent 根会话，先检查文件 `>= 64 KB` 的前 45 条再检查 `< 64 KB` 的前 15 条（要求清洗后至少 2 轮对话；传 `--prefer-compacted` 时仅保留 `generation > 0` 的压缩后会话）。遇到首条清洗后对话 `>= 65,000` 字符的会话立即选用；未达该阈值时取已扫描候选中字符数最大的一条；本机无可用根会话时自动回退至约 `145K` 字符的合成多块会话。传 `--wav` 时自动在隔离 `sessions/` 下写入对应会话注册 JSON，确保端到端管线同样命中完整上下文。
- 连续 6 轮探针套件：提取最多 2 个安全代码标识符（文件名与符号名），依次执行：
  1. `R1`（`cold_ident`）：冷启动 + 口语打散的会话内标识符还原（观测 `init_ms`、首轮 `refine_ms` 与精确拼写恢复）。
  2. `R2`（`anti_answer`）：热态静止前缀 + 直接反问句（检验前缀缓存命中率跃升，且保留问句不误答）。
  3. `R3`（`self_correct`）：热态 + 口语停顿与自我纠错（检验消解前序口误、仅保留修正后数值）。
  4. `R4`（`delta_append`）：在内存副本末尾模拟主会话新增 1 轮问答（不改磁盘原文件），检验已封口 50K 分块继续字节级命中前缀缓存。
  5. `R5`（`tech_casing`）：中英混排技术词大小写（`JSONL`、`HTTP` 及会话标识符）与祈使指令抗劫持（不把背景日志总结成列表）。
  6. `R6`（`multi_clause`）：多分句长口述首尾完整性（检验前句 `0600`/`零六零零` 与尾句 `结果` 均未丢失）。
- 保真度标记：`PASS` 表示全部通过；异常标记包括 `ANSWERED_QUESTION?`（疑似回答了提问）、`HIJACKED_SUMMARY?`（被指令劫持生成长文或列表）、`MISSING_IDENT(...)`（丢失目标标识符或技术词大小写）、`UNRESOLVED_CORRECTION?`（未消解口语纠错前词）、`DROPPED_CLAUSE?`（输出异常缩水或丢失首尾分句）、`LEAKED_MARKER!`（泄漏 XML/角色标签或代码块）与 `FALLBACK(...)!`（精修降级回退）。

## 场景 2：零打扰全链路真实系统测试（`scripts/e2e_system.py`）

无需人工开口录音，也不启动 `win_shim.ps1` / `mac_shim` 抢占宿主机热键、HUD 或剪贴板，一键验证真机端到端闭环：

```bash
# 自动利用系统内置 TTS 合成并缓存 3 条技术口述 WAV，并回放最新最多 2 条已归档真机录音，跑完整 5 项系统自检
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py

# 控制真机录音回放条数（--saved-limit 0 跳过宿主机回放）、输出脱敏 JSON 或指定外部测试 WAV
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py --saved-limit 2 --json
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py --wav /tmp/sample.wav
```

自检覆盖范围：
- `C1_doctor`：调用 `bin/agent-voice-input doctor` 校验真实配置与凭据就绪且零明文密钥泄漏；若宿主机主守护进程在线，顺带只读记录其 `shim_ok` 状态。
- `C2_serial_e2e_tmux`：在独立 `tmux -L avi-e2e-*` 服务器与无头 `PlatformBackend` 下拉起真实 `VoiceInputDaemon`，驱动 U1 冷启动及 U2+U3 背靠背连续入队（验证串行队列不丢句、保序执行、稳态轮触发 `cached_input_tokens > 0`，并通过 `tmux capture-pane` 读回真实落字核验代码标识符还原、问句保留与口语纠错）。
- `C3_telemetry_privacy_contract`：校验 `status --history` 与 `telemetry.jsonl` 权限严格为 `0600`、分段耗时字段齐全，且全文不含任何语音口述原文（含回放真实录音原文）或明文密钥。
- `C4_fault_recovery_redaction`：注入单次上游故障，验证当次音频以 `0600` 原子暂存至隔离 `failed/`、异常信息中敏感 token 被 `redact_secrets` 脱敏，且 `diagnose_live.py --json` 正常闭环。
- `C5_mp3_archive_and_saved_replay`：在隔离 `XDG_CACHE_HOME` 验证 `audio.save_recordings=true` 真实产出 `0600` `rec-*.mp3` 且能经 `ffmpeg` 还原为有效 `16kHz WAV`；同时检查宿主机 `resolve_saved_recordings_dir()`，若存在真实 `.mp3`/`.m4a`/`.wav` 历史录音则挑最新 `1~2` 条（`--saved-limit`）还原后送入隔离守护进程 + 真 ASR + 真精修 + 隔离 `tmux` 落字验证（仅输出纯数值指标、零原文打印），尚无历史录音时标明 `SKIPPED (0 saved recordings yet)` 并跑通隔离 MP3 往返真链路回放。

## 场景 3：本机运行期慢速与异常只读诊断（`scripts/diagnose_live.py`）

秒级只读诊断、无外网请求：

```bash
# 汇总守护进程健康度、当前终端会话匹配快照、最近 10 条分段耗时与缓存命中率
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/diagnose_live.py --last 10

# 输出脱敏 JSON（供脚本或排障工具消费），或通过核心 CLI 查看数值历史
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/diagnose_live.py --json
agent-voice-input status --history 10
```

判读要点：
- 会话匹配：检查 `control.sock` 在线状态、`pending_count`、`shim_ok` 以及当前终端匹配到的 `<cli>#<hash>` 与字符规模；出现 `NO_SESSION_CTX` 时优先核查终端标题钩子（`integrations/`）与 `runtime_dir/sessions/` 注册表。
- 耗时与缓存归因：对比 `sync_ms`、`asr_ms`、`refine_init_ms`、`refine_ms`、`deliver_ms` 与 `total_ms`。`refine_init_ms` 偏高说明连接预热未在 ASR 期间隐藏；`ctx_chars` 较大但非首轮触发 `CACHE_MISS` 时排查提示词前缀稳定性或上游路由粘性；`REASONING_BLOAT` 时检查 `refiner.effort` 是否被设为中高思考档位；`DELIVER_STALL` 时排查宿主机剪贴板桥接阻塞。
- 失败录音与日志：统计 `runtime_dir/failed/` 暂存 WAV 数量（可提示用 `agent-voice-input retry -l` 恢复）及脱敏 `daemon.log` 尾部；若存在旧版 `codex-home/logs_2.sqlite` 则只读补充计数，不存在时不影响诊断。
