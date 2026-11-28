---
name: manage-hotwords
description: 冷启动梳理、增量迭代、排查误听别名或清理优化 agent-voice-input 个人热词表（hotwords/personal.txt）时加载；日常仅增删一两个词条或常规语音链路开发与诊断不加载。
---

# 个人热词冷启动与增量维护（manage-hotwords）

面向 macOS、WSL2 与 Linux 环境，基于本地真实口述记录与 AI Agent 主会话语料，完成 `agent-voice-input` 个人热词表（默认 `~/.agent-voice-input/hotwords/personal.txt`，受 `AGENT_VOICE_INPUT_HOME` 覆盖）的冷启动建表、增量迭代、误听别名（`<-`）治理与分层清理。日常仅增删一两个词条直接运行 `agent-voice-input hotwords add/remove` 即可，无需启动本流程。

## 安全与隐私红线

- 红线（只读抽取、零正文打 stdout、仓外独占隔离）：语料抽取脚本对所有用户目录严格只读；抽取报告与跨端导出文件必须落入仓外及数据目录外的新建独占 `0700` 目录与 `0600` 文件（拒绝仓内路径、符号链接与既有非空目录，绝不修改既有父目录权限），任务结束后立即删除本次新建目录。`--collect-local` 含脱敏后的私密语料正文，只写仓外 `0600` 文件、严禁把用户发言或 `draft`/`final` 打印到 stdout；严禁将语料、草稿或私密专名写入代码仓库或公共日志。
- 红线（先确认后落盘）：**未获用户明确确认前**，严禁覆写 `personal.txt`。向用户呈现目标路径与 dotfiles 边界核对结果、分层统计、各渠道实际投影边界、相对旧表的增删改摘要以及全部新增或调整的 `<-` 别名与证据，经拍板后再落盘。
- 红线（落盘前零告警门禁）：正式落盘前，必须运行 `scripts/validate_hotwords.py` 调用仓库 `src/` 下的真实 `hotwords.py` 解析器与原始未截断写侧校验器干跑，确认 0 报错、0 解析告警、0 写侧告警（含 0 条 2 字符别名告警，拦截 `>5` 别名、空别名、自指别名及跨词冲突）。

## 语料优先级与双路径

三类语料按信号置信度从高到低依次采用：

1. 归档转录（`<app_home>/transcripts/YYYY-MM.jsonl`，需开启 `[archive].save_transcripts = true`）：仅严格读取 `YYYY-MM.jsonl` 月度文件并整体排除 `refiner_fallback = true` 的降级记录（既不计入词频也不作纠错对），14 字段记录（兼容旧 12 字段）中的 `draft` 与 `final` 直接反映真实口语词频与 ASR 误听对，是最高优先级真源；脚本单点消费 `agent`、`agent_session_id` 等会话字段并兼容旧记录与可选 `project` 字段。
2. 回放评测对照（`${XDG_CACHE_HOME:-~/.cache}/agent-voice-input/eval-runs/`，传 `--include-eval-runs` 纳入）：`asr_cache.json` 与回放样本提供跨渠道及多热词档位敏感度实证（同录音模型差异仅作人工复核线索，不直接等同用户认可纠错）；跨端合并时按音频哈希去重。
3. 本机 AI Agent 主会话（Claude Code、Codex、Antigravity）：当转录归档为空、样本偏少或执行冷启动大修时，复用仓库 `_remote_probe` 提取器扫描主会话纯用户发言，并挖掘助手首段本意对齐回显与用户自纠线索。

按当前热词表成熟度选择对应路径：

- 增量迭代路径：已有可用 `personal.txt` 且 `transcripts/` 有近期积累时，优先传 `--days 14`（或 `30`）并加 `--no-agent-sessions` 聚焦真实 `draft` → `final` 差异，定向增补新高频词、高置信别名、提权 `!` 或淘汰僵尸词。
- 冷启动与定期大修路径：首次建表、切换主力技术栈或长期未清理时，运行 `scripts/extract_corpus.py` 合并全部可用语料做全量梳理。存在双端使用场景（如 macOS 与 WSL2）时，在另一端用 `--collect-local --host-label <远端名> --out-file <仓外0600路径>` 导出含私密语料的本地采集包（stdout 仅输出路径与计数），私密传回主端后用 `--merge-json` 配对相同 `--days` 与范围合并去重，完成后两端均立即清理导出文件。同时提醒用户手动补充编码会话天然覆盖不到的日常通用词（高频人名与称呼、单位/部门/地名、非编码在手专项词、刚立项的新代号）。

## 语料抽取与人工筛词分工

从仓库根目录运行只读抽取脚本（优先导入本仓库 `src/`，缺失某个 Agent 目录时自动跳过；省略 `--output-dir` 时自动在系统临时目录新建独占 `0700` 目录）：

