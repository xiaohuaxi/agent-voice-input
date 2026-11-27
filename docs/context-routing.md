# 上下文路由与防串台机制（Context Routing）

按下热键说话时，`agent-voice-input` 需要在不打断输入焦点的前提下回答三个问题：**当前是什么应用场景、对应哪个工作目录、该读取哪条 Agent 会话历史**。本文档说明本地终端、桌面 GUI 与 SSH 远端 Linux 三类场景的真实匹配优先级与防串台隔离边界。

---

## 全局匹配总表

| 环境类型 | 焦点与工作目录怎么拿 | Session 怎么定 | 是否需要装钩子 / 配白名单 |
| :--- | :--- | :--- | :--- |
| **本地终端 CLI Agent**<br>（WSL2 Windows Terminal、macOS Ghostty / Terminal.app / iTerm2 等） | **Windows/WSL2**：`win_shim.ps1` 读取活动窗口进程与标题；<br>**macOS**：Ghostty 读分屏 `tty`/`cwd`/`pid`，Terminal.app 读选中标签页 `tty`，iTerm2 零弹窗预检后读 `tty` 或回退窗口子进程 PTY 树 | **第一档**：标题 `[#id8]` / `[§...]` / Codex UUID 优先查本地 `sessions/` 注册表或前缀直连；<br>**第二档**：有焦点 `tty`/`cwd` 或带路径/项目名的标题时按焦点与目录收敛（精确目录优先于非宽泛父子目录；标题与所有候选的路径、末级目录名、CLI 名及线程名均不匹配时返回空，无焦点 `tty` 下同名末级目录仍会通过目录名保留）；无焦点 `tty`/`cwd` 且标题为通用终端名（`Ubuntu`/`bash`）或仅含同类 CLI 名/动画符时，按 `max(pts_mtime, jsonl_mtime)` 选最近活跃会话 | **免配置开箱可用**；WSL2 多窗口跨项目（含同名子目录）并发时推荐挂 `session-hook` |
| **本地 `tmux` 多分屏**<br>（WSL2 / macOS） | 自动查询当前 attached 客户端的活跃窗格（`pane_id`、`pane_pid`、`pane_tty`、`pane_current_path`、`pane_title`） | 优先匹配焦点窗格 `#{pane_title}` 指纹或注册表 `tmux_pane`；未挂钩子时将进程扫描严格限制在该焦点窗格后代进程树内，硬隔离相邻分屏与空 shell 分屏 | **零配置**（无需改 `~/.tmux.conf`） |
| **桌面代码编辑器 / IDE**<br>（VS Code、Cursor、Windsurf、Antigravity IDE、Zed、JetBrains、Xcode、Sublime） | 硬跳过终端 `sync_session`；通过 macOS `AXDocument`（`file://`）或窗口标题项目名/显式路径非递归定位本地目录（Git 仓或非 Git 普通目录） | 注入当前工作区 `git status` 或浅层文件名作热词与拼写参考；**Antigravity 2.0 独立桌面版**优先只读 `conversation_summaries.db` 按窗口标题精确/截断前缀直连 `brain/<cid>` 主会话并经结构化 URI 解析绑定本地工作区（老版 VS Code Fork 回退三层路径证据扫描） | **零配置** |
| **日常桌面 GUI 应用**<br>（浏览器、IM、桌面聊天、邮件、文档等） | 识别为 `general_gui` 应用；默认硬跳过终端会话与代码工作区扫描 | 默认不绑定代码 Session，切入 `general` 通用润色画像（仅挂载个人热词，零代码上下文串台）；**ChatGPT / Codex 统一桌面端**（只读 `~/.codex/sqlite/codex.db` / `state_5.sqlite`）与 **Claude Desktop**（经 `mac_shim` 有界只读 `AXWebArea.AXTitle` 并联合扫描 `Claude` / `Claude-3p` 的 Code 与 Cowork 本地会话）例外——命中本地有效会话后动态升格 `agent` 模式，普通云端聊天保持 `general` 模式零串台 | **零配置** |
| **远程桌面 / 虚拟机客户端**<br>（`mstsc`、`Windows App`、Parsec、RustDesk、VMware 等） | `hotkey.yield_remote_desktop = true` 在触发入口直接识别前台远程桌面进程（豁免 Win11 WSLg `msrdc.exe`） | 本地静默让行新热键触发，把按键原样留给远端桌面处理 | **零配置** |
| **SSH 远端 Linux 主机**<br>（Windows `ssh.exe`、PuTTY / MobaXterm / WindTerm、WSL2 / macOS `ssh`、远端 `tmux`、Remote-SSH IDE） | **Phase 0 远程否决闸门**识别 SSH 客户端进程、标题 `[@host]` / `user@host:cwd` / `[SSH: host]` 或本地 TTY 前台 `ssh` 进程，第一时间阻断本地后台会话串台 | 未配白名单时干净直出、零本地泄漏；配 `[remote] hosts` 后按下热键后台通过 `ssh -- <target> python3 -` 只读预取远端会话（ETag 短路 + 正向 `50K` 整块对齐裁剪） | **防串台零配置**；拉取远端上下文需在本地配一行 `[remote] hosts = [...]`（远端零安装） |

