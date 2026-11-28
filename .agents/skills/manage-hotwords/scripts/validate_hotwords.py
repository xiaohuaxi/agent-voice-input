#!/usr/bin/env python3
"""Dry-run validator for candidate personal.txt hotwords files.

Always imports agent_voice_input from this repository's src/ first (failing
loudly if unavailable), runs both the real parser and an untruncated raw-line
write-side validator (catching >5 aliases, empty aliases, self-referential
aliases, 2-char alias warnings, and cross-entry bidirectional collisions),
then prints Stage 1 multi-provider projections (including OpenRouter keyterms)
and Stage 2 budget summary.

Exits 0 only when parser errors/warnings and write-side errors/warnings are all 0.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
_REPO_SRC = _REPO_ROOT / "src"


def _import_repo_modules() -> tuple[Any, tuple[str, ...]]:
    if not (_REPO_SRC / "agent_voice_input" / "hotwords.py").is_file():
        raise RuntimeError(f"repository source not found under {_REPO_SRC}")
    if not (_REPO_SRC / "agent_voice_input" / "workspace_ctx.py").is_file():
        raise RuntimeError(f"repository workspace_ctx.py not found under {_REPO_SRC}")
    src_str = str(_REPO_SRC)
    if not sys.path or sys.path[0] != src_str:
        sys.path.insert(0, src_str)
    from agent_voice_input import hotwords, workspace_ctx  # type: ignore
    from agent_voice_input.workspace_ctx import BUILTIN_AGENT_HOTWORDS  # type: ignore

    repo_src_resolved = _REPO_SRC.resolve()
    for mod_name, mod in (("hotwords.py", hotwords), ("workspace_ctx.py", workspace_ctx)):
        mod_path = Path(mod.__file__).resolve()
        if repo_src_resolved not in mod_path.parents:
            raise RuntimeError(f"expected repo {mod_name} under {_REPO_SRC}, got {mod_path}")
    return hotwords, tuple(BUILTIN_AGENT_HOTWORDS)


SAMPLE_DYNAMIC_TERMS: tuple[str, ...] = tuple(f"workspace_sym_{i:02d}" for i in range(1, 21))
PROJECTION_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("doubao", ""),
    ("openai", "gpt-transcribe"),
    ("openrouter", "openai/gpt-transcribe"),
    ("whisper", "whisper-1"),
    ("dashscope", "qwen3-asr-flash"),
    ("single_hop", ""),
)


def _read_bounded_regular(hw: Any, target: Path) -> tuple[str | None, int, str]:
    """Single-pass bounded read of a non-symlink regular UTF-8 file; any failure aborts immediately."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None, 0, f"file not found: {target}"
    except OSError as exc:
        return None, 0, f"cannot stat file {target}: {exc}"

    if stat.S_ISLNK(st.st_mode):
        return (
            None,
            0,
            f"refusing to read hotwords through symlink {target}; symlink the hotwords/ directory instead",
        )
    if not stat.S_ISREG(st.st_mode):
        return None, 0, f"unsafe hotwords target (not a regular file): {target}"

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(target), os.O_RDONLY | nofollow)
    except OSError as exc:
        return None, 0, f"cannot open hotwords file {target}: {exc}"
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return None, 0, f"unsafe hotwords target (not a regular file): {target}"
        raw_bytes = hw._read_bounded_fd(fd, hw.HOTWORDS_MAX_BYTES + 1)
    except OSError as exc:
        return None, 0, f"cannot read hotwords file {target}: {exc}"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if len(raw_bytes) > hw.HOTWORDS_MAX_BYTES:
        return (
            None,
            int(fst.st_size),
            f"hotwords file {target} exceeds the {hw.HOTWORDS_MAX_BYTES // 1024} KiB limit",
        )
    try:
        decoded, _ = hw._decode_hotwords_bytes(target, raw_bytes)
    except Exception as exc:
        return None, len(raw_bytes), str(exc)
    return decoded, len(raw_bytes), ""