```bash
# 冷启动或全量梳理（近 30 天，自动创建仓外独占 0700 目录）
python3 .agents/skills/manage-hotwords/scripts/extract_corpus.py --days 30 --host-label mac

# 仅基于近期归档转录做快速增量迭代
python3 .agents/skills/manage-hotwords/scripts/extract_corpus.py --days 14 --no-agent-sessions

# 远端机器先在明确仓外路径建独占 0700 目录再导出私密 0600 采集包（stdout 仅输出路径与计数，不含正文）
REMOTE_DIR="$(mktemp -d /tmp/avi-hw-remote.XXXXXX)" && chmod 700 "$REMOTE_DIR"
python3 .agents/skills/manage-hotwords/scripts/extract_corpus.py \
  --days 90 --include-eval-runs --host-label wsl2 \
  --exclude-project sandbox-demo \
  --archive-project legacy-monorepo \
  --collect-local --out-file "$REMOTE_DIR/avi-remote-wsl2.json"

# 主端先在明确仓外路径建本地独占 0700 目录存放传入的远端包再合并（需对齐 --days、窗口与全部筛选参数，缺文件、范围不符或损坏直接非零报错）
MERGE_DIR="$(mktemp -d /tmp/avi-hw-merge.XXXXXX)" && chmod 700 "$MERGE_DIR"
# 将远端包安全传入 "$MERGE_DIR/avi-remote-wsl2.json"（0600）后执行：
python3 .agents/skills/manage-hotwords/scripts/extract_corpus.py \
  --days 90 --include-eval-runs --host-label mac \
  --exclude-project sandbox-demo \
  --archive-project legacy-monorepo \
  --merge-json "$MERGE_DIR/avi-remote-wsl2.json"
```

脚本内置与人工复核的分工边界如下：

- 三端主会话精准过滤（脚本内置）：
  - Codex：按文件修改时间预筛、逐行流式读取并按事件时间戳分桶（保留长会话近期发言）；按仓库 `session_ctx.py` 优先级消费 `item_completed`（`UserMessage`/`AgentMessage`）与 `response_item`；仅在首条非注入用户消息命中 `codex-run` 或自动化特征时踢除整会话，不因后续轮次提及关键字误杀人类会话。
  - Antigravity：只读查询 `conversation_summaries.db`，严格要求 `nesting_depth = 0`、`parent_conversation_id` 与 `agent_name` 为空且非自动化工作区；DB 缺失或不可读时直接跳过并报告覆盖缺口（不盲扫 `brain/*`）；事件经 `_clean_agy_record` 核对 `source == USER_EXPLICIT` 并按 `step_index` 恢复 `transcript_full.jsonl` 截断正文。
  - Claude Code：跳过 `subagents/`、`agent-*`、`isCompactSummary`，复用 `_clean_claude_record` 过滤 `isSidechain`/`agentId`/`agent_id`/`entrypoint == "sdk-cli"`/`isMeta`/`isApiErrorMessage`，并对 `projects/*.jsonl` 与 `history.jsonl` 做内容指纹去重。
- 清洗、加权与历史降权（脚本内置）：剥离 `[Pasted text]`、`[Image]`、fenced code block、终端日志、diff、堆栈、URL、绝对路径与 UUID/Hex，并复用仓库 `config.redact_secrets` 脱敏凭据；时间分桶权重为归档转录 `1.3` > 评测回放 `1.2` > 近 30 天主会话 `1.0` > 更早会话 `0.6`。`--archive-project` 仅将命中项目超过近 30 天窗口的历史发言降权为 `0.08`（近 30 天发言仍保持正常权重），并在专项报告中分别标出近期与历史条数。
- 候选筛纯（脚本统计 + 人工复核）：脚本负责英文大小写归并、双词专名统计、基础停用词过滤、`BUILTIN_AGENT_HOTWORDS` 命中标记，以及中文 PMI、左右邻接熵与子串裁剪；英文单复数合并、消费品牌取舍、高频日常词与易听对常见词剔除、以及是否补录命中的内置词覆盖 `general` 模式盲区，均由人工/Agent 在候选复核阶段结合语境裁决。

## 分层排版与配额参考

不按最窄 ASR 渠道人为砍总词数（`personal.txt` 上限 `256 KiB` / `2000` 条）。`hotwords.py` 在 Stage 1 与 Stage 2 均按「`!` 置顶 → 有效 `<-` 别名 → 普通词原文件从上到下顺序」保序截断，并会在字符超限时跳过放不下的词继续回填后续短词。排版推荐三层所见即所得结构（具体数量随主力渠道与高价值词密度调整，最终以 `validate_hotwords.py` 实测投影为准）：

