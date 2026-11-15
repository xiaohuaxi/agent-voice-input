[English](README.md) | [简体中文](README.zh-CN.md)

# agent-voice-input

面向命令行 Coding Agent（如 **Codex**、**Claude Code**、**Antigravity CLI / `agy`**）的语境感知语音输入工具。

不同于缺乏代码上下文的通用语音输入法，`agent-voice-input` 采用「极速语音识别 + 结合当前活跃会话上下文的轻量精修」两阶段设计，在粘贴进终端前自动修准同音代码标识符、中英混说术语与口语中途改口。

> **早期版本说明（`v0.3.0`）**：当前已在 **Windows Terminal + WSL2（含 `tmux` 多分屏与远程桌面）** 与 **macOS + Ghostty（含 `tmux`）** 下打通日常语音输入工作流；更多终端适配仍在持续推进中。

---

## 核心思路

1. **活跃会话上下文抽取**：自动定位前台终端中的主 Agent 会话，原生支持 `tmux` 分屏焦点窗格识别而无需修改 `~/.tmux.conf`（支持终端/窗格标题标签、运行时会话注册表、macOS Ghostty 分屏 `tty`/`cwd` 直连匹配，以及 Linux `/proc` / macOS `ps` 根会话扫描，严格排除后台 subagent 与相邻分屏干扰），增量提取近期对话与 `git status` 文件名作为上下文参考。
2. **一阶段语音识别（ASR）**：先在本地做 `-50 dBFS` 静音门控与 `-20 dBFS` 响度归一化（180s 以内整段直发，超过 3 分钟的长语音自动挑自然停顿处切分后小并发转写；若识别或上屏失败，自动将音频暂存于运行目录 `failed/` 供 `retry` 恢复），再调用内置云端或本地兼容渠道（支持 `dashscope`、`doubao`、`openrouter` 双协议 STT 与多模态音频、`gemini`、`openai`、`groq`、`siliconflow` 或 `openai-compatible`）。
3. **二阶段轻量语境精修**：结合抽取出的会话上下文修正同音标识符、大小写与明确口语改口。默认通过本地 Codex CLI（`codex` + `gpt-6-luna`）完成整理，也支持直接切换为 `dashscope`、`volcengine`、`openrouter`、`gemini`、`openai`、`groq`、`siliconflow`、`openai-compatible` 或关闭（`off`；当一阶段已完成多模态单跳整理时默认自动跳过二阶段）。
4. **焦点安全粘贴**：Windows 与 macOS 均提供不抢焦点的底部悬浮胶囊提示（可配 `"top"` 置顶；录音、识别、精修阶段，默认随桌面 UI 语言自动切换中英文提示）；默认仅在允许的终端窗口前台时响应新热键录音，开始录音时锁定原终端窗口/分屏，上屏仅粘贴不自动按回车，中途切走焦点则仅写入剪贴板防串窗。
5. **零额外 Python 依赖**：仅使用 Python 3.10+ 标准库与宿主机 `ffmpeg`（WSL2 复用系统自带 `powershell.exe`，macOS 首次启动使用 `/usr/bin/swiftc` 幂等编译缓存原生辅助程序 `mac_shim.swift`），无需额外安装第三方 Python 包。

---

## 快速开始

### 1. 前置条件

- **Python** `>= 3.10`
- **ffmpeg**：
  - Ubuntu / WSL2：`sudo apt install ffmpeg`
  - macOS：`brew install ffmpeg`
- **macOS 原生热键与胶囊辅助程序**（macOS 推荐）：安装 Xcode Command Line Tools（`xcode-select --install`）以提供 `/usr/bin/swiftc`。首次启动时自动幂等编译并缓存 `mac_shim.swift`；若未安装 `swiftc`，会自动降级为命令行 `toggle` / `once` 模式继续可用。

### 2. 安装

```bash
uv tool install agent-voice-input
# 或使用 pipx / pip：
# pipx install agent-voice-input
```

从源码本地软链开发：

```bash
git clone https://github.com/xiaohuaxi/agent-voice-input.git ~/repos/agent-voice-input
mkdir -p ~/.local/bin
ln -s ~/repos/agent-voice-input/bin/agent-voice-input ~/.local/bin/agent-voice-input
```

### 3. 配置凭据

在 `~/.config/agent-voice-input/agent-voice-input.env`（或直接通过环境变量）配置至少一项识别凭据即可启动——例如阿里百炼 `DASHSCOPE_API_KEY`，或用一把 `OPENROUTER_API_KEY` 同时打通两阶段：