---

## 一、本地终端 CLI Agent（WSL2、macOS 终端与本地 `tmux`）

本地终端运行 **Codex CLI**、**Claude Code** 或 **Antigravity CLI（`agy`）** 时，解析器按“精确指纹优先、焦点/目录收敛兜底”分两档匹配，并自动过滤后台 subagent（如 Claude `isSidechain` / `agentId`、AGY 首行 `[Message]` / `<subagent_reminder>` / `[agy-run]`）与无头调用（`-p`、`--print`、`exec`、`app-server`、`mcp`、`acp`）。

### 1.1 第一档：标题指纹与本地会话注册表直连

当终端窗口标题（或 `tmux` 活跃窗格的 `#{pane_title}`）带有会话标识时，优先按指纹精确匹配：

- **识别格式**：支持显式前缀 `[claude:#a1b2c3d4]` / `[agy:#a1b2c3d4]` / `[codex:#a1b2c3d4]`、短哈希标签 `[#a1b2c3d4]` / `[§a1b2c3]`（6–36 位十六进制或完整 UUID），以及 Codex CLI 开启 `terminal_title = [..., "thread-id"]` 后输出的完整 UUID。
- **注册表直查与存活核验**：Claude Code 与 `agy` 的轻量钩子（`agent-voice-input session-hook`）会将根会话的 `session_id`、`jsonl_path`、`cwd`、控制终端 `tty`、`tmux_pane` 及可识别到的父 CLI `agent_pid` 原子写入 `<runtime_dir>/sessions/<cli>-<id8>.json`（`0700` 目录 + `0600` 文件，读取侧强制 `O_NOFOLLOW` 与 UID 校验）：
  - **8 位及以上标签**直接按 `<cli>-<id8>.json` O(1) 定位注册表文件（若记录了 `agent_pid > 0` 则实时校验该进程仍存活且类型一致，`agy` 同时核验 `presence/<cid>.lock` 文件锁）；
  - **6–7 位短标签**（如 `[§abcdef]`）或未配钩子仅有标题 UUID/前缀时，自动回退匹配存活 `/proc` 根会话与会话存储目录，并在真实系统环境下核验对应 CLI 会话仍在线，防止命中已退出的历史残留。
- **WSL2 不用 `tmux` 也能精确直连**：在 Windows Terminal（或其他 Windows 终端）直接开启多个 WSL2 标签页/窗口而不使用 `tmux` 时，常驻的 `win_shim.ps1` 会直接读取当前焦点窗口标题。只要接入 `session-hook` 或 Codex `thread-id` 标题（详见 [`integrations/`](../integrations/README.zh-CN.md)），无需套 `tmux` 即可直接走第一档命中。

### 1.2 第二档：零钩子启发式定位与目录消歧

未配置任何标题钩子时，守护进程按当前焦点终端的进程树、窗口标题与工作目录自动收敛：

1. **焦点终端与 `tmux` 窗格锁定**：
   - **macOS 终端**：`Ghostty` 直接通过 AppleScript 获取当前焦点分屏（`focused terminal`）的 `tty`、`cwd`、`pid` 与 `surface_id`；系统 `Terminal.app` 读取当前选中标签页 `tty`；`iTerm2` 先经 `AEDeterminePermissionToAutomateTarget` 做零弹窗 TCC 预检——已授权则直接读取当前 session `tty`，未授权则静默回退到窗口子进程 PTY 树（在首个控制终端层截断，排除 Agent 内部工具子 PTY），全程不会弹出授权框打断口述。
   - **本地 `tmux`**：无需改动 `~/.tmux.conf`，自动定位当前 attached 客户端的焦点窗格（多客户端并存时综合外层 `window_tty`/`window_pid`、标题标记与客户端活跃度消歧），仅在该窗格的后代进程树内查找活跃根会话。切到空 shell 分屏或隔壁跑着后台任务时，绝不抓错隔壁窗格的历史。
   - **WSL2 / Linux `/proc` 扫描**：扫描非 `tmux` 的存活交互式 CLI 根进程（Codex 核对打开的 `rollout-*.jsonl` 首行 `source=="cli"`，`agy` 核对持有的 `presence/<cid>.lock`，Claude 核对工作目录与未被其他存活 PID 认领的根 JSONL），再结合窗口标题做亲和过滤。
