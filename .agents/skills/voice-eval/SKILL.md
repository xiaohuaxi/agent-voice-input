---
name: voice-eval
description: 在本机真实长会话或合成样本上实测语音输入精修器（Refiner）、运行零打扰全链路系统自检与双端路由回归矩阵、按历史会话快照做存量录音 A/B 回放评测，或只读诊断本机守护进程近期口述变慢、降级与缓存未命中问题。改动 ASR、精修提示词、上下文分块、缓存复用、会话路由、桌面端绑定、渠道实现或守护进程投递管线需做实机基准、双端回归或桌面 GUI 端到端验收，或需对比历史录音回放、排查实机口述延迟与润色异常时加载；日常纯离线单元测试、热键或 HUD 界面样式调整及常规文档修改不加载。
---

# 语音链路实测、全链路自检与线上诊断（voice-eval）

覆盖五类本机实证验证与诊断任务：
1. 真实长会话精修基准（`scripts/bench_refiner.py`）：改动 Stage 2 精修器、提示词、`<session_reference_log>` 分块或前缀缓存机制后，用本机真实长会话（或 `--synthetic` 合成会话）跑连续多轮冷/热基准。
2. 零打扰全链路系统自检（`scripts/e2e_system.py`）：触及音频管线、守护进程排队取消或终端落字改动后，在不触碰桌面热键、HUD、剪贴板与主守护进程的前提下，自动跑通「真实人声 WAV → 真 ASR → 真多块上下文 → 真无状态精修 → 真 `VoiceInputDaemon` 连续入队与按序落字 → 隔离 `tmux` 粘贴读回 → 遥测与故障恢复」全链路。
3. 本机运行期只读诊断（`scripts/diagnose_live.py`）：用户反馈近期口述变慢、缓存未命中、专有名词未修对或偶发降级时，本地只读汇总分段耗时、缓存命中与会话匹配线索。
4. 存量真实录音按时间点 A/B 回放评测（`scripts/eval_recordings.py`）：将已归档录音对齐当时的历史会话快照（`--manifest-from` 锁定同批样本、`--asr-from` 冻结 Stage 1 初稿以隔离转写波动纯测 Stage 2、`--compare` 生成配对对照），结果与逐条 diff 以 `0600` 写入 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/eval-runs/`（或 `--eval-dir`）。
5. 双端对称路由回归与真机桌面端验收（`scripts/regress_matrix.py`）：跨 macOS 与 WSL2 统一验证离线全路由矩阵（Layer A，默认启用、零费用）、本机运行中守护进程 `control.sock` 探活与可选实机落字（Layer B，`--live-daemon` / `--deliver-live`），以及本机已装 CLI 与桌面端存储只读兼容性抽检（Layer C，`--probe-real-clis`）。

运行前提：调用在线渠道的步骤需对应云端凭据与网络（`bench_refiner.py` 默认仅调精修渠道，附加 `--wav` 时串跑 ASR；`e2e_system.py` 需所选 ASR 渠道凭据，启用独立 Stage 2 精修时还需对应精修凭据，并依赖本地 `ffmpeg`、`tmux`，纯非 WSL Linux 无系统内置 TTS 时传 `--wav` 指定测试音频；`eval_recordings.py` 传 `--asr-from` 可免重跑 ASR，传 `--compare` 无需联网或云端凭据但会在评测目录写入或覆盖 `0600` 的 `diff_*.md`；`regress_matrix.py` 的 Layer A 与 Layer C 纯本地零联网零费用，Layer B 需本机守护进程在线，且未设 `--skip-selftest-wav` 时需云端凭据与本地 TTS 或 `--wav`；`diagnose_live.py` 纯本地只读、无需联网或云端凭据）。当前环境缺少所需凭据或宿主工具时，先完成可运行的离线检查并在交付说明中注明未验环节。

## 安全与隐私红线

- 红线：调用 `CodexRefiner` 或任何 `codex` 子进程做实测时，严禁通过参数、配置或环境变量开启 Fast 模式。
- 红线：严禁将任何真实会话内容、语音样本、`telemetry.jsonl` / `daemon.log` 运行流水、宿主机隐私路径或未脱敏凭据写入版本库。
- 本地输出脱敏与隔离边界：终端报告与仓外文件统一经 `redact_secrets` 过滤密钥，但不同产物的正文与路径留存边界不同。`diagnose_live.py` 不输出任何会话或口述正文（结果记录将 `saved_audio` 转为布尔字段 `has_saved_audio` 隐去失败录音路径，但脱敏后的 `daemon.log` 尾部及异常文本仍可能保留本机路径）；`e2e_system.py` 对宿主机 `recordings/` 真实历史录音回放仅报告音频时长、分段耗时、token/缓存命中数与落字字符长度，不打印真实用户口述原文或隐私路径；`bench_refiner.py` 仅对合成测试句及最多 2 个从会话提取的安全代码标识符展示精修对照（超长异常输出自动截断）；`eval_recordings.py` 终端仅打印聚合指标，但写入仓外私有评测目录（默认 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/eval-runs/`，目录权限 `0700`）的 `0600` 文件 `<tag>.json` 与 `diff_*.md` 保留过滤密钥后的识别初稿、精修正文与历史参考文本供本机复盘，不可当作无口述正文的公开报告外发。`bench_refiner.py`、`e2e_system.py` 与 `regress_matrix.py` Layer A 全程在临时 `0700` `runtime_dir`（及隔离 `HOME` 或 `XDG_CACHE_HOME`）运行并自动清理；`regress_matrix.py` 默认零对话正文输出（仅传 `--show-text` 时在本地终端回显合成测试句结果），`e2e_system.py` 与 `regress_matrix.py` 复用的合成测试 WAV 仅缓存于仓外 `~/.cache/agent-voice-input/eval-wavs/`（权限 `0700`）。

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
- 会话选样：默认按最近修改时间扫描 `~/.gemini/antigravity-cli/brain/`、`~/.codex/sessions/`、`~/.claude/projects/` 中的非 subagent 根会话（要求清洗后至少 2 轮对话；传 `--prefer-compacted` 时仅保留 `generation > 0` 的压缩后会话），优先选用清洗后对话 `>= 65,000` 字符（跨越 `50K` 分块边界）的长会话，未达该阈值时取已扫描候选中字符数最大的一条；本机无可用根会话时自动回退至约 `145K` 字符的合成多块会话。传 `--wav` 时自动在隔离 `sessions/` 下写入对应会话注册 JSON，确保音频链路命中同一上下文。
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
# 自动利用系统内置 TTS 合成并缓存技术口述 WAV，并回放最新最多 2 条已归档真机录音，跑完整 7 项系统自检
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py

