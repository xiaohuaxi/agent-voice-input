**English** | [简体中文](providers.zh-CN.md)

# Providers, Models & Configuration Recipes

> **Status Legend**: `✅` Hardware-verified on real devices · `🧪` Implemented & covered by offline tests/CI, seeking field reports.

## Mainstream Stage 1 (ASR) Providers

| Provider | Default Model | Hotwords / Context | Highlights & Best Fit |
| :--- | :--- | :--- | :--- |
| **Alibaba Bailian (`dashscope`)** `✅` | `qwen3-asr-flash` | **150 terms / 1,800 chars** + recent dialogue | Top choice for Chinese & mixed CN/EN dictation; wide hotword capacity and low latency |
| **OpenRouter (`openrouter`)** `✅` | `microsoft/mai-transcribe-2` | Native `keyterms` (**50 terms**) | ~`$0.10/hr` base rate; rock-solid transcription + native keyterms; great global/aggregator default |
| **Doubao (`doubao`)** `✅` | `bigmodel` | **50 terms / 1,000 chars** + dialogue | Fast Mainland China alternative; single-header `DOUBAO_SPEECH_API_KEY` (no `APP_ID` required) |
| **OpenAI (`openai`)** `✅` | `gpt-transcribe` *(also `gpt-4o-transcribe`)* | Native `keywords[]` (**50 terms** on `gpt-transcribe`) | Strong English & multilingual STT; pairs natively with `gpt-6-luna` refinement |
| **DeepSeek (`deepseek`)** `✅` | `deepseek-asr` | None *(rely on Stage 2 refiner)* | Official `/v1/tools/asr` with 21-language auto-detect; ultra-low cost single-key two-stage |
| **Google Gemini (`gemini`)** `✅` | `gemini-3.7-flash` | Stage 2-grade **200 items / 3,000 chars** + 50K context | Native **single-hop** multimodal audio + context refinement (skips Stage 2) |

## Stage 2 (Refiner) Quality Tiers

- **Tier 1 — Recommended (Best Refinement Quality)**: **`codex`** (`gpt-6-luna`, reuses local Codex CLI login, never enables Fast mode), **`agy`** (`gemini-3.8-flash`, explicit opt-in via local AGY CLI), **`openrouter`** (`google/gemini-3.8-flash`, default `effort = "low"`), **`openai`** (`gpt-6-luna`), and **`gemini`** (`gemini-3.7-flash`). These models handle mid-sentence self-corrections, mixed CN/EN technical terminology, and long-session grounding noticeably better than lightweight domestic models—prefer them whenever refinement quality matters most.
- **Tier 2 — Budget / Single-Key Convenience**: **`deepseek`** (`deepseek-flash`), **`dashscope`** (`qwen3.8-flash`), and **`volcengine`** (`doubao-seed-2-0-mini-260428`). Fast and inexpensive for 1-key Mainland China setups, with slightly weaker complex edit accuracy than Tier 1.

## Recommended Combinations by Scenario