1. `!` 置顶核心层（参考区间约 20–45 条）：放最核心、最高痛度术语。若使用 `whisper`（35 词 / 353 字符，`agent` 模式预留 30% 动态槽位且 Pass 1 个人字符上限 70%），把最核心词尽量排在最前并控制字符紧凑；以校验报告中各渠道 `general` 与 `agent+20dyn` 的实际 `pinned_kept`/`pinned_dropped` 为准。
2. 专项安全 `<-` 别名层：紧跟置顶层，使文件视觉顺序与解析生效顺序一致；与置顶层合计在 `doubao`、`openai(keywords)`、`openrouter(keyterms)` 的 `general` 模式 50 词配额内尽量容纳高价值词。
3. 高到中频普通词层：按活跃度与重要性从上到下分组（例如工程核心 → AI 与工具链 → 近期活跃项目 → 中文专名 → 扩展技术栈 → 非编码专项 → 历史项目），使高频词依次覆盖 `single_hop` / Stage 2（200 词 / 3000 字符）与 `dashscope`（以 `validate_hotwords.py` 实测投影为准）。分组注释统一用 `# === 分组名 ===`，不用已废弃的 `[section]` 方括号头。

## 误听别名（`<-`）零碰撞准则

`<-` 别名只从真实语料证据（归档转录 `draft` → `final`、`eval-runs` 差异、助手回显或用户自纠）提炼，不凭空臆造，并逐条核验：

- 红线（字面零碰撞）：绝不用正常高频中英文词、常规动词/形容词或正规术语作别名（如 `Timeless`、`SUMMIT`、`户口`、`信用`、`你的`），否则在 `general` 聊天或跨语境下会诱发 Stage 2 误纠；高频但常被听成常用词的术语，改为 `!` 置顶靠 Stage 1 声学偏置压制。
- 红线（真实软件与竞品隔离）：绝不把真实模型名、软件名、开源工具或平行平台互配为别名（如 `Llama`、`ToDesk`、`GitLab Pages`）。
- 红线（原语保真）：中文原生称呼与英文专名各自独立收录（如 `Antigravity` 与 `反重力`、`Qwen` 与 `千问` 分别单列），不互配别名，避免 Stage 2 翻译用户原话。
- 形态去重与清杂音：解析器大小写不敏感，不重复写大小写等价别名（如同时写 `CodeX` 与 `codex`），并剥离误听片段前的口语语气词。
- 红线（写侧零告警）：单条最多 5 个别名、单别名 2–24 字符、不得为空或与自身规范词相同、且绝不与任何规范词或其他词条的别名冲突；候选表默认只留 `>= 3` 字符的高信息量别名以避开 2 字符写侧告警（高危 2 字符误听优先改为 `!` 置顶）。

## 校验、确认与安全落盘收尾

1. 核对目标路径与 dotfiles 边界：先运行 `agent-voice-input hotwords path`（或 `--json`）确认 `personal.txt` 真实路径以及 `hotwords/` 是否软链到受版本管理的 dotfiles 仓库；若目录受版本管理，提醒用户核实私密人名/项目名的入库边界；注意 `personal.txt` 自身不得为符号链接。同时记录当前旧表内容快照（供落盘前并发变更检测）。
2. 真解析器干跑门禁：将候选稿暂存本次仓外独占目录的 `0600` 文件，运行校验脚本并确认退出码为 0：

```bash
python3 .agents/skills/manage-hotwords/scripts/validate_hotwords.py <out_dir>/personal_candidate.txt
```

3. 交用户拍板：汇总输出目标路径与版本管理提示、词表分层计数、各主流渠道（`doubao`、`openai(keywords)`、`openrouter(keyterms)`、`whisper`、`dashscope`、`single_hop`）在 `general` / `agent+20dyn` 模式下的实际投影边界与置顶词保留/跳过情况、Stage 2 预算占位、相对现有 `personal.txt` 的增删改摘要，以及全部 `<-` 别名与对应实证来源；提醒用户补充生活与办公专名。
4. 目录锁原子落盘与零告警复核：
   - 小幅增量优先直接走 `agent-voice-input hotwords add/remove`。
   - 全量重写时，获用户明确确认后复用仓库 `agent_voice_input.hotwords` 的目录锁与原子写原语（`_hotwords_edit_lock` + `_inspect_writable_target` + 锁内对比当前磁盘文件与起草前旧表快照，若期间有其他终端改动则中止并重新展示差异 + `_atomic_write_bytes_0600` 原子替换），避免裸 `cp` 覆写并发改动或破坏 `0600` 权限与软链保护。
   - 落盘后运行 `agent-voice-input hotwords list --json`（或 `hotwords list`）核实生效条目且 **`diagnostics.warnings_count == 0`**（`hotwords list` 存在告警时仍会退出 0，不可只看退出码）。热词表通过 stat 缓存热加载，落盘即生效，无需重启守护进程。
   - 最后删除本次新建的仓外临时抽取与草稿目录（以及跨端场景下两端的 `--collect-local` 导出文件）。