def _validate_raw_untruncated(
    hw: Any,
    raw_text: str,
    table: Any,
) -> tuple[list[str], list[str]]:
    """Validate raw untruncated lines with write-side rules before read-side drops/truncation."""
    clean_text = raw_text[1:] if raw_text.startswith("\ufeff") else raw_text
    cli_errors: list[str] = []
    cli_warnings: list[str] = []

    canonical_display: dict[str, str] = {}
    raw_aliases_by_term: dict[str, list[str]] = {}
    raw_alias_lows_by_term: dict[str, set[str]] = {}
    alias_owners: dict[str, set[str]] = {}

    conflict_depth = 0
    for line_no, line_body in enumerate(hw._split_physical_lines(clean_text, keepends=False), start=1):
        cm = hw._CONFLICT_MARKER_RE.match(line_body)
        if cm is not None:
            marker = cm.group(1)
            if marker == "<<<<<<<":
                conflict_depth += 1
                continue
            if conflict_depth > 0:
                if marker == ">>>>>>>":
                    conflict_depth -= 1
                continue
        elif conflict_depth > 0:
            continue

        if any(ch in hw._UNICODE_LINE_SEPS for ch in line_body):
            continue
        cleaned = hw._strip_line_comment(line_body)
        if not cleaned or hw._parse_section_header(cleaned) is not None:
            continue

        if hw._ALIAS_SEPARATOR in cleaned:
            left_raw, right_raw = cleaned.split(hw._ALIAS_SEPARATOR, 1)
        else:
            left_raw, right_raw = cleaned, None

        left = left_raw.strip(" ")
        if left.startswith("!"):
            left = left[1:].strip(" ")
        if left.startswith(r"\!"):
            left = left[1:]

        try:
            term = hw.validate_hotword_term(left)
        except ValueError:
            # Already recorded by parser's invalid_lines.
            continue

        term_low = term.lower()
        canonical_display.setdefault(term_low, term)
        raw_aliases_by_term.setdefault(term_low, [])
        raw_alias_lows_by_term.setdefault(term_low, set())

        if right_raw is not None:
            for piece in hw._ALIAS_SPLIT_RE.split(right_raw):
                try:
                    al = hw.validate_hotword_alias(piece)
                except ValueError as exc:
                    cli_errors.append(f"line {line_no} ({term!r}): {exc}")
                    continue
                al_low = al.lower()
                if al_low == term_low:
                    cli_errors.append(
                        f"line {line_no} ({term!r}): mishearing alias {al!r} "
                        f"cannot match its own canonical term {term!r}"
                    )
                    continue
                if al_low not in raw_alias_lows_by_term[term_low]:
                    raw_alias_lows_by_term[term_low].add(al_low)
                    raw_aliases_by_term[term_low].append(al)
                alias_owners.setdefault(al_low, set()).add(canonical_display[term_low])

    all_canonical_lows = set(canonical_display) | {e.term.lower() for e in table.entries}
    for term_low, aliases in raw_aliases_by_term.items():
        if not aliases:
            continue
        term = canonical_display[term_low]
        other_canonicals = all_canonical_lows - {term_low}
        try:
            _, warns = hw.validate_hotword_aliases(
                term,
                aliases,
                existing_canonical_lows=other_canonicals,
            )
            cli_warnings.extend(warns)
        except ValueError as exc:
            cli_errors.append(f"{term!r}: {exc}")
            # Still surface canonical collisions and 2-char warnings not reached before ValueError.
            for al in aliases:
                if al.lower() in other_canonicals and "conflicts with an existing canonical" not in str(exc):
                    cli_errors.append(
                        f"{term!r}: mishearing alias {al!r} conflicts with an existing canonical hotword"
                    )
                if len(al) == 2:
                    cli_warnings.append(
                        f"2-character mishearing alias {al!r} for {term!r} may risk colliding with common words; "
                        "keep only if you observe repeated ASR mishearings"
                    )

    for al_low, owners in sorted(alias_owners.items()):
        if len(owners) >= 2:
            cli_errors.append(
                f"alias {al_low!r} claimed by multiple entries: {', '.join(sorted(owners))}"
            )

    return cli_errors, cli_warnings


def _normalized_personal_maps(
    hw: Any,
    spec: Any,
    personal_terms: tuple[str, ...],
    pinned_terms: tuple[str, ...],
    extra_terms: tuple[str, ...],
) -> tuple[set[str], list[str]]:
    canonical_by_low: dict[str, str] = {}
    if spec.collapse_file_ext:
        for w in (*personal_terms, *extra_terms):
            canonical_by_low.setdefault(w.lower(), w)
    personal_lows: set[str] = set()
    for raw_w in personal_terms:
        w = hw._normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w:
            personal_lows.add(w.lower())
    pinned_norm: list[str] = []
    seen_pin: set[str] = set()
    for raw_w in pinned_terms:
        w = hw._normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w and w.lower() not in seen_pin:
            seen_pin.add(w.lower())
            pinned_norm.append(w)
    return personal_lows, pinned_norm