```bash
mkdir -p ~/.config/agent-voice-input && chmod 700 ~/.config/agent-voice-input
cat > ~/.config/agent-voice-input/agent-voice-input.env <<'EOF'
DASHSCOPE_API_KEY="<your-dashscope-api-key>"
EOF
chmod 600 ~/.config/agent-voice-input/agent-voice-input.env
```

如需自定义快捷键（单点切换 `tap_toggle`、按住说话 `ptt` 或 `only_in_terminals = true | false`）、胶囊提示语言与位置（`[ui] language = "auto" | "zh" | "en"`、`position = "bottom" | "top"`，或环境变量 `AGENT_VOICE_INPUT_UI_LANGUAGE` / `AGENT_VOICE_INPUT_UI_POSITION`）、单次最长录音时长（`[audio] max_duration_sec = 600.0`，默认 10 分钟，最后 15 秒胶囊倒计时预警并到点自动完成转写；设 `0` 不限制）、macOS 录音设备（`[audio] macos_device = "default"` 或环境变量 `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`）、渠道、模型或二阶段精修方式，可将 [`config.example.toml`](config.example.toml) 复制为 `~/.config/agent-voice-input/config.toml` 按需修改（全部凭据变量见 [`config.example.env`](config.example.env)）。

### 4. 自检与运行

```bash
# 检查音频输入、mac_shim/win_shim 状态、解析后的渠道/模型、凭据与会话识别
agent-voice-input doctor

# 启动后台守护进程（WSL2 与 macOS 下默认单点右 Ctrl 开始/结束录音）
agent-voice-input start

# 其它常用子命令
agent-voice-input status
agent-voice-input restart
agent-voice-input toggle
agent-voice-input once --duration 4 --dry-run
agent-voice-input retry --list
agent-voice-input stop
```

---

## macOS + Ghostty 开箱体验与权限说明

在 **macOS + Ghostty** 下无需配置终端钩子或高侵入全局权限即可开箱使用：

- **终端限定热键与底部胶囊提示**：默认单点右 Ctrl（`RControl`）开始/结束录音，亦可在配置中设为 `trigger_mode = "ptt"` 按住说话（默认 `[hotkey] only_in_terminals = true` 仅在前台为终端窗口时启动新录音，录音中途切到其他应用仍可正常停止或取消）；录音、识别、精修各阶段由底部不抢焦点的悬浮胶囊（可配 `[ui] position = "top"`）按桌面语言自动显示中英文提示。
- **Ghostty 原生分屏直投与防焦点漂移**：按下录音时记录当前聚焦的 Ghostty 分屏 surface，识别完成后通过原生脚本接口直接向该分屏注入 bracketed paste 文本（免辅助功能按键模拟）；若等待期间切到了别的分屏或窗口，仅写入剪贴板不误投。
- **分屏 TTY 与 `tmux` 免钩子会话自动关联**：自动采集聚焦 Ghostty 分屏的 `tty` 与 `cwd`（连接 `tmux` 时进一步锁定对应客户端的焦点窗格），直接匹配该分屏内正在运行的 `codex`、`claude` 或 `agy` 活跃会话，多分屏并行开发互不串台。
- **录音设备指定**：默认 `[audio] macos_device = "default"` 跟随系统默认麦克风；也可在 `config.toml` 或环境变量 `AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE` 中指定设备序号或名称（可用 `agent-voice-input doctor` 查看可用输入设备）。
- **macOS 系统权限说明（TCC）**：
  - **麦克风权限（Microphone）**：首次录音时授权给启动守护进程的终端/Python 环境，供 `ffmpeg`（`avfoundation`）采集音频。
  - **自动化权限（Automation → Ghostty）**：首次向 Ghostty 读取分屏或上屏时弹窗确认一次即可。
  - **辅助功能（Accessibility）与输入监控（Input Monitoring）**：默认 `RControl` + Ghostty 组合**不需要开启**；仅在向非 Ghostty 应用回退模拟 `Cmd+V` 粘贴（需辅助功能）或改用非修饰键热键 / 运行 `agent-voice-input key-test`（需输入监控 / 辅助功能）时按需授权。

---

## 配置结构与内置渠道

配置采用清晰的两层文件分工（优先级：`CLI 参数 > AGENT_VOICE_INPUT_* 环境变量 > config.toml > 内置默认值`）：

- **`~/.config/agent-voice-input/config.toml`**：管理 `[hotkey]`、`[ui]`、`[audio]`、`[asr]`、`[refiner]` 结构化参数（`provider`、`model`、`api`、`effort`、`base_url`、`api_key_env` 等）。
- **`~/.config/agent-voice-input/agent-voice-input.env`**：集中存放各厂商 API 凭据（及可选的环境变量覆盖项）。

