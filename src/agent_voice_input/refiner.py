"""Stage 2 context-aware text refinement engines for agent-voice-input."""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import random
import re
import selectors
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.parse
import urllib.request
import uuid

from . import __version__
from ._remote_probe import (
    PRIOR_SESSION_PREFIX,
    PRIOR_SESSION_SUFFIX,
    SESSION_REF_HEADER_BODY,
    SESSION_REF_SUFFIX,
)
from .http_transport import (
    CancelToken,
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_USER_AGENT,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    HttpTransportError,
    _HttpProxyRoute,
    _KeepAlivePool,
    _RATE_PACER,
    _can_reuse_pooled_conn,
    _close_quietly,
    _combine_cancel_check,
    _get_proxies_from_env,
    _is_idle_socket_stale,
    _is_loopback_url,
    _proxy_bypass_no_dns,
    _redact_proxy_error_text,
    _register_conn_abort,
    _resolve_http_proxy_route,
    _select_system_proxy_url,
    _unregister_conn_abort,
    _validate_request_url,
    classify_http_failure,
    classify_transport_error,
    compute_retry_delay,
    make_rate_pacer_key,
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
    _extract_gemini_usage,
    _extract_token_usage,
    _is_openai_reasoning_model,
    _is_openrouter_thinking_model,
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
from .config import (
    _CODEX_KEYRING_STORE_RE,
    CODEX_PUBLIC_FALLBACK_MODELS,
    CODEX_SUBSCRIPTION_BASE_URL,
    DEFAULT_REFINER_MODELS,
    OPENAI_DEFAULT_BASE_URL,
    Config,
    ConfigError,
    ProviderCredentials,
    VALID_EFFORTS,
    VALID_REFINER_PROVIDERS,
    _read_codex_models_cache_slugs,
    classify_codex_auth_json,
    ensure_secure_dir,
    expand_user_path,
    find_agy_bin,
    find_codex_bin,
    get_runtime_dir,
    has_codex_local_auth,
    redact_secrets,
    resolve_codex_default_model,
    resolve_codex_home_dir,
    resolve_credentials,
    resolve_deepseek_endpoint_url,
    select_refiner_provider,
)
from .prompts import (
    CODEX_NO_TOOLS_NOTE,
    REFINER_AGENT_MODE_ADDENDUM,
    REFINER_CORE_PROMPT,
    REFINER_GENERAL_MODE_ADDENDUM,
    REFINER_GENERAL_SYSTEM_PROMPT,
    REFINER_SYSTEM_PROMPT,
    REFINER_TASK_REMINDER,
    format_target_app_hint,
    strip_target_hint_echo,
)
from .session_ctx import SessionTracker

DEFAULT_REFINER_PROMPT = REFINER_SYSTEM_PROMPT

CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/0.169.0"

CODEX_REFRESH_WINDOW_SEC: float = 1800.0
CODEX_HARD_EXPIRY_SKEW_SEC: float = 60.0
CODEX_REFRESH_FAILURE_COOLDOWN_SEC: float = 300.0
CODEX_AUTH_RPC_TIMEOUT_SEC: float = 10.0

ALLOWED_AFFINITY_COOKIES: frozenset[str] = frozenset(
    {"__cf_bm", "__cflb", "__cfruid", "_cfuvid", "cf_clearance", "__oailb"}
)
_CONCRETE_RESPONSES_EFFORTS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_REFINER_SESSION_UUID_NS = uuid.UUID("7f9c4e12-5a38-4b91-9e24-3c8a1d7f0b62")

_PURE_CHAT_CONFIG_OVERRIDES = (
    "mcp_servers={}",
    "hooks={}",
    'web_search="disabled"',
    "project_doc_max_bytes=0",
    "project_root_markers=[]",
    "allow_login_shell=false",
    "skills.include_instructions=false",
    "include_environment_context=false",
    "include_permissions_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_apps_instructions=false",
    "agents.enabled=false",
)
_DISABLE = (
    "code_mode_host",
    "shell_tool",
    "shell_snapshot",
    "unified_exec",
    "multi_agent",
    "multi_agent_v2",
    "image_generation",
    "apps",
    "plugins",
    "computer_use",
    "browser_use",
    "goals",
    "skill_search",
    "tool_suggest",
    "memories",
    "hooks",
    "view_image",
    "guardian_approval",
)

_RESPONSES_STALE_CONN_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    ConnectionError,
    BrokenPipeError,
    ConnectionResetError,
    EOFError,
    ssl.SSLError,
)


@dataclass(frozen=True)
class RefinerResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    used_fallback: str = ""
    warning: str = ""
    retries: int = 0
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    init_ms: int = 0
    context_chars: int = 0
    context_chunks: int = 0
    audio_tokens: int | None = None
    used_prior_session: bool = False


class _RefinerAttemptError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_kind: str = "never",
        retry_after_sec: float | None = None,
        is_concurrency_limit: bool = False,
        http_status: int | None = None,
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.retry_kind = retry_kind
        self.retry_after_sec = retry_after_sec
        self.is_concurrency_limit = is_concurrency_limit
        self.http_status = http_status
        self.error_code = error_code


def _ms(t0: float, clock_fn: Callable[[], float] = time.monotonic) -> int:
    return max(0, int(round((clock_fn() - t0) * 1000)))


def _unpack(asr_input: AsrResult | str) -> tuple[str, bool]:
    return (
        (asr_input.text.strip(), asr_input.is_single_hop_refined)
        if isinstance(asr_input, AsrResult)
        else (str(asr_input).strip(), False)
    )


def _clean_out(text: str, fallback: str = "") -> str:
    cleaned = strip_lenticular_artifacts(text).strip()
    if cleaned and "<target_app_hint" not in fallback.lower() and "</target_app_hint>" not in fallback.lower():
        cleaned = strip_target_hint_echo(cleaned).strip()
    if cleaned and "<asr_draft_to_clean>" not in fallback and "</asr_draft_to_clean>" not in fallback:
        if cleaned.startswith("<asr_draft_to_clean>"):
            cleaned = cleaned.removeprefix("<asr_draft_to_clean>").strip()
        if cleaned.endswith("</asr_draft_to_clean>"):
            cleaned = cleaned.removesuffix("</asr_draft_to_clean>").strip()
    return cleaned or strip_lenticular_artifacts(fallback).strip()


def _clean_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PYTHONSTARTUP")}


def _fallback_result(
    clean_draft: str,
    provider_name: str,
    model: str,
    t0: float,
    clock_fn: Callable[[], float],
    fallback: str,
    warning: str = "",
    *,
    retries: int = 0,
    init_ms: int = 0,
    context_chars: int = 0,
    context_chunks: int = 0,
    used_prior_session: bool = False,
) -> RefinerResult:
    return RefinerResult(
        _clean_out(clean_draft),
        provider_name,
        model,
        _ms(t0, clock_fn),
        fallback,
        warning,
        retries=retries,
        init_ms=init_ms,
        context_chars=context_chars,
        context_chunks=context_chunks,
        used_prior_session=used_prior_session,
    )


def _resolve_refiner_api_key(
    cfg: Config,
    creds: ProviderCredentials,
    provider_name: str,
    url: str = "",
) -> str:
    explicit_env = cfg.refiner.api_key_env.strip()
    if explicit_env:
        if creds.refiner_custom is None or not creds.refiner_custom.value:
            raise RuntimeError(f"configured refiner.api_key_env {explicit_env!r} is not set or empty")
        return creds.refiner_custom.value
    item = creds.for_provider(provider_name)
    if item is not None and item.value:
        return item.value
    if provider_name == "openai-compatible" and url and _is_loopback_url(url):
        return ""
    raise RuntimeError(_missing_key_hint(provider_name, stage="refiner"))


_CODEX_MODEL_UNSUPPORTED_RE = re.compile(
    r"(?i)(?:model_not_found|model_not_supported"
    r"|\bmodel\b[^:;\n]{0,80}?(?:does\s+not\s+exist|is\s+not\s+found|not\s+found"
    r"|is\s+not\s+supported|not\s+supported|is\s+unsupported|unsupported"
    r"|is\s+unavailable|unavailable|(?:do\s+not|don't)\s+have\s+access|no\s+access)"
    r"|(?:(?:do\s+not|don't)\s+have\s+access|no\s+access)\s+to\s+(?:the\s+)?model\b)"
)
_CODEX_PARAM_OR_CTX_ERR_RE = re.compile(
    r"(?i)\b(?:unsupported|invalid|unknown)\s+(?:parameter|value|argument|field|option|header)\b"
    r"|context_length_exceeded|maximum\s+context\s+length"
)


def _is_codex_model_unsupported_error(err: _RefinerAttemptError) -> bool:
    if err.http_status not in (400, 404, None):
        return False
    if err.error_code.strip().lower() in ("model_not_found", "model_not_supported"):
        return True
    msg = str(err)
    if _CODEX_PARAM_OR_CTX_ERR_RE.search(msg):
        return False
    return bool(_CODEX_MODEL_UNSUPPORTED_RE.search(msg))


def resolve_refiner_prompt(cfg: Config | None = None, *, mode: str = "agent") -> str:
    if cfg is not None and cfg.refiner.prompt.strip():
        return cfg.refiner.prompt.strip()
    return REFINER_GENERAL_SYSTEM_PROMPT if mode == "general" else REFINER_SYSTEM_PROMPT


def _compose_codex_instructions(sys_prompt: str) -> str:
    base = sys_prompt.strip()
    return base if "no tools available" in base.lower() else f"{base}{CODEX_NO_TOOLS_NOTE}"


def format_prior_session_reference_chunk(text: str) -> str:
    body = strip_lenticular_artifacts(text).strip()
    if not body:
        return ""
    return f"{PRIOR_SESSION_PREFIX}{body}{PRIOR_SESSION_SUFFIX}"


def format_session_reference_chunk(text: str, *, index: int | None = None) -> str:
    body = strip_lenticular_artifacts(text).strip()
    if not body:
        return ""
    idx_attr = f' chunk="{index}"' if index is not None else ""
    return f'<session_reference_log{idx_attr} read_only="true">\n{SESSION_REF_HEADER_BODY}{body}{SESSION_REF_SUFFIX}'


def format_workspace_git_block(git_context: str) -> str:
    if clean_git := strip_lenticular_artifacts(git_context).strip():
        return f'<workspace_git_status read_only="true">\n{clean_git}\n</workspace_git_status>'
    return ""


def build_final_refiner_block(
    draft_text: str, *, git_context: str = "", target_hint: str = ""
) -> str:
    parts: list[str] = []
    if git_blk := format_workspace_git_block(git_context):
        parts.append(git_blk)
    if hint_blk := format_target_app_hint(target_hint):
        parts.append(hint_blk)
    parts.append(f"<asr_draft_to_clean>\n{draft_text.strip()}\n</asr_draft_to_clean>")
    parts.append(REFINER_TASK_REMINDER)
    return "\n".join(parts)


def build_refiner_context_chunks(
    cfg: Config,
    *,
    session_key: str = "",
    tracker: SessionTracker | None = None,
    fallback_context: str = "",
    workspace_cwd: str = "",
) -> tuple[list[str], str]:
    max_chars = max(0, int(cfg.refiner.context_max_chars))
    include_git = bool(cfg.refiner.include_git_status)
    allow_prior = (cfg.refiner.cold_start_fallback or "auto").strip().lower() == "auto"
    prior_chunk = ""
    raw_chunks: list[str] = []
    git_ctx = ""
    if tracker is not None and (
        session_key or workspace_cwd or getattr(tracker, "has_bound_session", False)
    ):
        ctx_kw: dict[str, Any] = {"max_chars": max_chars, "include_git_status": include_git}
        if workspace_cwd:
            ctx_kw["workspace_cwd"] = workspace_cwd
        with_prior_fn = getattr(tracker, "get_refiner_context_with_prior", None)
        if callable(with_prior_fn):
            prior_chunk, raw_chunks, git_ctx = with_prior_fn(
                session_key, **ctx_kw, allow_prior=allow_prior
            )
        else:
            raw_chunks, git_ctx = tracker.get_refiner_context(session_key, **ctx_kw)
    if not prior_chunk and not raw_chunks and max_chars > 0 and fallback_context.strip():
        if not (tracker is not None and session_key and tracker.get_state(session_key) is not None):
            fb_trimmed = strip_lenticular_artifacts(fallback_context).strip()
            if len(fb_trimmed) > max_chars:
                fb_trimmed = fb_trimmed[-max_chars:]
            if fb_trimmed:
                raw_chunks = [fb_trimmed]
    blocks: list[str] = []
    if prior_chunk and (p_blk := format_prior_session_reference_chunk(prior_chunk)):
        blocks.append(p_blk)
    for chunk in raw_chunks:
        if f := format_session_reference_chunk(chunk):
            blocks.append(f)
    return blocks, git_ctx


def attach_single_hop_context(
    cfg: Config,
    base_ctx: AsrContext,
    *,
    tracker: SessionTracker | None = None,
    session_key: str = "",
    mode: str = "agent",
    workspace_cwd: str = "",
    target_hint: str = "",
) -> AsrContext:
    eff_mode = (mode or "agent").strip().lower() or "agent"
    eff_hint = (target_hint or base_ctx.target_hint or "").strip().lower()
    if eff_mode == "general":
        return replace(
            base_ctx,
            git_status="",
            dialog_text="",
            context_blocks=(),
            git_block="",
            mode="general",
            single_hop_prepared=True,
            target_hint=eff_hint,
        )
    context_blocks, git_ctx = build_refiner_context_chunks(
        cfg,
        session_key=session_key,
        tracker=tracker,
        fallback_context=base_ctx.dialog_text,
        workspace_cwd=workspace_cwd,
    )
    return replace(
        base_ctx,
        git_status="",
        dialog_text="",
        context_blocks=tuple(context_blocks),
        git_block=format_workspace_git_block(git_ctx),
        mode=eff_mode,
        single_hop_prepared=True,
        target_hint=eff_hint,
    )


def build_refiner_request_blocks(
    cfg: Config,
    clean_draft: str,
    *,
    session_key: str = "",
    tracker: SessionTracker | None = None,
    fallback_context: str = "",
    mode: str = "agent",
    workspace_cwd: str = "",
    target_hint: str = "",
) -> tuple[list[str], str]:
    if mode == "general":
        return [], build_final_refiner_block(clean_draft, git_context="", target_hint=target_hint)
    context_blocks, git_ctx = build_refiner_context_chunks(
        cfg,
        session_key=session_key,
        tracker=tracker,
        fallback_context=fallback_context,
        workspace_cwd=workspace_cwd,
    )
    return context_blocks, build_final_refiner_block(
        clean_draft, git_context=git_ctx, target_hint=target_hint
    )


def effective_refiner_timeout_sec(configured_sec: float, draft_chars: int) -> float:
    """Scale Stage 2 timeout for long transcripts (>600 chars) while preserving explicit short timeouts."""
    base = max(0.05, float(configured_sec))
    if draft_chars <= 600 or base < 10.0:
        return base
    return min(max(base, 240.0), base + (draft_chars - 600) * 0.04)


def _decode_jwt_claims_unverified(token: str) -> dict[str, Any]:
    """Best-effort decode of JWT payload claims without signature check."""
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        return {}
    payload_b64 = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_jwt_metadata(token: str) -> tuple[int | None, str, bool]:
    """Return ``(exp_epoch_sec, chatgpt_account_id, is_fedramp)`` from a JWT string."""
    claims = _decode_jwt_claims_unverified(token)
    if not claims:
        return None, "", False
    exp = _opt_int(claims.get("exp"))
    auth_ns = claims.get("https://api.openai.com/auth")
    if isinstance(auth_ns, Mapping):
        return exp, str(auth_ns.get("chatgpt_account_id") or "").strip(), bool(auth_ns.get("chatgpt_account_is_fedramp"))
    return exp, "", False


def _build_responses_url(base_url: str, *, default_base_url: str, provider_name: str = "") -> str:
    if provider_name.strip().lower() == "deepseek":
        return resolve_deepseek_endpoint_url(base_url, "/responses")
    base = (base_url.strip() or default_base_url).rstrip("/")
    return base if base.endswith("/responses") else f"{base}/responses"


def _derive_session_uuid(session_key: str, fallback_uuid: str) -> str:
    s = session_key.strip()
    return str(uuid.uuid5(_REFINER_SESSION_UUID_NS, s)) if s else fallback_uuid


def _extract_responses_http_error(data: Mapping[str, Any], raw_text: str) -> tuple[dict[str, Any], str]:
    if (compat := extract_openai_compat_error(data)) is not None:
        raw_err, msg = compat
        return (dict(raw_err) if isinstance(raw_err, Mapping) else {"message": msg}), msg
    if (detail := data.get("detail")) is not None:
        if isinstance(detail, str):
            return {"message": detail}, detail
        if isinstance(detail, Mapping):
            msg = str(detail.get("message") or detail.get("detail") or json.dumps(dict(detail), ensure_ascii=False))
            return dict(detail), msg
        msg = json.dumps(detail, ensure_ascii=False)
        return {"message": msg}, msg
    return {}, raw_text


def _extract_message_items_text(items: Any) -> tuple[list[str], bool]:
    texts: list[str] = []
    saw_refusal = False
    if not isinstance(items, list):
        return texts, False
    for it in items:
        if not isinstance(it, Mapping) or it.get("type") != "message":
            continue
        content = it.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            ptype = str(part.get("type") or "").strip()
            if ptype == "refusal":
                saw_refusal = True
            elif ptype in ("output_text", "text", "") and isinstance(txt := part.get("text"), str) and txt:
                texts.append(txt)
    return texts, saw_refusal


_RESPONSES_PERM_ERROR_TOKENS = (
    "context_length_exceeded",
    "usage_not_included",
    "quota_exceeded",
    "invalid_prompt",
    "content_filter",
    "content_policy",
    "policy_violation",
    "model_not_found",
)
_RESPONSES_TRANSIENT_TOKENS = ("server_is_overloaded", "overloaded", "try again")


