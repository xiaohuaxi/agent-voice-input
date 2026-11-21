"""Shared cross-stage data contracts and multi-vendor LLM request/response helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from .config import (
    CREDENTIAL_ENV_ORDER,
    GEMINI_DEFAULT_BASE_URL,
    ConfigError,
    VALID_EFFORTS,
    _host_matches_domain,
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


@dataclass
class AsrContext:
    hotwords: list[str] = field(default_factory=list)
    git_status: str = ""
    dialog_text: str = ""
    context_blocks: tuple[str, ...] = ()
    git_block: str = ""
    mode: str = "agent"
    single_hop_prepared: bool = False
    target_hint: str = ""
    hotwords_scope: str = ""
    manual_hotwords_loaded: bool = False


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


def openrouter_cache_breakpoints(provider: str, model: str, n_blocks: int) -> tuple[int, ...]:
    """Return 0-based ``context_blocks`` indices that should carry ``cache_control`` on OpenRouter.

    For ``google/*`` (where OpenRouter converts only the last ``cache_control`` into a single
    ``cachedContent``), ``n_blocks >= 2`` targets the last sealed 50K block ``n_blocks - 2`` so
    appended turns never invalidate the cached prefix, while ``n_blocks == 1`` intentionally
    targets ``0`` so consecutive utterances reuse the current snapshot before the next reply.
    """
    if provider.strip().lower() != "openrouter" or n_blocks <= 0:
        return ()
    m = model.strip().lower()
    sealed = n_blocks - 2 if n_blocks >= 2 else 0
    if m.startswith("google/"):
        return (sealed,)
    if m.startswith("anthropic/"):
        return tuple(sorted({sealed, n_blocks - 1}))
    return ()


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
    "auto": "minimal",
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
    eff = (effort or "auto").strip().lower()
    if eff not in VALID_EFFORTS:
        raise ConfigError(f"effort must be one of {sorted(VALID_EFFORTS)}, got {effort!r}")
    gen_cfg: dict[str, Any] = {"temperature": temperature}
    if eff == "default" or not _gemini_supports_thinking_config(model):
        return gen_cfg
    slug = model.strip().lower().removeprefix("models/").split("/", 1)[-1].split(":", 1)[0]
    if _is_gemini3_plus_model(model):
        lvl = _GEMINI_THINKING_LEVELS[eff]
        supports_minimal = (
            slug.startswith(("gemini-3-flash", "gemini-3.5-flash"))
            or slug == "gemini-flash-latest"
        )
        if lvl == "minimal" and not supports_minimal:
            lvl = "low"
        gen_cfg["thinkingConfig"] = {"thinkingLevel": lvl}
        return gen_cfg
    budget = 0 if eff == "auto" else _GEMINI_THINKING_BUDGETS[eff]
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
    """Return True only for OpenRouter models known to incur internal thinking overhead by default."""
    m = model.strip().lower()
    slug = m.split("/", 1)[-1].split(":", 1)[0]
    if "thinking" in m or "reasoning" in m:
        return True
    if _openrouter_reasoning_mandatory(model) or slug.startswith("gemini-2.5"):
        return True
    if slug.startswith("qwen3") and "instruct" not in slug and "-asr" not in slug and "_asr" not in slug:
        return True
    if slug.startswith(("deepseek-v4", "deepseek-r1", "mimo-v2", "o1", "o3", "o4", "gpt-5", "gpt-6")):
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
    """Return True for OpenAI models that accept ``reasoning_effort`` on ``/chat/completions``.

    Non-reasoning models such as ``gpt-4o-mini`` and ``gpt-audio-mini`` reject
    ``reasoning_effort`` with HTTP 400 on the official OpenAI API; keeping this strict
    ensures ``[refiner] provider = 'openai'`` (where ``refiner.effort`` defaults to ``'low'``
    and ``model`` defaults to ``'gpt-4o-mini'``) never fails with 400.
    """
    slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
    return slug.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6", "codex-mini")) or "reasoning" in slug


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
    eff = (effort or "auto").strip().lower()
    if eff not in VALID_EFFORTS:
        raise ConfigError(f"effort must be one of {sorted(VALID_EFFORTS)}, got {effort!r}")
    is_oai_reasoning = _is_openai_reasoning_model(model)
    if is_oai_reasoning or "reasoning_effort" in payload:
        payload.pop("temperature", None)
    if eff == "default":
        return

    wire_eff = "xhigh" if eff == "max" else eff
    prov = provider.strip().lower()
    if prov == "openrouter":
        if _openrouter_reasoning_mandatory(model):
            payload["reasoning"] = {"effort": "minimal" if eff in ("auto", "none") else wire_eff}
        elif eff == "auto":
            if _is_openrouter_thinking_model(model):
                payload["reasoning"] = {"effort": "none"}
        else:
            payload["reasoning"] = {"effort": wire_eff}
        return

    if prov == "dashscope":
        if _is_dashscope_hybrid_thinking_model(model):
            enable_th = eff not in ("auto", "none", "minimal")
            payload["enable_thinking"] = enable_th
            slug_ds = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
            if enable_th and "omni" in slug_ds and not slug_ds.startswith("qwen3-omni"):
                payload["reasoning_effort"] = wire_eff
        return

    if prov == "volcengine":
        if _is_volcengine_thinking_model(model):
            if eff in ("auto", "none", "minimal"):
                payload["thinking"] = {"type": "disabled"}
            else:
                volc_eff = "high" if wire_eff in ("high", "xhigh") else wire_eff
                payload["thinking"] = {"type": "enabled"}
                if volc_eff in ("minimal", "low", "medium", "high"):
                    payload["reasoning_effort"] = volc_eff
                    payload.pop("temperature", None)
        return

    if prov == "deepseek" or (
        prov == "openai-compatible" and base_url and _host_matches_domain(base_url, "deepseek.com")
    ):
        slug = model.strip().lower().split("/", 1)[-1].split(":", 1)[0]
        if eff in ("auto", "none", "minimal"):
            if "reasoner" not in slug:
                payload["thinking"] = {"type": "disabled"}
        else:
            payload["thinking"] = {"type": "enabled"}
        return

    if prov == "openai":
        if eff not in ("auto", "default", "none") and is_oai_reasoning:
            payload["reasoning_effort"] = wire_eff
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
    envs = CREDENTIAL_ENV_ORDER.get(prov)
    if envs:
        joined = ", ".join(envs)
        return f"missing {prov} API key (set {joined})"
    opt_name = f"{stage}.api_key_env"
    return f"missing {prov} API key (set {opt_name} or OPENAI_API_KEY)"