2. **Git 仓库与非 Git 普通目录的词汇提取**：
   - **Git 仓库**：只读执行 `git status --short` 提取变更文件名与词干；当工作树干净时，自动回退提取近期提交与核心源码目录下的追踪文件名（`repo_files`，缓存 `300s`）。
   - **非 Git 普通文件夹**：面向文稿写作、数据分析或未初始化 Git 的普通目录，自动执行有界浅层扫描（`os.scandir` 仅扫根目录及 1 层子目录，最多检查 `200` 个条目、提取前 `20` 个常见文档与代码文件名，缓存 `300s`）。扫描前强制拦截 `~`、`/`、`/tmp`、`/var`、`/Users`、`/home`、`/mnt`（以及 WSL2 下任何指向 `/mnt/` 慢速挂载盘的路径或软链）等宽泛系统与隐私目录。
3. **多会话消歧边界（有目录线索严防串台、无 `tty` 纯通用/同名 CLI 标题按活跃时间兜底）**：
   - **已知焦点 `tty`/`cwd` 或带目录/任务线索的标题**：目录匹配按“精确同目录（`rank=2`）优先于非宽泛父子子目录（`rank=1`，已排除 `~`/`/home` 等宽泛根目录）”分级，再结合标题中的 CLI 名、末级目录名与 Codex `thread_name` 打分；当窗口标题与所有候选的完整路径、末级目录名（非 `~` 时）、CLI 名及 `thread_name` 均无交集，或明确为 Windows 原生 `cmd` / `PowerShell` 时，直接返回空（注：无焦点 `tty`/`cwd` 时，不同父路径下的同名末级目录如 `/work/a/app` 与 `/work/b/app` 仍会通过末级目录名亲和保留并按活跃时间决胜）；
   - **无焦点 `tty`/`cwd` 且标题缺少目录区分度**（如 WSL2 默认通用标题 `Ubuntu`、`bash`、`zsh`，或仅含裸 CLI 名/TUI 动画符如 `codex`、`✳ ...` 而本机同时跑着多个同类会话）：由于宿主机未提供窗口 `tty` 或目录线索，解析器会在通过标题过滤的存活交互式候选中按终端/日志最新活跃时间（`max(pts_mtime, jsonl_mtime)`）选取刚交互过的会话（且含 CLI 名或动画符的标题会优先视为本地 CLI 窗口，跳过无焦点后台 SSH 活动探测）。若常在多个不同仓库同时开无 `tmux` 的独立 WSL2 窗口，配置 `session-hook` 即可升级为第一档精确隔离。

4. **冷启动回退（`refiner.cold_start_fallback = "auto" | "hotwords" | "off"`，默认 `"auto"`）**：
   - **Layer 0（终端无会话兜底）**：当终端 `sync_session` 未命中活跃会话（如刚启动尚未建 `rollout` 的 Codex、空 shell 或未集成的 CLI），且非远端否决、存在合法安全的焦点 `window_cwd` 时，自动回落注入该工作区的 `git status` / `repo_files` / `shallow_files` 静态参考与热词（遵守宽泛目录与 WSL2 `/mnt/` 守卫）。
   - **Layer 1（Stage 1 零风险历史热词）**：只要存在合法 `workspace_cwd`（含新会话、已超 4 条消息的成熟会话、无活跃会话终端及普通桌面 IDE 工作区）且未设为 `"off"`，即从同精确目录（`rank == 2`、不限天数、不看 Git 分支、排除 subagent 与当前 `session_id`）的近期 Top-5 充实历史会话提取高频技术标识符（`cold_start_prior_hotwords`），作为低优先级项目词汇**持续保留**并补入 Stage 1 热词（排在用户手工热词、`git` 变更文件与当前会话热词之后、`BUILTIN_AGENT_HOTWORDS` 之前；候选发现按 `cwd` 短时缓存 `60s`）。`4` 条消息门槛仅约束 Layer 2 的 `<prior_session_reference>` 历史正文块，不影响同目录跨 Session 历史热词的持续保留。
   - **Layer 2（Stage 2 & 单跳独立冻结第 0 块 + 自然淘汰）**：面向不足 2 轮（`< 4` 条消息）的 Agent 会话且设为 `"auto"` 时，冷启动首次从同精确目录 Top-5 充实历史会话（跨 Claude Code / Codex / `agy` 互通，优先满足 `>= 4` 条消息且 `>= 3,000` 字符的甜点会话，同档内同 CLI 略优先再按 `mtime` 降序；均不满 `3,000` 字符时取 `>= 1,000` 字符中内容最多者；Antigravity IDE 独立隔离在自身 `brain/` 目录）截取尾部 `<= 25,000` 字符作为**独立只读冻结块**（`<prior_session_reference read_only="true" scope="same_workspace_previous_session">`）置于当前会话分块之前，绝不混入 `clean_turns` 也不随原旧文件后续追加而变化，`prompt_cache_key` 始终绑定当前新会话；随着新会话增长直至超出总上下文滑窗或触发 compaction 时整块自然滑出淘汰且不再回拼（普通桌面 IDE 仅享 Layer 1 热词，不注入旧对话原文）。