def _track_agent_personal_outcomes(
    hw: Any,
    spec: Any,
    ra: Any,
    personal_terms: tuple[str, ...],
    dynamic_terms: tuple[str, ...],
    builtin_terms: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Mirror project_stage1_hotwords (agent mode) to attribute exact personal kept/dropped terms."""
    p_clean = hw.dedupe_hotwords_ci(personal_terms)
    d_clean = hw.dedupe_hotwords_ci(dynamic_terms)
    b_clean = hw.dedupe_hotwords_ci(builtin_terms)
    canonical_by_low: dict[str, str] = {}
    if spec.collapse_file_ext:
        for w in (*p_clean, *d_clean, *b_clean):
            canonical_by_low.setdefault(w.lower(), w)

    eff_max_terms = ra.max_terms
    eff_max_chars = ra.max_chars
    dyn_reserve = 0 if eff_max_terms < 3 else (eff_max_terms * 3 + 9) // 10
    p_term_cap = max(0, eff_max_terms - dyn_reserve)
    p_char_cap = int(eff_max_chars * 0.70) if eff_max_chars is not None else None
    sep_len = len(spec.separator)

    kept: list[str] = []
    personal_kept_terms: list[str] = []
    personal_dropped_terms: list[str] = []
    used_chars = 0
    seen_out_low: set[str] = set()
    dynamic_kept = 0

    def _hard_reject(w: str) -> bool:
        if eff_max_terms <= 0 or (eff_max_chars is not None and eff_max_chars <= 0):
            return True
        return spec.max_term_chars is not None and len(w) > spec.max_term_chars

    personal_overflow: list[str] = []
    p_overflow_low: set[str] = set()
    for raw_w in p_clean:
        w = hw._normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w is None:
            personal_dropped_terms.append(raw_w)
            continue
        w_low = w.lower()
        if w_low in seen_out_low or w_low in p_overflow_low:
            continue
        if _hard_reject(w):
            seen_out_low.add(w_low)
            personal_dropped_terms.append(w)
            continue
        add_len = len(w) + (sep_len if kept else 0)
        fits_terms = len(personal_kept_terms) < p_term_cap and len(kept) < eff_max_terms
        fits_chars = p_char_cap is None or used_chars + add_len <= p_char_cap
        if fits_terms and fits_chars:
            kept.append(w)
            personal_kept_terms.append(w)
            used_chars += add_len
            seen_out_low.add(w_low)
        else:
            p_overflow_low.add(w_low)
            personal_overflow.append(w)

    for raw_w in d_clean:
        w = hw._normalize_stage1_candidate(raw_w, spec, canonical_by_low)
        if w is None:
            continue
        w_low = w.lower()
        if w_low in seen_out_low:
            continue
        if _hard_reject(w):
            seen_out_low.add(w_low)
            continue
        add_len = len(w) + (sep_len if kept else 0)
        fits_terms = dynamic_kept < dyn_reserve and len(kept) < eff_max_terms
        fits_chars = eff_max_chars is None or used_chars + add_len <= eff_max_chars
        if fits_terms and fits_chars:
            kept.append(w)
            used_chars += add_len
            seen_out_low.add(w_low)
            dynamic_kept += 1

    for w in personal_overflow:
        w_low = w.lower()
        if w_low in seen_out_low:
            continue
        seen_out_low.add(w_low)
        add_len = len(w) + (sep_len if kept else 0)
        if (
            _hard_reject(w)
            or len(kept) >= eff_max_terms
            or (eff_max_chars is not None and used_chars + add_len > eff_max_chars)
        ):
            personal_dropped_terms.append(w)
            continue
        kept.append(w)
        personal_kept_terms.append(w)
        used_chars += add_len

    if len(personal_kept_terms) == ra.personal_kept and len(personal_dropped_terms) == ra.personal_dropped:
        return personal_kept_terms, personal_dropped_terms

    personal_lows, _ = _normalized_personal_maps(hw, spec, personal_terms, (), (*dynamic_terms, *builtin_terms))
    fallback_kept = [w for w in ra.kept if w.lower() in personal_lows][: ra.personal_kept]
    fallback_dropped = [w for w in ra.dropped if w.lower() in personal_lows][: ra.personal_dropped]
    return fallback_kept, fallback_dropped


def validate_candidate(path: Path, *, allow_2char_warnings: bool = False) -> dict[str, Any]:
    hw, builtin_terms = _import_repo_modules()
    target = path.expanduser()
    raw_text, file_bytes, read_err = _read_bounded_regular(hw, target)
    if read_err or raw_text is None:
        return {"ok": False, "error": read_err or f"failed to read hotwords file: {target}"}

    table = hw.parse_hotwords_text(raw_text, path=target, exists=True, size_limit_exceeded=False)
    cli_errors, cli_warnings = _validate_raw_untruncated(hw, raw_text, table)

    pinned_entries = [e for e in table.entries if e.pinned]
    aliased_only = [e for e in table.entries if (not e.pinned and e.aliases)]
    plain_entries = [e for e in table.entries if (not e.pinned and not e.aliases)]
    total_aliases = sum(len(e.aliases) for e in table.entries)

    stage1_terms = table.stage1_terms
    pinned_terms = tuple(e.term for e in pinned_entries)
    top35_whisper_chars = len("、".join(stage1_terms[:35]))

    ctx_general = SimpleNamespace(
        hotwords_bucketed=True,
        mode="general",
        personal_hotwords=stage1_terms,
        dynamic_hotwords=(),
        builtin_hotwords=(),
    )
    ctx_agent = SimpleNamespace(
        hotwords_bucketed=True,
        mode="agent",
        personal_hotwords=stage1_terms,
        dynamic_hotwords=SAMPLE_DYNAMIC_TERMS,
        builtin_hotwords=builtin_terms,
    )

    stage1_report: list[dict[str, Any]] = []
    for prov, model in PROJECTION_PROVIDERS:
        spec = hw.resolve_hotwords_quota_spec(prov, model=model)
        rg = hw.project_stage1_hotwords(ctx_general, prov, model=model)
        ra = hw.project_stage1_hotwords(ctx_agent, prov, model=model)

        _, pinned_norm_g = _normalized_personal_maps(hw, spec, stage1_terms, pinned_terms, ())
        _, pinned_norm_a = _normalized_personal_maps(
            hw, spec, stage1_terms, pinned_terms, (*SAMPLE_DYNAMIC_TERMS, *builtin_terms)
        )

        g_kept_lows = {w.lower() for w in rg.kept}
        g_pinned_dropped = [w for w in pinned_norm_g if w.lower() not in g_kept_lows]
        g_pinned_kept_count = len(pinned_norm_g) - len(g_pinned_dropped)

        a_kept_lows = {w.lower() for w in ra.kept}
        a_pinned_dropped = [w for w in pinned_norm_a if w.lower() not in a_kept_lows]
        a_pinned_kept_count = len(pinned_norm_a) - len(a_pinned_dropped)

        personal_kept_a, personal_dropped_a = _track_agent_personal_outcomes(
            hw, spec, ra, stage1_terms, SAMPLE_DYNAMIC_TERMS, builtin_terms
        )
        last_personal_a = personal_kept_a[-1] if personal_kept_a else ""
        first_personal_dropped_a = personal_dropped_a[0] if personal_dropped_a else ""

        stage1_report.append({
            "provider": rg.label,
            "model": model,
            "max_terms": rg.max_terms,
            "max_chars": rg.max_chars,
            "general": {
                "kept": rg.kept_count,
                "used_chars": rg.used_chars,
                "last_kept": rg.kept[-1] if rg.kept else "",
                "first_dropped": rg.dropped[0] if rg.dropped else "",
                "pinned_kept": g_pinned_kept_count,
                "pinned_kept_count": g_pinned_kept_count,
                "pinned_dropped_count": len(g_pinned_dropped),
                "pinned_dropped": g_pinned_dropped,
            },
            "agent_20dyn": {
                "personal_kept": ra.personal_kept,
                "personal_dropped": ra.personal_dropped,
                "dynamic_kept": ra.dynamic_kept,
                "builtin_kept": ra.builtin_kept,
                "used_chars": ra.used_chars,
                "last_personal_kept": last_personal_a,
                "first_personal_dropped": first_personal_dropped_a,
                "pinned_kept": a_pinned_kept_count,
                "pinned_kept_count": a_pinned_kept_count,
                "pinned_dropped_count": len(a_pinned_dropped),
                "pinned_dropped": a_pinned_dropped,
            },
        })

    s2 = hw.summarize_stage2_hotwords_budget(table, use_cache=False)
    parser_warnings = table.diagnostic_warnings()
    blocking_cli_warnings = [] if allow_2char_warnings else cli_warnings
    ok = (
        table.exists
        and not table.encoding_error
        and table.warnings_count == 0
        and not cli_errors
        and not blocking_cli_warnings
    )

    return {
        "ok": ok,
        "path": str(target),
        "bytes": file_bytes,
        "max_bytes": hw.HOTWORDS_MAX_BYTES,
        "entries": len(table.entries),
        "max_entries": hw.HOTWORDS_MAX_ENTRIES,
        "tiers": {
            "pinned": len(pinned_entries),
            "aliased_only": len(aliased_only),
            "plain": len(plain_entries),
            "total_aliased_entries": sum(1 for e in table.entries if e.aliases),
            "total_aliases": total_aliases,
        },
        "top35_whisper_chars": top35_whisper_chars,
        "parser_warnings_count": table.warnings_count,
        "parser_warnings": parser_warnings,
        "cli_errors": cli_errors,
        "cli_warnings": cli_warnings,
        "stage1": stage1_report,
        "stage2": {
            "kept": s2.kept_count,
            "dropped": s2.dropped_count,
            "used_chars": s2.used_chars,
            "max_items": s2.max_items,
            "max_chars": s2.max_chars,
            "first_dropped": list(s2.dropped[:5]),
        },
    }


def _print_human(res: dict[str, Any]) -> None:
    if "error" in res:
        print(f"[FAIL] {res['error']}")
        return
    status = "PASS" if res["ok"] else "FAIL"
    t = res["tiers"]
    print(
        f"[{status}] {res['path']} — {res['entries']}/{res['max_entries']} entries, "
        f"{res['bytes']}/{res['max_bytes']} bytes ({res['bytes'] * 100.0 / res['max_bytes']:.2f}%)"
    )
    print(
        f"  Tiers: pinned={t['pinned']}, aliased_only={t['aliased_only']}, plain={t['plain']} "
        f"(total aliased entries={t['total_aliased_entries']}, aliases={t['total_aliases']})"
    )
    print(f"  Top-35 Whisper chars (with '、'): {res['top35_whisper_chars']}/353")
    print(
        f"  Diagnostics: parser_warnings={res['parser_warnings_count']}, "
        f"cli_errors={len(res['cli_errors'])}, cli_warnings={len(res['cli_warnings'])}"
    )
    for w in res["parser_warnings"]:
        print(f"    [PARSER WARN] {w}")
    for e in res["cli_errors"]:
        print(f"    [CLI ERROR] {e}")
    for w in res["cli_warnings"]:
        print(f"    [CLI WARN] {w}")
    print("  Stage 1 projections (agent mode uses 20 sample dynamic terms + builtin backfill):")
    for row in res["stage1"]:
        cap = f"{row['max_terms']} terms" + (f" / {row['max_chars']} chars" if row["max_chars"] else "")
        g = row["general"]
        a = row["agent_20dyn"]
        pin_g = f", pinned_kept={g['pinned_kept_count']}"
        if g["pinned_dropped_count"]:
            preview_g = g["pinned_dropped"][:5]
            more_g = f", +{g['pinned_dropped_count'] - len(preview_g)} more" if g["pinned_dropped_count"] > len(preview_g) else ""
            pin_g += f", pinned_dropped={g['pinned_dropped_count']} ({preview_g}{more_g})"
        pin_a = f", pinned_kept={a['pinned_kept_count']}"
        if a["pinned_dropped_count"]:
            preview_a = a["pinned_dropped"][:5]
            more_a = f", +{a['pinned_dropped_count'] - len(preview_a)} more" if a["pinned_dropped_count"] > len(preview_a) else ""
            pin_a += f", pinned_dropped={a['pinned_dropped_count']} ({preview_a}{more_a})"
        bi_a = f"+builtin {a['builtin_kept']}" if a["builtin_kept"] else ""
        print(
            f"    - {row['provider']} ({cap}): "
            f"general={g['kept']} ({g['used_chars']}c, last={g['last_kept']!r}{pin_g}); "
            f"agent+20dyn=personal {a['personal_kept']}+dyn {a['dynamic_kept']}{bi_a} "
            f"({a['used_chars']}c, last_p={a['last_personal_kept']!r}{pin_a})"
        )
    s2 = res["stage2"]
    tail = f", first_dropped={s2['first_dropped']}" if s2["dropped"] else ""
    print(
        f"  Stage 2 budget: {s2['kept']}/{s2['max_items']} items, "
        f"{s2['used_chars']}/{s2['max_chars']} chars (dropped={s2['dropped']}{tail})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a candidate personal.txt hotwords file.")
    parser.add_argument("path", type=Path, help="Path to candidate personal.txt")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "--allow-2char-warnings",
        action="store_true",
        help="Do not fail exit code on 2-character alias warnings",
    )
    args = parser.parse_args(argv)
    res = validate_candidate(args.path, allow_2char_warnings=args.allow_2char_warnings)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_human(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