| Scenario | Stage 1 (ASR) | Stage 2 (Refiner) | Keys / Setup Needed |
| :--- | :--- | :--- | :--- |
| **Best Quality with Existing Codex / AGY Login** | `dashscope` `✅`, `openrouter` `✅`, `doubao` `✅`, `openai` `✅`, or `deepseek` `✅` | `codex` `✅` *(auto-detected by `auto`; never enables Fast)* or `agy` `✅` *(explicit opt-in)* | 1 Stage 1 speech key + local `codex` or `agy` CLI login |
| **Mainland China Daily Driver (CN / Mixed CN–EN)** | `dashscope` (`qwen3-asr-flash` `✅`, 150 terms / 1,800 chars) or `doubao` (`bigmodel` `✅`, 50 / 1,000) | Tier 1: `codex` `✅` / `agy` `✅` / `openrouter` `✅` *(or Tier 2 budget: `deepseek` `✅` / `dashscope` `✅`)* | `DASHSCOPE_API_KEY` or `DOUBAO_SPEECH_API_KEY` + preferred Stage 2 login/key |
| **Global / Aggregator Single-Key (High Quality)** | `openrouter` (`microsoft/mai-transcribe-2` `✅`, ~$0.10/hr, two-stage) or `openai` (`gpt-transcribe` `✅`) | `openrouter` (`google/gemini-3.8-flash` `✅`) or `openai` (`gpt-6-luna` `✅`) | Just 1 key: `OPENROUTER_API_KEY` or `OPENAI_API_KEY` |
| **Minimal 1-Key Budget or Single-Hop** | `deepseek` `✅` *(budget two-stage)*, `dashscope` `✅` *(two-stage or `qwen3.8-omni-flash` single-hop)*, or `gemini` `✅` *(native single-hop)* | Built-in default for two-stage, or skipped in single-hop | Just 1 key: `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, or `GEMINI_API_KEY` |

> **OpenRouter**: Use OpenRouter in **two-stage** mode (`microsoft/mai-transcribe-2` + `google/gemini-3.8-flash`). Single-hop multimodal chat-audio on OpenRouter is intentionally omitted from recommended recipes because skipping explicit cache writes on `< 50K` sessions makes repeated single-hop audio calls pricier than two-stage.
>
> **Two-stage vs. Single-hop**: **Two-stage** gives the widest provider choice (including 1-key `openrouter` and `deepseek`), lets you reuse existing Codex/AGY logins for top-tier refinement, and handles long technical dictation most reliably. **Single-hop** (`gemini`, DashScope `qwen3.8-omni-flash`, or OpenAI `gpt-audio-1.5` / `gpt-audio-mini`) is simpler with one request and one key, and sends audio plus session context directly to that single multimodal provider.
>
> **Local / self-hosted models**: A generic `openai-compatible` custom `base_url` hook exists (`🧪`); first-class verified support for compact local ASR + refiner setups is planned for **`v1.1+`**.

---

## Complete Built-in Provider Matrix

Configuration priority: `CLI flags > ~/.agent-voice-input/config.toml > built-in defaults` (root overridable via `AGENT_VOICE_INPUT_HOME`). Credentials live in `~/.agent-voice-input/.env` (see [`config.example.env`](../config.example.env)).

| `provider` | Stages | Status | Built-in Default `asr.model` | Built-in Default `refiner.model` | Credential Source / Env |
| :--- | :--- | :---: | :--- | :--- | :--- |
| **`codex`** | `refiner` | `✅` | — | `gpt-6-luna` *(auto-falls back to `gpt-5.4-mini` / `gpt-5-codex-mini` if unavailable on account)* | Local `codex` CLI login *(stateless HTTP `/responses` + 30m safe refresh; never enables Fast mode)* |
| **`agy`** | `refiner` | `✅` | — | `gemini-3.8-flash` *(default `effort = "low"`; explicit opt-in only, never chosen by `auto`)* | Local `agy` CLI login *(zero-tool `agy acp` stdio JSON-RPC with incremental session resume)* |
| **`deepseek`** | `asr`, `refiner` | `✅` | `deepseek-asr` *(official `POST /v1/tools/asr`; `asr.api = "auto"` only; 21-language auto-detect, no hotwords/language params)* | `deepseek-flash` | `DEEPSEEK_API_KEY` *(compat: `API_KEY_DEEPSEEK`; 1 key runs full two-stage)* |
| **`dashscope`** | `asr`, `refiner` | `✅` | `qwen3-asr-flash` *(or single-hop `qwen3.8-omni-flash`)* | `qwen3.8-flash` | `DASHSCOPE_API_KEY` *(compat: `ALI_API_KEY`)* |
| **`doubao`** | `asr` | `✅` | `bigmodel` | — | `DOUBAO_SPEECH_API_KEY` *(single-header `X-Api-Key`, no `APP_ID` needed; compat: `DOUBAO_ASR_API_KEY` or legacy `DOUBAO_ASR_APP_ID` + `DOUBAO_ASR_API_KEY`; `asr.doubao_resource_id = "auto"` prefers sync `volc.bigasr.auc_turbo` with cached fallback to `volc.seedasr.auc` / `volc.bigasr.auc`)* |
| **`volcengine`** | `refiner` | `✅` | — | `doubao-seed-2-0-mini-260428` | `ARK_API_KEY` *(compat: `VOLCENGINE_API_KEY`, `HUOSHAN_API_KEY`)* |
| **`openrouter`** | `asr`, `refiner` | `✅` | `microsoft/mai-transcribe-2` *(~$0.10/hr, native `keyterms`)* | `google/gemini-3.8-flash` *(default `effort = "low"`)* | `OPENROUTER_API_KEY` |
| **`gemini`** | `asr`, `refiner` | `✅` | `gemini-3.7-flash` *(single-hop)* | `gemini-3.7-flash` | `GEMINI_API_KEY` |
| **`openai`** | `asr`, `refiner` | `✅` | `gpt-transcribe` *(or single-hop `gpt-audio-1.5` / `gpt-audio-mini`)* | `gpt-6-luna` | `OPENAI_API_KEY` |
| **`groq`** | `asr`, `refiner` | `🧪` | `whisper-large-v3-turbo` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| **`siliconflow`** | `asr`, `refiner` | `🧪` | `FunAudioLLM/SenseVoiceSmall` | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **`openai-compatible`** | `asr`, `refiner` | `🧪` | `gpt-4o-mini-transcribe` *(needs `base_url`)* | *(needs explicit `base_url` + `model`)* | `api_key_env` or `OPENAI_API_KEY` *(loopback URLs need no key)* |
| **`off`** | `refiner` | `✅` | — | *(Stage 2 disabled; raw Stage 1 output)* | None |

### Built-in `auto` Resolution Order

- **`asr.provider = "auto"`** (default): picks the first configured credential in order `dashscope -> doubao -> deepseek -> openai -> gemini -> groq -> openrouter -> siliconflow`.
- **`refiner.provider = "auto"`** (default): puts local `codex` CLI first **only** when authenticated in `chatgpt` subscription login mode (never enables Fast mode); next picks configured API keys in order `openrouter -> deepseek -> dashscope -> gemini -> volcengine -> openai -> groq -> siliconflow`; then falls back to `codex` in `keyring` / `api_key` mode; otherwise degrades cleanly to `"off"`. Set `refiner.provider = "codex"` explicitly if you always want Codex regardless of other configured keys. (`agy` is explicit opt-in only via `refiner.provider = "agy"`.)

---

## Common `config.toml` Recipes

Full template: [`config.example.toml`](../config.example.toml)

```toml
# Recipe 1: Fast Stage 1 STT + reuse local Codex or AGY CLI login for Stage 2
[asr]
provider = "dashscope"           # Or "openrouter" / "doubao" / "openai" / "deepseek"

[refiner]
provider = "codex"               # Or "agy" for Antigravity CLI login
```

```toml
# Recipe 2: Single-key two-stage via OpenRouter or DeepSeek (uses built-in defaults)
[asr]
provider = "openrouter"          # microsoft/mai-transcribe-2; or "deepseek" (deepseek-asr)

[refiner]
provider = "openrouter"          # google/gemini-3.8-flash; or "deepseek" (deepseek-flash)
```

```toml
# Recipe 3: Single-hop multimodal audio + 50K cached session context (skips Stage 2)
[asr]
provider = "gemini"              # Or "dashscope" + model = "qwen3.8-omni-flash", or "openai" + model = "gpt-audio-1.5" / "gpt-audio-mini"
```