# 控制真机录音回放条数（--saved-limit 0 跳过宿主机回放）、输出脱敏 JSON 或指定外部测试 WAV
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py --saved-limit 2 --json
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/e2e_system.py --wav /tmp/sample.wav
```

自检覆盖范围：
- `C1_doctor`：调用 `bin/agent-voice-input doctor` 与 `doctor --ping` 校验真实配置、凭据就绪与线上连通探活通过，且输出零明文密钥泄漏；若宿主机主守护进程在线，顺带只读记录其 `shim_ok` 状态。
- `C2_serial_e2e_tmux`：在独立 `tmux -L avi-e2e-*` 服务器与无头 `PlatformBackend` 下拉起真实 `VoiceInputDaemon`，驱动 U1 冷启动及 U2、U3 背靠背连续入队，通过 `tmux capture-pane` 读回真实落字核验不丢句、按序落字，以及默认探针的代码标识符还原、问句保留与口语纠错（预期执行 Stage 2 精修时，校验稳态轮触发 `cached_input_tokens > 0`）。
- `C3_telemetry_privacy_contract`：校验 `telemetry.jsonl` 为 `0600` 普通文件且成功记录的分段耗时字段齐全，核对磁盘记录与 `status --history` 输出条数一致，且两者均不含任何测试口述、回放原文或明文密钥。
- `C4_fault_recovery_redaction`：注入单次上游故障，校验当次音频以 `0600` 暂存至隔离 `failed/`、失败记录与异常摘要经 `redact_secrets` 脱敏，且 `diagnose_live.py --json` 报告 `has_saved_audio` 正常闭环。
- `C5_mp3_archive_and_saved_replay`：在隔离 `XDG_CACHE_HOME` 验证 `audio.save_recordings=true` 真实产出 `0600` `64 kbps` `rec-*.mp3` 且能经 `ffmpeg` 还原为有效 `16kHz WAV`；同时检查宿主机 `resolve_saved_recordings_dir()`，若存在真实 `.mp3`/`.m4a`/`.wav` 历史录音则按 `--saved-limit`（默认最多 2 条，`0` 跳过宿主录音）选取最新录音还原后送入隔离守护进程 + 真 ASR + 真精修 + 隔离 `tmux` 落字验证（仅输出纯数值指标、零原文打印），尚无历史录音时标明 `SKIPPED (0 saved recordings yet)` 并跑通隔离 MP3 往返真链路回放。
- `C6_non_git_and_ssh_isolation`：在隔离临时目录创建不含 `.git` 的普通文件夹（含 `budget_q4_plan.xlsx` / `custom_widget_spec.md`）并绑定隔离会话连续跑通两轮语音精修，验证非 Git 浅层文件名提取、热词还原与增量多轮上下文命中；随后在本地双会话在线场景下，向无头后端分别模拟终端 SSH 窗口标题与专用客户端（`putty.exe`）前台信息，验证触发 Phase 0 远端防串台硬否决（零本地上下文混入）。
- `C7_native_hud_self_check`：在 macOS 执行 `mac_shim --self-check`、在 WSL2 执行 `win_shim.ps1 -Mode self-check`（纯非 WSL Linux 自动跳过），零屏幕/剪贴板打扰地离屏校验原生 HUD 胶囊像素布局用例（`hud_layout_cases > 0`）与桥接自检状态。

## 场景 3：本机运行期慢速与异常只读诊断（`scripts/diagnose_live.py`）

从仓库根目录运行，纯本地只读诊断、无外网请求：

```bash
# 汇总守护进程健康度、当前终端会话匹配快照、最近 10 条分段耗时与缓存命中率
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/diagnose_live.py --last 10