---

## 二、桌面 GUI 应用（代码 IDE 与日常窗口）

默认 `hotkey.scope = "all"` 下，守护进程按前台窗口的进程名与 macOS `bundle_id` 将桌面窗口划分为 `terminal`、`coding_gui`、`general_gui` 三类，从源头隔离不同语境：

### 2.1 日常桌面窗口：通用画像、硬隔离与 ChatGPT/Codex、Claude Desktop 本地会话动态升格（`general_gui`）

- 当焦点位于浏览器、文档、邮件、即时通讯或普通桌面聊天客户端时，管线自动切入 `general` 通用润色模式（仅挂载个人热词 `hotwords/personal.txt`），并**硬跳过终端 `sync_session`**。即便后台终端正挂着 200K 上下文的 Coding Agent，也绝不会把代码变量名或仓库术语塞进日常聊天。
- **ChatGPT / Codex 统一桌面端本地仓动态升格、云端隔离与同窗复核**：
  - 仅在明确识别为统一桌面端窗口（排除终端进程后，macOS 匹配 `com.openai.codex`，无 `bundle_id` 时匹配进程 `ChatGPT` 或带桌面标题后缀的 `Codex`）且未显式强制 `mode = "general"` 时触发。
  - **只读索引与 fail-closed 约束**：优先以 `mode=ro`（WAL 安全、拒绝软链与 WSL2 `/mnt/` 慢盘）只读 `~/.codex/sqlite/codex.db` 的 `local_thread_catalog` 表（若 `codex.db` 存在但损坏、被锁或缺少必要列则直接 fail-closed 返回空，绝不静默降级以防绕过云端否决）；仅当 `codex.db` 不存在时，回退只读 `~/.codex/state_5.sqlite` 的 `threads` 表（限定 `archived = 0` 且 `originator IN ('Codex Desktop', 'codex_work_desktop')`）。
  - **标题匹配与云端硬隔离**：同时用原始窗口标题与剥离 `- ChatGPT` / `- Codex` 后的标题匹配 `display_title`（屏蔽 `ChatGPT`/`Codex`/`New Chat` 等泛化标题，支持精确匹配与尾部 `...`/`…` 截断前缀匹配）。**全局否决优先**：只要命中池中存在任意云端/非本地条目（`source_kind == 'chatgpt'`、`host_id != 'local'` 或空 `cwd`），立即整体否决不升格；若命中多个不同本地仓库目录，同样拒绝不猜。
  - **本地仓升格与 rollout 核验**：仅当所有命中候选收敛至唯一合法本地工作区目录（避开宽泛系统/家目录），且对应线程在 `~/.codex/sessions/` 下具备真实非软链、同 UID、首行 `session_meta` 的 `id` 一致且非 subagent 的 `rollout-*.jsonl`（同时尊重 `state_5.sqlite` 的 `archived = 1` 否决）时，才动态升格为 `mode = "agent"`（`matched_by = "codex_desktop_db"`），挂载本地仓库热词与增量前缀缓存；普通云端聊天则稳定保持 `general` 模式零串台。
  - **投递前同窗切会话复核**：对 `codex_desktop_db` 升格的投递附加 `same_window_verifier`，在最终粘贴前核验同一窗口的当前标题——若标题发生变化则重新经 `resolve_codex_desktop_session_by_title` 复核是否仍为原 `session_id`，一旦检测到用户在录音或处理期间于同窗口内切到其他会话或云端聊天，立即中止粘贴并退回剪贴板（`focus_changed`）。
