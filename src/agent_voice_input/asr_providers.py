"""Pluggable cloud and local-compatible ASR providers for agent-voice-input.

Implements verified contracts for:
- ``dashscope``: Alibaba Bailian / DashScope dual-route (``compatible-mode`` for ``qwen3-asr-*``
  and native ``multimodal-generation`` for ``qwen-audio-*`` / ``fun-asr-*`` / ``paraformer*``)
- ``doubao``: Volcengine BigModel ``recognize/flash`` + inline-Base64 ``submit``/``query``
  with process-local slot auto-fallback cache and dual/single-header auth
- ``gemini``: native ``:generateContent`` single-hop audio + session context refinement
- ``openrouter`` / ``openai`` / ``groq`` / ``siliconflow`` / ``openai-compatible``:
  dual-protocol routing across dedicated STT (``/audio/transcriptions``) and multimodal
  single-hop chat audio (``/chat/completions`` with anti-hijacking sandwich prompt)
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import random
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.parse
import uuid

from .audio_proc import (
    SINGLE_HOP_MP3_MIN_WAV_BYTES,
    encode_wav_to_mp3_bytes,
    pack_pcm16_mono_wav,
)
from .config import (
    CREDENTIAL_ENV_ORDER,
    DASHSCOPE_COMPAT_BASE_URL,
    DASHSCOPE_NATIVE_MULTIMODAL_URL,
    DOUBAO_DEFAULT_BASE_URL,
    OPENAI_DEFAULT_BASE_URL,
    Config,
    ConfigError,
    CredentialItem,
    ProviderCredentials,
    VALID_EFFORTS,
    _host_matches_domain,
    is_dashscope_native_multimodal_model,
    is_dashscope_omni_chat_model,
    is_gemini_native_url,
    redact_secrets,
    resolve_asr_api,
    resolve_credentials,
    select_asr_provider,
)
from .hotwords import (
    budget_hotwords_for_provider,
    dedupe_hotwords_ci,
    load_hotwords_file as _load_hotwords_file_impl,
)
from .http_transport import (
    CancelToken,
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_USER_AGENT,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    HttpTransportError,
    _KEEPALIVE_POOL,
    _KeepAlivePool,
    _RATE_PACER,
    _SameOriginSafeRedirectHandler,
    _SplitTimeoutHTTPConnection,
    _SplitTimeoutHTTPSConnection,
    _can_reuse_pooled_conn,
    _close_quietly,
    _combine_cancel_check,
    _header_ci,
    _is_doubao_rate_limit,
    _is_loopback_url,
    _register_conn_abort,
    _sleep_cooperative,
    _unregister_conn_abort,
    _validate_request_url,
    classify_http_failure,
    classify_transport_error,
    compute_retry_delay,
    make_rate_pacer_key,
    parse_retry_after,
    reset_rate_limit_pacers,
    sleep_cooperative,
    urllib_http_transport,
)
from .llm_common import (
    AsrContext,
    AsrResult,
    GEMINI_DEFAULT_BASE_URL,
    _EMPTY_USAGE,
    _GEMINI_BLOCKED_FINISH_REASONS,
    _TokenUsageTuple,
    _extract_audio_tokens,
    _extract_gemini_audio_tokens,
    _extract_gemini_usage,
    _extract_token_usage,
    _gemini_supports_thinking_config,
    _is_dashscope_hybrid_thinking_model,
    _is_gemini3_plus_model,
    _is_openai_reasoning_model,
    _is_openrouter_thinking_model,
    _is_volcengine_thinking_model,
    _missing_key_hint,
    _openrouter_reasoning_mandatory,
    _opt_int,
    _pick_opt_int,
    apply_chat_effort_params,
    build_gemini_generation_config,
    extract_chat_message_text,
    extract_openai_compat_error,
    openrouter_cache_breakpoints,
    strip_lenticular_artifacts,
)
from .prompts import (
    SINGLE_HOP_GENERAL_SYSTEM_PROMPT,
    SINGLE_HOP_SYSTEM_PROMPT,
    SINGLE_HOP_TASK_REMINDER,
    format_target_app_hint,
    strip_target_hint_echo,
)

class AsrError(RuntimeError):
    """Redacted ASR provider error (suppresses upstream cause/context chains)."""

    def __init__(
        self,
        message: str,
        *,
        env: Mapping[str, str] | None = None,
        cfg: Config | None = None,
        extra_secrets: Sequence[str] = (),
        retry_kind: str = "never",
        retry_after_sec: float | None = None,
        is_concurrency_limit: bool = False,
    ) -> None:
        safe = redact_secrets(message, env, cfg=cfg, extra_secrets=extra_secrets)
        super().__init__(safe)
        self.retry_kind = retry_kind
        self.retry_after_sec = retry_after_sec
        self.is_concurrency_limit = is_concurrency_limit
        self.__cause__ = None
        self.__context__ = None
        self.__suppress_context__ = True



def load_hotwords_file(
    path_str: str,
    *,
    env: Mapping[str, str] | None = None,
    scene: str = "global",
) -> list[str]:
    """Load optional user hotwords file for ``scene`` (delegates to ``hotwords.load_hotwords_file``)."""
    return _load_hotwords_file_impl(path_str, env=env, scene=scene)


def _derive_context_hotwords_scope(mode: str, target_hint: str, explicit_scope: str = "") -> str:
    s = (explicit_scope or "").strip().lower()
    if s in ("global", "chat", "coding"):
        return s
    hint = (target_hint or "").strip().lower()
    if hint in ("im_chat", "ai_chat"):
        return "chat"
    return "global" if (mode or "agent").strip().lower() == "general" else "coding"


def _coerce_context(
    ctx: AsrContext | Mapping[str, Any] | None,
    hotwords_file: str = "",
    *,
    env: Mapping[str, str] | None = None,
) -> AsrContext:
    has_hw_file = bool(hotwords_file and hotwords_file.strip())
    if ctx is None:
        file_words = load_hotwords_file(hotwords_file, env=env, scene="global") if has_hw_file else []
        return AsrContext(
            hotwords=file_words,
            hotwords_scope="global",
            manual_hotwords_loaded=has_hw_file,
        )
    if isinstance(ctx, AsrContext):
        eff_mode = (ctx.mode or "agent").strip().lower() or "agent"
        eff_hint = (ctx.target_hint or "").strip().lower()
        eff_scope = _derive_context_hotwords_scope(eff_mode, eff_hint, ctx.hotwords_scope)
        if ctx.manual_hotwords_loaded or not has_hw_file:
            merged_words = dedupe_hotwords_ci(ctx.hotwords)
            loaded = bool(ctx.manual_hotwords_loaded)
        else:
            file_words = load_hotwords_file(hotwords_file, env=env, scene=eff_scope)
            merged_words = dedupe_hotwords_ci([*file_words, *ctx.hotwords])
            loaded = True
        return AsrContext(
            hotwords=merged_words,
            git_status=ctx.git_status or "",
            dialog_text=ctx.dialog_text or "",
            context_blocks=tuple(str(b) for b in (ctx.context_blocks or ()) if str(b).strip()),
            git_block=ctx.git_block or "",
            mode=eff_mode,
            single_hop_prepared=bool(ctx.single_hop_prepared),
            target_hint=eff_hint,
            hotwords_scope=eff_scope,
            manual_hotwords_loaded=loaded,
        )
    if isinstance(ctx, Mapping):
        raw_hw = ctx.get("hotwords") or []
        hw_list = [str(w).strip() for w in raw_hw if str(w).strip()]
        eff_mode = str(ctx.get("mode") or "agent").strip().lower() or "agent"
        eff_hint = str(ctx.get("target_hint") or "").strip().lower()
        eff_scope = _derive_context_hotwords_scope(
            eff_mode, eff_hint, str(ctx.get("hotwords_scope") or "")
        )
        preloaded = bool(ctx.get("manual_hotwords_loaded", False))
        if preloaded or not has_hw_file:
            merged_words = dedupe_hotwords_ci(hw_list)
            loaded = preloaded
        else:
            file_words = load_hotwords_file(hotwords_file, env=env, scene=eff_scope)
            merged_words = dedupe_hotwords_ci([*file_words, *hw_list])
            loaded = True
        raw_blocks = ctx.get("context_blocks") or ()
        blk_tuple = (
            (str(raw_blocks),)
            if isinstance(raw_blocks, str) and raw_blocks.strip()
            else tuple(str(b) for b in raw_blocks if str(b).strip())
        )
        return AsrContext(
            hotwords=merged_words,
            git_status=str(ctx.get("git_status") or ""),
            dialog_text=str(ctx.get("dialog_text") or ""),
            context_blocks=blk_tuple,
            git_block=str(ctx.get("git_block") or ""),
            mode=eff_mode,
            single_hop_prepared=bool(ctx.get("single_hop_prepared", False)),
            target_hint=eff_hint,
            hotwords_scope=eff_scope,
            manual_hotwords_loaded=loaded,
        )
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")



def _format_vocabulary_hints(hotwords: Sequence[str]) -> str:
    budget = budget_hotwords_for_provider(hotwords, "single_hop")
    if not budget.kept:
        return ""
    body = ", ".join(budget.kept)
    return f'<vocabulary_hints read_only="true">\n{body}\n</vocabulary_hints>'


_LITERAL_EMPTY_QUOTES = frozenset({'""', "''", "“”", "‘’", "``"})


def _normalize_single_hop_text(raw: str) -> str:
    cleaned = strip_target_hint_echo(strip_lenticular_artifacts(raw)).strip()
    return "" if cleaned in _LITERAL_EMPTY_QUOTES else cleaned


def _prepare_single_hop_audio(wav_bytes: bytes) -> tuple[bytes, str]:
    if len(wav_bytes) >= SINGLE_HOP_MP3_MIN_WAV_BYTES:
        mp3_bytes = encode_wav_to_mp3_bytes(wav_bytes)
        if mp3_bytes is not None:
            return mp3_bytes, "mp3"
    return wav_bytes, "wav"


def effective_single_hop_timeout_sec(configured_sec: float, wav_bytes: bytes) -> float:
    """Scale single-hop timeout for >60s audio while preserving explicit short timeouts."""
    base = max(0.005, float(configured_sec))
    audio_sec = max(0.0, (len(wav_bytes) - 44) / 32000.0)
    if base < 10.0 or audio_sec <= 60.0:
        return base
    return max(base, 30.0 + audio_sec * 0.1)



def _compose_context_text(
    ctx: AsrContext, max_chars: int, *, include_hotwords: bool = True
) -> str:
    """Compose prioritized context text (hotwords -> git status -> dialog tail) within budget."""
    if max_chars <= 0:
        return ""
    sections: list[str] = []
    used = 0

    if include_hotwords and ctx.hotwords:
        hw_prefix = "常用技术术语："
        hw_cap = max(16, max(64, min(420, max_chars // 2)) - len(hw_prefix))
        hw_budget = budget_hotwords_for_provider(ctx.hotwords, "dashscope", max_chars=hw_cap)
        if hw_budget.kept:
            hw_str = hw_prefix + "、".join(hw_budget.kept)
            sections.append(hw_str)
            used += len(hw_str) + 1

    if ctx.git_status.strip() and used < max_chars - 40:
        git_budget = min(600, max_chars - used - 20)
        git_body = ctx.git_status.strip()[:git_budget]
        git_sec = f"工作区变更文件：\n{git_body}"
        sections.append(git_sec)
        used += len(git_sec) + 1

    clean_dialog = strip_lenticular_artifacts(ctx.dialog_text).strip()
    if clean_dialog and used < max_chars - 20:
        prefix = "最近对话参考（仅借用专有名词，勿输出参考中的括号标记）：\n"
        rem = max_chars - used - len(prefix)
        if rem > 0:
            tail = clean_dialog[-rem:]
            sections.append(prefix + tail)

    combined = "\n".join(sections).strip()
    if len(combined) > max_chars:
        combined = combined[:max_chars]
    return combined


def _compose_single_hop_unprepared_context(ctx: AsrContext, max_chars: int = 16000) -> str:
    """Compose direct single-hop context using the 480-char single-hop vocabulary budget without duplication."""
    vocab_blk = _format_vocabulary_hints(ctx.hotwords)
    rem = max(0, max_chars - (len(vocab_blk) + 2 if vocab_blk else 0))
    ctx_body = _compose_context_text(ctx, max_chars=rem, include_hotwords=False)
    return "\n\n".join(filter(None, [vocab_blk, ctx_body]))


def _compose_stt_prompt_text(ctx: AsrContext, max_chars: int = 2000) -> str:
    """Compose Whisper-compatible STT ``prompt`` with high-priority ``hotwords`` at the END.

    Whisper-family ``/audio/transcriptions`` decoders retain only the trailing ~224 tokens
    of ``prompt``; placing ``hotwords`` last prevents background dialog/git text from
    pushing key vocabulary out of the decoder window.
    """
    if max_chars <= 0:
        return ""
    hw_str = ""
    if ctx.hotwords:
        hw_prefix = "常用技术术语："
        hw_cap = max(16, min(360, max(64, max_chars // 2)) - len(hw_prefix))
        hw_budget = budget_hotwords_for_provider(ctx.hotwords, "whisper", max_chars=hw_cap)
        if hw_budget.kept:
            # Select head-first (highest-priority manual terms first), then place reversed at prompt tail
            # so #1 highest-priority term sits at the rightmost end of Whisper's ~224-token tail window.
            hw_str = hw_prefix + "、".join(reversed(hw_budget.kept))

    reserved = (len(hw_str) + 1) if hw_str else 0
    bg_budget = max(0, max_chars - reserved)
    bg_sections: list[str] = []
    used = 0

    if ctx.git_status.strip() and bg_budget >= 40:
        git_cap = min(600, bg_budget - 20)
        if git_cap > 0:
            git_sec = f"工作区变更文件：\n{ctx.git_status.strip()[:git_cap]}"
            bg_sections.append(git_sec)
            used += len(git_sec) + 1

    clean_dialog = strip_lenticular_artifacts(ctx.dialog_text).strip()
    if clean_dialog and used < bg_budget - 20:
        prefix = "最近对话参考：\n"
        rem = bg_budget - used - len(prefix)
        if rem > 0:
            bg_sections.append(prefix + clean_dialog[-rem:])

    sections = [*bg_sections, *([hw_str] if hw_str else [])]
    combined = "\n".join(sections).strip()
    if len(combined) > max_chars:
        combined = combined[-max_chars:]
    return combined



class BaseAsrProvider:
    provider_name: str = "base"
    single_hop: bool = False

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials,
        *,
        env: Mapping[str, str] | None = None,
        transport: HttpTransport | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        rand_fn: Callable[[], float] = random.random,
    ) -> None:
        self.cfg = cfg
        self.creds = creds
        self.env = os.environ if env is None else env
        self.transport = transport or urllib_http_transport
        self.clock_fn = clock_fn
        self.sleep_fn = sleep_fn
        self.rand_fn = rand_fn

    def _fail(
        self,
        msg: str,
        extra_secrets: Sequence[str] = (),
        *,
        retry_kind: str = "never",
        retry_after_sec: float | None = None,
        is_concurrency_limit: bool = False,
    ) -> AsrError:
        secrets = [*self.creds.all_secret_values(), *extra_secrets]
        return AsrError(
            msg,
            env=self.env,
            cfg=self.cfg,
            extra_secrets=secrets,
            retry_kind=retry_kind,
            retry_after_sec=retry_after_sec,
            is_concurrency_limit=is_concurrency_limit,
        )

    def _fail_http(
        self,
        msg: str,
        resp: HttpResponse,
        data: Mapping[str, Any] | None = None,
        extra_secrets: Sequence[str] = (),
        *,
        provider: str | None = None,
        non_json: bool = False,
        api_code: str = "",
        api_msg: str = "",
    ) -> AsrError:
        rkind, rafter, is_conc = classify_http_failure(
            provider or self.provider_name,
            resp.status,
            resp.headers,
            data,
            non_json=non_json,
            api_code=api_code,
            api_msg=api_msg,
        )
        return self._fail(
            msg,
            extra_secrets=extra_secrets,
            retry_kind=rkind,
            retry_after_sec=rafter,
            is_concurrency_limit=is_conc,
        )

    def _safe_excerpt(
        self, raw_text: str, extra_secrets: Sequence[str] = (), *, limit: int = 240
    ) -> str:
        """Redact FULL text first before slicing so credentials spanning ``limit`` never leak."""
        secrets = [*self.creds.all_secret_values(), *extra_secrets]
        redacted = redact_secrets(raw_text, self.env, cfg=self.cfg, extra_secrets=secrets)
        return redacted[:limit]

    def _resolve_custom_or_vendor_key(
        self, vendor_item: CredentialItem | None, missing_hint: str
    ) -> CredentialItem:
        explicit_env = self.cfg.asr.api_key_env.strip()
        if explicit_env:
            if self.creds.asr_custom is None or not self.creds.asr_custom.value:
                raise self._fail(
                    f"configured asr.api_key_env {explicit_env!r} is not set or empty"
                )
            return self.creds.asr_custom
        if vendor_item is None or not vendor_item.value:
            raise self._fail(missing_hint)
        return vendor_item

    def _send(
        self,
        req: HttpRequest,
        extra_secrets: Sequence[str] = (),
        *,
        deadline: float | None = None,
        pacer_key: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> HttpResponse:
        eff_tok = cancel_token if cancel_token is not None else req.cancel_token
        eff_cancelled = _combine_cancel_check(is_cancelled, eff_tok)
        now = self.clock_fn()
        budget_sec = max(float(self.cfg.asr.timeout_sec), float(req.timeout_sec))
        logical_deadline = deadline if deadline is not None else (now + float(req.timeout_sec))
        if eff_cancelled is not None and eff_cancelled():
            raise self._fail(
                "ASR request cancelled",
                extra_secrets=extra_secrets,
                retry_kind="never",
            )
        if now > logical_deadline:
            raise self._fail(
                f"ASR HTTP request timed out before send (budget={budget_sec:.2f}s)",
                extra_secrets=extra_secrets,
                retry_kind="transient",
            )
        try:
            with _RATE_PACER.guard(
                pacer_key,
                deadline=logical_deadline,
                clock_fn=self.clock_fn,
                sleep_fn=self.sleep_fn,
                is_cancelled=eff_cancelled,
                cancel_token=eff_tok,
            ) as pacer_waited:
                if eff_cancelled is not None and eff_cancelled():
                    raise self._fail(
                        "ASR request cancelled",
                        extra_secrets=extra_secrets,
                        retry_kind="never",
                    )
                now_send = self.clock_fn()
                if now_send > logical_deadline:
                    raise self._fail(
                        f"ASR HTTP request timed out before send (budget={budget_sec:.2f}s)",
                        extra_secrets=extra_secrets,
                        retry_kind="transient",
                    )
                eff_req = req
                if eff_tok is not None and eff_req.cancel_token is None:
                    eff_req = replace(eff_req, cancel_token=eff_tok)
                rem_send = max(0.005, logical_deadline - now_send)
                if pacer_waited or rem_send < float(req.timeout_sec) - 0.02:
                    eff_req = replace(
                        eff_req,
                        timeout_sec=min(float(req.timeout_sec), rem_send),
                    )
                if self.clock_fn is time.monotonic and eff_req.deadline is None:
                    eff_req = replace(eff_req, deadline=logical_deadline)
                resp = self.transport(eff_req)
        except AsrError:
            raise
        except Exception as exc:
            if eff_cancelled is not None and eff_cancelled():
                raise self._fail(
                    "ASR request cancelled",
                    extra_secrets=extra_secrets,
                    retry_kind="never",
                ) from None
            rkind = classify_transport_error(exc)
            raise self._fail(
                str(exc), extra_secrets=extra_secrets, retry_kind=rkind
            ) from None

        if eff_cancelled is not None and eff_cancelled():
            raise self._fail(
                "ASR request cancelled",
                extra_secrets=extra_secrets,
                retry_kind="never",
            )
        if self.clock_fn() > logical_deadline + 1e-9:
            raise self._fail(
                f"ASR HTTP response exceeded timeout budget ({budget_sec:.2f}s)",
                extra_secrets=extra_secrets,
                retry_kind="transient",
            )
        return resp

    def _maybe_backoff_before_retry(
        self,
        err: AsrError,
        *,
        retries_used: int,
        max_retries: int,
        deadline: float,
        pacer_key: str,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> bool:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        if err.retry_kind not in ("rate_limit", "transient"):
            return False
        now = self.clock_fn()
        if err.retry_kind == "rate_limit":
            _RATE_PACER.note_rate_limit(
                pacer_key,
                now=now,
                retry_after_sec=err.retry_after_sec,
                is_concurrency=err.is_concurrency_limit,
            )
        if retries_used >= max_retries:
            return False
        if eff_cancelled is not None and eff_cancelled():
            return False
        rem = deadline - now
        if rem <= 0.02:
            return False
        if err.retry_after_sec is not None and err.retry_after_sec >= rem:
            return False
        delay = compute_retry_delay(
            err.retry_kind,
            retries_used + 1,
            retry_after_sec=err.retry_after_sec,
            rand_fn=self.rand_fn,
        )
        if delay >= rem - 0.01:
            return False
        _sleep_cooperative(
            delay,
            sleep_fn=self.sleep_fn,
            is_cancelled=eff_cancelled,
            cancel_token=cancel_token,
        )
        if eff_cancelled is not None and eff_cancelled():
            return False
        if (deadline - self.clock_fn()) <= 0:
            return False
        return True

    def _execute_with_retry(
        self,
        *,
        deadline: float,
        pacer_key: str,
        is_cancelled: Callable[[], bool] | None,
        attempt_fn: Callable[[float], str],
        budget_sec: float | None = None,
        cancel_token: CancelToken | None = None,
    ) -> tuple[str, int, bool]:
        """Run ``attempt_fn(rem_sec)`` with bounded retries; returns ``(text, retries_used, cancelled)``."""
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        max_retries = max(0, int(self.cfg.asr.max_retries))
        eff_budget = float(self.cfg.asr.timeout_sec) if budget_sec is None else float(budget_sec)
        retries_used = 0
        while True:
            if eff_cancelled is not None and eff_cancelled():
                return "", retries_used, True
            raw_rem = deadline - self.clock_fn()
            rem = (
                eff_budget
                if (retries_used == 0 and raw_rem >= eff_budget - 0.02)
                else max(0.005, raw_rem)
            )
            try:
                out = attempt_fn(rem)
                if eff_cancelled is not None and eff_cancelled():
                    return "", retries_used, True
                return out, retries_used, False
            except AsrError as err:
                if eff_cancelled is not None and eff_cancelled():
                    return "", retries_used, True
                if not self._maybe_backoff_before_retry(
                    err,
                    retries_used=retries_used,
                    max_retries=max_retries,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=eff_cancelled,
                    cancel_token=cancel_token,
                ):
                    if eff_cancelled is not None and eff_cancelled():
                        return "", retries_used, True
                    raise
                retries_used += 1

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Alibaba Bailian / DashScope (dashscope)
# ---------------------------------------------------------------------------

DASHSCOPE_COMPAT_DEFAULT_URL = f"{DASHSCOPE_COMPAT_BASE_URL}/chat/completions"
DASHSCOPE_NATIVE_DEFAULT_URL = DASHSCOPE_NATIVE_MULTIMODAL_URL


def _resolve_dashscope_url(base_url: str, use_native: bool) -> str:
    b = base_url.strip().rstrip("/")
    if not b:
        return DASHSCOPE_NATIVE_DEFAULT_URL if use_native else DASHSCOPE_COMPAT_DEFAULT_URL
    if use_native:
        if b.endswith("/generation"):
            return b
        if "/compatible-mode" in b:
            host_root = b.split("/compatible-mode", 1)[0]
            return f"{host_root}/api/v1/services/aigc/multimodal-generation/generation"
        if b.endswith("/api/v1"):
            return f"{b}/services/aigc/multimodal-generation/generation"
        return f"{b}/api/v1/services/aigc/multimodal-generation/generation"
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/compatible-mode/v1"):
        return f"{b}/chat/completions"
    if b.endswith("/api/v1"):
        host_root = b[: -len("/api/v1")]
        return f"{host_root}/compatible-mode/v1/chat/completions"
    if b.endswith("/v1"):
        return f"{b}/chat/completions"
    return f"{b}/compatible-mode/v1/chat/completions"


class DashScopeAsrProvider(BaseAsrProvider):
    provider_name = "dashscope"

    def _require_key(self) -> CredentialItem:
        return self._resolve_custom_or_vendor_key(
            self.creds.dashscope,
            "missing DashScope API key (set DASHSCOPE_API_KEY, ALI_API_KEY, or API_KEY_ALI)",
        )

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        model = self.cfg.asr.effective_model("dashscope")
        if not wav_bytes:
            return AsrResult(
                text="",
                provider=self.provider_name,
                model=model,
                latency_ms=0,
                is_single_hop_refined=False,
            )
        cred = self._require_key()
        ctx = _coerce_context(context, self.cfg.asr.hotwords_file, env=self.env)
        ctx_text = _compose_context_text(ctx, max_chars=2500)
        b64_wav = base64.b64encode(wav_bytes).decode("ascii")
        data_uri = f"data:audio/wav;base64,{b64_wav}"

        use_native = is_dashscope_native_multimodal_model(model, self.cfg.asr.base_url)
        url = _resolve_dashscope_url(self.cfg.asr.base_url, use_native=use_native)
        headers = {
            "Authorization": f"Bearer {cred.value}",
            "Content-Type": "application/json",
        }
        pacer_key = make_rate_pacer_key(self.provider_name, url, cred.value)

        eff_timeout = float(self.cfg.asr.timeout_sec)
        t0 = self.clock_fn()
        deadline = t0 + eff_timeout
        route_fn = self._call_native_route if use_native else self._call_compat_route
        raw_text, retries_used, cancelled = self._execute_with_retry(
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=eff_cancelled,
            cancel_token=cancel_token,
            attempt_fn=lambda rem: route_fn(
                url,
                headers,
                model,
                data_uri,
                ctx_text,
                cred.value,
                timeout_sec=rem,
                deadline=deadline,
                pacer_key=pacer_key,
                is_cancelled=eff_cancelled,
                cancel_token=cancel_token,
            ),
        )
        text = "" if cancelled else strip_lenticular_artifacts(raw_text).strip()
        latency_ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
        return AsrResult(
            text=text,
            provider=self.provider_name,
            model=model,
            latency_ms=latency_ms,
            is_single_hop_refined=False,
            retries=retries_used,
        )

    def _call_compat_route(
        self,
        url: str,
        headers: Mapping[str, str],
        model: str,
        data_uri: str,
        ctx_text: str,
        secret: str,
        *,
        timeout_sec: float | None = None,
        deadline: float | None = None,
        pacer_key: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if ctx_text:
            messages.append(
                {
                    "role": "system",
                    "content": [{"type": "text", "text": ctx_text}],
                }
            )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": data_uri},
                    }
                ],
            }
        )
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "asr_options": {"enable_itn": True},
        }
        resp = self._send(
            HttpRequest(
                url=url,
                method="POST",
                headers=headers,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout_sec=self.cfg.asr.timeout_sec if timeout_sec is None else timeout_sec,
                cancel_token=cancel_token,
            ),
            extra_secrets=(secret,),
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
        )
        try:
            data = resp.json()
        except ValueError:
            excerpt = self._safe_excerpt(resp.text(), (secret,))
            raise self._fail_http(
                f"DashScope compatible-mode returned non-JSON (HTTP {resp.status}): {excerpt}",
                resp,
                extra_secrets=(secret,),
                non_json=True,
            ) from None

        compat_err = extract_openai_compat_error(data)
        if resp.status != 200 or compat_err is not None:
            raw_err = compat_err[0] if compat_err is not None else data.get("error")
            err_msg = (
                compat_err[1]
                if compat_err is not None
                else (
                    str(raw_err.get("message") or data)
                    if isinstance(raw_err, dict)
                    else str(raw_err or data)
                )
            )
            err_code = (
                str(raw_err.get("code") or "")
                if isinstance(raw_err, Mapping)
                else str(data.get("code") or "")
            )
            if err_code == "ASR_RESPONSE_HAVE_NO_WORDS" or "ASR_RESPONSE_HAVE_NO_WORDS" in err_msg:
                return ""
            excerpt = self._safe_excerpt(err_msg, (secret,))
            raise self._fail_http(
                f"DashScope compatible-mode HTTP {resp.status}: {excerpt}",
                resp,
                data,
                extra_secrets=(secret,),
                api_code=err_code,
                api_msg=err_msg,
            )

        return extract_chat_message_text(data)

    def _call_native_route(
        self,
        url: str,
        headers: Mapping[str, str],
        model: str,
        data_uri: str,
        ctx_text: str,
        secret: str,
        *,
        timeout_sec: float | None = None,
        deadline: float | None = None,
        pacer_key: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if ctx_text:
            messages.append({"role": "system", "content": [{"text": ctx_text}]})
        messages.append({"role": "user", "content": [{"audio": data_uri}]})
        payload = {
            "model": model,
            "input": {"messages": messages},
            "parameters": {
                "format": "wav",
                "sample_rate": 16000,
                "asr_options": {"enable_itn": True},
            },
        }
        resp = self._send(
            HttpRequest(
                url=url,
                method="POST",
                headers=headers,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout_sec=self.cfg.asr.timeout_sec if timeout_sec is None else timeout_sec,
                cancel_token=cancel_token,
            ),
            extra_secrets=(secret,),
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
        )
        try:
            data = resp.json()
        except ValueError:
            excerpt = self._safe_excerpt(resp.text(), (secret,))
            raise self._fail_http(
                f"DashScope multimodal-generation returned non-JSON (HTTP {resp.status}): {excerpt}",
                resp,
                extra_secrets=(secret,),
                non_json=True,
            ) from None

        code = str(data.get("code") or "")
        message = str(data.get("message") or "")
        if "ASR_RESPONSE_HAVE_NO_WORDS" in message or code == "ASR_RESPONSE_HAVE_NO_WORDS":
            return ""

        if resp.status != 200 or (code and not data.get("output")):
            excerpt = self._safe_excerpt(message or str(data), (secret,))
            raise self._fail_http(
                f"DashScope multimodal-generation HTTP {resp.status} ({code}): {excerpt}",
                resp,
                data,
                extra_secrets=(secret,),
                api_code=code,
                api_msg=message,
            )

        output = data.get("output")
        if not isinstance(output, dict):
            return ""
        if isinstance(output.get("text"), str):
            return output["text"].strip()
        return extract_chat_message_text(output)


# ---------------------------------------------------------------------------
# 2. Volcengine Doubao BigModel ASR (doubao)
# ---------------------------------------------------------------------------

DOUBAO_AUTO_SLOT_CHAIN: tuple[tuple[str, str], ...] = (
    ("flash", "volc.bigasr.auc_turbo"),
    ("submit", "volc.seedasr.auc"),
    ("submit", "volc.bigasr.auc"),
)


def _doubao_poll_wait_sec(poll_elapsed: float) -> float:
    """Return adaptive /query poll interval (0.30s -> 0.50s -> 0.80s) to trim redundant 20000001 calls."""
    if poll_elapsed > 6.0:
        return 0.80
    if poll_elapsed > 2.0:
        return 0.50
    return 0.30

_DOUBAO_SLOT_LOCK = threading.Lock()
_DOUBAO_SLOT_CACHE: dict[str, tuple[str, str]] = {}


def reset_doubao_slot_cache() -> None:
    """Clear process-local Doubao slot cache (used in unit tests)."""
    with _DOUBAO_SLOT_LOCK:
        _DOUBAO_SLOT_CACHE.clear()


def get_cached_doubao_slot(cache_key: str) -> tuple[str, str] | None:
    with _DOUBAO_SLOT_LOCK:
        return _DOUBAO_SLOT_CACHE.get(cache_key)


class _DoubaoSlotUnavailableError(Exception):
    """Internal signal that a candidate resource_id is not granted or not allowed on endpoint."""

    def __init__(self, status_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _resolve_doubao_base_url(raw_base: str) -> str:
    b = raw_base.strip().rstrip("/")
    if not b:
        return DOUBAO_DEFAULT_BASE_URL
    parsed = urllib.parse.urlparse(b)
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        return f"{b}/api/v3/auc/bigmodel"
    return b


def _build_doubao_corpus_context(ctx: AsrContext) -> str | None:
    """Build serialized Doubao corpus.context strictly within 30 words/180 chars + 600 dialog chars."""
    hw_budget = budget_hotwords_for_provider(ctx.hotwords, "doubao")
    hw_objs = [{"word": w} for w in hw_budget.kept]

    dialog_parts: list[str] = []
    if ctx.git_status.strip():
        dialog_parts.append(ctx.git_status.strip()[:200])
    if ctx.dialog_text.strip():
        dialog_parts.append(strip_lenticular_artifacts(ctx.dialog_text).strip())
    combined_dialog = "\n".join(dialog_parts).strip()
    if len(combined_dialog) > 600:
        combined_dialog = combined_dialog[-600:]
    ctx_data = [{"text": combined_dialog}] if combined_dialog else []

    if not hw_objs and not ctx_data:
        return None

    corpus_obj: dict[str, Any] = {
        "hotwords": hw_objs,
        "context_type": "dialog_ctx",
        "context_data": ctx_data,
    }
    return json.dumps(corpus_obj, ensure_ascii=False)


class DoubaoAsrProvider(BaseAsrProvider):
    provider_name = "doubao"

    def _resolve_auth(self) -> tuple[str | None, str, tuple[str, ...]]:
        key_item = self._resolve_custom_or_vendor_key(
            self.creds.doubao_key,
            "missing Doubao Speech API key (set DOUBAO_SPEECH_API_KEY; legacy aliases: DOUBAO_ASR_API_KEY / API_KEY_DOUBAO_ASR)",
        )
        # Canonical DOUBAO_SPEECH_API_KEY uses single-header X-Api-Key auth; ignore any
        # lingering legacy DOUBAO_ASR_APP_ID / APP_ID_DOUBAO_ASR to prevent 401/403 mismatches.
        if key_item.source_env.strip().upper() == "DOUBAO_SPEECH_API_KEY":
            app_id = None
        else:
            app_id = self.creds.doubao_app_id.value if self.creds.doubao_app_id is not None else None
        secrets: tuple[str, ...] = (
            (app_id, key_item.value) if app_id else (key_item.value,)
        )
        return app_id, key_item.value, secrets

    @staticmethod
    def _auth_headers(app_id: str | None, api_key: str) -> dict[str, str]:
        if app_id:
            return {
                "X-Api-App-Key": app_id,
                "X-Api-Access-Key": api_key,
            }
        return {"X-Api-Key": api_key}

    @staticmethod
    def _make_cache_key(base_url: str, app_id: str | None, api_key: str) -> str:
        digest = hashlib.sha256(f"{base_url}|{app_id or ''}|{api_key}".encode("utf-8")).hexdigest()
        return digest[:24]

    def _build_candidate_slots(self, cache_key: str) -> tuple[bool, list[tuple[str, str]]]:
        rid = (self.cfg.asr.resource_id or "auto").strip()
        explicit_mode = (self.cfg.asr.doubao_mode or "auto").strip().lower()

        if rid.lower() == "auto" or not rid:
            cached = get_cached_doubao_slot(cache_key)
            chain = list(DOUBAO_AUTO_SLOT_CHAIN)
            if explicit_mode in ("flash", "submit"):
                filtered = [slot for slot in chain if slot[0] == explicit_mode]
                if filtered:
                    chain = filtered
            if cached is not None and cached in chain:
                chain = [cached, *[slot for slot in chain if slot != cached]]
            return True, chain

        if explicit_mode in ("flash", "submit"):
            mode = explicit_mode
        else:
            mode = "flash" if rid.endswith("_turbo") else "submit"

        slots = [(mode, rid)]
        if mode == "flash" and rid == "volc.seedasr.auc_turbo":
            slots.append(("submit", "volc.seedasr.auc"))
        return False, slots

    def _remaining_timeout(
        self,
        deadline: float,
        rid: str,
        stage: str,
        secrets: Sequence[str],
        *,
        budget_sec: float | None = None,
    ) -> float:
        rem = deadline - self.clock_fn()
        if rem <= 0:
            eff_budget = float(self.cfg.asr.timeout_sec) if budget_sec is None else budget_sec
            raise self._fail(
                f"Doubao {stage} ({rid}) timed out (budget={eff_budget:.2f}s)",
                extra_secrets=secrets,
                retry_kind="transient",
            )
        return rem

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        model = self.cfg.asr.effective_model("doubao")
        if not wav_bytes:
            return AsrResult(text="", provider=self.provider_name, model=model, latency_ms=0)

        app_id, api_key, secrets = self._resolve_auth()
        base_url = _resolve_doubao_base_url(self.cfg.asr.base_url)
        cache_key = self._make_cache_key(base_url, app_id, api_key)
        pacer_key = make_rate_pacer_key(self.provider_name, base_url, f"{app_id or ''}|{api_key}")
        is_auto_slot, candidates = self._build_candidate_slots(cache_key)

        ctx = _coerce_context(context, self.cfg.asr.hotwords_file, env=self.env)
        corpus_json = _build_doubao_corpus_context(ctx)
        b64_wav = base64.b64encode(wav_bytes).decode("ascii")

        req_block: dict[str, Any] = {
            "model_name": model or "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
        }
        if corpus_json is not None:
            req_block["corpus"] = {"context": corpus_json}

        payload = {
            "user": {"uid": "agent-voice-input"},
            "audio": {
                "data": b64_wav,
                "format": "wav",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": req_block,
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        auth_hdrs = self._auth_headers(app_id, api_key)

        eff_timeout = float(self.cfg.asr.timeout_sec)
        t0 = self.clock_fn()
        deadline = t0 + eff_timeout
        slot_errors: list[str] = []
        retries_box = [0]

        for mode, rid in candidates:
            if eff_cancelled is not None and eff_cancelled():
                return AsrResult(
                    text="",
                    provider=self.provider_name,
                    model=model,
                    latency_ms=max(0, int(round((self.clock_fn() - t0) * 1000))),
                    retries=retries_box[0],
                )
            try:
                if mode == "flash":
                    text, cancelled = self._run_flash(
                        base_url,
                        auth_hdrs,
                        rid,
                        body_bytes,
                        deadline,
                        secrets,
                        budget_sec=eff_timeout,
                        pacer_key=pacer_key,
                        retries_box=retries_box,
                        is_cancelled=eff_cancelled,
                        cancel_token=cancel_token,
                    )
                else:
                    text, cancelled = self._run_submit_and_query(
                        base_url,
                        auth_hdrs,
                        rid,
                        body_bytes,
                        t0,
                        deadline,
                        secrets,
                        budget_sec=eff_timeout,
                        pacer_key=pacer_key,
                        retries_box=retries_box,
                        is_cancelled=eff_cancelled,
                        cancel_token=cancel_token,
                    )
                if not cancelled and is_auto_slot:
                    with _DOUBAO_SLOT_LOCK:
                        _DOUBAO_SLOT_CACHE[cache_key] = (mode, rid)
                latency_ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
                return AsrResult(
                    text="" if cancelled else strip_lenticular_artifacts(text).strip(),
                    provider=self.provider_name,
                    model=model,
                    latency_ms=latency_ms,
                    is_single_hop_refined=False,
                    retries=retries_box[0],
                )
            except _DoubaoSlotUnavailableError as slot_exc:
                with _DOUBAO_SLOT_LOCK:
                    if _DOUBAO_SLOT_CACHE.get(cache_key) == (mode, rid):
                        _DOUBAO_SLOT_CACHE.pop(cache_key, None)
                slot_errors.append(f"{rid}({mode}): {slot_exc.status_code} {slot_exc.detail}")
                continue

        raise self._fail(
            "Doubao ASR: no granted/allowed resource slot available: " + "; ".join(slot_errors),
            extra_secrets=secrets,
        )

    def probe_ping(self, *, timeout_sec: float = 5.0) -> tuple[str, int, str]:
        """Lightweight online connectivity, auth, and slot probe for Doubao BigModel ASR (1.0/2.0)."""
        t0 = self.clock_fn()
        budget = max(0.5, float(timeout_sec))
        deadline = t0 + budget
        secrets: tuple[str, ...] = ()
        try:
            app_id, api_key, secrets = self._resolve_auth()
            base_url = _resolve_doubao_base_url(self.cfg.asr.base_url)
            cache_key = self._make_cache_key(base_url, app_id, api_key)
            _, candidates = self._build_candidate_slots(cache_key)
            auth_hdrs = self._auth_headers(app_id, api_key)
            silent_wav = pack_pcm16_mono_wav([0] * 1600, sample_rate=16000)
            payload = {
                "user": {"uid": "agent-voice-input"},
                "audio": {
                    "data": base64.b64encode(silent_wav).decode("ascii"),
                    "format": "wav",
                    "rate": 16000,
                    "bits": 16,
                    "channel": 1,
                },
                "request": {
                    "model_name": self.cfg.asr.effective_model("doubao") or "bigmodel",
                },
            }
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            slot_errors: list[str] = []
            for mode, rid in candidates:
                rem = deadline - self.clock_fn()
                if rem <= 0.05:
                    ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
                    return "FAIL", ms, f"FAIL ({ms}ms: Doubao ping exceeded {budget:.1f}s deadline)"
                endpoint = f"{base_url}/recognize/flash" if mode == "flash" else f"{base_url}/submit"
                headers = {
                    **auth_hdrs,
                    "Content-Type": "application/json",
                    "X-Api-Resource-Id": rid,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                    "X-Api-Sequence": "-1",
                }
                resp = self.transport(
                    HttpRequest(
                        url=endpoint,
                        method="POST",
                        headers=headers,
                        body=body_bytes,
                        timeout_sec=rem,
                        connect_timeout_sec=rem,
                    )
                )
                ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                api_code, api_msg = self._extract_doubao_status(resp, data, secrets)
                if 200 <= resp.status < 300:
                    is_ok = api_code in ("20000000", "20000003") or (
                        mode == "flash"
                        and not api_code
                        and isinstance(data, Mapping)
                        and "result" in data
                    )
                    if is_ok:
                        return "OK", ms, f"OK ({ms}ms, slot={rid}/{mode})"
                if self._is_recoverable_slot_error(resp.status, api_code, api_msg):
                    slot_errors.append(f"{rid}({mode}): {api_code or resp.status}")
                    continue
                if resp.status in (401, 403):
                    auth_hint = (
                        "DOUBAO_ASR_APP_ID / DOUBAO_ASR_API_KEY"
                        if app_id
                        else "DOUBAO_SPEECH_API_KEY"
                    )
                    return (
                        "FAIL",
                        ms,
                        f"FAIL (HTTP {resp.status} unauthorized, {ms}ms; check {auth_hint})",
                    )
                detail = api_msg or "unexpected status"
                return (
                    "FAIL",
                    ms,
                    f"FAIL (HTTP {resp.status} code={api_code or '-'}, {ms}ms: {detail[:140]})",
                )
            ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
            return (
                "FAIL",
                ms,
                f"FAIL ({ms}ms: no granted Doubao ASR slot; tried {', '.join(slot_errors)})",
            )
        except Exception as exc:
            ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
            safe = self._safe_excerpt(str(exc), secrets, limit=160)
            return "FAIL", ms, f"FAIL ({ms}ms: {safe})"

    def _extract_doubao_status(
        self, resp: HttpResponse, data: Mapping[str, Any], secrets: Sequence[str]
    ) -> tuple[str, str]:
        api_code = _header_ci(resp.headers, "x-api-status-code")
        api_msg = _header_ci(resp.headers, "x-api-message")
        header_obj = data.get("header") if isinstance(data.get("header"), dict) else {}
        if not api_code:
            api_code = str(header_obj.get("code") or data.get("code") or "").strip()
        if not api_msg:
            raw_msg = str(header_obj.get("message") or data.get("message") or resp.text()).strip()
            api_msg = self._safe_excerpt(raw_msg, secrets, limit=200)
        else:
            api_msg = self._safe_excerpt(api_msg, secrets, limit=200)
        return api_code, api_msg

    @staticmethod
    def _is_recoverable_slot_error(http_status: int, api_code: str, api_msg: str) -> bool:
        if api_code == "45000030":
            return True
        low = api_msg.lower()
        if api_code == "45000000" and (
            not low
            or "resource" in low
            or "not allowed" in low
            or "not granted" in low
            or "permission" in low
        ):
            return True
        if http_status == 403 and ("not granted" in low or "resource" in low):
            return True
        if http_status == 400 and "resourceid" in low and "not allowed" in low:
            return True
        return False

    def _run_flash(
        self,
        base_url: str,
        auth_hdrs: Mapping[str, str],
        rid: str,
        body_bytes: bytes,
        deadline: float,
        secrets: Sequence[str],
        *,
        budget_sec: float | None = None,
        pacer_key: str = "",
        retries_box: list[int] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> tuple[str, bool]:
        max_retries = max(0, int(self.cfg.asr.max_retries))
        rbox = retries_box if retries_box is not None else [0]
        while True:
            if is_cancelled is not None and is_cancelled():
                return "", True
            try:
                rem = self._remaining_timeout(deadline, rid, "flash", secrets, budget_sec=budget_sec)
                req_id = str(uuid.uuid4())
                headers = {
                    **auth_hdrs,
                    "Content-Type": "application/json",
                    "X-Api-Resource-Id": rid,
                    "X-Api-Request-Id": req_id,
                    "X-Api-Sequence": "-1",
                }
                resp = self._send(
                    HttpRequest(
                        url=f"{base_url}/recognize/flash",
                        method="POST",
                        headers=headers,
                        body=body_bytes,
                        timeout_sec=rem,
                        cancel_token=cancel_token,
                    ),
                    extra_secrets=secrets,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                )
                non_json = False
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                    non_json = bool(resp.body.strip())
                api_code, api_msg = self._extract_doubao_status(resp, data, secrets)
                if api_code == "20000003":
                    return "", False
                if (
                    resp.status == 200
                    and not non_json
                    and (api_code == "20000000" or (not api_code and "result" in data))
                ):
                    res = data.get("result")
                    return (
                        str(res.get("text") or "").strip() if isinstance(res, dict) else ""
                    ), False
                is_rl, _ = _is_doubao_rate_limit(resp.status, api_code, api_msg)
                if not is_rl and self._is_recoverable_slot_error(resp.status, api_code, api_msg):
                    raise _DoubaoSlotUnavailableError(api_code or str(resp.status), api_msg)
                raise self._fail_http(
                    f"Doubao flash ({rid}) failed HTTP {resp.status} code={api_code}: {api_msg}",
                    resp,
                    data,
                    extra_secrets=secrets,
                    provider="doubao",
                    non_json=non_json and not api_code,
                    api_code=api_code,
                    api_msg=api_msg,
                )
            except AsrError as err:
                if is_cancelled is not None and is_cancelled():
                    return "", True
                if not self._maybe_backoff_before_retry(
                    err,
                    retries_used=rbox[0],
                    max_retries=max_retries,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                ):
                    if is_cancelled is not None and is_cancelled():
                        return "", True
                    raise
                rbox[0] += 1

    def _run_submit_and_query(
        self,
        base_url: str,
        auth_hdrs: Mapping[str, str],
        rid: str,
        body_bytes: bytes,
        t0: float,
        deadline: float,
        secrets: Sequence[str],
        *,
        budget_sec: float | None = None,
        pacer_key: str = "",
        retries_box: list[int] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> tuple[str, bool]:
        max_retries = max(0, int(self.cfg.asr.max_retries))
        rbox = retries_box if retries_box is not None else [0]
        common_headers: dict[str, str] = {}

        # Phase 1: submit task (retries on rate_limit / transient errors)
        while True:
            if is_cancelled is not None and is_cancelled():
                return "", True
            try:
                rem = self._remaining_timeout(deadline, rid, "submit", secrets, budget_sec=budget_sec)
                req_id = str(uuid.uuid4())
                common_headers = {
                    **auth_hdrs,
                    "Content-Type": "application/json",
                    "X-Api-Resource-Id": rid,
                    "X-Api-Request-Id": req_id,
                    "X-Api-Sequence": "-1",
                }
                submit_resp = self._send(
                    HttpRequest(
                        url=f"{base_url}/submit",
                        method="POST",
                        headers=common_headers,
                        body=body_bytes,
                        timeout_sec=rem,
                        cancel_token=cancel_token,
                    ),
                    extra_secrets=secrets,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                )
                sub_non_json = False
                try:
                    submit_data = submit_resp.json()
                except ValueError:
                    submit_data = {}
                    sub_non_json = bool(submit_resp.body.strip())
                sub_code, sub_msg = self._extract_doubao_status(submit_resp, submit_data, secrets)
                if sub_code == "20000003":
                    return "", False
                if submit_resp.status == 200 and sub_code == "20000000":
                    break
                is_rl, _ = _is_doubao_rate_limit(submit_resp.status, sub_code, sub_msg)
                if not is_rl and self._is_recoverable_slot_error(
                    submit_resp.status, sub_code, sub_msg
                ):
                    raise _DoubaoSlotUnavailableError(sub_code or str(submit_resp.status), sub_msg)
                raise self._fail_http(
                    f"Doubao submit ({rid}) failed HTTP {submit_resp.status} code={sub_code}: {sub_msg}",
                    submit_resp,
                    submit_data,
                    extra_secrets=secrets,
                    provider="doubao",
                    non_json=sub_non_json and not sub_code,
                    api_code=sub_code,
                    api_msg=sub_msg,
                )
            except AsrError as err:
                if is_cancelled is not None and is_cancelled():
                    return "", True
                if not self._maybe_backoff_before_retry(
                    err,
                    retries_used=rbox[0],
                    max_retries=max_retries,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                ):
                    if is_cancelled is not None and is_cancelled():
                        return "", True
                    raise
                rbox[0] += 1

        # Phase 2: poll /query reusing the exact same X-Api-Request-Id under unified deadline
        query_body = b"{}"
        wait_sec = _doubao_poll_wait_sec(0.0)
        poll_t0 = self.clock_fn()
        need_poll_sleep = True

        while True:
            if is_cancelled is not None and is_cancelled():
                return "", True
            try:
                if need_poll_sleep:
                    rem_before_sleep = self._remaining_timeout(
                        deadline, rid, "query", secrets, budget_sec=budget_sec
                    )
                    _sleep_cooperative(
                        min(wait_sec, rem_before_sleep),
                        sleep_fn=self.sleep_fn,
                        is_cancelled=is_cancelled,
                        cancel_token=cancel_token,
                    )
                    if is_cancelled is not None and is_cancelled():
                        return "", True
                need_poll_sleep = True
                rem_for_query = self._remaining_timeout(
                    deadline, rid, "query", secrets, budget_sec=budget_sec
                )

                q_resp = self._send(
                    HttpRequest(
                        url=f"{base_url}/query",
                        method="POST",
                        headers=common_headers,
                        body=query_body,
                        timeout_sec=rem_for_query,
                        cancel_token=cancel_token,
                    ),
                    extra_secrets=secrets,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                )
                q_non_json = False
                try:
                    q_data = q_resp.json()
                except ValueError:
                    q_data = {}
                    q_non_json = bool(q_resp.body.strip())
                q_code, q_msg = self._extract_doubao_status(q_resp, q_data, secrets)
                if q_code == "20000000" and q_resp.status == 200 and not q_non_json:
                    res = q_data.get("result")
                    return (
                        str(res.get("text") or "").strip() if isinstance(res, dict) else ""
                    ), False
                if q_code == "20000003":
                    return "", False
                if q_code in ("20000001", "20000002"):
                    total_elapsed = self.clock_fn() - t0
                    poll_elapsed = self.clock_fn() - poll_t0
                    if self.clock_fn() >= deadline:
                        raise self._fail(
                            f"Doubao query ({rid}) timed out after {total_elapsed:.2f}s (last code={q_code})",
                            extra_secrets=secrets,
                            retry_kind="transient",
                        )
                    wait_sec = _doubao_poll_wait_sec(poll_elapsed)
                    continue
                raise self._fail_http(
                    f"Doubao query ({rid}) failed HTTP {q_resp.status} code={q_code}: {q_msg}",
                    q_resp,
                    q_data,
                    extra_secrets=secrets,
                    provider="doubao",
                    non_json=q_non_json and not q_code,
                    api_code=q_code,
                    api_msg=q_msg,
                )
            except AsrError as err:
                if is_cancelled is not None and is_cancelled():
                    return "", True
                if not self._maybe_backoff_before_retry(
                    err,
                    retries_used=rbox[0],
                    max_retries=max_retries,
                    deadline=deadline,
                    pacer_key=pacer_key,
                    is_cancelled=is_cancelled,
                    cancel_token=cancel_token,
                ):
                    if is_cancelled is not None and is_cancelled():
                        return "", True
                    raise
                rbox[0] += 1
                need_poll_sleep = False


# ---------------------------------------------------------------------------
# 3. Google Gemini Single-Hop Audio Provider (gemini)
# ---------------------------------------------------------------------------

SINGLE_HOP_AUDIO_TASK_REMINDER = SINGLE_HOP_TASK_REMINDER
GEMINI_AUDIO_SYSTEM_PROMPT = SINGLE_HOP_SYSTEM_PROMPT


def _resolve_single_hop_system_prompt(mode: str) -> str:
    return (
        SINGLE_HOP_GENERAL_SYSTEM_PROMPT
        if (mode or "agent").strip().lower() == "general"
        else SINGLE_HOP_SYSTEM_PROMPT
    )


class GeminiAudioProvider(BaseAsrProvider):
    provider_name = "gemini"
    single_hop = True

    def _require_key(self) -> CredentialItem:
        return self._resolve_custom_or_vendor_key(
            self.creds.gemini,
            "missing Gemini API key (set GEMINI_API_KEY or API_KEY_GEMINI)",
        )

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        model = self.cfg.asr.effective_model("gemini")
        gen_cfg = build_gemini_generation_config(model, self.cfg.asr.effort, temperature=0.1)
        if not wav_bytes:
            return AsrResult(
                text="",
                provider=self.provider_name,
                model=model,
                latency_ms=0,
                is_single_hop_refined=True,
            )

        cred = self._require_key()
        base = (self.cfg.asr.base_url.strip() or GEMINI_DEFAULT_BASE_URL).rstrip("/")
        model_path = model if model.startswith("models/") else f"models/{model}"
        url = f"{base}/{model_path}:generateContent"

        t0 = self.clock_fn()
        ctx = _coerce_context(context, self.cfg.asr.hotwords_file, env=self.env)
        audio_bytes, audio_fmt = _prepare_single_hop_audio(wav_bytes)
        b64_audio = base64.b64encode(audio_bytes).decode("ascii")
        mime_type = f"audio/{audio_fmt}"

        parts: list[dict[str, Any]] = []
        ctx_chunks = 0
        ctx_chars = 0
        hint_blk = format_target_app_hint(ctx.target_hint)
        if ctx.single_hop_prepared or ctx.context_blocks or ctx.git_block:
            blocks = list(ctx.context_blocks)
            ctx_chunks = len(blocks)
            ctx_chars = sum(len(b) for b in blocks)
            for blk in blocks:
                parts.append({"text": blk})
            tail = "\n\n".join(
                filter(None, [ctx.git_block, _format_vocabulary_hints(ctx.hotwords), hint_blk])
            )
            if tail:
                parts.append({"text": tail})
        else:
            ctx_body = _compose_single_hop_unprepared_context(ctx, max_chars=16000)
            if ctx_body:
                parts.append({"text": f"<session_context>\n{ctx_body}\n</session_context>"})
            if hint_blk:
                parts.append({"text": hint_blk})
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": b64_audio,
                }
            }
        )
        parts.append({"text": SINGLE_HOP_AUDIO_TASK_REMINDER})
        payload = {
            "systemInstruction": {"parts": [{"text": _resolve_single_hop_system_prompt(ctx.mode)}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_cfg,
        }
        headers = {
            "x-goog-api-key": cred.value,
            "Content-Type": "application/json",
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        pacer_key = make_rate_pacer_key(self.provider_name, url, cred.value)

        eff_timeout = effective_single_hop_timeout_sec(self.cfg.asr.timeout_sec, wav_bytes)
        deadline = self.clock_fn() + eff_timeout
        usage_box: list[_TokenUsageTuple] = [_EMPTY_USAGE]
        audio_tok_box: list[int | None] = [None]

        def _attempt(rem: float) -> str:
            resp = self._send(
                HttpRequest(
                    url=url,
                    method="POST",
                    headers=headers,
                    body=body_bytes,
                    timeout_sec=rem,
                    cancel_token=cancel_token,
                ),
                extra_secrets=(cred.value,),
                deadline=deadline,
                pacer_key=pacer_key,
                is_cancelled=eff_cancelled,
                cancel_token=cancel_token,
            )
            try:
                data = resp.json()
            except ValueError:
                excerpt = self._safe_excerpt(resp.text(), (cred.value,))
                raise self._fail_http(
                    f"Gemini generateContent returned non-JSON (HTTP {resp.status}): {excerpt}",
                    resp,
                    extra_secrets=(cred.value,),
                    non_json=True,
                ) from None

            if resp.status != 200 or (data.get("error") and not data.get("candidates")):
                raw_err = data.get("error")
                err_msg = (
                    str(raw_err.get("message") or data)
                    if isinstance(raw_err, dict)
                    else str(raw_err or data)
                )
                excerpt = self._safe_excerpt(err_msg, (cred.value,))
                raise self._fail_http(
                    f"Gemini generateContent HTTP {resp.status}: {excerpt}",
                    resp,
                    data,
                    extra_secrets=(cred.value,),
                )

            usage_box[0] = _extract_gemini_usage(data)
            audio_tok_box[0] = _extract_gemini_audio_tokens(data)
            candidates = data.get("candidates")
            if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
                pf = data.get("promptFeedback")
                if isinstance(pf, dict) and pf.get("blockReason"):
                    excerpt = self._safe_excerpt(str(pf), (cred.value,))
                    raise self._fail(
                        f"Gemini generateContent blocked ({pf.get('blockReason')}): {excerpt}",
                        extra_secrets=(cred.value,),
                        retry_kind="never",
                    )
                return ""

            finish_reason = str(candidates[0].get("finishReason") or "").strip().upper()
            if finish_reason in _GEMINI_BLOCKED_FINISH_REASONS:
                raise self._fail(
                    f"Gemini generateContent blocked ({finish_reason})",
                    extra_secrets=(cred.value,),
                    retry_kind="never",
                )

            cand_content = candidates[0].get("content")
            resp_parts = cand_content.get("parts") if isinstance(cand_content, dict) else None
            if not isinstance(resp_parts, list):
                resp_parts = []
            out_chunks: list[str] = []
            for p in resp_parts:
                if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str):
                    out_chunks.append(p["text"])
            return "".join(out_chunks)

        raw_out, retries_used, cancelled = self._execute_with_retry(
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=eff_cancelled,
            attempt_fn=_attempt,
            budget_sec=eff_timeout,
            cancel_token=cancel_token,
        )
        latency_ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
        in_tok, cached_tok, out_tok, rsn_tok = usage_box[0] if not cancelled else _EMPTY_USAGE
        return AsrResult(
            text="" if cancelled else _normalize_single_hop_text(raw_out),
            provider=self.provider_name,
            model=model,
            latency_ms=latency_ms,
            is_single_hop_refined=True,
            retries=retries_used,
            input_tokens=in_tok,
            cached_input_tokens=cached_tok,
            output_tokens=out_tok,
            reasoning_tokens=rsn_tok,
            context_chars=ctx_chars,
            context_chunks=ctx_chunks,
            audio_tokens=audio_tok_box[0] if not cancelled else None,
        )


# ---------------------------------------------------------------------------
# 4. Dedicated STT Audio Transcriptions (/audio/transcriptions)
# ---------------------------------------------------------------------------


def _encode_multipart_formdata(
    fields: Sequence[tuple[str, str]],
    file_field: tuple[str, str, str, bytes],
) -> tuple[str, bytes]:
    boundary = f"----VoiceInputBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    bnd_line = f"--{boundary}\r\n".encode("ascii")

    for name, val in fields:
        chunks.append(bnd_line)
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(val.encode("utf-8"))
        chunks.append(b"\r\n")

    fname_field, filename, content_type, file_bytes = file_field
    chunks.append(bnd_line)
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{fname_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class _BearerProviderMixin(BaseAsrProvider):
    """Shared base_url + strict per-provider Bearer key resolver."""

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials,
        *,
        provider_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(cfg, creds, **kwargs)
        if provider_name:
            self.provider_name = provider_name.strip().lower()

    def _resolve_base_url(self) -> str:
        base = self.cfg.asr.effective_base_url(self.provider_name).strip().rstrip("/")
        if not base:
            if self.provider_name == "openai-compatible":
                raise ConfigError(
                    "asr.base_url must be explicitly set when asr.provider is 'openai-compatible' "
                    "(use asr.provider='openai' for the official OpenAI API)"
                )
            base = OPENAI_DEFAULT_BASE_URL
        return base

    def _resolve_bearer_key(self, url: str) -> str | None:
        explicit_env = self.cfg.asr.api_key_env.strip()
        if explicit_env:
            if self.creds.asr_custom is None or not self.creds.asr_custom.value:
                raise self._fail(
                    f"configured asr.api_key_env {explicit_env!r} is not set or empty"
                )
            return self.creds.asr_custom.value
        vendor_item = self.creds.for_provider(self.provider_name)
        if vendor_item is not None and vendor_item.value:
            return vendor_item.value
        if self.provider_name == "openai-compatible" and _is_loopback_url(url):
            return None
        raise self._fail(_missing_key_hint(self.provider_name, stage="asr"))


class AudioTranscriptionsAsrProvider(_BearerProviderMixin):
    provider_name = "openai-compatible"

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        model = self.cfg.asr.effective_model(self.provider_name)
        base = self._resolve_base_url()
        if not wav_bytes:
            return AsrResult(
                text="",
                provider=self.provider_name,
                model=model,
                latency_ms=0,
                is_single_hop_refined=False,
            )

        url = base if base.endswith("/audio/transcriptions") else f"{base}/audio/transcriptions"
        api_key = self._resolve_bearer_key(url)
        secrets: tuple[str, ...] = (api_key,) if api_key else ()
        pacer_key = make_rate_pacer_key(self.provider_name, url, api_key or "")
        ctx = _coerce_context(context, self.cfg.asr.hotwords_file, env=self.env)

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        if self.provider_name == "openrouter":
            # OpenRouter /api/v1/audio/transcriptions accepts JSON input_audio (raw Base64 wav),
            # ignores Whisper 'prompt', and only supports 'keyterms' on Deepgram/AssemblyAI models.
            b64_wav = base64.b64encode(wav_bytes).decode("ascii")
            json_payload: dict[str, Any] = {
                "model": model,
                "input_audio": {"data": b64_wav, "format": "wav"},
                "response_format": "json",
            }
            if ctx.hotwords:
                kt_budget = budget_hotwords_for_provider(
                    ctx.hotwords, "openrouter", model=model, api="audio-transcriptions"
                )
                if kt_budget.kept:
                    json_payload["keyterms"] = list(kt_budget.kept)
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        else:
            prompt = _compose_stt_prompt_text(ctx, max_chars=2000)
            fields: list[tuple[str, str]] = [
                ("model", model),
                ("response_format", "json"),
            ]
            if prompt:
                fields.append(("prompt", prompt))
            content_type, body = _encode_multipart_formdata(
                fields,
                ("file", "audio.wav", "audio/wav", wav_bytes),
            )
            headers["Content-Type"] = content_type

        eff_timeout = float(self.cfg.asr.timeout_sec)
        t0 = self.clock_fn()
        deadline = t0 + eff_timeout

        def _attempt(rem: float) -> str:
            resp = self._send(
                HttpRequest(
                    url=url,
                    method="POST",
                    headers=headers,
                    body=body,
                    timeout_sec=rem,
                    cancel_token=cancel_token,
                ),
                extra_secrets=secrets,
                deadline=deadline,
                pacer_key=pacer_key,
                is_cancelled=eff_cancelled,
                cancel_token=cancel_token,
            )

            if resp.status != 200:
                try:
                    err_data = resp.json()
                    non_json = False
                except ValueError:
                    err_data = {}
                    non_json = True
                excerpt = self._safe_excerpt(resp.text(), secrets)
                raise self._fail_http(
                    f"{self.provider_name} audio/transcriptions HTTP {resp.status}: {excerpt}",
                    resp,
                    err_data,
                    extra_secrets=secrets,
                    non_json=non_json,
                )

            try:
                data = resp.json()
            except ValueError:
                excerpt = self._safe_excerpt(resp.text(), secrets)
                raise self._fail_http(
                    f"{self.provider_name} audio/transcriptions returned non-JSON (HTTP {resp.status}): {excerpt}",
                    resp,
                    extra_secrets=secrets,
                    non_json=True,
                ) from None

            if data.get("error"):
                raw_err = data.get("error")
                err_msg = (
                    str(raw_err.get("message") or data)
                    if isinstance(raw_err, dict)
                    else str(raw_err or data)
                )
                excerpt = self._safe_excerpt(err_msg, secrets)
                raise self._fail_http(
                    f"{self.provider_name} audio/transcriptions error: {excerpt}",
                    resp,
                    data,
                    extra_secrets=secrets,
                )

            raw_text = data.get("text")
            return str(raw_text) if isinstance(raw_text, str) else ""

        raw_out, retries_used, cancelled = self._execute_with_retry(
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=eff_cancelled,
            attempt_fn=_attempt,
            cancel_token=cancel_token,
        )
        latency_ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
        text = "" if cancelled else strip_lenticular_artifacts(raw_out).strip()

        return AsrResult(
            text=text,
            provider=self.provider_name,
            model=model,
            latency_ms=latency_ms,
            is_single_hop_refined=False,
            retries=retries_used,
        )


OpenAICompatAsrProvider = AudioTranscriptionsAsrProvider


# ---------------------------------------------------------------------------
# 5. Multimodal Single-Hop Chat Audio Provider (/chat/completions)
# ---------------------------------------------------------------------------

CHAT_AUDIO_SYSTEM_PROMPT = SINGLE_HOP_SYSTEM_PROMPT
CHAT_AUDIO_TASK_REMINDER = SINGLE_HOP_TASK_REMINDER


class ChatAudioAsrProvider(_BearerProviderMixin):
    provider_name = "openrouter"
    single_hop = True

    def transcribe(
        self,
        wav_bytes: bytes,
        context: AsrContext | Mapping[str, Any] | None = None,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsrResult:
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        model = self.cfg.asr.effective_model(self.provider_name)
        if self.provider_name == "dashscope":
            url = _resolve_dashscope_url(self.cfg.asr.base_url, use_native=False)
        else:
            base = self._resolve_base_url()
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        if not wav_bytes:
            return AsrResult(
                text="",
                provider=self.provider_name,
                model=model,
                latency_ms=0,
                is_single_hop_refined=True,
            )

        api_key = self._resolve_bearer_key(url)
        secrets: tuple[str, ...] = (api_key,) if api_key else ()
        pacer_key = make_rate_pacer_key(self.provider_name, url, api_key or "")

        t0 = self.clock_fn()
        ctx = _coerce_context(context, self.cfg.asr.hotwords_file, env=self.env)
        audio_bytes, audio_fmt = _prepare_single_hop_audio(wav_bytes)
        b64_audio = base64.b64encode(audio_bytes).decode("ascii")
        audio_obj = (
            {"data": f"data:audio/{audio_fmt};base64,{b64_audio}", "format": audio_fmt}
            if self.provider_name == "dashscope"
            else {"data": b64_audio, "format": audio_fmt}
        )

        ctx_chunks = 0
        ctx_chars = 0
        hint_blk = format_target_app_hint(ctx.target_hint)
        if ctx.single_hop_prepared or ctx.context_blocks or ctx.git_block:
            blocks = list(ctx.context_blocks)
            ctx_chunks = len(blocks)
            ctx_chars = sum(len(b) for b in blocks)
            bps = set(openrouter_cache_breakpoints(self.provider_name, model, len(blocks)))
            ctx_parts: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": blk,
                    **({"cache_control": {"type": "ephemeral"}} if idx in bps else {}),
                }
                for idx, blk in enumerate(blocks)
            ]
            tail = "\n\n".join(
                filter(None, [ctx.git_block, _format_vocabulary_hints(ctx.hotwords), hint_blk])
            )
            user_content: list[dict[str, Any]] = [
                *ctx_parts,
                *([{"type": "text", "text": tail}] if tail else []),
                {"type": "input_audio", "input_audio": audio_obj},
                {"type": "text", "text": CHAT_AUDIO_TASK_REMINDER},
            ]
        else:
            ctx_body = _compose_single_hop_unprepared_context(ctx, max_chars=16000)
            session_tag = (
                f"<session_context>\n{ctx_body}\n</session_context>"
                if ctx_body
                else "<session_context>\n</session_context>"
            )
            # Anti-hijacking sandwich: <session_context> -> <target_app_hint> -> input_audio -> <task_reminder>
            user_content = [
                {"type": "text", "text": session_tag},
                *([{"type": "text", "text": hint_blk}] if hint_blk else []),
                {"type": "input_audio", "input_audio": audio_obj},
                {"type": "text", "text": CHAT_AUDIO_TASK_REMINDER},
            ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _resolve_single_hop_system_prompt(ctx.mode)},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        }
        if not _is_openai_reasoning_model(model):
            payload["temperature"] = 0.1
        if (
            self.provider_name in ("openrouter", "dashscope")
            or "audio" in model.lower()
            or "omni" in model.lower()
        ):
            payload["modalities"] = ["text"]
        apply_chat_effort_params(
            payload,
            provider=self.provider_name,
            model=model,
            effort=self.cfg.asr.effort,
            base_url=url,
        )

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        eff_timeout = effective_single_hop_timeout_sec(self.cfg.asr.timeout_sec, wav_bytes)
        deadline = self.clock_fn() + eff_timeout
        usage_box: list[_TokenUsageTuple] = [_EMPTY_USAGE]
        audio_tok_box: list[int | None] = [None]

        def _attempt(rem: float) -> str:
            resp = self._send(
                HttpRequest(
                    url=url,
                    method="POST",
                    headers=headers,
                    body=body_bytes,
                    timeout_sec=rem,
                    cancel_token=cancel_token,
                ),
                extra_secrets=secrets,
                deadline=deadline,
                pacer_key=pacer_key,
                is_cancelled=eff_cancelled,
                cancel_token=cancel_token,
            )
            try:
                data = resp.json()
            except ValueError:
                excerpt = self._safe_excerpt(resp.text(), secrets)
                raise self._fail_http(
                    f"{self.provider_name} chat/completions returned non-JSON (HTTP {resp.status}): {excerpt}",
                    resp,
                    extra_secrets=secrets,
                    non_json=True,
                ) from None

            compat_err = extract_openai_compat_error(data)
            if resp.status != 200 or compat_err is not None:
                raw_err = compat_err[0] if compat_err is not None else data.get("error")
                err_msg = (
                    compat_err[1]
                    if compat_err is not None
                    else str(data.get("error") or data)
                )
                err_code = (
                    str(raw_err.get("code") or "")
                    if isinstance(raw_err, Mapping)
                    else str(data.get("code") or "")
                )
                excerpt = self._safe_excerpt(err_msg, secrets)
                raise self._fail_http(
                    f"{self.provider_name} chat/completions HTTP {resp.status}: {excerpt}",
                    resp,
                    data,
                    extra_secrets=secrets,
                    api_code=err_code,
                    api_msg=err_msg,
                )

            usage_box[0] = _extract_token_usage(data, default_zero_details=False)
            audio_tok_box[0] = _extract_audio_tokens(data)
            return extract_chat_message_text(data)

        raw_out, retries_used, cancelled = self._execute_with_retry(
            deadline=deadline,
            pacer_key=pacer_key,
            is_cancelled=eff_cancelled,
            attempt_fn=_attempt,
            budget_sec=eff_timeout,
            cancel_token=cancel_token,
        )
        latency_ms = max(0, int(round((self.clock_fn() - t0) * 1000)))
        out_text = "" if cancelled else _normalize_single_hop_text(raw_out)
        in_tok, cached_tok, out_tok, rsn_tok = usage_box[0] if not cancelled else _EMPTY_USAGE
        return AsrResult(
            text=out_text,
            provider=self.provider_name,
            model=model,
            latency_ms=latency_ms,
            is_single_hop_refined=True,
            retries=retries_used,
            input_tokens=in_tok,
            cached_input_tokens=cached_tok,
            output_tokens=out_tok,
            reasoning_tokens=rsn_tok,
            context_chars=ctx_chars,
            context_chunks=ctx_chunks,
            audio_tokens=audio_tok_box[0] if not cancelled else None,
        )


# ---------------------------------------------------------------------------
# Factory & top-level convenience entry point
# ---------------------------------------------------------------------------


def create_asr_provider(
    cfg: Config,
    creds: ProviderCredentials | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    clock_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    rand_fn: Callable[[], float] = random.random,
) -> BaseAsrProvider:
    eff_env = os.environ if env is None else env
    if cfg.asr.effort.strip().lower() not in VALID_EFFORTS:
        raise ConfigError(
            f"asr.effort must be one of {sorted(VALID_EFFORTS)}, got {cfg.asr.effort!r}"
        )
    eff_creds = creds if creds is not None else resolve_credentials(cfg, eff_env)
    provider_name = select_asr_provider(cfg, eff_creds)
    if provider_name == "openai-compatible" and not cfg.asr.base_url.strip():
        raise ConfigError(
            "asr.base_url must be explicitly set when asr.provider is 'openai-compatible' "
            "(use asr.provider='openai' for the official OpenAI API)"
        )

    eff_model = cfg.asr.effective_model(provider_name)
    resolved_api = resolve_asr_api(
        provider_name,
        eff_model,
        cfg.asr.api,
        cfg.asr.base_url,
    )

    kwargs: dict[str, Any] = {
        "env": eff_env,
        "transport": transport,
        "clock_fn": clock_fn,
        "sleep_fn": sleep_fn,
        "rand_fn": rand_fn,
    }
    if provider_name == "dashscope":
        if is_dashscope_omni_chat_model(eff_model, cfg.asr.base_url):
            return ChatAudioAsrProvider(cfg, eff_creds, provider_name="dashscope", **kwargs)
        return DashScopeAsrProvider(cfg, eff_creds, **kwargs)
    if provider_name == "doubao":
        return DoubaoAsrProvider(cfg, eff_creds, **kwargs)
    if provider_name == "gemini" and is_gemini_native_url(cfg.asr.base_url):
        return GeminiAudioProvider(cfg, eff_creds, **kwargs)
    if resolved_api == "chat-completions":
        return ChatAudioAsrProvider(cfg, eff_creds, provider_name=provider_name, **kwargs)
    if resolved_api == "audio-transcriptions":
        return AudioTranscriptionsAsrProvider(
            cfg, eff_creds, provider_name=provider_name, **kwargs
        )
    raise AsrError(f"unsupported ASR provider/api: {provider_name!r}/{resolved_api!r}", env=eff_env, cfg=cfg)


def transcribe(
    wav_bytes: bytes,
    cfg: Config,
    *,
    creds: ProviderCredentials | None = None,
    context: AsrContext | Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    clock_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    rand_fn: Callable[[], float] = random.random,
    is_cancelled: Callable[[], bool] | None = None,
    cancel_token: CancelToken | None = None,
) -> AsrResult:
    """Top-level transcribe entry point fixed across Chunk 1 and Chunk 2."""
    provider = create_asr_provider(
        cfg,
        creds,
        env=env,
        transport=transport,
        clock_fn=clock_fn,
        sleep_fn=sleep_fn,
        rand_fn=rand_fn,
    )
    return provider.transcribe(
        wav_bytes,
        context=context,
        is_cancelled=is_cancelled,
        cancel_token=cancel_token,
    )