# 输出脱敏 JSON（供脚本或排障工具消费），或通过核心 CLI 查看数值历史
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/diagnose_live.py --json
agent-voice-input status --history 10
```

判读要点：
- 会话匹配：检查 `control.sock` 在线状态、`pending_count`、`shim_ok` 以及当前终端匹配到的 `<cli>#<hash>` 与字符规模；出现 `NO_SESSION_CTX` 时优先核查终端标题钩子（`integrations/`）与 `runtime_dir/sessions/` 注册表。
- 耗时与缓存线索：先对比 `sync_ms`、`asr_ms`、`refine_init_ms`、`refine_ms`、`deliver_ms` 与 `total_ms`。`refine_init_ms` 偏高时核对连接预热与 ASR 的重叠情况；`ctx_chars` 较大但非首轮出现 `CACHE_MISS` 时排查提示词前缀稳定性或上游路由粘性；`REASONING_BLOAT` 可结合 `refiner.effort` 思考档位排查；`DELIVER_STALL` 可沿当次实际剪贴板/终端桥接路径排查。
- 失败录音与日志：统计 `runtime_dir/failed/` 暂存 WAV 数量及脱敏 `daemon.log` 尾部（`agent-voice-input retry -l` 列出可恢复录音，`agent-voice-input retry [序号]` 恢复，省略参数默认重试最新一条）；若存在旧版 `codex-home/logs_2.sqlite` 则只读补充计数，缺失不影响诊断。

## 场景 4：存量真实录音按时间点 A/B 回放评测（`scripts/eval_recordings.py`）

基于 `${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/recordings/` 已归档录音重建录音时刻的历史会话快照做配对对比（`<tag>.json` 按标签名覆盖写入，建议使用带实验标识的唯一 `--tag` 或独立 `--eval-dir`）：

```bash
# 1) 用两阶段配置跑基线标签（--limit 按文件修改时间从早到晚取前 N 条通过门控的录音，0 为全量）
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/eval_recordings.py --tag base_v1 --limit 20

# 2) 锁定基线样本集合并冻结 Stage 1 初稿，切换候选精修配置（若基线为单跳 ASR 需加 --force-single-hop-refine 才会执行候选 Stage 2）
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/eval_recordings.py \
  --tag cand_v1 --manifest-from base_v1 --asr-from base_v1 --refiner-provider gemini

# 3) 纯本地读取两次结果生成配对对比表与 0600 逐条 diff（注意核对输出头部的 paired 样本数）
PYTHONPATH=src python3 .agents/skills/voice-eval/scripts/eval_recordings.py --compare base_v1 cand_v1
```

## 场景 5：双端对称路由回归与真机验收（`scripts/regress_matrix.py`）

macOS 与 WSL2 共用同一入口，全报告经 `redact_secrets` 脱敏、默认零正文输出：