- **Claude Desktop（Code + Cowork）动态升格与 Chat 纯云端零串台**：
  - **纯只读有界嵌套 `AXWebArea` 路由与标题采集**：macOS `mac_shim/*.swift` 仅在起点事件（`ptt_start`/`toggle`）与 `fg_window` RPC 且前台为 `com.anthropic.claudefordesktop` 时（`ptt_stop`/`ptt_cancel`/`hold_cancel` 直接跳过遍历），于后台队列执行纯只读有界 BFS（`maxDepth = 12`、`maxNodes = 96`、总预算 `80ms` 且每次 AX IPC 前按剩余预算动态设置超时、剩余 `<= 5ms` 早退，不写 `AXManualAccessibility`）。BFS 穿透外层 `file://.../main_window/index.html` 壳 `AXWebArea`（深度 8）定位嵌套内容 `AXWebArea`（深度 9）后不再深入业务 DOM，提取并清洗其 `AXTitle` 与 `AXURL`（仅保留脱敏 `scheme://host/path`，丢弃 userinfo/query/fragment、控制字符及 `> 512` 字符候选），经 `page_url` 透传给 Python。
  - **URL 精确路由优先 + 增量缓存安全兜底**：
    - 当存在 `page_url` 时：仅接受 `app://localhost` 或 `https://claude.ai`（无异常端口、无 userinfo）可信来源与完整 36 字符 UUID 路由段，`/cowork/local_<uuid>` 定点定位 `local-agent-mode-sessions` 对应 `local_<uuid>.json`（`matched_by = "claude_desktop_cowork"`），`/epitaxy/local_<uuid>`（或 `/code/local_<uuid>`）定点定位 `claude-code-sessions` 对应 `local_<uuid>.json`（`matched_by = "claude_desktop_code"`），并核对元数据内部 `sessionId` 与路由 `local_<uuid>` 一致、零全量目录扫描；`/new`、`/chat/...`、无 ID 的 `/epitaxy` / `/cowork` 及不可信 host/端口等非会话路由直接 fail-closed 返回空，确保同名云端 Chat 绝不撞上 Cowork；
    - 当缺少 `page_url`（如无 shim 或仅标题形态）时：裸 `" - Claude"` 无法区分 Chat 与 Cowork，直接 fail-closed 降级为 `general` 模式；仅 `" - Claude Code"` 允许按 `(mtime_ns, size)` 增量缓存回退扫描 `claude-code-sessions`（瞬时 I/O 异常不写入负缓存，且当轮存在未读候选时放弃消歧），同标题命中多个不同活跃 `session_id`（如官方版与 3P 版双开）或跨仓库冲突时一律 fail-closed 放弃绑定。
  - **双目录联合只读扫描、严格日志校验与 Cowork 隔离**：
    - 联合只读扫描 `~/Library/Application Support/Claude`（官方版）与 `~/Library/Application Support/Claude-3p`（3P 版），屏蔽泛化标题、过滤 `isArchived = true` 及超 `1 MiB`/软链元数据；
    - 主 JSONL（Code 的 `~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl` 与 Cowork 首选的 `<sid8>/.claude/projects/session/<cliSessionId>.jsonl`）及 Cowork 兜底 `audit.jsonl` 均执行有界严格身份校验（按剩余字节限读扫描至确认显式匹配 `sessionId`/`session_id` 且见有效对话，上限 `512` 物理行或 `512 KiB`），拒绝任何 sidechain 或冲突 SID 记录（增量摄取同步隔离异会话记录）；首选主文件存在但损坏时直接拒绑，不绕过回退 `audit.jsonl`；
    - Cowork 绝不将内部沙箱 `.../outputs` `cwd` 当作工作目录，改按显式路径优先消费、剩余未匹配项再走无歧义 `display` 或等长纯字符串数组解析 `userSelectedFolders` + `resolvedFolderKinds`（无法安全解释时保留会话并将 `cwd` 置空，同时保留未解析原始目录用于冲突消歧；仅单本地目录时绑定 `cwd`），启用 `authoritative_cwd` 阻断 `/private/var/empty` 回写并跳过跨 CLI 编程先验块；Cowork `audit.jsonl` 仅在自身摄取路径同时按消息 `uuid` 与 `(message.id, role, cleaned_text)` 跨增量去重（单条 `desktop_app` 记录与同 `message.id` 不同正文记录均完整保留，真正重复写入行丢弃）。
  - **投递前同窗会话复核与边界**：对 `claude_desktop_code` / `claude_desktop_cowork` 升格的投递附加 `same_window_verifier`，粘贴前经 `mac_shim` `fg_window` 重新抓取当前 `page_url` 与标题并强制重核 `local_<uuid>` 与 `session_id`（即使标题相同也不跳过复核），切到 `/new`、`/chat/...`、另一会话或取不到路由时中止粘贴并退回剪贴板（`focus_changed`）。注：与 AGY 2.0 / ChatGPT 桌面端一致，最终 Python 复核通过后进入 `mac_shim` 剪贴板快照与 Cmd-V 阶段仅执行应用标识与窗口号焦点复核，不在粘贴路径重复发起第二轮 Electron AX 树遍历。

### 2.2 桌面 IDE：工作区感知与 Antigravity 桌面版会话联动（`coding_gui`）