### `[asr]` 与 `[refiner]` 核心字段说明

- **`provider` 与 `model`**：`model` 始终原样保留上游模型 ID（如 `"qwen/qwen3-asr-1.7b"`、`"google/gemini-2.5-flash"`），不做额外前缀拼接；留空 `""` 时自动选用该 `provider` 的默认模型。若通过高优先级层（CLI 或环境变量）覆盖了 `provider` 且未同时指定 `model`，会自动清空低优先级层继承的旧 `model` 并切回新渠道默认模型，避免模型名跨渠道错位。
- **`asr.api`（`"auto" | "audio-transcriptions" | "chat-completions"`）**：
  - `"auto"`（默认）：`dashscope`、`doubao` 与原生 `gemini` 自动走各自专属适配器；对 `openrouter`、`openai`、`groq`、`siliconflow` 与 `openai-compatible`，已知专用 STT 模型（如 `qwen3-asr`、`whisper`、`*-transcribe`、`sensevoice`、`deepgram/*`、`assemblyai/*`、`elevenlabs/*` 等）自动走 `"audio-transcriptions"` 并进入二阶段精修，已知多模态音频理解模型（如 `gemini-*`、`gpt-audio*`、`gpt-4o*audio*`、`*-omni*`、`mimo-*`、`voxtral-small*` 等）自动走 `"chat-completions"` 完成单跳识别与上下文整理（默认跳过二阶段，除非设置 `refiner.force_on_single_hop = true`）。注：`dashscope` 的 `qwen3-asr-flash` 虽经对话兼容端点传输，仍属纯语音转写并正常进入二阶段精修。
  - 在 `openrouter` 下尝鲜尚未内置收录的新模型时，请显式指定 `asr.api = "audio-transcriptions"` 或 `"chat-completions"`。
- **`asr.effort` 与 `refiner.effort`**：统一的思考/推理开销档位（`"auto" | "default" | "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max"`）。`asr.effort` 默认为 `"auto"`（对 Gemini 2.5 等支持思考控制的单跳多模态模型自动压低思考延迟，对纯 STT 或不支持推理参数的音频模型不乱塞字段）；`refiner.effort` 默认为 `"low"`；设为 `"default"` 则完全不传推理控制参数。

### 内置渠道一览

