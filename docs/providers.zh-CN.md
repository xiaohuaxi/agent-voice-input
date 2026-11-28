[English](providers.md) | **简体中文**

# 模型选型、渠道矩阵与配置配方

> **状态图例**：`✅` 真机端到端实测通过 · `🧪` 已适配并覆盖单测/CI，待真机反馈。

## 主流一阶段语音识别（ASR）渠道对比

| 渠道 | 默认模型 | 热词 / 上下文能力 | 核心特点与适用场景 |
| :--- | :--- | :--- | :--- |
| **阿里百炼 (`dashscope`)** `✅` | `qwen3-asr-flash` | **150 词 / 1,800 字**热词 + 近期对话 | 中文与中英混排主力；热词容量宽裕、延迟稳 |
| **OpenRouter (`openrouter`)** `✅` | `microsoft/mai-transcribe-2` | 原生 `keyterms`（**50 词**） | 约 **`$0.10/小时`**；转写稳且原生吃热词，海外与聚合首选 |
| **豆包语音 (`doubao`)** `✅` | `bigmodel` | **50 词 / 1,000 字**热词 + 对话 | 国内低延迟替代；单头 `DOUBAO_SPEECH_API_KEY` 免填 `APP_ID` |
| **OpenAI (`openai`)** `✅` | `gpt-transcribe` *（亦支持 `gpt-4o-transcribe`）* | 原生 `keywords[]`（`gpt-transcribe` 支持 **50 词**） | 纯英文与多语种主力；可同源搭配 `gpt-6-luna` 精修 |
| **DeepSeek (`deepseek`)** `✅` | `deepseek-asr` | 不支持热词 *（依赖二阶段精修）* | 官方 `/v1/tools/asr`，21 语言自动识别；极低成本单 Key 两段式 |
| **Google Gemini (`gemini`)** `✅` | `gemini-3.7-flash` | 二阶段同级 **200 条 / 3,000 字** + 50K 上下文 | 原生**单跳**多模态音频 + 上下文精修直出（自动跳过二阶段） |

## 主流二阶段精修（Refiner）模型梯队

- **第一梯队（推荐 · 精修质量首选）**：**`codex`**（复用本机 Codex CLI 登录，默认 `gpt-6-luna`，绝不开 Fast）、**`agy`**（显式配置复用本机 AGY CLI，默认 `gemini-3.8-flash`）、**`openrouter`**（默认 `google/gemini-3.8-flash`，默认 `effort = "low"`）、**`openai`**（默认 `gpt-6-luna`）与 **`gemini`**（默认 `gemini-3.7-flash`）。这一梯队在口语中途改口消解、中英技术术语还原与长上下文理解上明显强于轻量国产精修模型，追求落字质量时优先选用。
- **第二梯队（经济 / 单 Key 省心备选）**：**`deepseek`**（`deepseek-flash`）、**`dashscope`**（`qwen3.8-flash`）与 **`volcengine`**（`doubao-seed-2-0-mini-260428`）。延迟低、单价极省、国内一把 Key 开箱即用，在复杂口语纠偏与上下文推理上略逊于第一梯队。

## 场景化推荐组合