- **隔离后台终端 + 非递归定位工作区**：在 VS Code、Cursor、Windsurf、Zed、JetBrains 全家桶、Xcode、Sublime Text 等编辑器中口述时，同样跳过终端 CLI 会话扫描（防止后台终端跑 A 仓库时污染前台 IDE 打开的 B 仓库），改由 `resolve_ide_workspace_cwd` 定位当前窗口的本地目录：
  1. macOS 优先读取窗口 `AXDocument`（`file://` URL），向上有界回溯匹配窗口标题项目名或 `.git` 根；
  2. 解析窗口标题中的显式路径（如 JetBrains `[~/repos/app]`、Sublime 路径），或提取标题项目名与已知活跃工作目录及 `~/repos`、`~/projects`、`~/code`、`~/Developer`、`~/Documents` 等常见一级目录按 `(st_dev, st_ino)` 去重比对（同名目录冲突时唯一含 `.git` 的目录优先，仍冲突则放弃不猜；WSL2 下逐段校验路径，零 `lstat` 避开 `/mnt/` 慢盘与远程 URI）。
- 定位到本地目录（兼容 Git 仓库与非 Git 文件夹）后，自动注入该目录的 `git status`、核心追踪文件名或浅层文件名供识别与精修纠偏。
- **Antigravity 2.0 独立桌面版专属绑定（优先路径，`matched_by = "ide_db_title"`）**：
  - 严格与命令行 `~/.gemini/antigravity-cli` 物理隔离，仅扫描 `~/.gemini/antigravity` 与 `~/.gemini/antigravity-ide`。以 `mode=ro`（WAL 安全、拒软链）只读 `conversation_summaries.db` 的 `conversation_summaries` 表，过滤 CLI 来源（`source != 'CORTEX_TRAJECTORY_SOURCE_CLI'`）、子会话（`parent_conversation_id = ''`、`nesting_depth = 0`）与内部精修会话（`agent_name != 'avi-refiner'`）。
  - 屏蔽 `New Chat`、`New Conversation`、`Agent Manager`、`Welcome` 等空窗/泛化标题（无 `AXDocument` 时同时跳过旧路径兜底防跨仓误抓）；其余标题先执行精确匹配，未命中再执行尾部 `...` / `…` 截断前缀匹配（中文前缀 `>= 4` 字符、英文 `>= 8` 字符），多候选并列时按 `annotations/<cid>.pbtxt` 修改时间 → DB `last_modified_time` → `transcript` `mtime` 择优。
  - **结构化 URI 解析与工作区绑定**：对 DB 返回的 `workspace_uris` JSON 经结构化 URL 解析（`_decode_structured_workspace_uri`，仅接受本地 `file://` 及 WSL 下 `vscode-remote://wsl+...`，保留路径合法末尾标点，拒绝远端 URI 与 WSL `/mnt/` 慢盘）解析本地工作区目录，并核验 `brain/<cid>` 非 subagent 主日志后绑定工作区热词与增量缓存；冷启动历史扫描同样优先复用 DB 精确同目录记录，并对 DB 中已明确绑定到其他仓库或远端 URI 的会话施加权威否决。
  - **同窗复核防错贴**：配合起点窗口锁定与投递前同窗会话复核，在录音/处理期间若于同窗口切到其他对话或切离窗口，中止直接粘贴并安全退回剪贴板，避免跨会话错贴。
- **老版 Antigravity VS Code Fork 回退路径（`matched_by = "ide_brain"`）**：当独立桌面版 DB 未命中且窗口携带有效工作区线索（如 `AXDocument` 或含工作区名的编辑器标题）时，回退扫描 `~/.gemini/antigravity/brain`（及 `antigravity-ide/brain`）6 小时内最新最多 `64` 条记录（评估前 `16` 个非 subagent 主会话），依次核验三层路径证据：
  1. `subagents/*.json` `workspaceUris` 或系统记录的 `cwd`；
  2. 最近一条 `<ADDITIONAL_METADATA>` 中的工作区路径多数派；
  3. 模型 `tool_calls` 参数路径（须至少命中 `2` 条当前工作区内路径且占非噪声路径比例 `>= 60%`）。
  命中后选用 `mtime` 最新的一条会话增量同步缓存上下文。

---

## 三、SSH 连接远端 Linux 主机（零远端安装跨机路由）

针对“本地 macOS 或 Windows+WSL2 采集音频与监听热键 → SSH 连接远端无头 Linux 服务器运行 Coding Agent”的场景，系统采用**前置硬否决防串台 + 按需白名单无状态探针**双层设计。

### 3.1 零配置防串台底线（Phase 0 Remote Veto）