| `provider` | 支持阶段 | 默认 `asr.model` | 默认 `refiner.model` | 推荐凭据环境变量 |
| :--- | :--- | :--- | :--- | :--- |
| **`dashscope`** | `asr`, `refiner` | `qwen3-asr-flash` | `qwen-turbo-latest` | `DASHSCOPE_API_KEY` *(兼容 `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `bigmodel` | — | `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY` |
| **`volcengine`** | `refiner` | — | `doubao-seed-1-6-flash` | `ARK_API_KEY` *(兼容 `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `qwen/qwen3-asr-1.7b` | `google/gemini-2.5-flash-lite` | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `gemini-2.5-flash` | `gemini-2.5-flash-lite` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`codex`** | `refiner` | — | `gpt-6-luna` | 本地 `codex` CLI 登录态 *(绝不开启 Fast 模式)* |
| **`openai-compatible`** | `asr`, `refiner` | `gpt-4o-mini-transcribe` *(需配 `base_url`)* | *(需显式配 `base_url` + `model`)* | `api_key_env` 或 `OPENAI_API_KEY` *(本机回环地址免 Key)* |

- **特殊值**：
  - `asr.provider = "auto"`（默认）：按 `dashscope -> doubao -> openrouter -> gemini -> openai -> groq -> siliconflow` 顺序自动选用首个已配妥凭据的渠道。
  - `refiner.provider = "off"`：关闭二阶段语境精修。
- 凭据变量同时兼容前缀写法（如 `API_KEY_OPENROUTER`、`API_KEY_GEMINI`、`APP_ID_DOUBAO_ASR` + `API_KEY_DOUBAO_ASR` 等），详见 [`config.example.env`](config.example.env)。

---

## 常见配置配方（`config.toml`）

### 配方 A — 国内低延迟主力（`dashscope` + 本地 `codex` / `volcengine` / `dashscope`）

```toml
[asr]
provider = "dashscope"
model = "qwen3-asr-flash"

[refiner]
# 默认复用本地常驻 codex app-server；若不装 Codex CLI 可改为 "volcengine" 或 "dashscope" 纯 API 精修
provider = "codex"
model = "gpt-6-luna"
effort = "low"
```

### 配方 B — OpenRouter 一把 Key · 专用 STT 两阶段

仅需配置 `OPENROUTER_API_KEY`，一阶段走 `/audio/transcriptions` 极速转写，二阶段结合会话上下文轻量修准术语：

```toml
[asr]
provider = "openrouter"
model = "qwen/qwen3-asr-1.7b"

[refiner]
provider = "openrouter"
model = "google/gemini-2.5-flash-lite"
effort = "low"
```

### 配方 C — OpenRouter 一把 Key · 多模态单跳识别精修

将音频与会话上下文通过单次 `/chat/completions` 请求一并完成转写与口语整理（内置防指令劫持提示词结构），并自动跳过二阶段：

```toml
[asr]
provider = "openrouter"
model = "google/gemini-2.5-flash"
api = "auto"
effort = "auto"
```

### 配方 D — Gemini 原生单跳 / Groq 极速 STT

```toml
# 方案 1：Google Gemini 原生 :generateContent 单跳音频+上下文整理（默认跳过二阶段）
[asr]
provider = "gemini"
model = "gemini-2.5-flash"
effort = "auto"
```

```toml
# 方案 2：Groq Whisper 极速转写 + Groq 二阶段精修
[asr]
provider = "groq"
model = "whisper-large-v3-turbo"

[refiner]
provider = "groq"
model = "llama-3.3-70b-versatile"
```

---

## 命令行参数与环境变量覆盖

`daemon`、`start`、`restart`、`doctor`、`once`、`retry`、`status`、`toggle`、`stop`、`key-test` 均支持以下覆盖参数：

| CLI 参数 | 环境变量覆盖（`AGENT_VOICE_INPUT_*`） | 说明 |
| :--- | :--- | :--- |
| `--config PATH` | `AGENT_VOICE_INPUT_CONFIG` | 指定 `config.toml` 或 `config.json` 路径 |
| `--asr`, `--asr-provider` | `AGENT_VOICE_INPUT_ASR_PROVIDER` | 一阶段渠道（`auto`, `dashscope`, `doubao`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`） |
| `--asr-model` | `AGENT_VOICE_INPUT_ASR_MODEL` | 一阶段上游模型 ID |
| `--asr-api` | `AGENT_VOICE_INPUT_ASR_API` | 一阶段协议（`auto`, `audio-transcriptions`, `chat-completions`） |
| `--asr-effort` | `AGENT_VOICE_INPUT_ASR_EFFORT` | 一阶段思考档位（`auto`, `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`） |
| `--refiner`, `--refiner-provider` | `AGENT_VOICE_INPUT_REFINER_PROVIDER` | 二阶段渠道（`codex`, `dashscope`, `volcengine`, `openrouter`, `gemini`, `openai`, `groq`, `siliconflow`, `openai-compatible`, `off`） |
| `--refiner-model` | `AGENT_VOICE_INPUT_REFINER_MODEL` | 二阶段上游模型 ID |
| `--refiner-effort`, `--effort` | `AGENT_VOICE_INPUT_REFINER_EFFORT` | 二阶段思考档位（`auto`, `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`） |

*（环境变量同时兼容 `VOICE_INPUT_*` 短前缀，以及 `AGENT_VOICE_INPUT_HOTKEY`、`AGENT_VOICE_INPUT_TRIGGER_MODE`、`AGENT_VOICE_INPUT_ONLY_IN_TERMINALS`、`AGENT_VOICE_INPUT_UI_LANGUAGE`、`AGENT_VOICE_INPUT_UI_POSITION`、`AGENT_VOICE_INPUT_MAX_DURATION_SEC`、`AGENT_VOICE_INPUT_MACOS_AUDIO_DEVICE`、`AGENT_VOICE_INPUT_ASR_BASE_URL`、`AGENT_VOICE_INPUT_REFINER_BASE_URL`、`AGENT_VOICE_INPUT_FORCE_REFINE` 等覆盖项。）*

### 可选：多窗口与 `tmux` 会话标题钩子

`tmux` 分屏下的焦点窗格识别开箱即用，无需改动 `~/.tmux.conf`。若常在同一项目目录下并行打开多个 Agent 终端窗口或分屏（如 WSL2、`tmux` 内或不支持暴露分屏 `tty`/`cwd` 的终端），可按 [`integrations/`](integrations/README.zh-CN.md) 为 Codex、Claude Code 或 Antigravity CLI 接入轻量终端/窗格标题标签（`agent-voice-input session-hook`）。

---

## 开发与测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 致谢

本项目早期探索受到 [`Einstellung/voice-asr`](https://github.com/Einstellung/voice-asr) 的启发，尤其是将 `Qwen3-ASR` 与技术术语上下文偏置结合以改善中英混说识别，以及转写前的静音门控与响度归一化思路，特此致谢。

## License

[Apache-2.0 License](LICENSE)
