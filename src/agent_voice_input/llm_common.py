"""Shared cross-stage data contracts and multi-vendor LLM request/response helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence

from .config import (
    CREDENTIAL_ENV_ORDER,
    GEMINI_DEFAULT_BASE_URL,
    ConfigError,
    VALID_EFFORTS,
    _host_matches_domain,
    validate_effort_value,
)

_GEMINI_BLOCKED_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}
)


@dataclass(frozen=True)
class AsrResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    is_single_hop_refined: bool = False
    retries: int = 0
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    context_chars: int = 0
    context_chunks: int = 0
    audio_tokens: int | None = None
    missed_chunks: int = 0
    request_id: str = ""
    duration_ms: int | None = None


@dataclass
class AsrContext:
    hotwords: list[str] = field(default_factory=list)
    git_status: str = ""
    dialog_text: str = ""
    context_blocks: tuple[str, ...] = ()
    git_block: str = ""
    mode: str = "agent"
    single_hop_prepared: bool = False
    manual_hotwords_loaded: bool = False
    personal_hotwords: tuple[str, ...] = ()
    dynamic_hotwords: tuple[str, ...] = ()
    builtin_hotwords: tuple[str, ...] = ()
    hotwords_bucketed: bool = False


_TokenUsageTuple = tuple[int | None, int | None, int | None, int | None]
_EMPTY_USAGE: _TokenUsageTuple = (None, None, None, None)


def _opt_int(val: Any) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and val.is_integer():
        return int(val)
    return None


def _pick_opt_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for k in keys:
        if k in mapping and (val := _opt_int(mapping.get(k))) is not None:
            return val
    return None


def _extract_token_usage(
    data: Mapping[str, Any], *, default_zero_details: bool = False
) -> _TokenUsageTuple:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return _EMPTY_USAGE
    in_keys = (
        ("input_tokens", "prompt_tokens")
        if default_zero_details
        else ("prompt_tokens", "input_tokens")
    )
    out_keys = (
        ("output_tokens", "completion_tokens")
        if default_zero_details
        else ("completion_tokens", "output_tokens")
    )
    in_tok = _pick_opt_int(usage, *in_keys)
    if in_tok is None and not default_zero_details:
        hit, miss = (
            _opt_int(usage.get("prompt_cache_hit_tokens")),
            _opt_int(usage.get("prompt_cache_miss_tokens")),
        )
        if hit is not None and miss is not None:
            in_tok = hit + miss

    cached_tok: int | None = None
    for dkey in ("input_tokens_details", "prompt_tokens_details"):
        if isinstance(details := usage.get(dkey), Mapping):
            if (
                cached_tok := _pick_opt_int(details, "cached_tokens", "cached_input_tokens")
            ) is not None:
                break
    if cached_tok is None:
        cached_tok = _pick_opt_int(
            usage, "prompt_cache_hit_tokens", "cached_input_tokens", "cached_tokens"
        )
    if default_zero_details and in_tok is not None and cached_tok is None:
        cached_tok = 0

    out_tok = _pick_opt_int(usage, *out_keys)
    rsn_tok: int | None = None
    for dkey in ("output_tokens_details", "completion_tokens_details"):
        if isinstance(details := usage.get(dkey), Mapping):
            if (
                rsn_tok := _pick_opt_int(details, "reasoning_tokens", "reasoning_output_tokens")
            ) is not None:
                break
    if rsn_tok is None:
        rsn_tok = _pick_opt_int(usage, "reasoning_tokens", "reasoning_output_tokens")
    if default_zero_details and out_tok is not None and rsn_tok is None:
        rsn_tok = 0
    return (in_tok, cached_tok, out_tok, rsn_tok)


def _extract_gemini_usage(data: Mapping[str, Any]) -> _TokenUsageTuple:
    um = data.get("usageMetadata")
    if not isinstance(um, Mapping):
        return _EMPTY_USAGE
    in_tok = _pick_opt_int(um, "promptTokenCount", "prompt_token_count")
    cached_tok = _pick_opt_int(um, "cachedContentTokenCount", "cached_content_token_count")
    if in_tok is not None and cached_tok is None:
        cached_tok = 0
    out_tok = _pick_opt_int(um, "candidatesTokenCount", "candidates_token_count")
    rsn_tok = _pick_opt_int(um, "thoughtsTokenCount", "thoughts_token_count")
    return (in_tok, cached_tok, out_tok, rsn_tok)


def _extract_audio_tokens(data: Mapping[str, Any]) -> int | None:
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        return None
    for dkey in ("input_tokens_details", "prompt_tokens_details"):
        if isinstance(details := usage.get(dkey), Mapping):
            if (aud := _pick_opt_int(details, "audio_tokens", "audio_input_tokens")) is not None:
                return aud
    return _pick_opt_int(usage, "audio_tokens", "audio_input_tokens")


def _extract_gemini_audio_tokens(data: Mapping[str, Any]) -> int | None:
    um = data.get("usageMetadata")
    if not isinstance(um, Mapping):
        return None
    details = um.get("promptTokensDetails") or um.get("prompt_tokens_details")
    if not isinstance(details, list):
        return None
    total = 0
    found = False
    for item in details:
        if isinstance(item, Mapping) and str(item.get("modality") or "").strip().upper() == "AUDIO":
            if (cnt := _pick_opt_int(item, "tokenCount", "token_count")) is not None:
                total += cnt
                found = True
    return total if found else None


def openrouter_cache_breakpoints(
    provider: str,
    model: str,
    n_blocks: int = 0,
    *,
    context_blocks: Sequence[str] | None = None,
) -> tuple[int, ...]:
    """Return 0-based ``context_blocks`` indices that should carry ``cache_control`` on OpenRouter.

    For ``google/*`` (where OpenRouter translates the final ``cache_control`` into a synchronous
    explicit Gemini ``cachedContents`` create call), a breakpoint is placed ONLY once at least
    one truly sealed immutable history block exists (``history_blocks >= 2``, landing on
    ``offset + history_blocks - 2``). Deliberately omitting explicit breakpoints when
    ``history_blocks <= 1`` avoids per-turn explicit cache rebuild churn and 20s+ latency spikes
    on actively growing single-block (<50K) sessions.
    """
    if provider.strip().lower() != "openrouter":
        return ()
    if context_blocks is not None:
        total = len(context_blocks)
        offset = (
            1
            if (total > 0 and context_blocks[0].lstrip().startswith("<vocabulary_hints"))
            else 0
        )
    else:
        total = n_blocks
        offset = 0
    if total <= 0:
        return ()
    history_blocks = total - offset
    model_l = model.strip().lower()
    if model_l.startswith("google/"):
        # Deliberately skip explicit cache_control when history_blocks <= 1: on google/*
        # OpenRouter turns it into a synchronous cachedContents create, causing write churn
        # and latency spikes whenever a single growing tail block changes each turn.
        if history_blocks < 2:
            return ()
        raw_bps: tuple[int, ...] = (offset + history_blocks - 2,)
    elif model_l.startswith("anthropic/"):
        if history_blocks == 0:
            sealed = offset - 1
        elif history_blocks == 1:
            sealed = offset
        else:
            sealed = offset + history_blocks - 2
        raw_bps = tuple(sorted({sealed, total - 1}))
    else:
        return ()
    if context_blocks is not None:
        return tuple(idx for idx in raw_bps if context_blocks[idx].strip())
    return raw_bps


_EMPTY_LENTICULAR_RE = re.compile(r"【[ \t]*】")


def strip_lenticular_artifacts(text: str) -> str:
    """Strip empty '【】' and unmatched inverted '】【' artifacts while preserving paired 【...】."""
    if "】" not in text or "【" not in text:
        return text
    while True:
        nxt = _EMPTY_LENTICULAR_RE.sub("", text)
        if nxt == text:
            break
        text = nxt
    if "】" not in text or "【" not in text:
        return text
    stack: list[int] = []
    unmatched: set[int] = set()
    for idx, ch in enumerate(text):
        if ch == "【":
            stack.append(idx)
        elif ch == "】":
            if stack:
                stack.pop()
            else:
                unmatched.add(idx)
    unmatched.update(stack)
    if len(unmatched) < 2:
        return text
    drop: set[int] = set()
    n = len(text)
    for idx in sorted(unmatched):
        if idx in drop or text[idx] != "】":
            continue
        j = idx + 1
        while j < n and text[j] in " \t":
            j += 1
        if j < n and j in unmatched and text[j] == "【":
            drop.add(idx)
            drop.add(j)
    return "".join(ch for i, ch in enumerate(text) if i not in drop) if drop else text


_GEMINI_THINKING_BUDGETS: dict[str, int] = {
    "none": 0,
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 24576,
}

_GEMINI_THINKING_LEVELS: dict[str, str] = {
    "none": "minimal",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _is_gemini3_plus_model(model: str) -> bool:
    slug = model.strip().lower().removeprefix("models/").split("/", 1)[-1].split(":", 1)[0]
    return slug.startswith(("gemini-3", "gemini-4")) or slug in (
        "gemini-flash-latest",
        "gemini-pro-latest",
    )


def _gemini_supports_thinking_config(model: str) -> bool:
    slug = model.strip().lower().removeprefix("models/").split("/", 1)[-1]
    slug = slug.split(":", 1)[0]
    return (
        slug.startswith(("gemini-2.5", "gemini-3", "gemini-4"))
        or slug in ("gemini-flash-latest", "gemini-pro-latest")
        or "thinking" in slug
    )


def build_gemini_generation_config(
    model: str, effort: str, *, temperature: float = 0.1
) -> dict[str, Any]:
    """Build Gemini ``generationConfig`` honoring ``effort`` without breaking non-thinking models."""
    eff = validate_effort_value(effort, "effort", allow_empty=True) or "none"
    gen_cfg: dict[str, Any] = {"temperature": temperature}
    if eff == "default" or not _gemini_supports_thinking_config(model):
        return gen_cfg
    slug = model.strip().lower().removeprefix("models/").split("/", 1)[-1].split(":", 1)[0]
    if _is_gemini3_plus_model(model):
        lvl = _GEMINI_THINKING_LEVELS[eff]
        supports_minimal = slug.startswith(("gemini-3-flash", "gemini-3.5-flash"))
        if lvl == "minimal" and not supports_minimal:
            lvl = "low"
        gen_cfg["thinkingConfig"] = {"thinkingLevel": lvl}
        return gen_cfg
    budget = _GEMINI_THINKING_BUDGETS[eff]
    if budget == 0 and "2.5-pro" in slug:
        budget = 128
    gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
    return gen_cfg


def _openrouter_reasoning_mandatory(model: str) -> bool:
    """Return True for OpenRouter models that reject ``effort='none'`` with HTTP 400."""
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    return slug.startswith(("gemini-3", "gemini-4", "minimax-m3")) or slug in (
        "gemini-flash-latest",
        "gemini-pro-latest",
    )


def _is_openrouter_thinking_model(model: str) -> bool:
    """Return True only for OpenRouter models known to accept/use reasoning parameters."""
    m = model.strip().lower()
    slug = m.split("/", 1)[-1].split(":", 1)[0]
    if "thinking" in m or "reasoning" in m:
        return True
    if _openrouter_reasoning_mandatory(model) or slug.startswith("gemini-2.5"):
        return True
    if slug.startswith("qwen3") and "instruct" not in slug and "-asr" not in slug and "_asr" not in slug:
        return True
    if slug.startswith(
        (
            "deepseek-v3.1",
            "deepseek-v3.2",
            "deepseek-v4",
            "deepseek-r1",
            "mimo-v2",
            "o1",
            "o3",
            "o4",
            "gpt-5",
            "gpt-6",
            "codex-mini",
            "claude-3.7",
            "claude-4",
            "claude-5",
            "claude-sonnet-4",
            "claude-opus-4",
            "claude-haiku-4.5",
            "claude-sonnet-5",
            "claude-opus-5",
            "claude-haiku-5",
        )
    ):
        return True
    return False


def _is_dashscope_hybrid_thinking_model(model: str) -> bool:
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    if "thinking" in slug or "-asr" in slug or "_asr" in slug or "instruct" in slug:
        return False
    return slug.startswith(
        ("qwen3", "qwen-flash", "qwen-plus", "deepseek-v3.1", "deepseek-v3.2", "deepseek-v4")
    )


def _is_volcengine_thinking_model(model: str) -> bool:
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    return slug.startswith(("doubao-seed-", "deepseek-v", "deepseek-r1"))


def _is_openai_reasoning_model(model: str) -> bool:
    """Return True for OpenAI models that accept ``reasoning`` / ``reasoning_effort``.

    Non-reasoning models such as ``gpt-4o-mini`` and ``gpt-audio-mini`` reject
    ``reasoning_effort`` with HTTP 400 on the official OpenAI API; keeping this strict
    ensures non-reasoning overrides under ``[refiner] provider = 'openai'`` (where
    ``refiner.effort`` defaults to ``'low'``) never fail with 400.
    """
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    return slug.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6", "codex-mini")) or "reasoning" in slug


_GPT5_DOT_NONE_RE = re.compile(r"^gpt-5\.[1-9]")
_GPT5_1_RE = re.compile(r"^gpt-5\.1(?:$|-)")
_GPT5_PRO_RE = re.compile(r"^gpt-5-pro(?:$|-)")
_GPT6_LUNA_RE = re.compile(r"^gpt-6(?:\.\d+)?-luna(?:$|-)")


def _openai_min_reasoning_effort(model: str) -> str:
    """Return the lowest reasoning effort accepted by an OpenAI reasoning model."""
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    if _GPT5_PRO_RE.match(slug):
        return "high"
    if slug.startswith("gpt-6"):
        return "none" if _GPT6_LUNA_RE.match(slug) else "low"
    if _GPT5_DOT_NONE_RE.match(slug) and "codex" not in slug and "-pro" not in slug:
        return "none"
    if slug.startswith("gpt-5") and "codex" not in slug and "-pro" not in slug:
        return "minimal"
    return "low"


def _openai_max_reasoning_effort(model: str) -> str:
    """Return the highest reasoning effort accepted by an OpenAI reasoning model."""
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    if (
        slug.startswith("gpt-5")
        and (not _GPT5_DOT_NONE_RE.match(slug) or _GPT5_1_RE.match(slug))
        and "max" not in slug
    ):
        return "high"
    return "max"


def _resolve_openai_reasoning_effort(
    model: str, eff: str, *, allow_max: bool = False
) -> str | None:
    """Resolve OpenAI wire reasoning effort, clamping both floor and ceiling per model."""
    if eff == "default" or not _is_openai_reasoning_model(model):
        return None
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    wire = eff if (allow_max and eff == "max") else ("xhigh" if eff == "max" else eff)
    min_eff = _openai_min_reasoning_effort(model)
    if min_eff == "high":
        return "high"
    if min_eff == "low" and wire in ("none", "minimal"):
        return "low"
    if min_eff == "minimal" and wire == "none":
        return "minimal"
    if wire == "minimal" and _GPT5_1_RE.match(slug):
        return "low"
    max_eff = _openai_max_reasoning_effort(model)
    if max_eff == "high" and wire in ("xhigh", "max"):
        return "high"
    return wire


def _resolve_openrouter_reasoning_effort(model: str, eff: str) -> str | None:
    """Resolve OpenRouter ``reasoning.effort``, avoiding 400s on mandatory-thinking and non-thinking models."""
    if eff == "default":
        return None
    wire = "xhigh" if eff == "max" else eff
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    if _is_openai_reasoning_model(model):
        return _resolve_openai_reasoning_effort(model, eff, allow_max=False)
    if _openrouter_reasoning_mandatory(model):
        if eff in ("none", "minimal") and slug.startswith(("gemini-3", "gemini-4")) and "pro" in slug:
            return "low"
        return "minimal" if eff == "none" else wire
    if slug.startswith(("gemini-2.5-pro", "deepseek-r1")):
        return "minimal" if eff == "none" else wire
    if eff == "none":
        return "none" if _is_openrouter_thinking_model(model) else None
    return wire


def apply_chat_effort_params(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
    effort: str,
    base_url: str = "",
) -> None:
    """Inject provider-safe reasoning/effort parameters into a ``/chat/completions`` payload.

    Also strips non-default ``temperature`` when targeting OpenAI reasoning models (``o1*``,
    ``o3*``, ``o4*``, ``gpt-5*``, ``gpt-6*``) or whenever ``reasoning_effort`` is present,
    preventing HTTP 400 rejections on OpenAI's official API and pass-through proxies.
    """
    eff = validate_effort_value(effort, "effort", allow_empty=True) or "none"
    is_oai_reasoning = _is_openai_reasoning_model(model)
    if is_oai_reasoning or "reasoning_effort" in payload:
        payload.pop("temperature", None)
    if eff == "default":
        return

    wire_eff = "xhigh" if eff == "max" else eff
    prov = provider.strip().lower()
    if prov == "openrouter":
        or_eff = _resolve_openrouter_reasoning_effort(model, eff)
        if or_eff is not None:
            payload["reasoning"] = {"effort": or_eff}
        return

    if prov == "dashscope":
        if _is_dashscope_hybrid_thinking_model(model):
            enable_th = eff not in ("none", "minimal")
            payload["enable_thinking"] = enable_th
            slug_ds = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
            if enable_th and "omni" in slug_ds and not slug_ds.startswith("qwen3-omni"):
                payload["reasoning_effort"] = wire_eff
        return

    if prov == "volcengine":
        if _is_volcengine_thinking_model(model):
            slug_volc = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
            if eff in ("none", "minimal"):
                if not slug_volc.startswith("deepseek-r1"):
                    payload["thinking"] = {"type": "disabled"}
            else:
                volc_eff = "high" if wire_eff in ("high", "xhigh") else wire_eff
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = volc_eff
                payload.pop("temperature", None)
        return

    if prov == "deepseek" or (
        prov == "openai-compatible" and base_url and _host_matches_domain(base_url, "deepseek.com")
    ):
        slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
        if eff in ("none", "minimal"):
            if "reasoner" not in slug:
                payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}
        return

    if prov == "openai":
        oai_eff = _resolve_openai_reasoning_effort(model, eff, allow_max=False)
        if oai_eff is not None:
            payload["reasoning_effort"] = oai_eff
            payload.pop("temperature", None)
        return


def extract_chat_message_text(data: Mapping[str, Any]) -> str:
    """Extract assistant text from a ``/chat/completions`` JSON response (str or content parts)."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("thought") is True or item.get("type") in (
                    "reasoning",
                    "thinking",
                    "thought",
                ):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def extract_openai_compat_error(data: Mapping[str, Any]) -> tuple[Any, str] | None:
    """Detect top-level or choice-level upstream errors (including HTTP 200 mid-stream failures)."""
    raw_err = data.get("error")
    if raw_err:
        msg = (
            str(raw_err.get("message") or raw_err)
            if isinstance(raw_err, Mapping)
            else str(raw_err)
        )
        return raw_err, msg
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        c0 = choices[0]
        c_err = c0.get("error")
        if c_err:
            msg = (
                str(c_err.get("message") or c_err)
                if isinstance(c_err, Mapping)
                else str(c_err)
            )
            return c_err, msg
        if str(c0.get("finish_reason") or "").strip().lower() == "error":
            return {"type": "server_error"}, "upstream generation aborted with finish_reason=error"
    return None


def _missing_key_hint(provider_name: str, *, stage: str = "asr") -> str:
    prov = provider_name.strip().lower()
    cred_key = "doubao_key" if prov == "doubao" else prov
    envs = CREDENTIAL_ENV_ORDER.get(cred_key)
    if envs:
        joined = ", ".join(envs)
        return f"missing {prov} API key (set {joined})"
    opt_name = f"{stage}.api_key_env"
    return f"missing {prov} API key (set {opt_name} or OPENAI_API_KEY)"