哪怕完全不配置 `[remote]`（默认 `hosts = []`），只要检测到前台满足以下任一条件，解析器在入口阶段立即否决所有本地后台 CLI 会话、`tmux` 窗格与本地磁盘记录：

1. **Remote IDE 窗口**：VS Code / Cursor Remote-SSH、Codespaces、Tunnel 等（窗口标题带 `[SSH: host]` 等标签，或文档 URI 为非 `wsl+` 的 `vscode-remote://` / `ssh://`；此类 GUI 窗口仅阻断本地串台，不发起终端 SSH 探针）；
2. **专用 SSH 客户端进程**：`PuTTY`、Windows `KiTTY`（精确区分于 macOS 本地 GPU 终端 `net.kovidgoyal.kitty`）、`MobaXterm`、`WindTerm`、`SecureCRT`、`Xshell`、`Termius`、`FinalShell`、`Electerm` 等；
3. **窗口或 `tmux` 窗格标题带远端标记**：如 `[@devbox]`、`user@devbox: ~/repos/app`、`[user@devbox dir]$`、`ssh user@devbox`（自动排除本机 `hostname`）；
4. **本地终端前台正在运行交互式 `ssh`**：焦点 TTY、窗口子进程或本地活跃 `tmux` 窗格的前台进程组（Linux `/proc/<pid>/stat` `pgrp == tpgid` 或 macOS `ps` `+` 标记）为交互式 `ssh`（自动忽略后台 `git`、`scp` 与 `BatchMode=yes` 进程；WSL2 无焦点 TTY 时，若存在非 CLI 的活跃 SSH 终端且其 TTY 活动时间新于本地候选，同样触发中性否决）。

这保证了未配白名单时，任何命中上述四类远端信号的窗口都绝不会把本机同名代码仓或后台会话混入精修提示词（注：在无焦点 `tty` 的 Windows Terminal 中直接跑 Windows `ssh.exe` 时，若远端 TUI 将标题覆盖成不含 `[@host]` 的裸 CLI/动画标题，WSL2 侧无法看到 Windows `ssh.exe` 进程，此时配合下文的远端 `tmux set-titles` 或 `remote-title-hook.sh` 输出 `[@host]` 即可稳定携带远端信号）。

### 3.2 远端零常驻、零安装方案

只需在本地 `~/.agent-voice-input/config.toml` 将远端主机加入白名单即可开启跨机上下文感知：

```toml
[remote]
hosts = ["devbox"]
# timeout_ms = 400
# WSL2 下若 SSH 私钥 / 1Password Agent 位于 Windows 侧，可取消注释：
# ssh_bin = "ssh.exe"
```

（若终端提示符显示的远端短主机名与 `~/.ssh/config` 的 `Host` 别名不一致，可使用 `[[remote.hosts]]` 配置 `name`、`ssh_target` 与 `aliases`，详见 [`config.example.toml`](../config.example.toml)，并用 `agent-voice-input doctor --remote` 验证连通性。）

核心工作机制：

- **`stdin` 注入纯标准库只读探针**：本地通过 `ssh -T -x -a -o BatchMode=yes -o RequestTTY=no -o RemoteCommand=none -o ClearAllForwardings=yes ... -- <target> python3 -` 将压缩后的纯标准库探针（`_remote_probe.py`）写入 `stdin` 执行。远端无需安装 `agent-voice-input`、无需常驻守护进程、无需 `ffmpeg` 或任何云端凭据，仅需系统自带 Python 3.10+（macOS / Linux 本地 `ssh` 自动启用短路径 `ControlMaster` 复用连接）。
- **ETag 未变短路 + 正向 50K 整块对齐裁剪 + 单趟历史热词与冷启动回退**：探针定位目标会话后先比对远端 JSONL 的 `(st_dev, st_ino, st_mtime_ns, st_size)` ETag，文件未变时直接标记 `not_modified` 跳过全文读取与清洗（仅按需刷新远端 `git status`）；有新对话时在远端完成字节预筛与清洗，通过 `trim_turns_chunk_aligned` 从第 0 轮起按轮次边界与默认 `50K` 字符目标正向切块、整块淘汰超出默认 `200K`（随 `refiner.context_max_chars` 联动）的旧块后以 `gzip` 帧回传（通常仅数 KB）。只要未禁用 `cold_start_fallback` 且存在合法安全工作目录，同趟在远端复用 `_scan_storage_for_cwd` 从同精确目录（`rank == 2`、不限天数、不看分支、排除 subagent 与当前会话）的 Top-5 充实历史会话聚合提取 `prior_hotwords`（包括已超 `4` 条消息的成熟会话，持续保留）；仅当命中真实 Agent 会话（非 `workspace` 纯目录回退）、处于冷启动态（`< 4` 条消息且未发生滑窗淘汰或压缩）且为 `auto` 模式时，额外截取首选历史会话尾部 `<= 25K`（计入包装开销）随 `prior_turns` 字段回传作为 `<prior_session_reference>` 历史正文块（零额外 SSH RTT；`hotwords` 模式与成熟会话仅传历史热词、不传历史正文块）。由于切块边界与本地 `chunk_turns_for_refiner` 保持逐字节一致，无需双端同步 `byte_offset` 即可稳定复用上游前缀缓存；同时带回远端目录的 `git status`、追踪源码文件名或非 Git 浅层文件名（严禁扫描远端 `~`、`/`、`/tmp` 等宽泛目录，且绝不混读本机同名路径）。
- **按下热键异步预取 + 松键剩余预算控制 + 主机熔断器**：按下热键开始录音（`start_recording`）瞬间即脱锁在后台并发启动远端预取，把跨机握手与清洗耗时藏进说话时间里；松手结束录音进入 `sync_session` 时，若预取已在 `15s` 新鲜期内完成则直接内存命中，未完成最多等待扣减本地解析耗时后的剩余 `timeout_ms`（默认 `400ms`，可配 `0–10000ms`；设为 `0` 表示松手零等待、仅消费已完成预取）预算即平滑降级继续转写（后台任务完成后仍会预热下一句缓存）。目标主机连续 2 次连接超时自动熔断冷却 `60s`，遇鉴权或 HostKey 失败立即熔断冷却 `300s`。