```bash
# 默认零费用回归：Layer A 离线全路由矩阵 + Layer C 本机已装 CLI 与桌面端 DB 只读抽检
python3 .agents/skills/voice-eval/scripts/regress_matrix.py --probe-real-clis

# 叠加 Layer B：直连本机运行中守护进程 control.sock（status 漂移检查 + probe_route + no_telemetry selftest_wav）
python3 .agents/skills/voice-eval/scripts/regress_matrix.py --probe-real-clis --live-daemon --json

# 仅跑 Layer B 轻量探活（跳过云端 selftest_wav，允许旧版本代码漂移降级为 WARN）
python3 .agents/skills/voice-eval/scripts/regress_matrix.py --no-matrix --live-daemon --skip-selftest-wav --allow-drift

# 显式开启桌面 GUI 实机落字验收（默认关闭；等待 2s 后向当前前台窗口真实投递测试 WAV）
python3 .agents/skills/voice-eval/scripts/regress_matrix.py --no-matrix --live-daemon --deliver-live --delay-sec 2 --json
```

三层职责分工：
- Layer A（默认启用，可用 `--no-matrix` 跳过）：在隔离 `0700` 临时 `HOME` 与 `runtime_dir` 拉起真实无头 `VoiceInputDaemon`，经真实 `control.sock` 的 `probe_route` 与仓库 `bin/agent-voice-input session-hook --cli claude|agy --quiet` 验证 Codex / Claude Code（含 `2.1.330` 超大 `attachment` 字节预过滤）/ AGY 新旧会话与跨 CLI `prior_session` 继承、空 shell 零串台，以及同时覆盖 macOS 与 Windows 窗口特征的 Antigravity 2.0 WAL DB、ChatGPT/Codex 统一桌面端本地仓升格与云端否决、Claude 桌面版 / 微信 / 浏览器（`general_gui`）、VS Code / Cursor（`coding_gui`）与 SSH 远端防串台（`putty.exe`、`[@remote]`、`[SSH: devbox]`）。
- Layer B（`--live-daemon`，可选 `--runtime-dir`、`--wav`、`--allow-drift`、`--skip-selftest-wav`、`--deliver-live`、`--delay-sec`、`--show-text`）：不重启也不干扰主守护进程，校验 `status`（`ok`、`shim_ok`、`code_drift`）、虚拟与真机焦点 `probe_route` 以及 `selftest_wav(no_telemetry=True)`（核对 `telemetry.jsonl` 零行数增长与 `asr_ms`/`refine_ms`/`reasoning_tokens`/`routing`）。
- Layer C（`--probe-real-clis`）：纯只读抽检本机 `~/.codex/sessions`、`~/.claude/projects`、`~/.gemini/antigravity-cli/brain` 最新各 1 条非 subagent 根会话解析兼容性，并以 `mode=ro` 校验 `~/.gemini/antigravity/conversation_summaries.db` 与 `~/.codex/sqlite/codex.db` 表结构与可查性（未安装对应工具时标 `SKIP`，仅输出纯数值）。

## 桌面 GUI 真·端到端验收清单（Computer Use / 人工共用）

1. 安全与审批前置：若由支持桌面 Computer Use 的 Agent 执行，先遵守对应平台的本地输入保护与审批规范，不尝试操控被安全禁止的终端应用；遇到系统权限或审批弹窗一律交由人工确认。
2. 先完结非 GUI 检查：先运行 `python3 .agents/skills/voice-eval/scripts/regress_matrix.py --probe-real-clis --live-daemon`（默认 dry-run，不触碰桌面剪贴板与输入框），确认离线矩阵、本机存储兼容、主守护进程 `shim_ok`、真机焦点采集及无遥测污染的云端链路全部通过。
3. 真·端到端桌面落字验收：将目标桌面 App（如 Antigravity 2.0、ChatGPT 本地仓库任务与普通云端聊天、Claude 桌面版）切到前台并点进输入框，执行 `python3 .agents/skills/voice-eval/scripts/regress_matrix.py --no-matrix --live-daemon --deliver-live --delay-sec 2 --json`（本地终端核对合成句落字时可附加 `--show-text`），逐项核对输入框实际落字与返回 JSON 中的 `routing.mode`、`routing.session_key_masked`、`pasted` 与 `reasoning_tokens == 0`。
4. 清理与收尾：纯落字验证不按回车，核对完成后清空输入框中的测试文字；若需验证多轮上下文增长，仅在专用测试会话中发送极短消息（如“回复 OK”），避免污染日常会话。