def _classify_stream_error_event(
    provider_name: str,
    ev: Mapping[str, Any],
    ev_type: str,
    resp_headers: Mapping[str, str],
) -> _RefinerAttemptError:
    if ev_type == "response.incomplete":
        resp_obj = ev.get("response")
        inc = resp_obj.get("incomplete_details") if isinstance(resp_obj, Mapping) else None
        reason = str(inc.get("reason") or "unknown").strip().lower() if isinstance(inc, Mapping) else "unknown"
        return _RefinerAttemptError(
            f"Responses stream incomplete ({reason})",
            retry_kind="never" if reason in ("content_filter", "safety") else "transient",
        )

    resp_obj = ev.get("response") if ev_type == "response.failed" else None
    if isinstance(resp_obj, Mapping) and isinstance(resp_obj.get("error"), Mapping):
        err_obj: Mapping[str, Any] = resp_obj["error"]
    elif isinstance(ev.get("error"), Mapping):
        err_obj = ev["error"]
    else:
        err_obj = ev

    code = str(err_obj.get("code") or "").strip()
    etype = str(err_obj.get("type") or "").strip()
    msg = str(err_obj.get("message") or code or etype or ev_type).strip()
    norm_err = dict(err_obj)
    if code and not etype:
        norm_err["type"] = code

    rkind, rafter, conc = classify_http_failure(
        provider_name, 200, resp_headers, {"error": norm_err}
    )
    low = f"{code} {etype} {msg}".lower()
    if rkind == "never":
        if any(t in low for t in _RESPONSES_PERM_ERROR_TOKENS):
            rkind = "never"
        elif any(rl in low for rl in ("rate_limit", "ratelimit", "too_many_requests", "too many requests")):
            rkind = "rate_limit"
            conc = "concurrency" in low or "concurrent" in low
        elif any(tr in low for tr in _RESPONSES_TRANSIENT_TOKENS) or (
            ev_type == "response.failed" and not code and not etype
        ):
            rkind = "transient"
    return _RefinerAttemptError(
        f"{ev_type}: {msg}",
        retry_kind=rkind,
        retry_after_sec=rafter,
        is_concurrency_limit=conc,
        error_code=code,
    )


def _parse_responses_sse_lines(
    line_iter: Any,
    *,
    provider_name: str,
    resp_headers: Mapping[str, str],
    is_cancelled: Callable[[], bool] | None = None,
    allow_empty_output: bool = False,
) -> tuple[str, _TokenUsageTuple, bool]:
    """Consume an SSE line iterator to EOF and return ``(text, usage, saw_eof)``."""
    deltas: list[str] = []
    fallback_texts: list[str] = []
    usage: _TokenUsageTuple = _EMPTY_USAGE
    saw_completed = False
    saw_refusal = False
    stream_err: _RefinerAttemptError | None = None
    cur_event_name = ""
    cur_data_lines: list[str] = []

    def _flush_event() -> None:
        nonlocal cur_event_name, cur_data_lines, usage, saw_completed, saw_refusal, stream_err
        if not cur_data_lines:
            cur_event_name = ""
            return
        data_str = "\n".join(cur_data_lines).strip()
        ev_hint = cur_event_name
        cur_data_lines = []
        cur_event_name = ""
        if not data_str:
            return
        if data_str == "[DONE]":
            saw_completed = True
            return
        try:
            ev = json.loads(data_str)
        except ValueError:
            return
        if not isinstance(ev, Mapping):
            return
        ev_type = str(ev.get("type") or ev_hint or "").strip()
        if ev_type == "response.output_text.delta":
            if isinstance(d := ev.get("delta"), str):
                deltas.append(d)
        elif ev_type == "response.output_text.done":
            # Do NOT mark stream completed here: http.client returns b"" silently on truncated chunked EOF.
            if not fallback_texts and isinstance(t := ev.get("text"), str) and t:
                fallback_texts.append(t)
        elif ev_type == "response.output_item.done":
            if isinstance(it := ev.get("item"), Mapping):
                txts, ref = _extract_message_items_text([it])
                if not fallback_texts and txts:
                    fallback_texts.extend(txts)
                saw_refusal = saw_refusal or ref
        elif ev_type == "response.completed":
            saw_completed = True
            if isinstance(resp_obj := ev.get("response"), Mapping):
                if (parsed_u := _extract_token_usage(resp_obj, default_zero_details=True)) != _EMPTY_USAGE:
                    usage = parsed_u
                txts, ref = _extract_message_items_text(resp_obj.get("output"))
                if not fallback_texts and txts:
                    fallback_texts.extend(txts)
                saw_refusal = saw_refusal or ref
        elif ev_type in ("error", "response.failed", "response.incomplete") and stream_err is None:
            stream_err = _classify_stream_error_event(provider_name, ev, ev_type, resp_headers)

    saw_eof = False
    while True:
        if is_cancelled is not None and is_cancelled():
            return "", _EMPTY_USAGE, False
        raw_line = line_iter()
        if not raw_line:
            saw_eof = True
            _flush_event()
            break
        line = (
            raw_line.decode("utf-8", errors="replace")
            if isinstance(raw_line, (bytes, bytearray))
            else str(raw_line)
        ).rstrip("\r\n")
        if not line:
            _flush_event()
            if stream_err is not None:
                break
        elif line.startswith("event:"):
            cur_event_name = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            cur_data_lines.append(line[6:] if line.startswith("data: ") else line[5:])

    if stream_err is not None:
        raise stream_err

    out_txt = "".join(deltas).strip() or "\n".join(fallback_texts).strip()
    if not out_txt and saw_refusal:
        raise _RefinerAttemptError("Responses output blocked by refusal", retry_kind="never")
    if not saw_completed:
        raise _RefinerAttemptError(
            "Responses SSE stream closed before response.completed",
            retry_kind="transient",
        )
    if not out_txt and not allow_empty_output:
        raise _RefinerAttemptError("empty Responses output_text", retry_kind="transient")
    return out_txt, usage, saw_eof


def _cancel_fallback_kind(
    cancel_token: CancelToken | None,
    is_cancelled: Callable[[], bool] | None,
) -> str:
    if cancel_token is not None and cancel_token.is_cancelled():
        return cancel_token.reason or "cancelled"
    if is_cancelled is not None and is_cancelled():
        return "superseded"
    return ""


def _run_refiner_retry_loop(
    *,
    cfg: Config,
    env: Mapping[str, str],
    provider_name: str,
    model: str,
    clean_draft: str,
    t0: float,
    tsec: float,
    clock_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
    rand_fn: Callable[[], float],
    is_cancelled: Callable[[], bool] | None,
    pacer_key_fn: Callable[[], str],
    secrets_fn: Callable[[], Sequence[str]],
    attempt_fn: Callable[[float, float | None, bool], tuple[str, _TokenUsageTuple, int]],
    on_401_refresh: Callable[[], None] | None = None,
    model_fn: Callable[[], str] | None = None,
    on_model_fallback: Callable[[_RefinerAttemptError], bool] | None = None,
    context_chars: int = 0,
    context_chunks: int = 0,
    used_prior_session: bool = False,
    cancel_token: CancelToken | None = None,
) -> RefinerResult:
    deadline = t0 + tsec
    max_retries = max(0, int(cfg.refiner.max_retries))
    retries_used = 0
    refreshed_401 = False
    switched_model = False
    first_attempt = True
    total_init_ms = 0
    eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
    cancelled = (lambda: False) if eff_cancelled is None else eff_cancelled
    _cancel_kind = lambda: _cancel_fallback_kind(cancel_token, is_cancelled) or "superseded"
    _cur_model = (lambda: model) if model_fn is None else model_fn

    def _fb(kind: str, warn: str = "") -> RefinerResult:
        return _fallback_result(
            clean_draft,
            provider_name,
            _cur_model(),
            t0,
            clock_fn,
            kind,
            warn,
            retries=retries_used,
            init_ms=total_init_ms,
            context_chars=context_chars,
            context_chunks=context_chunks,
            used_prior_session=used_prior_session,
        )

    while True:
        if cancelled():
            return _fb(_cancel_kind())
        pacer_key = pacer_key_fn()
        secrets = secrets_fn()
        try:
            if clock_fn() > deadline:
                raise _RefinerAttemptError(
                    f"Refiner HTTP request timed out before send (budget={tsec:.2f}s)",
                    retry_kind="transient",
                )
            try:
                with _RATE_PACER.guard(
                    pacer_key,
                    deadline=deadline,
                    clock_fn=clock_fn,
                    sleep_fn=sleep_fn,
                    is_cancelled=cancelled,
                    cancel_token=cancel_token,
                ) as pacer_waited:
                    if cancelled():
                        return _fb(_cancel_kind())
                    now_send = clock_fn()
                    if now_send > deadline:
                        raise _RefinerAttemptError(
                            f"Refiner HTTP request timed out before send (budget={tsec:.2f}s)",
                            retry_kind="transient",
                        )
                    raw_rem = deadline - now_send
                    req_timeout = (
                        tsec
                        if (retries_used == 0 and not pacer_waited and raw_rem >= tsec - 0.02)
                        else min(tsec, max(0.005, raw_rem))
                    )
                    wall_dl = deadline if clock_fn is time.monotonic else None
                    is_first = first_attempt
                    first_attempt = False
                    out_txt, usage, step_init_ms = attempt_fn(req_timeout, wall_dl, is_first)
                    if is_first:
                        total_init_ms = step_init_ms
            except _RefinerAttemptError:
                raise
            except Exception as exc:
                raise _RefinerAttemptError(str(exc), retry_kind=classify_transport_error(exc)) from None

            if cancelled():
                return _fb(_cancel_kind())
            if clock_fn() > deadline + 1e-9:
                raise _RefinerAttemptError(
                    f"Refiner HTTP response exceeded timeout budget ({tsec:.2f}s)",
                    retry_kind="transient",
                )
            return RefinerResult(
                _clean_out(out_txt, clean_draft),
                provider_name,
                _cur_model(),
                _ms(t0, clock_fn),
                retries=retries_used,
                input_tokens=usage[0],
                cached_input_tokens=usage[1],
                output_tokens=usage[2],
                reasoning_tokens=usage[3],
                init_ms=total_init_ms,
                context_chars=context_chars,
                context_chunks=context_chunks,
                used_prior_session=used_prior_session,
            )
        except _RefinerAttemptError as err:
            if cancelled():
                return _fb(_cancel_kind())
            if err.http_status == 401 and on_401_refresh is not None and not refreshed_401:
                refreshed_401 = True
                try:
                    on_401_refresh()
                    continue
                except Exception as ref_exc:
                    if cancelled():
                        return _fb(_cancel_kind())
                    safe_401 = redact_secrets(
                        f"{err}; refresh failed: {ref_exc}",
                        env,
                        cfg=cfg,
                        extra_secrets=secrets_fn(),
                    )
                    return _fb("asr_draft", safe_401)

            if on_model_fallback is not None and not switched_model and on_model_fallback(err):
                switched_model = True
                continue

            now_err = clock_fn()
            if err.retry_kind == "rate_limit":
                _RATE_PACER.note_rate_limit(
                    pacer_key,
                    now=now_err,
                    retry_after_sec=err.retry_after_sec,
                    is_concurrency=err.is_concurrency_limit,
                )
            can_retry = err.retry_kind in ("rate_limit", "transient") and retries_used < max_retries
            if can_retry:
                rem = deadline - now_err
                if rem <= 0.02 or (err.retry_after_sec is not None and err.retry_after_sec >= rem):
                    can_retry = False
                else:
                    delay = compute_retry_delay(
                        err.retry_kind,
                        retries_used + 1,
                        retry_after_sec=err.retry_after_sec,
                        rand_fn=rand_fn,
                    )
                    if delay >= rem - 0.01:
                        can_retry = False
                    else:
                        sleep_cooperative(
                            delay,
                            sleep_fn=sleep_fn,
                            is_cancelled=cancelled,
                            cancel_token=cancel_token,
                        )
                        if cancelled():
                            return _fb(_cancel_kind())
                        if (deadline - clock_fn()) <= 0:
                            can_retry = False
            if cancelled():
                return _fb(_cancel_kind())
            if not can_retry:
                return _fb("asr_draft", redact_secrets(str(err), env, cfg=cfg, extra_secrets=secrets))
            retries_used += 1


class BaseRefiner:
    provider_name: str = "off"

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        raise NotImplementedError

    def prepare_session(self, session_key: str = "") -> None:
        pass

    def evict_connections(self) -> int:
        return 0

    def close(self) -> None:
        pass


class _HttpTransportRefiner(BaseRefiner):
    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials | None = None,
        *,
        env: Mapping[str, str] | None = None,
        transport: HttpTransport | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        rand_fn: Callable[[], float] = random.random,
    ) -> None:
        self.cfg, self.env = cfg, (os.environ if env is None else env)
        self.creds = creds or resolve_credentials(cfg, self.env)
        self.transport = transport or urllib_http_transport
        self.clock_fn, self.sleep_fn, self.rand_fn = clock_fn, sleep_fn, rand_fn

    def _send_http(
        self,
        *,
        model: str,
        clean_draft: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        secrets: Sequence[str],
        pacer_secret: str,
        t0: float,
        is_cancelled: Callable[[], bool] | None,
        cancel_token: CancelToken | None = None,
        parse_fn: Callable[[HttpResponse], tuple[str, _TokenUsageTuple]],
        context_chars: int,
        context_chunks: int,
        used_prior_session: bool = False,
    ) -> RefinerResult:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        tsec = effective_refiner_timeout_sec(self.cfg.refiner.timeout_sec, len(clean_draft))
        pacer_key = make_rate_pacer_key(self.provider_name, url, pacer_secret)

        def _attempt(req_timeout: float, wall_dl: float | None, _is_first: bool) -> tuple[str, _TokenUsageTuple, int]:
            resp = self.transport(
                HttpRequest(
                    url=url,
                    method="POST",
                    headers=headers,
                    body=body_bytes,
                    timeout_sec=req_timeout,
                    deadline=wall_dl,
                    cancel_token=cancel_token,
                )
            )
            if (cancel_token is not None and cancel_token.is_cancelled()) or (
                is_cancelled is not None and is_cancelled()
            ):
                raise _RefinerAttemptError("Refiner request cancelled", retry_kind="never")
            out_txt, usage = parse_fn(resp)
            return out_txt, usage, 0

        return _run_refiner_retry_loop(
            cfg=self.cfg,
            env=self.env,
            provider_name=self.provider_name,
            model=model,
            clean_draft=clean_draft,
            t0=t0,
            tsec=tsec,
            clock_fn=self.clock_fn,
            sleep_fn=self.sleep_fn,
            rand_fn=self.rand_fn,
            is_cancelled=is_cancelled,
            pacer_key_fn=lambda: pacer_key,
            secrets_fn=lambda: secrets,
            attempt_fn=_attempt,
            context_chars=context_chars,
            context_chunks=context_chunks,
            used_prior_session=used_prior_session,
            cancel_token=cancel_token,
        )


class NoopRefiner(BaseRefiner):
    provider_name = "off"

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        draft, single_hop = _unpack(asr_input)
        if single_hop and isinstance(asr_input, AsrResult):
            return RefinerResult(
                _clean_out(draft),
                "off",
                "",
                0,
                "single_hop_passthrough",
                input_tokens=asr_input.input_tokens,
                cached_input_tokens=asr_input.cached_input_tokens,
                output_tokens=asr_input.output_tokens,
                reasoning_tokens=asr_input.reasoning_tokens,
                context_chars=asr_input.context_chars,
                context_chunks=asr_input.context_chunks,
                audio_tokens=asr_input.audio_tokens,
            )
        return RefinerResult(_clean_out(draft), "off", "", 0, "single_hop_passthrough" if single_hop else "")


@dataclass(frozen=True)
class _ResponsesAuthTarget:
    url: str
    bearer_token: str
    identity_key: str
    account_id: str = ""
    is_codex_subscription: bool = False
    is_fedramp: bool = False
    can_refresh_on_401: bool = False


class _InstanceKeepAlivePool(_KeepAlivePool):
    """Per-refiner Keep-Alive pool reusing the unified HTTP proxy routing engine."""

    def __init__(self, env: Mapping[str, str], *, explicit_env: bool = False) -> None:
        super().__init__(max_per_origin=4, idle_timeout_sec=55.0)
        self._env = env
        self._explicit_env = explicit_env

    def _resolve_proxy_route(
        self, scheme: str, host: str, port: int
    ) -> tuple[_HttpProxyRoute | None, str]:
        if scheme != "https" or host in ("127.0.0.1", "localhost", "::1"):
            return None, ""
        src = self._env if self._explicit_env else os.environ
        if _proxy_bypass_no_dns(host, port, env=src):
            return None, ""
        proxies = _get_proxies_from_env(src) if self._explicit_env else urllib.request.getproxies()
        raw_proxy = _select_system_proxy_url(proxies, scheme)
        if not raw_proxy:
            return None, ""
        route, err = _resolve_http_proxy_route(raw_proxy, target_scheme=scheme)
        if route is None:
            safe_msg = _redact_proxy_error_text(
                f"HTTP proxy configuration error for {scheme}://{host}:{port}: {err}",
                raw_proxy,
                None,
            )
            raise HttpTransportError(safe_msg, retry_kind="never")
        return route, raw_proxy

    def _resolve_https_proxy(self, host: str, port: int) -> tuple[str, int, dict[str, str]] | None:
        route, _ = self._resolve_proxy_route("https", host, port)
        if route is None:
            return None
        hdrs = {"Proxy-Authorization": route.proxy_auth_header} if route.proxy_auth_header else {}
        return route.host, route.port, hdrs

    def _resolve_proxy_key(self, scheme: str, host: str, port: int) -> str:
        route, _ = self._resolve_proxy_route(scheme, host, port)
        return route.proxy_key if route is not None else ""

    def acquire(
        self,
        scheme: str,
        host: str,
        port: int,
        timeout_sec: float,
        *,
        allow_reuse: bool = True,
        connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC,
        io_deadline: float | None = None,
        proxy_key: str | None = None,
        proxy_route: _HttpProxyRoute | None = None,
    ) -> tuple[http.client.HTTPConnection, bool]:
        raw_proxy = ""
        if proxy_route is None and proxy_key is None:
            proxy_route, raw_proxy = self._resolve_proxy_route(scheme, host, port)
            proxy_key = proxy_route.proxy_key if proxy_route is not None else ""
        conn, reused = super().acquire(
            scheme,
            host,
            port,
            timeout_sec,
            allow_reuse=allow_reuse,
            connect_timeout_sec=connect_timeout_sec,
            io_deadline=io_deadline,
            proxy_key=proxy_key,
            proxy_route=proxy_route,
        )
        setattr(conn, "_avi_proxy_raw", raw_proxy)
        setattr(conn, "_avi_proxy_route", proxy_route)
        return conn, reused

    def release(
        self,
        scheme: str,
        host: str,
        port: int,
        conn: http.client.HTTPConnection,
        *,
        proxy_key: str | None = None,
    ) -> None:
        if proxy_key is None:
            try:
                proxy_key = self._resolve_proxy_key(scheme, host, port)
            except HttpTransportError:
                _close_quietly(conn)
                return
        super().release(scheme, host, port, conn, proxy_key=proxy_key)