| 使用场景 | 推荐一阶段（语音识别） | 推荐二阶段（语境精修） | 所需凭据 / 配置 |
| :--- | :--- | :--- | :--- |
| **复用本机 Codex / AGY 登录** *（高质量首选）* | `dashscope` `✅`、`openrouter` `✅`、`doubao` `✅`、`openai` `✅` 或 `deepseek` `✅` | `codex` `✅` *（`auto` 自动复用，绝不开 Fast）* 或 `agy` `✅` *（显式配置启用）* | 1 把语音识别 Key + 本机 `codex` 或 `agy` 登录态 |
| **国内主力（中文 / 中英混排）** | `dashscope`（阿里百炼 `qwen3-asr-flash` `✅`，150 词 / 1,800 字）或 `doubao`（豆包 `bigmodel` `✅`，50 词 / 1,000 字） | 第一梯队：`codex` `✅` / `agy` `✅` / `openrouter` `✅` *（或第二梯队省心：`deepseek` `✅` / `dashscope` `✅`）* | `DASHSCOPE_API_KEY` 或 `DOUBAO_SPEECH_API_KEY` + 所选精修登录态或 Key |
| **海外 / 聚合一把 Key（高质量两段式）** | `openrouter`（`microsoft/mai-transcribe-2` `✅`，约 `$0.10/小时`）或 `openai`（`gpt-transcribe` `✅`） | `openrouter`（`google/gemini-3.8-flash` `✅`）或 `openai`（`gpt-6-luna` `✅`） | 仅需 1 把 Key：`OPENROUTER_API_KEY` 或 `OPENAI_API_KEY` |
| **极简一把 Key（经济两段式 / 原生单跳）** | `deepseek` `✅`（低价两段式）、`dashscope` `✅`（两段式或 `qwen3.8-omni-flash` 单跳）或 `gemini` `✅`（海外原生单跳） | 两段式用同渠道内置默认，或单跳跳过二阶段 | 仅需 1 把 Key：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` 或 `GEMINI_API_KEY` |

> **OpenRouter 选型**：建议在 OpenRouter 使用**两段式**（`microsoft/mai-transcribe-2` + `google/gemini-3.8-flash`）；推荐配方刻意不列 OpenRouter 单跳，以避免 `< 50K` 会话不写显式缓存时单跳音频重复计费偏贵。
>
> **两段式 vs 单跳多模态**：**两段式**组合最灵活（含仅一把 `OPENROUTER_API_KEY` 或 `DEEPSEEK_API_KEY` 的完整两段式）、可复用现有 Codex/AGY 登录获取第一梯队精修质量，长句与复杂术语纠偏最稳；**单跳多模态**（原生 `gemini`、百炼 `qwen3.8-omni-flash` 或官方 OpenAI `gpt-audio-1.5` / `gpt-audio-mini`）只需一把 Key 和一次请求，但会把音频与会话上下文一并直发该多模态渠道。
>
> **本地 / 私有部署模型**：底层已提供通用的 `openai-compatible` 自定义 `base_url` 接口（`🧪`）；经过完整实测的本地/企业私有部署（自部署小型 ASR + 精修）一等公民支持规划在 **`v1.1+`**。

---

## 完整内置渠道矩阵

配置覆盖优先级：`CLI 参数 > ~/.agent-voice-input/config.toml > 内置默认值`（根目录可通过 `AGENT_VOICE_INPUT_HOME` 覆盖）。凭据存放于 `~/.agent-voice-input/.env`（见 [`config.example.env`](../config.example.env)）。

| `provider` | 可用阶段 | 状态 | 内置默认 `asr.model` | 内置默认 `refiner.model` | 凭据来源 / 环境变量 |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **`codex`** | `refiner` | `✅` | — | `gpt-6-luna` *（账号未启用默认模型时自动回退 `gpt-5.4-mini` / `gpt-5-codex-mini`）* | 复用本机 `codex` CLI 登录态 *（无状态 HTTP `/responses` 直连 + 30 分钟提前安全刷新，绝不开 Fast）* |
| **`agy`** | `refiner` | `✅` | — | `gemini-3.8-flash` *（默认 `effort = "low"`；需显式启用，不参与 `auto`）* | 复用本机已登录的 `agy` CLI *（零工具 `agy acp` stdio JSON-RPC + 增量续接前缀缓存）* |
| **`deepseek`** | `asr`, `refiner` | `✅` | `deepseek-asr` *（官方 `POST /v1/tools/asr`；仅支持 `asr.api = "auto"`；21 语种自动识别，无热词/语言参数）* | `deepseek-flash` | `DEEPSEEK_API_KEY` *（兼容 `API_KEY_DEEPSEEK`；一把 Key 零配置跑通两段式）* |
| **`dashscope`** | `asr`, `refiner` | `✅` | `qwen3-asr-flash` *（或单跳 `qwen3.8-omni-flash`）* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *（兼容 `ALI_API_KEY`）* |
| **`doubao`** | `asr` | `✅` | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *（单头 `X-Api-Key` 免 `APP_ID`；兼容旧 `DOUBAO_ASR_API_KEY` 或双头 `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`；`asr.doubao_resource_id = "auto"` 优先同步 `volc.bigasr.auc_turbo`，未开通时自动回退并缓存 `volc.seedasr.auc` / `volc.bigasr.auc`）* |
| **`volcengine`** | `refiner` | `✅` | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *（兼容 `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`）* |
| **`openrouter`** | `asr`, `refiner` | `✅` | `microsoft/mai-transcribe-2` *（约 `$0.10/小时`，原生 `keyterms`）* | `google/gemini-3.8-flash` *（默认 `effort = "low"`）* | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `✅` | `gemini-3.7-flash` *（单跳）* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `✅` | `gpt-transcribe` *（或单跳 `gpt-audio-1.5` / `gpt-audio-mini`）* | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `🧪` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `🧪` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `🧪` | `gpt-4o-mini-transcribe` *（需配 `base_url`）* | *（需显式配 `base_url` + `model`）* | `api_key_env` 或 `OPENAI_API_KEY` *（本机回环地址免 Key）* |
| **`off`** | `refiner` | `✅` | — | *（关闭二阶段，直出一阶段原文）* | 无需凭据 |

### 内置 `auto` 自动选路顺序

- **`asr.provider = "auto"`**（默认）：按 `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow` 顺序选用首个已配置凭据的渠道。
- **`refiner.provider = "auto"`**（默认）：仅当本机 `codex` CLI 为 `chatgpt` 订阅登录态时将其排在最前（绝不开 Fast）；否则按 `openrouter -> deepseek -> dashscope -> gemini -> volcengine -> openai -> groq -> siliconflow` 顺序选用常规 API Key 渠道；若以上均未配置，再回落至 `codex` 的 `keyring` / `api_key` 模式；无可用凭据时降级为 `"off"`。如需固定走 Codex，请显式配置 `refiner.provider = "codex"`。（`agy` 仅通过 `refiner.provider = "agy"` 显式启用。）

---

## 常见 `config.toml` 配方

完整配置模板：[`config.example.toml`](../config.example.toml)

```toml
# 配方 1：高速一阶段转写 + 复用本机 Codex 或 AGY 登录精修
[asr]
provider = "dashscope"           # 亦可改为 "openrouter" / "doubao" / "deepseek" / "openai"

[refiner]
provider = "codex"               # 或显式设为 "agy" 复用 Antigravity CLI
```

```toml
# 配方 2：一把 Key 两段式（OpenRouter 或 DeepSeek，直接使用内置默认模型）
[asr]
provider = "openrouter"          # 默认 microsoft/mai-transcribe-2；亦可改为 "deepseek"（deepseek-asr）

[refiner]
provider = "openrouter"          # 默认 google/gemini-3.8-flash；亦可改为 "deepseek"（deepseek-flash）
```

```toml
# 配方 3：多模态单跳音频 + 50K 缓存上下文直出（自动跳过二阶段）
[asr]
provider = "dashscope"           # 亦可设为 "gemini"（免填 model），或 "openai" + model = "gpt-audio-1.5" / "gpt-audio-mini"
model = "qwen3.8-omni-flash"
```