### 3.3 什么时候远端“完全零文件”就够用，什么时候挂可选单文件钩子

- **远端完全零文件即够用的场景**：
  1. **窗口标题带具体项目目录**（如默认提示符 `user@devbox: ~/repos/my-app`）：探针按 `cwd` 扫描远端 `/proc` 与落盘主会话，按“目录匹配等级（精确目录 `2` > 非宽泛父子目录 `1`）→ 标题 CLI 词提示 → 最新 `jsonl_mtime` / `pts_mtime`”排序选取（若提示符停在宽泛父目录 `~` 则直接拒绝匹配）；无 `cwd` 线索时先按标题中的 CLI 词、目录 basename 与 Codex `thread_name` 打分收敛，若收敛出唯一高分候选或剩余候选均属同一确认目录（选最新 `mtime`）则命中，仍跨不同目录并列则返回 `ambiguous` 不强猜；
  2. **远端使用单客户端 `tmux`**：探针会查询远端最近活跃 attached `tmux` 客户端的焦点窗格（优先查 `#{pane_title}` 指纹，其次扫该窗格进程树；若焦点窗格为空 shell，则继续向下回退走 `cwd` 匹配或全机存活 `/proc` 消歧）。若你在本地开了多个 SSH 标签页连接不同主机，推荐在远端 `~/.tmux.conf` 加两行透传窗格标题与主机标记：
     ```tmux
     set -g set-titles on
     set -g set-titles-string '#T [@#h]'
     ```
- **可选挂载单文件 `remote-title-hook.sh`（同主机多连接 / 同目录多会话精确锁定）**：
  - 当你在远端同一台机器开多个独立 SSH 连接，或在**同一项目目录**下并行跑多个 Claude Code / Antigravity CLI 会话时，可将单文件脚本 [`integrations/remote/remote-title-hook.sh`](../integrations/remote/remote-title-hook.sh) 放到远端 `~/.local/bin/remote-title-hook.sh`（`chmod +x`）并接入对应 CLI 钩子（Codex 仅需在远端 `~/.codex/config.toml` 配置 `terminal_title` 含 `"thread-id"`）。
  - 该独立脚本不建远端注册表、不依赖任何包，直接向控制终端写入 `claude · my-app [#a1b2c3d4] [@devbox]`（解析侧同时兼容 `[#xxxxxxxx]` 与 `[§xxxxxx]` 标签），在远端 `tmux` 内同步更新窗格 `#{pane_title}`；远端探针收到该指纹后按“远端注册表（若装有）→ 存活 `/proc` → 会话存储目录”直接锁定目标会话。

### 3.4 安全边界

守护进程仅当提取出的主机标识命中本地 `[remote]` 显式白名单时才发起只读 `BatchMode=yes` SSH 调用，连接参数固定以 `-- <target> python3 -` 结尾并对回包执行 `2 MiB` 流式解压上限与字段结构校验——从根源阻断恶意网页、容器日志或仓库文本通过终端 OSC 2 标题诱导连接任意外部主机（SSRF）或注入 `-oProxyCommand` 参数（注：终端标题本身属于未鉴权的界面提示，若不可信输出伪造了你已列入 `[remote]` 白名单的另一台主机标签，最多只会触发对该白名单主机的只读上下文探针）。