class OpenAIResponsesRefiner(BaseRefiner):
    """Purely stateless Stage 2 refiner over OpenAI/Codex ``/responses`` SSE with ``developer`` context chunks.

    REDLINE: Never sends ``previous_response_id`` (`store=false`, `stream=true`) and strictly
    forbids ``service_tier="fast"`` or any Fast-mode flag.
    """

    provider_name = "openai"

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials | None = None,
        *,
        provider_name: str | None = None,
        env: Mapping[str, str] | None = None,
        transport: HttpTransport | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        rand_fn: Callable[[], float] = random.random,
    ) -> None:
        self._explicit_env = env is not None and env is not os.environ
        self.cfg = cfg
        self.env: Mapping[str, str] = os.environ if env is None else dict(env)
        self.creds = creds or resolve_credentials(cfg, self.env)
        self.transport = transport
        self.clock_fn, self.sleep_fn, self.rand_fn = clock_fn, sleep_fn, rand_fn

        resolved_prov = (provider_name or cfg.refiner.provider or "openai").strip().lower()
        self.provider_name = resolved_prov
        if resolved_prov == "openai-compatible":
            if not cfg.refiner.base_url.strip():
                raise ConfigError("refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'")
            if not cfg.refiner.model.strip():
                raise ConfigError("refiner.model must be explicitly set when refiner.provider is 'openai-compatible'")

        self._default_session_uuid = str(uuid.uuid4())
        self._pool = _InstanceKeepAlivePool(self.env, explicit_env=self._explicit_env)
        self._state_lock = threading.Lock()
        self._prewarm_lock = threading.Lock()
        self._prewarm_active = 0
        self._prewarm_threads: set[threading.Thread] = set()
        self._active_conns: set[http.client.HTTPConnection] = set()
        self._cookies: OrderedDict[str, str] = OrderedDict()
        self._last_identity_key: str = ""
        self._closed = False

    def _resolve_endpoint_and_auth(
        self,
        *,
        force_refresh: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> _ResponsesAuthTarget:
        _ = (force_refresh, is_cancelled)
        base = self.cfg.refiner.effective_base_url(self.provider_name).strip()
        if self.provider_name == "openai-compatible" and not base:
            raise ConfigError("refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'")
        url = _build_responses_url(
            base,
            default_base_url=OPENAI_DEFAULT_BASE_URL,
            provider_name=self.provider_name,
        )
        key = _resolve_refiner_api_key(self.cfg, self.creds, self.provider_name, url)
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else "anonymous"
        return _ResponsesAuthTarget(url=url, bearer_token=key, identity_key=f"{origin}|key:{key_hash}")

    def _extra_redaction_secrets(self, auth_target: _ResponsesAuthTarget | None = None) -> tuple[str, ...]:
        items: list[str] = list(self.creds.all_secret_values())
        if auth_target is not None:
            if auth_target.bearer_token:
                items.append(auth_target.bearer_token)
            if auth_target.account_id:
                items.append(auth_target.account_id)
        return tuple(items)

    def _sync_auth_identity(self, auth_target: _ResponsesAuthTarget) -> None:
        reset_pool = False
        with self._state_lock:
            if self._last_identity_key and self._last_identity_key != auth_target.identity_key:
                self._cookies.clear()
                reset_pool = True
            self._last_identity_key = auth_target.identity_key
        if reset_pool:
            self._pool.close_all()

    def _ingest_set_cookie_headers(self, raw_set_cookies: Sequence[str], identity_key: str) -> None:
        if not raw_set_cookies:
            return
        with self._state_lock:
            if self._last_identity_key and self._last_identity_key != identity_key:
                return
            for raw_sc in raw_set_cookies:
                if not raw_sc:
                    continue
                segments = [seg.strip() for seg in raw_sc.split(";")]
                if not segments or "=" not in segments[0]:
                    continue
                cname, cval = (x.strip() for x in segments[0].split("=", 1))
                if cname not in ALLOWED_AFFINITY_COOKIES:
                    continue
                if not cval or any(attr.lower().replace(" ", "") == "max-age=0" for attr in segments[1:]):
                    self._cookies.pop(cname, None)
                else:
                    self._cookies[cname] = cval

    def _format_cookie_header(self, identity_key: str) -> str:
        with self._state_lock:
            if (self._last_identity_key and self._last_identity_key != identity_key) or not self._cookies:
                return ""
            return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    @staticmethod
    def _is_conn_stale(conn: http.client.HTTPConnection) -> bool:
        return _is_idle_socket_stale(getattr(conn, "sock", None))

    @staticmethod
    def _shutdown_and_close_conn(conn: http.client.HTTPConnection) -> None:
        if callable(abort_fn := getattr(conn, "abort_inflight", None)):
            abort_fn()
        elif (sock := getattr(conn, "sock", None)) is not None:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except Exception:
                pass
        _close_quietly(conn)

    def _acquire_conn(
        self,
        scheme: str,
        host: str,
        port: int,
        io_timeout: float,
        *,
        allow_reuse: bool,
        conn_timeout: float,
        io_deadline: float | None = None,
    ) -> tuple[http.client.HTTPConnection, bool]:
        conn, reused = self._pool.acquire(
            scheme,
            host,
            port,
            io_timeout,
            allow_reuse=allow_reuse,
            connect_timeout_sec=conn_timeout,
            io_deadline=io_deadline,
        )
        if reused and self._is_conn_stale(conn):
            _close_quietly(conn)
            conn, reused = self._pool.acquire(
                scheme,
                host,
                port,
                io_timeout,
                allow_reuse=False,
                connect_timeout_sec=conn_timeout,
                io_deadline=io_deadline,
            )
        abort = False
        with self._state_lock:
            if self._closed:
                abort = True
            else:
                self._active_conns.add(conn)
        if abort:
            self._shutdown_and_close_conn(conn)
            raise RuntimeError("refiner closed")
        return conn, reused

    def _finish_conn(
        self,
        conn: http.client.HTTPConnection,
        *,
        scheme: str,
        host: str,
        port: int,
        identity_key: str,
        can_reuse: bool,
    ) -> None:
        pooled = False
        with self._state_lock:
            self._active_conns.discard(conn)
            if can_reuse and not self._closed and self._last_identity_key == identity_key:
                self._pool.release(scheme, host, port, conn)
                pooled = True
        if not pooled:
            _close_quietly(conn)

    def prepare_session(self, session_key: str = "") -> None:
        if self._closed or self.transport is not None:
            return
        with self._state_lock:
            if self._closed or self._prewarm_active > 0:
                return
            self._prewarm_active += 1
        th = threading.Thread(target=self._run_prewarm, name="avi-responses-prewarm", daemon=True)
        with self._state_lock:
            if self._closed:
                self._prewarm_active = max(0, self._prewarm_active - 1)
                return
            self._prewarm_threads.add(th)
        try:
            th.start()
        except Exception:
            with self._state_lock:
                self._prewarm_threads.discard(th)
                self._prewarm_active = max(0, self._prewarm_active - 1)

    def _run_prewarm(self) -> None:
        try:
            with self._prewarm_lock:
                if self._closed:
                    return
                auth_target = self._resolve_endpoint_and_auth(force_refresh=False)
                self._sync_auth_identity(auth_target)
                _, origin = _validate_request_url(auth_target.url)
                scheme, host, port, _, _ = origin
                assert port is not None
                conn_t = min(DEFAULT_CONNECT_TIMEOUT_SEC, max(0.2, float(self.cfg.refiner.timeout_sec)))
                conn, reused = self._acquire_conn(
                    scheme, host, port, conn_t, allow_reuse=True, conn_timeout=conn_t
                )
                can_reuse = False
                try:
                    if not reused:
                        conn.connect()
                    can_reuse = True
                finally:
                    self._finish_conn(
                        conn,
                        scheme=scheme,
                        host=host,
                        port=port,
                        identity_key=auth_target.identity_key,
                        can_reuse=can_reuse,
                    )
        except Exception:
            pass
        finally:
            with self._state_lock:
                self._prewarm_active = max(0, self._prewarm_active - 1)
                self._prewarm_threads.discard(threading.current_thread())

    def _build_headers(
        self,
        *,
        auth_target: _ResponsesAuthTarget,
        model: str,
        session_uuid: str,
    ) -> dict[str, str]:
        hdrs: dict[str, str] = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        if auth_target.bearer_token:
            hdrs["Authorization"] = f"Bearer {auth_target.bearer_token}"
        if auth_target.is_codex_subscription:
            if auth_target.account_id:
                hdrs["ChatGPT-Account-ID"] = auth_target.account_id
            if auth_target.is_fedramp:
                hdrs["X-OpenAI-Fedramp"] = "true"
            hdrs["originator"] = CODEX_ORIGINATOR
            hdrs["User-Agent"] = CODEX_USER_AGENT
            hdrs["session-id"] = session_uuid
            hdrs["thread-id"] = session_uuid
            hdrs["x-client-request-id"] = str(uuid.uuid4())
            hdrs["x-codex-routing-hint"] = f"model={model}"
        else:
            hdrs["User-Agent"] = DEFAULT_USER_AGENT
        if cookie_hdr := self._format_cookie_header(auth_target.identity_key):
            hdrs["Cookie"] = cookie_hdr
        return hdrs

    def _build_payload(
        self,
        *,
        model: str,
        effort: str,
        instructions: str,
        context_blocks: Sequence[str],
        final_block: str,
        session_uuid: str,
        is_codex_subscription: bool = False,
    ) -> dict[str, Any]:
        # Deliberately keep role="developer" for <session_reference_log>: chatgpt.com/backend-api/codex/responses
        # with store=false only indexes leading developer blocks synchronously for prefix caching (~91% vs 0% on user).
        input_items: list[dict[str, Any]] = [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": blk}]}
            for blk in context_blocks
        ]
        input_items.append(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": final_block}]}
        )
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "prompt_cache_key": session_uuid,
        }
        eff = (effort or "").strip().lower()
        if is_codex_subscription:
            if eff in _CONCRETE_RESPONSES_EFFORTS:
                payload["reasoning"] = {"effort": eff}
        elif self.provider_name == "openrouter":
            if eff == "default":
                pass
            elif _openrouter_reasoning_mandatory(model):
                payload["reasoning"] = {
                    "effort": "minimal"
                    if eff in ("", "auto", "none")
                    else ("xhigh" if eff == "max" else eff)
                }
            elif eff == "auto":
                if _is_openrouter_thinking_model(model):
                    payload["reasoning"] = {"effort": "none"}
            elif eff in _CONCRETE_RESPONSES_EFFORTS:
                payload["reasoning"] = {"effort": "xhigh" if eff == "max" else eff}
        elif eff in _CONCRETE_RESPONSES_EFFORTS and _is_openai_reasoning_model(model):
            wire_eff = "xhigh" if (eff == "max" and self.provider_name in ("openai", "codex")) else eff
            payload["reasoning"] = {"effort": wire_eff}
        return payload

    def _execute_streaming_attempt(
        self,
        *,
        auth_target: _ResponsesAuthTarget,
        model: str,
        session_uuid: str,
        body_bytes: bytes,
        req_timeout: float,
        wall_deadline: float | None,
        secrets: Sequence[str],
        eff_cancelled: Callable[[], bool],
        measure_init: bool,
        t_init_start: float,
        allow_empty_output: bool = False,
        cancel_token: CancelToken | None = None,
    ) -> tuple[str, _TokenUsageTuple, int]:
        if self.transport is not None:
            init_ms = _ms(t_init_start, self.clock_fn) if measure_init else 0
            hdrs = self._build_headers(auth_target=auth_target, model=model, session_uuid=session_uuid)
            resp = self.transport(
                HttpRequest(
                    url=auth_target.url,
                    method="POST",
                    headers=hdrs,
                    body=body_bytes,
                    timeout_sec=req_timeout,
                    deadline=wall_deadline,
                    cancel_token=cancel_token,
                )
            )
            if eff_cancelled():
                return "", _EMPTY_USAGE, init_ms
            if sc_val := (resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie")):
                self._ingest_set_cookie_headers([sc_val], auth_target.identity_key)
            if resp.status != 200:
                self._raise_non_200_error(resp.status, resp.headers, resp.body, secrets)
            lines = iter(resp.body.splitlines(keepends=True))
            out_txt, usage, _ = _parse_responses_sse_lines(
                lambda: next(lines, b""),
                provider_name=self.provider_name,
                resp_headers=resp.headers,
                is_cancelled=eff_cancelled,
                allow_empty_output=allow_empty_output,
            )
            return out_txt, usage, init_ms

        parsed, origin = _validate_request_url(auth_target.url)
        scheme, host, port, _, _ = origin
        assert port is not None
        path = parsed.path or "/"
        target_path = f"{path}?{parsed.query}" if parsed.query else path
        conn_timeout = min(DEFAULT_CONNECT_TIMEOUT_SEC, max(0.05, req_timeout))

        conn: http.client.HTTPConnection | None = None
        resp: http.client.HTTPResponse | None = None
        hook_id = 0
        init_ms = 0

        for stale_attempt in range(2):
            if eff_cancelled():
                return "", _EMPTY_USAGE, init_ms
            eff_io_t = req_timeout
            if wall_deadline is not None:
                rem_io = wall_deadline - time.monotonic()
                if rem_io <= 0:
                    raise TimeoutError("Responses HTTP request exceeded deadline before send")
                eff_io_t = max(0.005, min(req_timeout, rem_io))

            conn, reused = self._acquire_conn(
                scheme,
                host,
                port,
                eff_io_t,
                allow_reuse=(stale_attempt == 0),
                conn_timeout=conn_timeout,
                io_deadline=wall_deadline,
            )
            hook_id = _register_conn_abort(cancel_token, conn)
            try:
                t_conn0 = self.clock_fn()
                if not reused:
                    conn.connect()
                if measure_init and stale_attempt == 0:
                    init_ms = _ms(t_init_start, self.clock_fn)
                elif not reused:
                    init_ms += _ms(t_conn0, self.clock_fn)

                hdrs = self._build_headers(auth_target=auth_target, model=model, session_uuid=session_uuid)
                conn.request("POST", target_path, body=body_bytes, headers=hdrs)
                if wall_deadline is not None:
                    rem_hdr = wall_deadline - time.monotonic()
                    if rem_hdr <= 0:
                        raise TimeoutError("Responses HTTP header wait exceeded total deadline")
                    if (sock := getattr(conn, "sock", None)) is not None:
                        sock.settimeout(max(0.005, min(req_timeout, rem_hdr)))
                resp = conn.getresponse()
                break
            except BaseException as exc:
                _unregister_conn_abort(cancel_token, hook_id)
                hook_id = 0
                was_aborted = bool(getattr(conn, "_aborted", False)) or eff_cancelled()
                proxy_raw = str(getattr(conn, "_avi_proxy_raw", "") or "")
                proxy_route = getattr(conn, "_avi_proxy_route", None)
                self._finish_conn(
                    conn,
                    scheme=scheme,
                    host=host,
                    port=port,
                    identity_key=auth_target.identity_key,
                    can_reuse=False,
                )
                conn = None
                if (
                    isinstance(exc, _RESPONSES_STALE_CONN_ERRORS)
                    and not isinstance(exc, ssl.SSLCertVerificationError)
                    and not was_aborted
                    and reused
                    and stale_attempt == 0
                ):
                    continue
                if (
                    not was_aborted
                    and isinstance(exc, (TimeoutError, socket.timeout, http.client.HTTPException, OSError))
                    and (proxy_raw or proxy_route is not None)
                ):
                    safe_msg = _redact_proxy_error_text(str(exc), proxy_raw, proxy_route)
                    raise HttpTransportError(safe_msg, retry_kind=classify_transport_error(exc)) from None
                raise

        assert conn is not None and resp is not None
        proxy_raw = str(getattr(conn, "_avi_proxy_raw", "") or "")
        proxy_route = getattr(conn, "_avi_proxy_route", None)
        can_reuse = False
        try:
            if eff_cancelled() or bool(getattr(conn, "_aborted", False)):
                return "", _EMPTY_USAGE, init_ms
            if raw_scs := (resp.headers.get_all("Set-Cookie") or []):
                self._ingest_set_cookie_headers(raw_scs, auth_target.identity_key)

            status = int(getattr(resp, "status", None) or resp.getcode() or 200)
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if status != 200:
                err_body = resp.read(65536)
                if eff_cancelled() or bool(getattr(conn, "_aborted", False)):
                    return "", _EMPTY_USAGE, init_ms
                self._raise_non_200_error(status, resp_hdrs, err_body, secrets)

            # CRITICAL: Codex backend HTTP 200 omits Content-Type header; never check Content-Type!
            out_txt, usage, saw_eof = _parse_responses_sse_lines(
                resp.readline,
                provider_name=self.provider_name,
                resp_headers=resp_hdrs,
                is_cancelled=eff_cancelled,
                allow_empty_output=allow_empty_output,
            )
            eof_drained = saw_eof and (
                (bool(getattr(resp, "chunked", False)) and resp.isclosed())
                or (not getattr(resp, "chunked", False) and getattr(resp, "length", None) == 0)
            )
            can_reuse = _can_reuse_pooled_conn(
                conn,
                resp,
                resp_hdrs.get("connection", ""),
                body_drained=eof_drained,
                aborted=bool(getattr(conn, "_aborted", False) or eff_cancelled()),
            )
            return out_txt, usage, init_ms
        except (TimeoutError, socket.timeout, http.client.HTTPException, OSError) as exc:
            if (
                not (bool(getattr(conn, "_aborted", False)) or eff_cancelled())
                and (proxy_raw or proxy_route is not None)
            ):
                safe_msg = _redact_proxy_error_text(str(exc), proxy_raw, proxy_route)
                raise HttpTransportError(safe_msg, retry_kind=classify_transport_error(exc)) from None
            raise
        finally:
            _unregister_conn_abort(cancel_token, hook_id)
            if bool(getattr(conn, "_aborted", False)) or eff_cancelled():
                can_reuse = False
            _close_quietly(resp)
            self._finish_conn(
                conn,
                scheme=scheme,
                host=host,
                port=port,
                identity_key=auth_target.identity_key,
                can_reuse=can_reuse,
            )

    def _raise_non_200_error(
        self,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        secrets: Sequence[str],
    ) -> None:
        raw_text = body.decode("utf-8", errors="replace").strip()
        try:
            parsed_json = json.loads(raw_text) if raw_text else {}
            non_json = not isinstance(parsed_json, dict)
            data: Mapping[str, Any] = parsed_json if isinstance(parsed_json, dict) else {}
        except ValueError:
            data, non_json = {}, True

        if non_json:
            err_code, err_msg, classify_data = "", raw_text or f"status {status}", None
        else:
            err_dict, err_msg = _extract_responses_http_error(data, raw_text)
            err_code = str(err_dict.get("code") or "").strip()
            classify_data = data if "error" in data else {"error": err_dict}

        excerpt = redact_secrets(err_msg, self.env, cfg=self.cfg, extra_secrets=secrets)[:180]
        rkind, rafter, is_conc = classify_http_failure(
            self.provider_name, status, headers, classify_data, non_json=non_json
        )
        raise _RefinerAttemptError(
            f"HTTP {status}: {excerpt}",
            retry_kind=rkind,
            retry_after_sec=rafter,
            is_concurrency_limit=is_conc,
            http_status=status,
            error_code=err_code,
        )

    def _select_request_model(self) -> str:
        return self.cfg.refiner.effective_model(self.provider_name)

    def _maybe_switch_fallback_model(self, err: _RefinerAttemptError, current_model: str) -> str | None:
        _ = (err, current_model)
        return None

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        model = self._select_request_model()
        if self.provider_name == "openai-compatible":
            if not self.cfg.refiner.base_url.strip():
                raise ConfigError("refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'")
            if not model.strip():
                raise ConfigError("refiner.model must be explicitly set when refiner.provider is 'openai-compatible'")
        eff = self.cfg.refiner.effective_effort(self.provider_name)
        if eff not in VALID_EFFORTS:
            raise ConfigError(f"refiner.effort must be one of {sorted(VALID_EFFORTS)}, got {self.cfg.refiner.effort!r}")
        clean_draft, single_hop = _unpack(asr_input)
        if not clean_draft or (single_hop and not self.cfg.refiner.force_on_single_hop):
            return NoopRefiner().refine(asr_input, mode=mode)

        t0 = self.clock_fn()
        _cancel_kind = lambda: _cancel_fallback_kind(cancel_token, is_cancelled) or (
            "shutdown" if self._closed else "superseded"
        )
        eff_cancelled = lambda: (
            self._closed
            or (cancel_token is not None and cancel_token.is_cancelled())
            or bool(is_cancelled is not None and is_cancelled())
        )
        if eff_cancelled():
            return _fallback_result(clean_draft, self.provider_name, model, t0, self.clock_fn, _cancel_kind())

        context_blocks, final_block = build_refiner_request_blocks(
            self.cfg,
            clean_draft,
            session_key=session_key,
            tracker=tracker,
            fallback_context=fallback_context,
            mode=mode,
            workspace_cwd=workspace_cwd,
            target_hint=target_hint,
        )
        ctx_chunks = len(context_blocks)
        ctx_chars = sum(len(b) for b in context_blocks)
        used_prior = any(b.startswith("<prior_session_reference") for b in context_blocks)
        session_uuid = _derive_session_uuid(
            session_key or (f"ws:{workspace_cwd}" if workspace_cwd else ""),
            self._default_session_uuid,
        )
        instructions = _compose_codex_instructions(resolve_refiner_prompt(self.cfg, mode=mode))
        tsec = effective_refiner_timeout_sec(self.cfg.refiner.timeout_sec, len(clean_draft))

        t_init0 = self.clock_fn()
        with self._state_lock:
            wait_prewarm = self._prewarm_active > 0
        if wait_prewarm:
            while not self._prewarm_lock.acquire(timeout=0.05):
                if eff_cancelled():
                    return _fallback_result(
                        clean_draft, self.provider_name, model, t0, self.clock_fn, _cancel_kind(),
                        context_chars=ctx_chars, context_chunks=ctx_chunks,
                    )
            self._prewarm_lock.release()

        try:
            auth_box = [self._resolve_endpoint_and_auth(force_refresh=False, is_cancelled=eff_cancelled)]
            self._sync_auth_identity(auth_box[0])
        except Exception as exc:
            if eff_cancelled():
                return _fallback_result(
                    clean_draft, self.provider_name, model, t0, self.clock_fn, _cancel_kind(),
                    context_chars=ctx_chars, context_chunks=ctx_chunks,
                )
            warn = redact_secrets(str(exc), self.env, cfg=self.cfg, extra_secrets=self._extra_redaction_secrets(None))
            return _fallback_result(
                clean_draft, self.provider_name, model, t0, self.clock_fn, "asr_draft", warn,
                context_chars=ctx_chars, context_chunks=ctx_chunks,
            )

        model_box = [model]

        def _encode_body(target: _ResponsesAuthTarget, req_model: str) -> bytes:
            payload = self._build_payload(
                model=req_model,
                effort=eff,
                instructions=instructions,
                context_blocks=context_blocks,
                final_block=final_block,
                session_uuid=session_uuid,
                is_codex_subscription=target.is_codex_subscription,
            )
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        body_box = [_encode_body(auth_box[0], model_box[0])]

        def _refresh_401() -> None:
            new_auth = self._resolve_endpoint_and_auth(force_refresh=True, is_cancelled=eff_cancelled)
            self._sync_auth_identity(new_auth)
            if new_auth.is_codex_subscription != auth_box[0].is_codex_subscription:
                body_box[0] = _encode_body(new_auth, model_box[0])
            auth_box[0] = new_auth

        def _switch_model(err: _RefinerAttemptError) -> bool:
            fb_mod = self._maybe_switch_fallback_model(err, model_box[0])
            if not fb_mod:
                return False
            model_box[0] = fb_mod
            body_box[0] = _encode_body(auth_box[0], fb_mod)
            return True

        return _run_refiner_retry_loop(
            cfg=self.cfg,
            env=self.env,
            provider_name=self.provider_name,
            model=model_box[0],
            model_fn=lambda: model_box[0],
            clean_draft=clean_draft,
            t0=t0,
            tsec=tsec,
            clock_fn=self.clock_fn,
            sleep_fn=self.sleep_fn,
            rand_fn=self.rand_fn,
            is_cancelled=eff_cancelled,
            pacer_key_fn=lambda: make_rate_pacer_key(
                self.provider_name, auth_box[0].url, auth_box[0].bearer_token or auth_box[0].identity_key
            ),
            secrets_fn=lambda: self._extra_redaction_secrets(auth_box[0]),
            attempt_fn=lambda req_timeout, wall_dl, is_first: self._execute_streaming_attempt(
                auth_target=auth_box[0],
                model=model_box[0],
                session_uuid=session_uuid,
                body_bytes=body_box[0],
                req_timeout=req_timeout,
                wall_deadline=wall_dl,
                secrets=self._extra_redaction_secrets(auth_box[0]),
                eff_cancelled=eff_cancelled,
                measure_init=is_first,
                t_init_start=t_init0,
                allow_empty_output=(len(clean_draft) <= 6),
                cancel_token=cancel_token,
            ),
            on_401_refresh=(_refresh_401 if auth_box[0].can_refresh_on_401 else None),
            on_model_fallback=_switch_model,
            context_chars=ctx_chars,
            context_chunks=ctx_chunks,
            used_prior_session=used_prior,
            cancel_token=cancel_token,
        )

    def evict_connections(self) -> int:
        return self._pool.evict_all()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            active = list(self._active_conns)
            self._active_conns.clear()
            threads = list(self._prewarm_threads)
            self._cookies.clear()
        for conn in active:
            self._shutdown_and_close_conn(conn)
        self._pool.close_all()
        deadline = time.monotonic() + 1.0
        for th in threads:
            if (rem := deadline - time.monotonic()) <= 0:
                break
            if th.is_alive() and th is not threading.current_thread():
                th.join(timeout=rem)


@dataclass(frozen=True)
class _CachedCodexCredentials:
    mode: str  # "chatgpt" | "api_key"
    token: str
    account_id: str = ""
    exp: int | None = None
    is_fedramp: bool = False
    source: str = "auth.json"


class CodexRefiner(OpenAIResponsesRefiner):
    """Stateless HTTP ``/responses`` Codex refiner with 30-minute proactive token refresh & Keyring support.

    REDLINE: Both HTTP refinement requests and short-lived ``getAuthStatus`` refresh helper
    processes strictly forbid Fast mode (`service_tier="fast"` is never sent).
    """

    provider_name = "codex"

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials | None = None,
        *,
        env: Mapping[str, str] | None = None,
        runtime_dir: Path | None = None,
        transport: HttpTransport | None = None,
        app_server_cmd: list[str] | None = None,
        time_fn: Callable[[], float] = time.time,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        rand_fn: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            cfg,
            creds,
            provider_name="codex",
            env=env,
            transport=transport,
            clock_fn=clock_fn,
            sleep_fn=sleep_fn,
            rand_fn=rand_fn,
        )
        self.runtime_dir = runtime_dir
        self._app_server_cmd = list(app_server_cmd) if app_server_cmd is not None else None
        self.time_fn = time_fn
        self._auth_lock = threading.Lock()
        self._cached_creds: _CachedCodexCredentials | None = None
        self._cached_mtime_ns: int | None = None
        self._refresh_cooldown_until: float = -1e9
        self._refresh_procs: set[subprocess.Popen[bytes]] = set()
        self._runtime_fallback_model: str = ""

    def _resolve_codex_home(self) -> Path | None:
        return resolve_codex_home_dir(self.env if self._explicit_env else None)

    def _sync_auth_identity(self, auth_target: _ResponsesAuthTarget) -> None:
        reset_pool = False
        with self._state_lock:
            if self._last_identity_key and self._last_identity_key != auth_target.identity_key:
                self._cookies.clear()
                self._runtime_fallback_model = ""
                reset_pool = True
            self._last_identity_key = auth_target.identity_key
        if reset_pool:
            self._pool.close_all()

    def _select_request_model(self) -> str:
        if self.cfg.refiner.has_explicit_model("codex"):
            return self.cfg.refiner.effective_model("codex")
        with self._state_lock:
            if self._runtime_fallback_model:
                return self._runtime_fallback_model
        return resolve_codex_default_model(self.cfg, self.env if self._explicit_env else None)

    def _pick_runtime_fallback_model(self, failed_model: str) -> str:
        slugs = [
            s
            for s in _read_codex_models_cache_slugs(self.env if self._explicit_env else None)
            if s != failed_model
        ]
        if slugs:
            slug_set = set(slugs)
            for cand in CODEX_PUBLIC_FALLBACK_MODELS:
                if cand in slug_set:
                    return cand
            for s in slugs:
                if "mini" in s.lower() or "luna" in s.lower():
                    return s
        for cand in CODEX_PUBLIC_FALLBACK_MODELS:
            if cand != failed_model:
                return cand
        return CODEX_PUBLIC_FALLBACK_MODELS[0]

    def _maybe_switch_fallback_model(self, err: _RefinerAttemptError, current_model: str) -> str | None:
        if self.cfg.refiner.has_explicit_model("codex") or not _is_codex_model_unsupported_error(err):
            return None
        with self._state_lock:
            if self._runtime_fallback_model:
                return self._runtime_fallback_model if current_model != self._runtime_fallback_model else None
            fb = self._pick_runtime_fallback_model(current_model)
            if not fb or fb == current_model:
                return None
            self._runtime_fallback_model = fb
            return fb

    def _resolve_auth_json_path(self) -> Path | None:
        ch = self._resolve_codex_home()
        return (ch / "auth.json") if ch is not None else None

    def _parse_auth_json_file(self, auth_path: Path) -> tuple[_CachedCodexCredentials | None, int | None]:
        try:
            st = os.stat(auth_path)
            raw = auth_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise RuntimeError(f"cannot read codex auth file {auth_path.name}: {exc}") from None

        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(f"invalid JSON in codex {auth_path.name}: {exc}") from None
        if not isinstance(data, Mapping):
            raise RuntimeError(f"invalid root in codex {auth_path.name}")

        mode, token_or_key, account_id = classify_codex_auth_json(
            data, self.env, allow_empty_auth_env_key=True
        )
        if mode == "api_key":
            return _CachedCodexCredentials(mode="api_key", token=token_or_key, source="auth.json"), st.st_mtime_ns
        if mode != "chatgpt":
            return None, st.st_mtime_ns
        exp, jwt_acct_id, is_fedramp = _extract_jwt_metadata(token_or_key)
        return (
            _CachedCodexCredentials(
                mode="chatgpt",
                token=token_or_key,
                account_id=account_id or jwt_acct_id,
                exp=exp,
                is_fedramp=is_fedramp,
                source="auth.json",
            ),
            st.st_mtime_ns,
        )

    def _build_auth_refresh_cmd(self) -> list[str]:
        if self._app_server_cmd is not None:
            return list(self._app_server_cmd)
        cbin = find_codex_bin(self.env)
        if not cbin:
            raise RuntimeError("codex binary not found (install codex or log in via 'codex login')")
        return [
            cbin,
            *(x for c in _PURE_CHAT_CONFIG_OVERRIDES for x in ("-c", c)),
            *(x for f in _DISABLE for x in ("--disable", f)),
            "--enable",
            "skip_host_skill_discovery",
            "--sandbox",
            "read-only",
            "-a",
            "never",
            "app-server",
            "--stdio",
        ]

    @staticmethod
    def _terminate_proc(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.poll() is not None:
                break
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=0.35)
                break
            except subprocess.TimeoutExpired:
                continue
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            _close_quietly(pipe)

    @staticmethod
    def _build_codex_subprocess_path(exe: str, cur_path: str, home: Path | None) -> str:
        """Ensure ``Path(exe).parent`` and standard Node/Homebrew dirs are in ``PATH`` for ``#!/usr/bin/env node``."""
        prepend: list[str] = []
        if exe and ("/" in exe or "\\" in exe):
            exe_p = Path(exe)
            if str(exe_p.parent):
                prepend.append(str(exe_p.parent))
            try:
                if exe_p.is_symlink():
                    real_p = exe_p.resolve()
                    if str(real_p.parent) and str(real_p.parent) not in prepend:
                        prepend.append(str(real_p.parent))
                    for anc in real_p.parents:
                        if anc.name == "lib":
                            prefix_bin = str(anc.parent / "bin")
                            if prefix_bin not in prepend:
                                prepend.append(prefix_bin)
                            break
            except OSError:
                pass
        existing = [seg for seg in (cur_path or "").split(os.pathsep) if seg]
        fallbacks: list[str] = []
        if home is not None:
            fallbacks.extend(
                str(home / rel)
                for rel in (
                    ".local/bin",
                    ".volta/bin",
                    ".local/share/mise/shims",
                    ".asdf/shims",
                    ".bun/bin",
                )
            )
        fallbacks.extend(
            ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin")
        )
        merged: list[str] = []
        seen: set[str] = set()
        for entry in (*prepend, *existing, *fallbacks):
            if entry and entry not in seen:
                seen.add(entry)
                merged.append(entry)
        return os.pathsep.join(merged)

    def _call_codex_get_auth_status(
        self,
        *,
        timeout_sec: float = CODEX_AUTH_RPC_TIMEOUT_SEC,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[str, str]:
        """Run one-shot ``codex app-server`` ``initialize`` + ``getAuthStatus(includeToken, refreshToken)``."""
        if self._closed or (is_cancelled is not None and is_cancelled()):
            raise RuntimeError("codex refiner closed or cancelled")
        cmd = self._build_auth_refresh_cmd()
        proc_env = _clean_env(self.env)
        proc_env.pop("OPENAI_API_KEY", None)
        proc_env.pop("OPENAI_BASE_URL", None)
        for k in list(proc_env.keys()):
            if "FAST" in k.upper() and "CODEX" in k.upper():
                proc_env.pop(k, None)
        codex_home = self._resolve_codex_home()
        if codex_home is not None:
            proc_env["CODEX_HOME"] = str(codex_home)
        raw_home = (proc_env.get("HOME") or "").strip()
        home_dir = Path(raw_home) if raw_home else (codex_home.parent if codex_home is not None else None)
        proc_env["PATH"] = self._build_codex_subprocess_path(
            cmd[0] if cmd else "", proc_env.get("PATH", ""), home_dir
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=proc_env,
            start_new_session=True,
        )
        abort_now = False
        with self._state_lock:
            if self._closed or (is_cancelled is not None and is_cancelled()):
                abort_now = True
            else:
                self._refresh_procs.add(proc)
        if abort_now:
            self._terminate_proc(proc)
            raise RuntimeError("codex refiner closed or cancelled")

        deadline = time.monotonic() + max(0.2, min(CODEX_AUTH_RPC_TIMEOUT_SEC, float(timeout_sec)))
        rx_buf = bytearray()

        def _send(payload: Mapping[str, Any]) -> None:
            if proc.stdin is None or proc.poll() is not None:
                raise RuntimeError("codex app-server closed during auth check")
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def _wait_id(sel: selectors.BaseSelector, stdout_fd: int, expected_id: int) -> dict[str, Any]:
            while True:
                if self._closed or (is_cancelled is not None and is_cancelled()):
                    raise RuntimeError("codex auth check cancelled")
                while (nl := rx_buf.find(b"\n")) >= 0:
                    raw_line = bytes(rx_buf[:nl]).strip()
                    del rx_buf[: nl + 1]
                    if not raw_line:
                        continue
                    try:
                        msg = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == expected_id and "method" not in msg:
                        if err := msg.get("error"):
                            raise RuntimeError(f"codex RPC {expected_id} error: {err}")
                        res = msg.get("result")
                        return res if isinstance(res, dict) else {}
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError("codex getAuthStatus timed out")
                if not sel.select(min(0.05, rem)):
                    continue
                chunk = os.read(stdout_fd, 8192)
                if not chunk:
                    raise RuntimeError("codex app-server exited before getAuthStatus completed")
                rx_buf.extend(chunk)

        try:
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            with selectors.DefaultSelector() as sel:
                sel.register(stdout_fd, selectors.EVENT_READ)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "agent-voice-input", "version": __version__},
                            "capabilities": {"experimentalApi": True},
                        },
                    }
                )
                _wait_id(sel, stdout_fd, 1)
                _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "getAuthStatus",
                        "params": {"includeToken": True, "refreshToken": True},
                    }
                )
                res = _wait_id(sel, stdout_fd, 2)
            auth_method = str(res.get("authMethod") or res.get("auth_method") or "").strip().lower()
            auth_token = str(res.get("authToken") or res.get("auth_token") or "").strip()
            return auth_method, auth_token
        finally:
            with self._state_lock:
                self._refresh_procs.discard(proc)
            self._terminate_proc(proc)

    def _to_auth_target(self, creds_item: _CachedCodexCredentials) -> _ResponsesAuthTarget:
        is_sub = creds_item.mode != "api_key"
        url = _build_responses_url(
            self.cfg.refiner.base_url,
            default_base_url=CODEX_SUBSCRIPTION_BASE_URL if is_sub else OPENAI_DEFAULT_BASE_URL,
        )
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        tok_hash = hashlib.sha256(creds_item.token.encode("utf-8")).hexdigest()[:16]
        if not is_sub:
            return _ResponsesAuthTarget(
                url=url,
                bearer_token=creds_item.token,
                identity_key=f"{origin}|key:{tok_hash}",
                is_codex_subscription=False,
                can_refresh_on_401=True,
            )
        acct_part = f"acct:{creds_item.account_id}" if creds_item.account_id else f"sub:{tok_hash}"
        return _ResponsesAuthTarget(
            url=url,
            bearer_token=creds_item.token,
            identity_key=f"{origin}|{acct_part}",
            account_id=creds_item.account_id,
            is_codex_subscription=True,
            is_fedramp=creds_item.is_fedramp,
            can_refresh_on_401=True,
        )

    def _extra_redaction_secrets(self, auth_target: _ResponsesAuthTarget | None = None) -> tuple[str, ...]:
        base = list(super()._extra_redaction_secrets(auth_target))
        if (cached := self._cached_creds) is not None:
            if cached.token:
                base.append(cached.token)
            if cached.account_id:
                base.append(cached.account_id)
        return tuple(base)

    def _update_refresh_cooldown(self, creds_item: _CachedCodexCredentials, now_epoch: float) -> None:
        if (
            creds_item.mode == "api_key"
            or creds_item.exp is None
            or (creds_item.exp - now_epoch) >= CODEX_REFRESH_WINDOW_SEC
        ):
            self._refresh_cooldown_until = -1e9
        else:
            self._refresh_cooldown_until = self.clock_fn() + CODEX_REFRESH_FAILURE_COOLDOWN_SEC

    def _resolve_codex_credentials(
        self,
        *,
        force_refresh: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> _CachedCodexCredentials:
        while not self._auth_lock.acquire(timeout=0.05):
            if self._closed or (is_cancelled is not None and is_cancelled()):
                raise RuntimeError("codex credential resolution cancelled")
        try:
            if self._closed or (is_cancelled is not None and is_cancelled()):
                raise RuntimeError("codex credential resolution cancelled")
            auth_path = self._resolve_auth_json_path()
            prev_creds = self._cached_creds
            parsed_this_call = False

            def _reload_from_disk() -> None:
                nonlocal parsed_this_call
                if auth_path is None:
                    return
                parsed_this_call = True
                disk_creds, mtime_ns = self._parse_auth_json_file(auth_path)
                if mtime_ns is None:
                    if self._cached_mtime_ns is not None:
                        self._cached_creds = None
                        self._cached_mtime_ns = None
                    return
                if (
                    mtime_ns != self._cached_mtime_ns
                    and disk_creds is not None
                    and (prev_creds is None or disk_creds.token != prev_creds.token)
                    and (
                        disk_creds.mode == "api_key"
                        or disk_creds.exp is None
                        or (disk_creds.exp - self.time_fn()) >= CODEX_REFRESH_WINDOW_SEC
                    )
                ):
                    self._refresh_cooldown_until = -1e9
                self._cached_creds = disk_creds
                self._cached_mtime_ns = mtime_ns

            if auth_path is not None:
                try:
                    st = os.stat(auth_path)
                except FileNotFoundError:
                    if self._cached_mtime_ns is not None:
                        self._cached_creds = None
                        self._cached_mtime_ns = None
                else:
                    if force_refresh or self._cached_creds is None or self._cached_mtime_ns != st.st_mtime_ns:
                        _reload_from_disk()

            now_epoch = self.time_fn()
            cur = self._cached_creds

            if (
                force_refresh
                and cur is not None
                and prev_creds is not None
                and cur.token != prev_creds.token
                and (cur.exp is None or (cur.exp - now_epoch) > CODEX_HARD_EXPIRY_SKEW_SEC)
            ):
                self._update_refresh_cooldown(cur, now_epoch)
                return cur

            if cur is not None and cur.mode == "api_key":
                if force_refresh:
                    raise _RefinerAttemptError("Codex API key rejected with HTTP 401", retry_kind="never")
                return cur

            rem_ttl: float | None = (cur.exp - now_epoch) if (cur is not None and cur.exp is not None) else None
            needs_proactive = rem_ttl is not None and rem_ttl < CODEX_REFRESH_WINDOW_SEC
            still_usable = (
                cur is not None
                and not force_refresh
                and (rem_ttl is None or rem_ttl > CODEX_HARD_EXPIRY_SKEW_SEC)
            )

            if cur is not None and not force_refresh and not needs_proactive:
                return cur

            if needs_proactive and still_usable and self.clock_fn() < self._refresh_cooldown_until:
                assert cur is not None
                return cur

            # Re-read disk once if not yet parsed in this call (catches same-mtime external rotation before RPC)
            if auth_path is not None and not force_refresh and not parsed_this_call:
                _reload_from_disk()
                cur = self._cached_creds
                if cur is not None:
                    if cur.mode == "api_key":
                        return cur
                    rem_ttl = (cur.exp - now_epoch) if cur.exp is not None else None
                    still_usable = rem_ttl is None or rem_ttl > CODEX_HARD_EXPIRY_SKEW_SEC
                    if rem_ttl is None or rem_ttl >= CODEX_REFRESH_WINDOW_SEC:
                        self._refresh_cooldown_until = -1e9
                        return cur

            try:
                rpc_method, rpc_token = self._call_codex_get_auth_status(
                    timeout_sec=min(CODEX_AUTH_RPC_TIMEOUT_SEC, float(self.cfg.refiner.timeout_sec)),
                    is_cancelled=is_cancelled,
                )
            except Exception:
                if self._closed or (is_cancelled is not None and is_cancelled()):
                    raise
                if still_usable and cur is not None:
                    self._refresh_cooldown_until = self.clock_fn() + CODEX_REFRESH_FAILURE_COOLDOWN_SEC
                    return cur
                raise

            disk_after, mtime_after = (
                self._parse_auth_json_file(auth_path) if auth_path is not None else (None, None)
            )
            now_after = self.time_fn()
            chosen: _CachedCodexCredentials | None = None

            if (
                disk_after is not None
                and (not rpc_token or disk_after.token == rpc_token or disk_after.mode == "api_key")
                and (disk_after.exp is None or (disk_after.exp - now_after) > CODEX_HARD_EXPIRY_SKEW_SEC)
            ):
                chosen = disk_after
            elif rpc_token:
                if rpc_method in ("apikey", "api_key"):
                    cand = _CachedCodexCredentials(mode="api_key", token=rpc_token, source="codex")
                else:
                    exp_rpc, jwt_acct_rpc, fedramp_rpc = _extract_jwt_metadata(rpc_token)
                    cand = _CachedCodexCredentials(
                        mode="chatgpt",
                        token=rpc_token,
                        account_id=jwt_acct_rpc or (disk_after.account_id if disk_after else ""),
                        exp=exp_rpc,
                        is_fedramp=fedramp_rpc,
                        source="codex",
                    )
                if cand.exp is None or (cand.exp - now_after) > CODEX_HARD_EXPIRY_SKEW_SEC:
                    chosen = cand

            if chosen is not None:
                if force_refresh and prev_creds is not None and chosen.token == prev_creds.token:
                    raise _RefinerAttemptError(
                        "Codex auth refresh returned unchanged token after HTTP 401",
                        retry_kind="never",
                    )
                self._cached_creds = chosen
                self._cached_mtime_ns = mtime_after
                self._update_refresh_cooldown(chosen, now_after)
                return chosen

            if still_usable and cur is not None:
                self._refresh_cooldown_until = self.clock_fn() + CODEX_REFRESH_FAILURE_COOLDOWN_SEC
                return cur

            if cur is None and auth_path is None:
                raise RuntimeError("Codex credentials unavailable (no HOME/CODEX_HOME configured)")
            raise RuntimeError("Codex credentials unavailable or expired (run 'codex login')")
        finally:
            self._auth_lock.release()

    def _resolve_endpoint_and_auth(
        self,
        *,
        force_refresh: bool = False,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> _ResponsesAuthTarget:
        return self._to_auth_target(
            self._resolve_codex_credentials(force_refresh=force_refresh, is_cancelled=is_cancelled)
        )

    def close(self) -> None:
        super().close()
        with self._state_lock:
            procs = list(self._refresh_procs)
            self._refresh_procs.clear()
        for proc in procs:
            self._terminate_proc(proc)


def inspect_codex_auth_status(cfg: Config, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Offline, zero-subprocess check of local Codex credential state (``'OK' | 'WARN' | 'FAIL'``)."""
    src = os.environ if env is None else env
    codex_home = resolve_codex_home_dir(env)
    if codex_home is None:
        return "FAIL", "no CODEX_HOME or HOME directory configured"
    cbin = find_codex_bin(env)

    auth_path = codex_home / "auth.json"
    try:
        st = os.stat(auth_path)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        return "FAIL", redact_secrets(f"cannot stat auth.json: {exc}", src, cfg=cfg)

    if st is not None:
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1_048_576:
            return "FAIL", "unsafe or oversized Codex auth.json"
        if st.st_size > 0:
            try:
                data = json.loads(auth_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return "FAIL", redact_secrets(f"invalid Codex auth.json: {exc}", src, cfg=cfg)
            if not isinstance(data, Mapping):
                return "FAIL", "invalid root in Codex auth.json"

            auth_mode = str(data.get("auth_mode") or "").strip().lower()
            file_api_key = str(data.get("OPENAI_API_KEY") or "").strip()
            env_api_key = str(src.get("OPENAI_API_KEY") or "").strip()
            tokens = data.get("tokens") if isinstance(data.get("tokens"), Mapping) else {}
            access_token = str(tokens.get("access_token") or "").strip()
            refresh_token = str(tokens.get("refresh_token") or "").strip()

            is_api_key_mode = auth_mode in ("api_key", "apikey") or (
                auth_mode not in ("chatgpt", "chatgptauthtokens")
                and not access_token
                and not refresh_token
                and bool(file_api_key or env_api_key)
            )
            if is_api_key_mode:
                if file_api_key:
                    return "OK", "auth=api_key (auth.json)"
                if env_api_key:
                    return "OK", "auth=api_key (OPENAI_API_KEY)"
                return "FAIL", "auth_mode=api_key in auth.json, but OPENAI_API_KEY is empty"

            if access_token:
                exp, _, _ = _extract_jwt_metadata(access_token)
                if exp is None or (exp - time.time()) > CODEX_HARD_EXPIRY_SKEW_SEC:
                    return "OK", "auth=chatgpt (auth.json)"
                if refresh_token and cbin is not None:
                    return (
                        "WARN",
                        "auth=chatgpt (auth.json expired; unverified offline, pass --ping to refresh & verify)",
                    )
                return "FAIL", "Codex JWT in auth.json is expired (run 'codex login')"

            if refresh_token:
                if cbin is not None:
                    return (
                        "WARN",
                        "auth=chatgpt (auth.json refresh_token; unverified offline, pass --ping to verify)",
                    )
                return "FAIL", "auth.json has refresh_token only, and codex binary was not found"

    cfg_path = codex_home / "config.toml"
    try:
        cst = os.stat(cfg_path)
    except OSError:
        cst = None
    if cst is not None and stat.S_ISREG(cst.st_mode) and 0 < cst.st_size <= 1_048_576:
        try:
            cfg_txt = cfg_path.read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            cfg_txt = ""
        if _CODEX_KEYRING_STORE_RE.search(cfg_txt):
            if cbin is not None:
                return "WARN", "auth=keyring (config.toml; unverified offline, pass --ping to verify)"
            return "FAIL", "keyring store configured in config.toml, but codex binary was not found"

    if cbin is not None:
        return (
            "WARN",
            "auth=codex-cli (no auth.json; keyring login unverified offline, pass --ping to verify)",
        )
    return "FAIL", "no Codex credentials found in ~/.codex/auth.json (run 'codex login')"


class GeminiRefiner(_HttpTransportRefiner):
    """Stage 2 text refiner backed by Google Gemini native ``:generateContent``."""

    provider_name = "gemini"

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        model = self.cfg.refiner.effective_model("gemini")
        gen_cfg = build_gemini_generation_config(
            model, self.cfg.refiner.effective_effort("gemini"), temperature=0.1
        )
        clean_draft, single_hop = _unpack(asr_input)
        if not clean_draft or (single_hop and not self.cfg.refiner.force_on_single_hop):
            return NoopRefiner().refine(asr_input, mode=mode)
        t0 = self.clock_fn()
        if c_kind := _cancel_fallback_kind(cancel_token, is_cancelled):
            return _fallback_result(clean_draft, self.provider_name, model, t0, self.clock_fn, c_kind)
        secrets: tuple[str, ...] = tuple(self.creds.all_secret_values())
        try:
            key = _resolve_refiner_api_key(self.cfg, self.creds, "gemini")
        except Exception as exc:
            warn = redact_secrets(str(exc), self.env, cfg=self.cfg, extra_secrets=secrets)
            return _fallback_result(clean_draft, self.provider_name, model, t0, self.clock_fn, "asr_draft", warn)

        secrets = (*secrets, key)
        base = (self.cfg.refiner.effective_base_url("gemini").strip() or GEMINI_DEFAULT_BASE_URL).rstrip("/")
        model_path = model if model.startswith("models/") else f"models/{model}"
        url = f"{base}/{model_path}:generateContent"
        context_blocks, final_block = build_refiner_request_blocks(
            self.cfg,
            clean_draft,
            session_key=session_key,
            tracker=tracker,
            fallback_context=fallback_context,
            mode=mode,
            workspace_cwd=workspace_cwd,
            target_hint=target_hint,
        )
        ctx_chunks, ctx_chars = len(context_blocks), sum(len(b) for b in context_blocks)
        used_prior = any(b.startswith("<prior_session_reference") for b in context_blocks)
        payload = {
            "systemInstruction": {"parts": [{"text": resolve_refiner_prompt(self.cfg, mode=mode)}]},
            "contents": [{"role": "user", "parts": [*({"text": blk} for blk in context_blocks), {"text": final_block}]}],
            "generationConfig": gen_cfg,
        }

        def _parse(resp: HttpResponse) -> tuple[str, _TokenUsageTuple]:
            try:
                data, non_json = resp.json(), False
            except ValueError:
                data, non_json = {}, True
            if non_json or resp.status != 200 or (data.get("error") and not data.get("candidates")):
                excerpt = redact_secrets(resp.text(), self.env, cfg=self.cfg, extra_secrets=secrets)[:180]
                rkind, rafter, is_conc = classify_http_failure("gemini", resp.status, resp.headers, data, non_json=non_json)
                raise _RefinerAttemptError(
                    f"HTTP {resp.status}: {excerpt}",
                    retry_kind=rkind,
                    retry_after_sec=rafter,
                    is_concurrency_limit=is_conc,
                )
            if isinstance(pf := data.get("promptFeedback"), Mapping) and pf.get("blockReason"):
                raise _RefinerAttemptError(f"Gemini generateContent blocked ({pf.get('blockReason')})", retry_kind="never")
            cands = data.get("candidates")
            cand0 = cands[0] if isinstance(cands, list) and cands and isinstance(cands[0], dict) else {}
            fin = str(cand0.get("finishReason") or "").strip().upper()
            if fin in _GEMINI_BLOCKED_FINISH_REASONS:
                raise _RefinerAttemptError(f"Gemini generateContent blocked ({fin})", retry_kind="never")
            parts = ((cand0.get("content") or {}).get("parts") or []) if cand0 else []
            out_txt = "".join(
                p["text"] for p in parts if isinstance(p, dict) and not p.get("thought") and isinstance(p.get("text"), str)
            ).strip()
            if not out_txt:
                raise _RefinerAttemptError("empty Gemini generateContent text", retry_kind="transient")
            return out_txt, _extract_gemini_usage(data)

        return self._send_http(
            model=model,
            clean_draft=clean_draft,
            url=url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            payload=payload,
            secrets=secrets,
            pacer_secret=key,
            t0=t0,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
            parse_fn=_parse,
            context_chars=ctx_chars,
            context_chunks=ctx_chunks,
            used_prior_session=used_prior,
        )


class OpenAICompatibleRefiner(_HttpTransportRefiner):
    provider_name = "openai-compatible"

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials | None = None,
        *,
        provider_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(cfg, creds, **kwargs)
        resolved_prov = (provider_name or cfg.refiner.provider or "openai-compatible").strip().lower()
        self.provider_name = resolved_prov
        if resolved_prov == "openai-compatible":
            if not cfg.refiner.base_url.strip():
                raise ConfigError("refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'")
            if not cfg.refiner.model.strip():
                raise ConfigError("refiner.model must be explicitly set when refiner.provider is 'openai-compatible'")

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        model = self.cfg.refiner.effective_model(self.provider_name)
        if self.provider_name == "openai-compatible":
            if not self.cfg.refiner.base_url.strip():
                raise ConfigError("refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'")
            if not model.strip():
                raise ConfigError("refiner.model must be explicitly set when refiner.provider is 'openai-compatible'")
        eff = self.cfg.refiner.effective_effort(self.provider_name)
        if eff not in VALID_EFFORTS:
            raise ConfigError(f"refiner.effort must be one of {sorted(VALID_EFFORTS)}, got {self.cfg.refiner.effort!r}")
        clean_draft, single_hop = _unpack(asr_input)
        if not clean_draft or (single_hop and not self.cfg.refiner.force_on_single_hop):
            return NoopRefiner().refine(asr_input, mode=mode)
        t0 = self.clock_fn()
        if c_kind := _cancel_fallback_kind(cancel_token, is_cancelled):
            return _fallback_result(clean_draft, self.provider_name, model, t0, self.clock_fn, c_kind)
        secrets: tuple[str, ...] = tuple(self.creds.all_secret_values())
        try:
            if self.provider_name == "deepseek":
                url = resolve_deepseek_endpoint_url(
                    self.cfg.refiner.effective_base_url(self.provider_name),
                    "/chat/completions",
                )
            else:
                base = (
                    self.cfg.refiner.effective_base_url(self.provider_name).strip()
                    or OPENAI_DEFAULT_BASE_URL
                ).rstrip("/")
                url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            key = _resolve_refiner_api_key(self.cfg, self.creds, self.provider_name, url)
        except Exception as exc:
            warn = redact_secrets(str(exc), self.env, cfg=self.cfg, extra_secrets=secrets)
            return _fallback_result(clean_draft, self.provider_name, model, t0, self.clock_fn, "asr_draft", warn)

        if key:
            secrets = (*secrets, key)
        context_blocks, final_block = build_refiner_request_blocks(
            self.cfg,
            clean_draft,
            session_key=session_key,
            tracker=tracker,
            fallback_context=fallback_context,
            mode=mode,
            workspace_cwd=workspace_cwd,
            target_hint=target_hint,
        )
        ctx_chunks, ctx_chars = len(context_blocks), sum(len(b) for b in context_blocks)
        used_prior = any(b.startswith("<prior_session_reference") for b in context_blocks)
        bps = set(openrouter_cache_breakpoints(self.provider_name, model, ctx_chunks))
        if bps:
            user_content: str | list[dict[str, Any]] = [
                *(
                    {
                        "type": "text",
                        "text": blk,
                        **({"cache_control": {"type": "ephemeral"}} if idx in bps else {}),
                    }
                    for idx, blk in enumerate(context_blocks)
                ),
                {"type": "text", "text": final_block},
            ]
        else:
            user_content = "\n\n".join((*context_blocks, final_block))
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": resolve_refiner_prompt(self.cfg, mode=mode)},
                {"role": "user", "content": user_content},
            ],
        }
        if not _is_openai_reasoning_model(model):
            payload["temperature"] = 0.1
        apply_chat_effort_params(
            payload,
            provider=self.provider_name,
            model=model,
            effort=eff,
            base_url=url,
        )

        def _parse(resp: HttpResponse) -> tuple[str, _TokenUsageTuple]:
            try:
                data, non_json = resp.json(), False
            except ValueError:
                data, non_json = {}, True
            compat_err = None if non_json else extract_openai_compat_error(data)
            if non_json or resp.status != 200 or compat_err is not None:
                raw_msg = compat_err[1] if compat_err is not None else resp.text()
                excerpt = redact_secrets(raw_msg, self.env, cfg=self.cfg, extra_secrets=secrets)[:180]
                rkind, rafter, is_conc = classify_http_failure(
                    self.provider_name, resp.status, resp.headers, data, non_json=non_json
                )
                raise _RefinerAttemptError(
                    f"HTTP {resp.status}: {excerpt}",
                    retry_kind=rkind,
                    retry_after_sec=rafter,
                    is_concurrency_limit=is_conc,
                )
            choices = data.get("choices")
            c0 = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
            fin = str(c0.get("finish_reason") or "").strip().lower()
            if fin in ("content_filter", "safety"):
                raise _RefinerAttemptError(f"refiner output blocked ({fin})", retry_kind="never")
            if not (out_txt := extract_chat_message_text(data)):
                raise _RefinerAttemptError("empty choices[0].message.content", retry_kind="transient")
            return out_txt, _extract_token_usage(data, default_zero_details=False)

        return self._send_http(
            model=model,
            clean_draft=clean_draft,
            url=url,
            headers=headers,
            payload=payload,
            secrets=secrets,
            pacer_secret=key,
            t0=t0,
            is_cancelled=is_cancelled,
            cancel_token=cancel_token,
            parse_fn=_parse,
            context_chars=ctx_chars,
            context_chunks=ctx_chunks,
            used_prior_session=used_prior,
        )


_AGY_AGENT_NAME = "avi-refiner"
_AGY_PROMPT_PREFIX = "[avi-refiner]\n"
_AGY_AGENT_MD_CONTENT = (
    "---\n"
    "name: avi-refiner\n"
    "description: Stateless text-only voice transcript refiner with zero tools.\n"
    "inheritCustomizations: false\n"
    "excludeDefaultComponents: true\n"
    "subagent: false\n"
    "tools: []\n"
    "---\n"
    "You are a stateless, non-conversational voice-dictation text editor with zero tools. "
    "Do NOT deliberate, analyze rules, or plan answers in internal reasoning—output the cleaned text directly and immediately.\n"
)
_AGY_ENV_KEEP: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TMPDIR",
        "TERM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "CLOUDSDK_CONFIG",
    }
)
_AGY_ENV_PREFIXES: tuple[str, ...] = (
    "AGY_",
    "ANTIGRAVITY_",
    "GEMINI_",
    "GOOGLE_",
    "XDG_",
    "LC_",
)
_AGY_INTERNAL_TOOLS: frozenset[str] = frozenset({"manage_task", "finish"})
_AGY_EFFORT_SUFFIXES: tuple[str, ...] = ("low", "medium", "high", "xhigh", "extra", "max")
_AGY_FILTER_NOTICE = "This request was blocked by Gemini's filters"
_AGY_REFUSAL_STOPS: frozenset[str] = frozenset({"refusal", "safety", "content_filter"})
_AGY_KNOWN_ERROR_STOPS: frozenset[str] = frozenset({"max_tokens", "cancelled", "max_turn_requests"})
_AGY_ERROR_LINE_RE = re.compile(r"AGY_ERROR:\s*(\{.*\})\s*$", re.M)
_AGY_PRINT_TIMEOUT_RE = re.compile(r"print timeout after \S+ with turn in progress")
_AGY_RESUME_MISS_RE = re.compile(
    r"(?:session|conversation).*(?:not found|invalid|unknown|missing|expired)", re.I
)
_AGY_NON_RETRYABLE_SOURCES: frozenset[str] = frozenset(
    {
        "denied",
        "tool",
        "filtered",
        "refusal",
        "config",
        "missing_cli",
        "max_tokens",
        "max_turn_requests",
        "cancelled",
    }
)
_AGY_MAX_THREADS = 64
_AGY_MAX_LOADED_SESSIONS = 64
# 15 min idle TTL covers normal coding/thinking pauses; sleep/shutdown still stops worker immediately.
_AGY_IDLE_TTL_SEC: float = 900.0


class _AgyRpcError(RuntimeError):
    def __init__(self, message: str, *, source: str) -> None:
        super().__init__(message)
        self.source = source


@dataclass
class _AgyThreadState:
    conversation_id: str = ""
    turn_count: int = 0
    sent_blocks: tuple[str, ...] = ()
    model: str = ""
    effort: str = ""
    prompt_hash: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


def _agy_child_env(env: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in env.items():
        if k in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PYTHONSTARTUP"):
            continue
        if k in _AGY_ENV_KEEP or k.startswith(_AGY_ENV_PREFIXES):
            out[k] = v
    return out


def _agy_select_reject_option_id(options: Any) -> str | None:
    if not isinstance(options, list):
        return None
    valid: list[tuple[str, str]] = []
    for opt in options:
        if not isinstance(opt, Mapping):
            continue
        oid = opt.get("optionId")
        kind = opt.get("kind")
        if not isinstance(oid, str) or not oid.strip() or not isinstance(kind, str) or not kind.strip():
            continue
        k_low = kind.strip().lower()
        o_low = oid.strip().lower()
        if "allow" in k_low or "allow" in o_low:
            continue
        valid.append((oid, k_low))
    for target in ("reject_once", "reject_always"):
        for oid, k_low in valid:
            if k_low == target:
                return oid
    return None


def _agy_config_entry(config_options: Any, cid: str) -> Mapping[str, Any] | None:
    for item in config_options or ():
        if isinstance(item, Mapping) and item.get("id") == cid:
            return item
    return None


def _agy_option_values(entry: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(entry, Mapping):
        return []
    return [
        val
        for opt in (entry.get("options") or ())
        if isinstance(opt, Mapping) and isinstance((val := opt.get("value")), str) and val
    ]


def _agy_canonical_effort_token(raw: str) -> str:
    low = raw.strip().lower()
    if low in ("none", "minimal"):
        return "low"
    if low in ("max", "extra"):
        return "xhigh"
    return low


def _agy_match_model(requested_model: str, available_models: Sequence[str]) -> tuple[str | None, str | None]:
    if not requested_model.strip():
        return None, None
    req = requested_model.strip().lower()
    if not available_models:
        raise _AgyRpcError("agy ACP returned an empty model list", source="config")
    exact = [m for m in available_models if m.lower() == req]
    if len(exact) == 1:
        return exact[0], None
    base, sep, suffix = req.rpartition("-")
    if sep and base and suffix in _AGY_EFFORT_SUFFIXES:
        exact_base = [m for m in available_models if m.lower() == base]
        if len(exact_base) == 1:
            return exact_base[0], suffix
    raise _AgyRpcError(
        f"unknown agy model {requested_model!r} (available: {', '.join(available_models)})",
        source="config",
    )


def _agy_match_effort(
    target_model: str | None,
    effort: str,
    *,
    effort_explicit: bool,
    implied_effort: str | None,
    config_options: Sequence[Any],
) -> str | None:
    req_e = (effort or "").strip().lower()
    is_passthrough = req_e in ("", "auto", "default")
    explicit_concrete = None if is_passthrough else req_e
    if effort_explicit and explicit_concrete and implied_effort:
        if _agy_canonical_effort_token(explicit_concrete) != _agy_canonical_effort_token(implied_effort):
            raise _AgyRpcError(
                f"agy model {target_model!r} suffix effort {implied_effort!r} conflicts with refiner.effort={effort!r}",
                source="config",
            )
    if effort_explicit and explicit_concrete:
        desired: str | None = explicit_concrete
    elif implied_effort:
        desired = implied_effort
    else:
        desired = explicit_concrete

    tl_entry = _agy_config_entry(config_options, "thought_level")
    tl_values = _agy_option_values(tl_entry)
    if not tl_values:
        if desired is None:
            return None
        if target_model and target_model.lower().endswith(f"-{desired}"):
            return None
        if not effort_explicit and not implied_effort:
            return None
        raise _AgyRpcError(
            f"agy model {target_model or '<default>'!r} does not support effort {desired!r}",
            source="config",
        )
    if desired is None:
        return None
    cand = _agy_canonical_effort_token(desired)
    if cand == "xhigh" and "extra" in tl_values and "xhigh" not in tl_values:
        cand = "extra"
    if cand not in tl_values:
        raise _AgyRpcError(
            f"agy model {target_model or '<default>'!r} does not accept effort {desired!r} "
            f"(available: {', '.join(tl_values)})",
            source="config",
        )
    return cand


def _agy_convert_usage(raw_usage: Any) -> _TokenUsageTuple:
    if not isinstance(raw_usage, Mapping) or not raw_usage:
        return _EMPTY_USAGE
    uncached_in = _opt_int(raw_usage.get("inputTokens"))
    cached_read = _opt_int(raw_usage.get("cachedReadTokens"))
    out_tok = _opt_int(raw_usage.get("outputTokens"))
    thought_tok = _opt_int(raw_usage.get("thoughtTokens"))
    total_in = (uncached_in + (cached_read or 0)) if uncached_in is not None else None
    if total_in is None and cached_read is None and out_tok is None and thought_tok is None:
        return _EMPTY_USAGE
    return (total_in, cached_read, out_tok, thought_tok or 0)


_AGY_SAFE_ERR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")


def _agy_safe_code(val: Any) -> str:
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, str):
        s = val.strip()
        if s and _AGY_SAFE_ERR_CODE_RE.match(s):
            return s
    return ""


def _agy_is_exact_filter_notice(text: str) -> bool:
    s = text.strip()
    return s == _AGY_FILTER_NOTICE or s == f"{_AGY_FILTER_NOTICE}."


def _agy_check_filter_and_refusal(
    texts: Sequence[str],
    *,
    stop_reason: Any = None,
    edata: Any = None,
) -> None:
    if any(_agy_is_exact_filter_notice(t) for t in texts if t):
        raise _AgyRpcError(
            "Gemini safety filter blocked the agy reply",
            source="filtered",
        )
    is_refusal = str(stop_reason or "").strip().lower() in _AGY_REFUSAL_STOPS
    if not is_refusal and isinstance(edata, Mapping):
        is_refusal = any(
            str(edata.get(k) or "").strip().lower() in _AGY_REFUSAL_STOPS
            for k in ("stopReason", "stop_reason", "finishReason", "finish_reason", "status", "reason")
        )
    if is_refusal:
        raise _AgyRpcError("agy refused the refinement turn", source="refusal")


def _agy_safe_stderr_summary(stderr_text: str) -> str:
    """Extract only validated short structured error codes from ``AGY_ERROR`` (never free-text messages)."""
    m = _AGY_ERROR_LINE_RE.search(stderr_text)
    if not m:
        return ""
    try:
        parsed = json.loads(m.group(1))
    except ValueError:
        return ""
    if not isinstance(parsed, Mapping):
        return ""
    parts: list[str] = []
    if code := _agy_safe_code(parsed.get("code")):
        parts.append(f"code={code}")
    if etype := _agy_safe_code(parsed.get("type") or parsed.get("status")):
        parts.append(f"type={etype}")
    return ", ".join(parts)


def _agy_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - OS permission edge for process group probe
        return True


def _agy_stop_process_group(proc: subprocess.Popen[str], *, fast: bool = False) -> None:
    pgid = proc.pid
    if not fast and proc.poll() is None:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        grace_dl = time.monotonic() + 0.3
        while time.monotonic() < grace_dl:
            if proc.poll() is not None and not _agy_group_alive(pgid):
                break
            time.sleep(0.02)
    stages = (
        ((signal.SIGTERM, 0.15), (signal.SIGKILL, 1.0))
        if fast
        else ((signal.SIGINT, 0.3), (signal.SIGTERM, 0.5), (signal.SIGKILL, 1.0))
    )
    for sig, wait_s in stages:
        if proc.poll() is not None and not _agy_group_alive(pgid):
            break
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        dl = time.monotonic() + wait_s
        while time.monotonic() < dl:
            if proc.poll() is not None and not _agy_group_alive(pgid):
                break
            time.sleep(0.02)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable D-state process
        pass


def _agy_close_pipes(proc: subprocess.Popen[str]) -> None:
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _agy_acquire_lock_until(
    lock: threading.Lock,
    deadline: float,
    clock_fn: Callable[[], float],
    is_aborted: Callable[[], bool],
) -> str:
    """Acquire ``lock`` within ``deadline``; returns ``""``, ``"timeout"``, or ``"cancelled"``."""
    while True:
        if is_aborted():
            return "cancelled"
        rem = deadline - clock_fn()
        if rem <= 0:
            return "timeout"
        if lock.acquire(timeout=min(0.05, max(0.0, rem))):
            if is_aborted():
                lock.release()
                return "cancelled"
            return ""
        if is_aborted():
            return "cancelled"


class _AgyWorker:
    """Single reusable ``agy acp`` subprocess with background stdout/stderr reader threads."""

    def __init__(
        self,
        *,
        agy_bin: str,
        workdir: Path,
        proc: subprocess.Popen[str],
        on_stopped: Callable[[subprocess.Popen[str]], None],
    ) -> None:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self.agy_bin = agy_bin
        self.workdir = workdir
        self.proc = proc
        self.stdin_fd = proc.stdin.fileno()
        os.set_blocking(self.stdin_fd, False)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.stderr_lock = threading.Lock()
        self.next_req_id = 1
        self.loaded_sessions: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self.closed = False
        self._stopped = False
        self._stop_lock = threading.Lock()
        self._on_stopped = on_stopped
        self.t_out = threading.Thread(target=self._stdout_reader, daemon=True)
        self.t_err = threading.Thread(target=self._stderr_reader, daemon=True)
        self.t_out.start()
        self.t_err.start()

    def _stdout_reader(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.events.put(("stdout", line))
        except (OSError, ValueError):
            pass
        finally:
            self.events.put(("eof", None))

    def _stderr_reader(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                with self.stderr_lock:
                    self.stderr_lines.append(line)
                    if len(self.stderr_lines) > 200:
                        del self.stderr_lines[:100]
        except (OSError, ValueError):
            pass

    def is_alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def stderr_text(self) -> str:
        with self.stderr_lock:
            return "".join(self.stderr_lines)

    def clear_stderr(self) -> None:
        with self.stderr_lock:
            self.stderr_lines.clear()

    def drain_idle_events(self) -> bool:
        """Drop stale idle messages; return False if the worker hit EOF or was cancelled."""
        while True:
            try:
                ev_type, _ = self.events.get_nowait()
            except queue.Empty:
                return self.is_alive()
            if ev_type in ("eof", "cancelled"):
                return False

    def alloc_req_id(self) -> int:
        rid = self.next_req_id
        self.next_req_id += 1
        return rid

    def record_loaded_session(self, sid: str, req_model: str, eff_key: str, res_model: str) -> None:
        self.loaded_sessions[sid] = (req_model, eff_key, res_model)
        self.loaded_sessions.move_to_end(sid)
        while len(self.loaded_sessions) > _AGY_MAX_LOADED_SESSIONS:
            self.loaded_sessions.popitem(last=False)

    def abort(self) -> None:
        self.closed = True
        self.events.put(("cancelled", None))
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def stop(self, *, fast: bool = False) -> None:
        self.closed = True
        self.events.put(("cancelled", None))
        if fast and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                _agy_stop_process_group(self.proc, fast=fast)
            finally:
                self.t_out.join(timeout=0.3)
                self.t_err.join(timeout=0.3)
                _agy_close_pipes(self.proc)
                if self.t_out.is_alive():
                    self.t_out.join(timeout=1.0)
                if self.t_err.is_alive():
                    self.t_err.join(timeout=1.0)
                self._on_stopped(self.proc)


class AgyRefiner(BaseRefiner):
    """Stage 2 text refiner backed by local Antigravity CLI (``agy acp``) with zero tools."""

    provider_name = "agy"

    def __init__(
        self,
        cfg: Config,
        creds: ProviderCredentials | None = None,
        *,
        env: Mapping[str, str] | None = None,
        runtime_dir: Path | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
        rand_fn: Callable[[], float] = random.random,
        idle_ttl_sec: float = _AGY_IDLE_TTL_SEC,
    ) -> None:
        self.cfg = cfg
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.creds = creds or resolve_credentials(cfg, self.env)
        self._runtime_dir = runtime_dir
        self.clock_fn, self.sleep_fn, self.rand_fn = clock_fn, sleep_fn, rand_fn
        self._idle_ttl_sec = max(0.0, float(idle_ttl_sec))
        self._closed = False
        self._threads_lock = threading.Lock()
        self._threads: OrderedDict[tuple[str, str], _AgyThreadState] = OrderedDict()
        self._procs_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen[str]] = set()
        self._workdir_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._worker: _AgyWorker | None = None
        self._worker_busy = False
        self._idle_timer: threading.Timer | None = None
        self._idle_gen = 0

    def _ensure_agent_workdir(self) -> Path:
        with self._workdir_lock:
            base_rt = self._runtime_dir if self._runtime_dir is not None else get_runtime_dir(self.env)
            ensure_secure_dir(base_rt)
            workdir = ensure_secure_dir(base_rt / "agy-refiner")
            agents_root = ensure_secure_dir(workdir / ".agents")
            agents_dir = ensure_secure_dir(agents_root / "agents")
            agent_md = agents_dir / f"{_AGY_AGENT_NAME}.md"
            try:
                st = os.lstat(agent_md)
            except FileNotFoundError:
                st = None
            if st is not None:
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    raise ConfigError(f"unsafe agy agent file (not a regular file): {agent_md}")
                try:
                    if (
                        agent_md.read_text(encoding="utf-8") == _AGY_AGENT_MD_CONTENT
                        and (st.st_mode & 0o777) == 0o600
                    ):
                        return workdir
                except OSError:
                    pass
            tmp_path = agents_dir / f".{_AGY_AGENT_NAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(tmp_path), flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(_AGY_AGENT_MD_CONTENT)
                os.replace(tmp_path, agent_md)
            except BaseException:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return workdir

    def _get_thread_state(self, key: tuple[str, str]) -> _AgyThreadState:
        with self._threads_lock:
            state = self._threads.get(key)
            if state is not None:
                self._threads.move_to_end(key)
                return state
            state = _AgyThreadState()
            self._threads[key] = state
            while len(self._threads) > _AGY_MAX_THREADS:
                self._threads.popitem(last=False)
            return state

    def _spawn_worker(self, *, agy_bin: str, workdir: Path) -> _AgyWorker:
        cmd = [
            agy_bin,
            "acp",
            "--agent",
            _AGY_AGENT_NAME,
            "--include-default-customizations=false",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workdir),
                env=_agy_child_env(self.env),
                start_new_session=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            raise _AgyRpcError(f"{agy_bin} CLI not found", source="missing_cli") from None

        with self._procs_lock:
            closed = self._closed
            if not closed:
                self._active_procs.add(proc)
        if closed:
            try:
                _agy_stop_process_group(proc, fast=True)
            finally:
                _agy_close_pipes(proc)
            raise _AgyRpcError("agy refiner closed", source="cancelled")

        def _on_stopped(p: subprocess.Popen[str]) -> None:
            with self._procs_lock:
                self._active_procs.discard(p)

        try:
            return _AgyWorker(agy_bin=agy_bin, workdir=workdir, proc=proc, on_stopped=_on_stopped)
        except BaseException:
            try:
                _agy_stop_process_group(proc, fast=True)
            finally:
                _agy_close_pipes(proc)
                _on_stopped(proc)
            raise

    def _cancel_idle_timer_locked(self) -> None:
        timer = self._idle_timer
        self._idle_timer = None
        self._idle_gen += 1
        if timer is not None:
            timer.cancel()

    def _arm_idle_timer_locked(self) -> bool:
        """Arm idle reaper timer under ``_state_lock``; return False if thread creation failed."""
        self._cancel_idle_timer_locked()
        if self._closed or self._worker is None or self._worker_busy or self._idle_ttl_sec <= 0:
            return True
        gen = self._idle_gen
        timer = threading.Timer(self._idle_ttl_sec, self._on_idle_timeout, args=(gen,))
        timer.daemon = True
        timer.name = "avi-agy-idle-reaper"
        try:
            timer.start()
        except Exception:
            self._idle_timer = None
            return False
        self._idle_timer = timer
        return True

    def _on_idle_timeout(self, expected_gen: int) -> None:
        with self._state_lock:
            if self._idle_gen != expected_gen or self._worker_busy or self._worker is None:
                return
            worker = self._worker
            self._worker = None
            self._idle_timer = None
        worker.stop(fast=False)

    def _discard_worker(self, target: _AgyWorker | None = None, *, fast: bool = True) -> None:
        with self._state_lock:
            if target is None or self._worker is target:
                worker = self._worker
                self._worker = None
            else:
                worker = target
        if worker is not None:
            worker.stop(fast=fast)

    def _run_acp_session(
        self,
        *,
        agy_bin: str,
        workdir: Path,
        timeout_sec: float,
        deadline: float | None = None,
        prompt_text: str | None = None,
        requested_model: str = "",
        requested_effort: str = "",
        effort_explicit: bool = False,
        conversation_id: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        reuse_worker: bool = True,
        track_loaded_session: bool = True,
    ) -> tuple[str, str, str, _TokenUsageTuple, int]:
        """Run one ACP turn (reusing the warm ``agy acp`` worker when ``reuse_worker=True``)."""
        t_start = time.monotonic()
        timeout_s = max(0.0, float(timeout_sec))
        eff_deadline = float(deadline) if deadline is not None else (t_start + timeout_s)
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        use_reusable = reuse_worker and prompt_text is not None

        def _is_aborted() -> bool:
            return self._closed or (eff_cancelled is not None and eff_cancelled())

        if _is_aborted():
            raise _AgyRpcError("agy request cancelled before spawn", source="cancelled")
        if eff_deadline - time.monotonic() <= 0:
            raise _AgyRpcError(f"agy did not finish within {timeout_s:.1f}s", source="hard_timeout")

        worker: _AgyWorker | None = None
        abort_hook_id = 0
        emergency_kill = False
        turn_ok = False
        active_prompt_sid: str | None = None
        text_chunks: list[str] = []
        tools_by_id: dict[str, dict[str, Any]] = {}
        denied_calls: list[bool] = []

        def _check_tools_and_denial() -> None:
            if denied_calls:
                raise _AgyRpcError(
                    "agy ACP permission denied",
                    source="denied",
                )
            bad = [c for c in tools_by_id.values() if c.get("name") and c.get("name") not in _AGY_INTERNAL_TOOLS]
            if bad:
                raise _AgyRpcError(
                    "agy ACP blocked tool call",
                    source="tool",
                )

        def _send_rpc(payload: Mapping[str, Any]) -> None:
            if worker is None:
                return
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            view = memoryview(data)
            sent = 0
            total = len(data)
            sel = selectors.DefaultSelector()
            with sel:
                try:
                    sel.register(worker.stdin_fd, selectors.EVENT_WRITE)
                except (OSError, ValueError) as exc:
                    if worker.proc.poll() is not None:
                        return
                    raise _AgyRpcError(f"agy stdin register failed: {exc}", source="protocol") from None
                while sent < total:
                    if _is_aborted():
                        raise _AgyRpcError("agy request cancelled", source="cancelled")
                    rem = eff_deadline - time.monotonic()
                    if rem <= 0:
                        raise _AgyRpcError(
                            f"agy did not finish within {timeout_s:.1f}s", source="hard_timeout"
                        )
                    if worker.proc.poll() is not None:
                        return
                    ready = sel.select(timeout=min(0.05, max(0.0, rem)))
                    if not ready:
                        continue
                    try:
                        n = os.write(worker.stdin_fd, view[sent:])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError:
                        return
                    except OSError as exc:
                        if worker.proc.poll() is not None:
                            return
                        raise _AgyRpcError(f"agy stdin write failed: {exc}", source="protocol") from None
                    if n == 0:
                        if worker.proc.poll() is not None:
                            return
                        raise _AgyRpcError("agy stdin write returned 0 bytes", source="protocol")
                    sent += n

        def _handle_server_msg(msg: Mapping[str, Any]) -> None:
            nonlocal emergency_kill
            if "id" in msg and "method" in msg:
                method = msg.get("method")
                if method == "session/request_permission":
                    params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
                    opts = params.get("options")
                    rej_id = _agy_select_reject_option_id(opts)
                    denied_calls.append(True)
                    if rej_id is not None:
                        _send_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {"outcome": {"outcome": "selected", "optionId": rej_id}},
                            }
                        )
                    else:
                        emergency_kill = True
                        _send_rpc(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {"outcome": {"outcome": "cancelled"}},
                            }
                        )
                        _check_tools_and_denial()
                else:
                    _send_rpc(
                        {
                            "jsonrpc": "2.0",
                            "id": msg["id"],
                            "error": {
                                "code": -32601,
                                "message": f"Method not supported in agent-voice-input: {method}",
                            },
                        }
                    )
                return

            if msg.get("method") == "session/update":
                params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
                upd = params.get("update") if isinstance(params.get("update"), Mapping) else {}
                st = upd.get("sessionUpdate")
                if st == "agent_message_chunk":
                    if active_prompt_sid is None:
                        return
                    msg_sid = params.get("sessionId")
                    if not isinstance(msg_sid, str) or msg_sid != active_prompt_sid:
                        return
                    content = upd.get("content")
                    if isinstance(content, Mapping) and content.get("type") == "text":
                        chunk_txt = content.get("text")
                        if isinstance(chunk_txt, str):
                            text_chunks.append(chunk_txt)
                elif st in ("tool_call", "tool_call_update"):
                    tc_id = str(upd.get("toolCallId") or f"anon_{len(tools_by_id)}")
                    entry = tools_by_id.setdefault(tc_id, {"name": None})
                    if upd.get("name") or upd.get("title"):
                        entry["name"] = upd.get("name") or entry["name"] or upd.get("title")
                    if entry["name"] and entry["name"] not in _AGY_INTERNAL_TOOLS:
                        emergency_kill = True
                        _check_tools_and_denial()

        def _rpc_call(method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
            assert worker is not None
            req_id = worker.alloc_req_id()
            _send_rpc({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            while True:
                if _is_aborted():
                    raise _AgyRpcError("agy request cancelled", source="cancelled")
                rem = eff_deadline - time.monotonic()
                if rem <= 0:
                    raise _AgyRpcError(
                        f"agy did not finish within {timeout_s:.1f}s", source="hard_timeout"
                    )
                try:
                    ev_type, payload = worker.events.get(timeout=min(0.05, max(0.0, rem)))
                except queue.Empty:
                    continue
                if ev_type == "cancelled":
                    raise _AgyRpcError("agy request cancelled", source="cancelled")
                if ev_type == "eof":
                    worker.t_out.join(timeout=0.3)
                    worker.t_err.join(timeout=0.3)
                    try:
                        worker.proc.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        pass
                    _check_tools_and_denial()
                    ef = worker.stderr_text()
                    _agy_check_filter_and_refusal(["".join(text_chunks), ef])
                    if _AGY_PRINT_TIMEOUT_RE.search(ef):
                        raise _AgyRpcError("agy hit internal print timeout", source="print_timeout")
                    rc = worker.proc.returncode
                    if rc not in (0, None):
                        safe_detail = _agy_safe_stderr_summary(ef)
                        suffix = f": {safe_detail}" if safe_detail else ""
                        raise _AgyRpcError(f"agy exited {rc} during {method}{suffix}", source="exit")
                    raise _AgyRpcError(
                        f"agy produced no result for {method} (EOF)",
                        source="no_result",
                    )
                if ev_type == "stdout" and isinstance(payload, str):
                    line = payload.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, Mapping):
                        continue
                    if "method" in msg:
                        _handle_server_msg(msg)
                        continue
                    if msg.get("id") == req_id:
                        return msg

        def _unwrap_rpc(resp_msg: Mapping[str, Any], method: str) -> Mapping[str, Any]:
            if method == "session/prompt":
                _check_tools_and_denial()
            err_obj = resp_msg.get("error")
            if err_obj is not None:
                if isinstance(err_obj, Mapping):
                    code = err_obj.get("code")
                    safe_code = _agy_safe_code(code)
                    raw_msg = str(err_obj.get("message") or "").strip()
                    edata = err_obj.get("data")
                else:
                    code, safe_code, raw_msg, edata = None, "", "", None
                edata_resp = (
                    str(edata.get("response"))
                    if isinstance(edata, Mapping) and isinstance(edata.get("response"), str)
                    else ""
                )
                edata_msg = (
                    str(edata.get("message"))
                    if isinstance(edata, Mapping) and isinstance(edata.get("message"), str)
                    else ""
                )
                _agy_check_filter_and_refusal(
                    ["".join(text_chunks), raw_msg, edata_msg, edata_resp], edata=edata
                )
                code_bit = f", code={safe_code}" if safe_code else ""
                if method == "session/set_config_option" and code in (-32600, -32602):
                    raise _AgyRpcError(
                        f"agy rejected config option ({method}{code_bit})", source="config"
                    )
                if method == "session/resume" and (
                    code in (-32600, -32602)
                    or bool(_AGY_RESUME_MISS_RE.search(f"{raw_msg} {edata_msg}"))
                ):
                    raise _AgyRpcError(
                        f"agy could not resume conversation ({method}{code_bit})",
                        source="conversation",
                    )
                if method == "session/prompt" and isinstance(edata, Mapping):
                    estatus = str(
                        edata.get("stopReason") or edata.get("stop_reason") or edata.get("status") or ""
                    ).strip().lower()
                    if estatus in ("max_tokens", "cancelled", "max_turn_requests"):
                        raise _AgyRpcError(
                            f"agy RPC error ({method}{code_bit}, stopReason={estatus})",
                            source=estatus,
                        )
                raise _AgyRpcError(f"agy RPC error ({method}{code_bit})", source="status")
            res = resp_msg.get("result")
            return res if isinstance(res, Mapping) else {}

        try:
            init_ms = 0
            reused_worker = False
            if use_reusable:
                with self._state_lock:
                    cand = self._worker
                if (
                    cand is not None
                    and cand.agy_bin == agy_bin
                    and cand.workdir == workdir
                    and cand.drain_idle_events()
                ):
                    worker = cand
                    worker.clear_stderr()
                    reused_worker = True
                elif cand is not None:
                    self._discard_worker(cand, fast=True)

            if worker is None:
                worker = self._spawn_worker(agy_bin=agy_bin, workdir=workdir)
                if use_reusable:
                    with self._state_lock:
                        if self._closed:
                            emergency_kill = True
                            raise _AgyRpcError("agy refiner closed", source="cancelled")
                        self._worker = worker

            if cancel_token is not None:
                active_w = worker

                def _on_abort() -> None:
                    nonlocal emergency_kill
                    emergency_kill = True
                    active_w.abort()

                abort_hook_id = cancel_token.register_abort(_on_abort)

            if not reused_worker:
                _unwrap_rpc(
                    _rpc_call(
                        "initialize",
                        {
                            "protocolVersion": 1,
                            "clientInfo": {"name": "agent-voice-input", "version": __version__},
                        },
                    ),
                    "initialize",
                )
                init_ms = max(0, int(round((time.monotonic() - t_start) * 1000)))
                if prompt_text is None:
                    turn_ok = True
                    return "", "", "", _EMPTY_USAGE, init_ms

            effort_key = f"{requested_effort}:{'1' if effort_explicit else '0'}"
            loaded_entry = worker.loaded_sessions.get(conversation_id) if conversation_id else None
            if (
                conversation_id
                and loaded_entry is not None
                and loaded_entry[0] == requested_model
                and loaded_entry[1] == effort_key
            ):
                # Hot path: live worker already holds this session with configured model & effort.
                sid = conversation_id
                resolved_model = loaded_entry[2]
                worker.loaded_sessions.move_to_end(sid)
            else:
                if conversation_id:
                    sess_res = _unwrap_rpc(
                        _rpc_call(
                            "session/resume",
                            {"sessionId": conversation_id, "cwd": str(workdir), "mcpServers": []},
                        ),
                        "session/resume",
                    )
                else:
                    sess_res = _unwrap_rpc(
                        _rpc_call(
                            "session/new",
                            {"cwd": str(workdir), "mcpServers": []},
                        ),
                        "session/new",
                    )

                sid_raw = sess_res.get("sessionId")
                if not isinstance(sid_raw, str) or not sid_raw.strip():
                    raise _AgyRpcError("agy did not report a sessionId", source="conversation")
                sid = sid_raw.strip()
                if conversation_id and sid != conversation_id:
                    raise _AgyRpcError(
                        "agy answered in an unexpected session",
                        source="conversation",
                    )

                config_options = list(sess_res.get("configOptions") or ())
                model_entry = _agy_config_entry(config_options, "model")
                available_models = _agy_option_values(model_entry)
                current_model = str(model_entry.get("currentValue") or "") if isinstance(model_entry, Mapping) else ""

                target_model, implied_effort = (
                    _agy_match_model(requested_model, available_models)
                    if requested_model.strip()
                    else (current_model or None, None)
                )
                if target_model and target_model != current_model:
                    cfg_res = _unwrap_rpc(
                        _rpc_call(
                            "session/set_config_option",
                            {"sessionId": sid, "configId": "model", "value": target_model},
                        ),
                        "session/set_config_option",
                    )
                    if isinstance(cfg_res.get("configOptions"), list):
                        config_options = list(cfg_res["configOptions"])
                    model_entry = _agy_config_entry(config_options, "model")
                    current_model = (
                        str(model_entry.get("currentValue") or "") if isinstance(model_entry, Mapping) else ""
                    ) or target_model

                resolved_model = target_model or current_model or requested_model
                eff_tl = _agy_match_effort(
                    resolved_model,
                    requested_effort,
                    effort_explicit=effort_explicit,
                    implied_effort=implied_effort,
                    config_options=config_options,
                )
                if eff_tl is not None:
                    tl_entry = _agy_config_entry(config_options, "thought_level")
                    current_tl = str(tl_entry.get("currentValue") or "") if isinstance(tl_entry, Mapping) else ""
                    if eff_tl != current_tl:
                        _unwrap_rpc(
                            _rpc_call(
                                "session/set_config_option",
                                {"sessionId": sid, "configId": "thought_level", "value": eff_tl},
                            ),
                            "session/set_config_option",
                        )
                if track_loaded_session and use_reusable:
                    worker.record_loaded_session(sid, requested_model, effort_key, resolved_model)

            active_prompt_sid = sid
            try:
                prompt_res = _unwrap_rpc(
                    _rpc_call(
                        "session/prompt",
                        {"sessionId": sid, "prompt": [{"type": "text", "text": prompt_text}]},
                    ),
                    "session/prompt",
                )
            finally:
                active_prompt_sid = None
            _check_tools_and_denial()

            stop_reason = prompt_res.get("stopReason")
            streamed = "".join(text_chunks)
            raw_res_resp = prompt_res.get("response") if isinstance(prompt_res.get("response"), str) else ""
            out_text = streamed.strip() or raw_res_resp.strip()
            _agy_check_filter_and_refusal(
                [out_text],
                stop_reason=stop_reason,
                edata=prompt_res,
            )
            if stop_reason not in ("end_turn", None):
                s_low = str(stop_reason).strip().lower()
                if s_low in _AGY_KNOWN_ERROR_STOPS:
                    raise _AgyRpcError(f"agy stopReason={s_low}", source=s_low)
                raise _AgyRpcError("agy ACP unexpected stopReason", source="status")

            if not out_text:
                raise _AgyRpcError("agy returned an empty answer", source="empty")
            usage = _agy_convert_usage(prompt_res.get("usage"))
            turn_ok = True
            return out_text, sid, resolved_model, usage, init_ms
        finally:
            if cancel_token is not None and abort_hook_id:
                cancel_token.unregister_abort(abort_hook_id)
            if worker is not None:
                if not use_reusable:
                    worker.stop(fast=emergency_kill or _is_aborted())
                elif not turn_ok or emergency_kill or _is_aborted():
                    self._discard_worker(worker, fast=True)

    def probe_ping(self, *, timeout_sec: float = 5.0) -> tuple[str, int, str]:
        """Lightweight ``agy acp`` ``initialize`` handshake probe (zero model prompt / zero quota)."""
        t0 = self.clock_fn()
        agy_bin = find_agy_bin(self.env)
        if not agy_bin:
            return "FAIL", 0, "FAIL (agy CLI not found in PATH)"
        try:
            workdir = self._ensure_agent_workdir()
            _, _, _, _, _ = self._run_acp_session(
                agy_bin=agy_bin,
                workdir=workdir,
                timeout_sec=timeout_sec,
                prompt_text=None,
                reuse_worker=False,
            )
            ms = _ms(t0, self.clock_fn)
            return "OK", ms, f"OK ({ms}ms, ACP initialize handshake)"
        except Exception as exc:
            ms = _ms(t0, self.clock_fn)
            safe = redact_secrets(str(exc), self.env, cfg=self.cfg)[:160]
            return "FAIL", ms, f"FAIL ({ms}ms: {safe})"

    def refine(
        self,
        asr_input: AsrResult | str,
        *,
        session_key: str = "",
        tracker: SessionTracker | None = None,
        fallback_context: str = "",
        is_cancelled: Callable[[], bool] | None = None,
        cancel_token: CancelToken | None = None,
        mode: str = "agent",
        workspace_cwd: str = "",
        target_hint: str = "",
    ) -> RefinerResult:
        requested_model = self.cfg.refiner.effective_model("agy")
        requested_effort = self.cfg.refiner.effective_effort("agy")
        if requested_effort not in VALID_EFFORTS:
            raise ConfigError(
                f"refiner.effort must be one of {sorted(VALID_EFFORTS)}, got {self.cfg.refiner.effort!r}"
            )
        clean_draft, single_hop = _unpack(asr_input)
        if not clean_draft or (single_hop and not self.cfg.refiner.force_on_single_hop):
            return NoopRefiner().refine(asr_input, mode=mode)
        t0 = self.clock_fn()
        if c_kind := _cancel_fallback_kind(cancel_token, is_cancelled):
            return _fallback_result(clean_draft, self.provider_name, requested_model, t0, self.clock_fn, c_kind)

        agy_bin = find_agy_bin(self.env)
        if not agy_bin:
            return _fallback_result(
                clean_draft,
                self.provider_name,
                requested_model,
                t0,
                self.clock_fn,
                "asr_draft",
                "agy CLI not found in PATH (install Antigravity CLI or set AGY_BIN)",
            )

        eff_mode = (mode or "agent").strip().lower() or "agent"
        context_blocks, final_block = build_refiner_request_blocks(
            self.cfg,
            clean_draft,
            session_key=session_key,
            tracker=tracker,
            fallback_context=fallback_context,
            mode=eff_mode,
            workspace_cwd=workspace_cwd,
            target_hint=target_hint,
        )
        ctx_chunks, ctx_chars = len(context_blocks), sum(len(b) for b in context_blocks)
        used_prior = any(b.startswith("<prior_session_reference") for b in context_blocks)

        try:
            workdir = self._ensure_agent_workdir()
        except Exception as exc:
            warn = redact_secrets(str(exc), self.env, cfg=self.cfg)
            return _fallback_result(
                clean_draft,
                self.provider_name,
                requested_model,
                t0,
                self.clock_fn,
                "asr_draft",
                warn,
                context_chars=ctx_chars,
                context_chunks=ctx_chunks,
                used_prior_session=used_prior,
            )

        sys_prompt = _compose_codex_instructions(resolve_refiner_prompt(self.cfg, mode=eff_mode))
        prompt_hash = hashlib.sha256(sys_prompt.encode("utf-8")).hexdigest()[:16]
        effort_explicit = self.cfg.refiner.has_explicit_effort("agy")
        effort_key = f"{requested_effort}:{'1' if effort_explicit else '0'}"
        max_turns = max(1, int(self.cfg.refiner.max_turns_per_thread))

        # Only stateful-resume when an explicit session_key identifies a single session;
        # stateless GUI apps without session_key must never share a conversation thread.
        norm_session_key = session_key.strip()
        thread_state: _AgyThreadState | None = (
            self._get_thread_state((norm_session_key, eff_mode)) if norm_session_key else None
        )

        tsec = effective_refiner_timeout_sec(self.cfg.refiner.timeout_sec, len(clean_draft))
        overall_deadline = t0 + tsec
        eff_cancelled = _combine_cancel_check(is_cancelled, cancel_token)
        resolved_model_box = [requested_model]
        secrets: tuple[str, ...] = tuple(self.creds.all_secret_values())
        pacer_key = make_rate_pacer_key("agy", f"local://{agy_bin}", "")

        def _is_aborted() -> bool:
            return self._closed or (eff_cancelled is not None and eff_cancelled())

        def _lock_wait_fallback(kind: str, label: str) -> RefinerResult:
            if kind == "cancelled":
                c_kind = _cancel_fallback_kind(cancel_token, is_cancelled) or "cancelled"
                return _fallback_result(
                    clean_draft,
                    self.provider_name,
                    requested_model,
                    t0,
                    self.clock_fn,
                    c_kind,
                    context_chars=ctx_chars,
                    context_chunks=ctx_chunks,
                    used_prior_session=used_prior,
                )
            return _fallback_result(
                clean_draft,
                self.provider_name,
                requested_model,
                t0,
                self.clock_fn,
                "asr_draft",
                f"agy refiner timed out waiting for {label} (budget={tsec:.2f}s)",
                context_chars=ctx_chars,
                context_chunks=ctx_chunks,
                used_prior_session=used_prior,
            )

        if thread_state is not None:
            if lock_err := _agy_acquire_lock_until(
                thread_state.lock, overall_deadline, self.clock_fn, _is_aborted
            ):
                return _lock_wait_fallback(lock_err, "session lock")

        worker_lock_acquired = False
        worker_busy_marked = False
        keep_worker = False
        pending_thread_commit: list[tuple[str, int]] = []
        try:
            if lock_err := _agy_acquire_lock_until(
                self._worker_lock, overall_deadline, self.clock_fn, _is_aborted
            ):
                return _lock_wait_fallback(lock_err, "worker lock")
            worker_lock_acquired = True

            with self._state_lock:
                self._cancel_idle_timer_locked()
                if self._closed:
                    return _lock_wait_fallback("cancelled", "worker lock")
                self._worker_busy = True
                worker_busy_marked = True

            def _attempt(
                req_timeout: float, _wall_dl: float | None, _is_first: bool
            ) -> tuple[str, _TokenUsageTuple, int]:
                pending_thread_commit.clear()
                attempt_deadline = min(overall_deadline, self.clock_fn() + req_timeout)
                full_prompt = (
                    f"{_AGY_PROMPT_PREFIX}{sys_prompt}\n\n"
                    + "\n\n".join((*context_blocks, final_block))
                )
                resume_cid = ""
                prev_turns = 0
                prev_sent: tuple[str, ...] = ()
                if thread_state is not None:
                    can_resume = (
                        bool(thread_state.conversation_id)
                        and thread_state.turn_count < max_turns
                        and thread_state.model == requested_model
                        and thread_state.effort == effort_key
                        and thread_state.prompt_hash == prompt_hash
                        and len(context_blocks) >= len(thread_state.sent_blocks)
                        and tuple(context_blocks[: len(thread_state.sent_blocks)]) == thread_state.sent_blocks
                    )
                    if can_resume:
                        resume_cid = thread_state.conversation_id
                        prev_turns = thread_state.turn_count
                        prev_sent = thread_state.sent_blocks
                    # Clear before running ACP so any timeout/cancel/EOF/RPC failure never leaves a dirty thread behind.
                    thread_state.conversation_id = ""
                    thread_state.turn_count = 0
                    thread_state.sent_blocks = ()

                def _remaining_or_timeout() -> float:
                    rem = attempt_deadline - self.clock_fn()
                    if rem <= 0:
                        raise _AgyRpcError(
                            f"agy did not finish within {tsec:.1f}s", source="hard_timeout"
                        )
                    return rem

                wall_attempt_dl = attempt_deadline if self.clock_fn is time.monotonic else None
                track_sess = thread_state is not None
                try:
                    if resume_cid:
                        delta_blocks = context_blocks[len(prev_sent) :]
                        resume_prompt = f"{_AGY_PROMPT_PREFIX}" + "\n\n".join((*delta_blocks, final_block))
                        try:
                            out_txt, sid, res_mod, usage, init_ms = self._run_acp_session(
                                agy_bin=agy_bin,
                                workdir=workdir,
                                timeout_sec=_remaining_or_timeout(),
                                deadline=wall_attempt_dl,
                                prompt_text=resume_prompt,
                                requested_model=requested_model,
                                requested_effort=requested_effort,
                                effort_explicit=effort_explicit,
                                conversation_id=resume_cid,
                                is_cancelled=is_cancelled,
                                cancel_token=cancel_token,
                                track_loaded_session=track_sess,
                            )
                            resumed = True
                        except _AgyRpcError as exc:
                            if exc.source != "conversation":
                                raise
                            out_txt, sid, res_mod, usage, init_ms = self._run_acp_session(
                                agy_bin=agy_bin,
                                workdir=workdir,
                                timeout_sec=_remaining_or_timeout(),
                                deadline=wall_attempt_dl,
                                prompt_text=full_prompt,
                                requested_model=requested_model,
                                requested_effort=requested_effort,
                                effort_explicit=effort_explicit,
                                conversation_id="",
                                is_cancelled=is_cancelled,
                                cancel_token=cancel_token,
                                track_loaded_session=track_sess,
                            )
                            resumed = False
                    else:
                        out_txt, sid, res_mod, usage, init_ms = self._run_acp_session(
                            agy_bin=agy_bin,
                            workdir=workdir,
                            timeout_sec=_remaining_or_timeout(),
                            deadline=wall_attempt_dl,
                            prompt_text=full_prompt,
                            requested_model=requested_model,
                            requested_effort=requested_effort,
                            effort_explicit=effort_explicit,
                            conversation_id="",
                            is_cancelled=is_cancelled,
                            cancel_token=cancel_token,
                            track_loaded_session=track_sess,
                        )
                        resumed = False
                except _AgyRpcError as exc:
                    rkind = "never" if exc.source in _AGY_NON_RETRYABLE_SOURCES else "transient"
                    raise _RefinerAttemptError(str(exc), retry_kind=rkind, error_code=exc.source) from None

                pending_thread_commit.append((sid, (prev_turns + 1) if resumed else 1))
                if res_mod:
                    resolved_model_box[0] = res_mod
                return out_txt, usage, init_ms

            res = _run_refiner_retry_loop(
                cfg=self.cfg,
                env=self.env,
                provider_name=self.provider_name,
                model=requested_model,
                model_fn=lambda: resolved_model_box[0],
                clean_draft=clean_draft,
                t0=t0,
                tsec=tsec,
                clock_fn=self.clock_fn,
                sleep_fn=self.sleep_fn,
                rand_fn=self.rand_fn,
                is_cancelled=is_cancelled,
                pacer_key_fn=lambda: pacer_key,
                secrets_fn=lambda: secrets,
                attempt_fn=_attempt,
                context_chars=ctx_chars,
                context_chunks=ctx_chunks,
                used_prior_session=used_prior,
                cancel_token=cancel_token,
            )
            succeeded = (
                not res.used_fallback
                and not self._closed
                and not (eff_cancelled is not None and eff_cancelled())
                and self.clock_fn() <= overall_deadline + 1e-9
            )
            if succeeded:
                keep_worker = self._idle_ttl_sec > 0
                if thread_state is not None and pending_thread_commit:
                    committed_sid, committed_turns = pending_thread_commit[-1]
                    thread_state.conversation_id = committed_sid
                    thread_state.turn_count = committed_turns
                    thread_state.sent_blocks = tuple(context_blocks)
                    thread_state.model = requested_model
                    thread_state.effort = effort_key
                    thread_state.prompt_hash = prompt_hash
            return res
        finally:
            try:
                if worker_busy_marked:
                    worker_to_stop: _AgyWorker | None = None
                    stop_fast = not keep_worker
                    with self._state_lock:
                        self._worker_busy = False
                        if keep_worker and not self._closed and self._worker is not None:
                            if not self._arm_idle_timer_locked():
                                # Timer thread creation failed; reap worker synchronously instead of leaking or raising.
                                worker_to_stop = self._worker
                                self._worker = None
                                stop_fast = False
                        else:
                            self._cancel_idle_timer_locked()
                            worker_to_stop = self._worker
                            self._worker = None
                    if worker_to_stop is not None:
                        worker_to_stop.stop(fast=stop_fast)
            finally:
                if worker_lock_acquired:
                    self._worker_lock.release()
                if thread_state is not None:
                    thread_state.lock.release()

    def evict_connections(self) -> int:
        """Stop any idle reusable ``agy acp`` worker (e.g. on system ``power_resume``)."""
        with self._state_lock:
            self._cancel_idle_timer_locked()
            if self._worker is None or self._worker_busy:
                return 0
            worker = self._worker
            self._worker = None
        worker.stop(fast=False)
        return 1

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._cancel_idle_timer_locked()
            worker = self._worker
            self._worker = None
        if worker is not None:
            worker.abort()
            worker.stop(fast=True)
        with self._procs_lock:
            procs = list(self._active_procs)
            self._active_procs.clear()
        for proc in procs:
            _agy_stop_process_group(proc, fast=True)


def create_refiner(
    cfg: Config,
    creds: ProviderCredentials | None = None,
    *,
    env: Mapping[str, str] | None = None,
    runtime_dir: Path | None = None,
    transport: HttpTransport | None = None,
    clock_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    rand_fn: Callable[[], float] = random.random,
) -> BaseRefiner:
    eff_env = os.environ if env is None else env
    eff_creds = creds or resolve_credentials(cfg, eff_env)
    prov = select_refiner_provider(cfg, eff_creds, env)
    if prov == "openai-compatible":
        if not cfg.refiner.base_url.strip():
            raise ConfigError(
                "refiner.base_url must be explicitly set when refiner.provider is 'openai-compatible'"
            )
        if not cfg.refiner.model.strip():
            raise ConfigError(
                "refiner.model must be explicitly set when refiner.provider is 'openai-compatible'"
            )
    eff_api = cfg.refiner.effective_api(prov)
    if prov != "off" and cfg.refiner.effective_effort(prov) not in VALID_EFFORTS:
        raise ConfigError(
            f"refiner.effort must be one of {sorted(VALID_EFFORTS)}, got {cfg.refiner.effort!r}"
        )
    if eff_api == "off":
        return NoopRefiner()
    common_kw: dict[str, Any] = {
        "env": env,
        "transport": transport,
        "clock_fn": clock_fn,
        "sleep_fn": sleep_fn,
        "rand_fn": rand_fn,
    }
    if prov == "codex":
        return CodexRefiner(cfg, eff_creds, runtime_dir=runtime_dir, **common_kw)
    if prov == "agy":
        return AgyRefiner(
            cfg,
            eff_creds,
            env=env,
            runtime_dir=runtime_dir,
            clock_fn=clock_fn,
            sleep_fn=sleep_fn,
            rand_fn=rand_fn,
        )
    if eff_api == "gemini":
        return GeminiRefiner(cfg, eff_creds, **common_kw)
    if eff_api == "responses":
        return OpenAIResponsesRefiner(cfg, eff_creds, provider_name=prov, **common_kw)
    return OpenAICompatibleRefiner(cfg, eff_creds, provider_name=prov, **common_kw)
