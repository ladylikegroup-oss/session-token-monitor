#!/usr/bin/env python3
"""Generate an OpenClaw transcript/context footer without exposing secrets.

This helper intentionally treats transcript size and context-token pressure as
separate signals:

- transcript size comes from the local session jsonl file;
- observed context tokens must come from a runtime/session-status source,
  status text, or an explicit CLI override;
- the displayed cumulative context is the trusted high-water mark within the
  current session and compaction segment.

Do not infer context pressure from per-call API usage fields such as
``totalTokens`` in message usage records. Those values can represent a single
model call, not the current conversation context.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SESSIONS_JSON = "~/.openclaw/agents/main/sessions/sessions.json"
DEFAULT_SESSIONS_DB = "~/.openclaw/agents/main/agent/openclaw-agent.sqlite"
DEFAULT_HISTORY_JSON = "~/.openclaw/session-token-monitor/history.json"

CONTEXT_RE = re.compile(r"📚\s*Context:\s*([0-9][0-9,]*(?:\.\d+)?)\s*([KkMm])?\s*/\s*([0-9][0-9,]*(?:\.\d+)?)\s*([KkMm])?")
MODEL_RE = re.compile(r"🧠\s*Model:\s*([^·\n]+)")
COMPACTIONS_RE = re.compile(r"🧹\s*Compactions:\s*([0-9][0-9,]*)")
RUNTIME_RE = re.compile(r"Runtime:\s*([^·\n]+)")


def load_sessions(path: str) -> Dict[str, Any]:
    p = Path(os.path.expanduser(path))
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("sessions JSON root is not an object")
    return data


def find_session_entry(
    data: Dict[str, Any],
    session_key: Optional[str],
    to_value: Optional[str],
    channel_id: Optional[str],
    allow_latest: bool,
) -> Tuple[str, Dict[str, Any]]:
    if session_key:
        entry = data.get(session_key)
        if not isinstance(entry, dict):
            raise KeyError(f"session_key not found: {session_key}")
        return session_key, entry

    if to_value:
        matches = [(k, v) for k, v in data.items() if isinstance(v, dict) and v.get("lastTo") == to_value]
        if not matches:
            raise KeyError(f"no session with lastTo={to_value}")
        matches.sort(key=lambda kv: kv[1].get("updatedAt", 0), reverse=True)
        return matches[0]

    if channel_id:
        wanted = f"channel:{channel_id}"
        matches = [(k, v) for k, v in data.items() if isinstance(v, dict) and v.get("lastTo") == wanted]
        if not matches:
            marker = f":channel:{channel_id}"
            matches = [(k, v) for k, v in data.items() if isinstance(v, dict) and marker in k and v.get("sessionFile")]
        if not matches:
            raise KeyError(f"no session with channel_id={channel_id}")
        matches.sort(key=lambda kv: kv[1].get("updatedAt", 0), reverse=True)
        return matches[0]

    if not allow_latest:
        raise ValueError("refusing to guess the current session; pass --session-key, --channel-id, or --to, or opt in with --allow-latest")

    candidates = [(k, v) for k, v in data.items() if isinstance(v, dict) and v.get("sessionFile")]
    if not candidates:
        raise KeyError("no session entries with sessionFile found")
    candidates.sort(key=lambda kv: kv[1].get("updatedAt", 0), reverse=True)
    return candidates[0]


def read_sqlite_session(path: str, session_key: Optional[str], strict: bool) -> Tuple[str, Dict[str, Any], Optional[int]]:
    """Read one current generation; never load conversation text or token usage.

    Size is logical UTF-8 event payload bytes, not SQLite/WAL file size. Cold
    archives or unsupported/incomplete byte metadata remain unknown.
    """
    if not session_key:
        raise ValueError("SQLite requires an exact --session-key; refusing to scan or guess")
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # Consistent key/generation/size snapshot, read-only.
        row = db.execute(
            "SELECT n.current_session_id, "
            "json_extract(n.entry_json, '$.compactionCount'), "
            "json_type(n.entry_json, '$.compactionCount') FROM session_nodes AS n "
            "JOIN session_windows AS w ON w.session_id=n.current_session_id "
            "AND w.session_key=n.session_key "
            "WHERE n.session_key=? AND n.entry_valid=1",
            (session_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session_key not found or unavailable in SQLite: {session_key}")
        # Extract only the count in SQL: never return full entry_json, tokens,
        # credential/profile fields, or conversation text to the helper.
        value, value_type = row[1], row[2]
        count = 0 if value_type in (None, "null") else (
            value if value_type == "integer" and value >= 0 else None
        )
        entry = {"sessionId": row[0], "compactionCount": count,
                 "_compactionSource": "sqlite-session-entry",
                 "_compactionCountUnavailable": count is None}
        size_bytes = None
        try:
            if db.execute("PRAGMA encoding").fetchone()[0] != "UTF-8":
                raise ValueError("unsupported transcript storage encoding")
            if db.execute(
                "SELECT 1 FROM session_transcript_cold_archives WHERE session_id=?",
                (row[0],),
            ).fetchone():
                raise ValueError("transcript is cold-archived; byte measurement unavailable")
            count, known, total = db.execute(
                "SELECT COUNT(*), COUNT(payload_bytes), SUM(payload_bytes) FROM ("
                "SELECT CASE "
                "WHEN event_json IS NOT NULL AND event_zstd IS NULL "
                "THEN length(CAST(event_json AS BLOB)) "
                "WHEN event_json IS NULL AND event_zstd IS NOT NULL "
                "AND event_utf8_bytes BETWEEN 1 AND 4194304 THEN event_utf8_bytes "
                "END AS payload_bytes FROM transcript_events WHERE session_id=?)",
                (row[0],),
            ).fetchone()
            if not count or known != count:
                raise ValueError("transcript byte measurement unavailable or incomplete")
            size_bytes = total
        except (sqlite3.DatabaseError, ValueError):
            if strict:
                raise
        return session_key, entry, size_bytes


def classify_transcript(size_mb: float) -> str:
    if size_mb < 1:
        return "輕微"
    if size_mb < 3:
        return "中度"
    if size_mb < 6:
        return "擁擠"
    return "極限"


def parse_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def parse_token_number(number: str, suffix: Optional[str]) -> Optional[int]:
    try:
        value = float(number.replace(",", ""))
    except (TypeError, ValueError):
        return None
    multiplier = 1
    if suffix and suffix.lower() == "k":
        multiplier = 1_000
    elif suffix and suffix.lower() == "m":
        multiplier = 1_000_000
    parsed = int(round(value * multiplier))
    return parsed if parsed >= 0 else None


def read_optional_text(value: Optional[str], file_value: Optional[str]) -> Optional[str]:
    if value:
        return value
    if file_value:
        return Path(os.path.expanduser(file_value)).read_text(encoding="utf-8")
    return None


def parse_status_text(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    result: Dict[str, Any] = {}
    m = CONTEXT_RE.search(text)
    if m:
        result["contextTokens"] = parse_token_number(m.group(1), m.group(2))
        result["contextLimit"] = parse_token_number(m.group(3), m.group(4))
    m = MODEL_RE.search(text)
    if m:
        result["model"] = m.group(1).strip()
    m = COMPACTIONS_RE.search(text)
    if m:
        result["compactions"] = parse_int(m.group(1))
    m = RUNTIME_RE.search(text)
    if m:
        result["runtime"] = m.group(1).strip()
    return result


def format_k_tokens(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    if value < 1000:
        return str(value)
    k = value / 1000.0
    if k >= 100:
        return f"{round(k):.0f}K"
    if k >= 10:
        return f"{k:.1f}K".replace(".0K", "K")
    return f"{k:.1f}K"


def load_history(path: str) -> Dict[str, Any]:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_private_parent(path: Path) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent_existed:
        os.chmod(path.parent, 0o700)


def save_history(path: str, data: Dict[str, Any]) -> None:
    """Atomically replace history so interruption cannot leave partial JSON."""
    p = Path(os.path.expanduser(path))
    ensure_private_parent(p)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, p)
        os.chmod(p, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def append_audit(path: Optional[str], record: Dict[str, Any]) -> None:
    if not path:
        return
    p = Path(os.path.expanduser(path))
    ensure_private_parent(p)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def is_trusted_token_source(source: str) -> bool:
    return source in {"runtime-status", "cli"}


def resolve_context_tokens(args: argparse.Namespace, entry: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], str, Dict[str, Any]]:
    """Return (current_context_tokens, context_limit, source, status_meta).

    `sessions.json.totalTokens` is intentionally not used here: in some
    OpenClaw/provider paths it reflects a single model-call usage value. Prefer
    values parsed from runtime status text or explicit values supplied by the
    caller. The local session index is only trusted for the context limit when
    available.
    """
    status_text = read_optional_text(args.status_text, args.status_file)
    status_meta = parse_status_text(status_text)

    context_tokens = status_meta.get("contextTokens")
    context_limit = status_meta.get("contextLimit")
    source = "runtime-status" if context_tokens is not None else "unknown"

    cli_tokens = parse_int(args.context_tokens)
    cli_limit = parse_int(args.context_limit)
    if context_tokens is None and cli_tokens is not None:
        context_tokens = cli_tokens
        source = args.token_source or "cli"
    elif context_tokens is not None and cli_tokens is not None and context_tokens != cli_tokens:
        status_meta["cliContextConflict"] = cli_tokens
    if context_limit is None and cli_limit is not None:
        context_limit = cli_limit
        if context_tokens is None and source == "unknown":
            source = "cli-limit-only"
    elif context_limit is not None and cli_limit is not None and context_limit != cli_limit:
        status_meta["cliLimitConflict"] = cli_limit

    if context_limit is None:
        context_limit = parse_int(entry.get("contextTokens"))
        if context_limit is not None and source == "unknown":
            source = "session-index-limit-only"

    return context_tokens, context_limit, source, status_meta


def resolve_compactions(status_meta: Dict[str, Any], entry: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Return the selected session's compaction count and source.

    OpenClaw's native status card reads ``compactionCount`` from the selected
    session entry and treats a missing value as zero. Prefer a freshly parsed
    runtime-status value when present, then mirror that native fallback.
    """
    runtime_value = parse_int(status_meta.get("compactions"))
    if runtime_value is not None:
        return runtime_value, "runtime-status"

    if entry.get("_compactionCountUnavailable"):
        return None, "sqlite-session-entry-unavailable"
    session_value = parse_int(entry.get("compactionCount"))
    if session_value is not None:
        return session_value, entry.get("_compactionSource", "session-index")

    return 0, "session-index-default"


def anomaly_warnings(
    session_key: str,
    context_tokens: Optional[int],
    context_limit: Optional[int],
    token_source: str,
    status_meta: Dict[str, Any],
    args: argparse.Namespace,
    entry: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], Optional[Dict[str, Any]], Dict[str, Any], Optional[int]]:
    warnings: List[str] = []
    history = {} if args.no_history else load_history(args.history_json)
    previous = history.get(session_key) if isinstance(history.get(session_key), dict) else None

    source_trusted = context_tokens is not None and is_trusted_token_source(token_source)
    if context_tokens is None:
        warnings.append("⚠️ Context token unavailable：未取得可靠 runtime context，已拒絕猜測。")
    elif not source_trusted:
        warnings.append(f"⚠️ Context token source suspicious：目前來源是 {token_source}，請優先改用 runtime status。")
    if "cliContextConflict" in status_meta or "cliLimitConflict" in status_meta:
        warnings.append("⚠️ Context source conflict：runtime status 與 CLI 值不一致，已保留 runtime status。")

    current_compactions = status_meta.get("compactions")
    current_model = status_meta.get("model") or args.model
    previous_compactions = parse_int(previous.get("compactions")) if previous else None
    current_session_id = str((entry or {}).get("sessionId") or "") or None
    previous_session_id = previous.get("sessionId") if previous else None
    session_changed = bool(previous_session_id and current_session_id and previous_session_id != current_session_id)
    compaction_changed = bool(
        previous_compactions is not None
        and current_compactions is not None
        and current_compactions != previous_compactions
    )

    if previous:
        prev_limit = parse_int(previous.get("contextLimit"))
        if prev_limit is not None and context_limit is not None and prev_limit != context_limit:
            hint = "可能是模型切換或 runtime 回報變動"
            warnings.append(f"⚠️ Context limit changed：{format_k_tokens(prev_limit)} → {format_k_tokens(context_limit)}（{hint}）。")

    previous_cumulative = None
    if previous and not session_changed and not compaction_changed:
        previous_cumulative = parse_int(previous.get("cumulativeContextTokens"))
        if previous_cumulative is None:
            previous_cumulative = parse_int(previous.get("contextTokens"))

    cumulative_context_tokens = context_tokens
    if context_tokens is not None and previous_cumulative is not None:
        cumulative_context_tokens = max(context_tokens, previous_cumulative)

    sample_trusted = bool(source_trusted)
    if not sample_trusted:
        cumulative_context_tokens = None
    current_record = {
        "time": int(time.time()),
        "sessionKey": session_key,
        "sessionId": current_session_id,
        "observedContextTokens": context_tokens,
        "cumulativeContextTokens": cumulative_context_tokens,
        # Retain this field for compatibility with older history readers. It
        # now matches the user-visible cumulative value.
        "contextTokens": cumulative_context_tokens,
        "contextLimit": context_limit,
        "tokenSource": token_source,
        "model": current_model,
        "runtime": status_meta.get("runtime") or args.runtime,
        "compactions": current_compactions,
        "trusted": sample_trusted,
        "historyUpdated": bool(sample_trusted and not args.no_history),
    }
    if not args.no_history and sample_trusted:
        history[session_key] = current_record
        save_history(args.history_json, history)
    return warnings, previous, current_record, cumulative_context_tokens


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    session_file = None
    transcript_line = None
    size_bytes = None
    size_mb = None
    level = None
    use_sqlite = getattr(args, "sessions_db", None) or (
        args.sessions_json is None and Path(DEFAULT_SESSIONS_DB).expanduser().exists()
    )
    if use_sqlite:
        key, entry, size_bytes = read_sqlite_session(
            getattr(args, "sessions_db", None) or DEFAULT_SESSIONS_DB,
            args.session_key, args.strict,
        )
        transcript_source = "sqlite-event-payload-bytes"
    else:
        data = load_sessions(args.sessions_json or DEFAULT_SESSIONS_JSON)
        key, entry = find_session_entry(data, args.session_key, args.to, args.channel_id, args.allow_latest)
        session_file = entry.get("sessionFile")
        transcript_source = "jsonl-file"
        if session_file:
            expanded = os.path.expanduser(str(session_file))
            try:
                size_bytes = os.stat(expanded).st_size
            except OSError as exc:
                if args.strict:
                    raise FileNotFoundError(f"cannot stat sessionFile: {expanded}: {exc}") from exc
    if size_bytes is not None:
        size_mb = size_bytes / (1024 * 1024)
        level = classify_transcript(size_mb)
        transcript_line = f"📝 Transcript: {size_mb:.2f} MB｜{level}"

    context_tokens, context_limit, token_source, status_meta = resolve_context_tokens(args, entry)
    compactions, compaction_source = resolve_compactions(status_meta, entry)
    status_meta["compactions"] = compactions
    status_meta["compactionSource"] = compaction_source
    compaction_line = f"🧹 對話壓縮：{compactions if compactions is not None else 'unknown'} 次"
    # Routine context-capacity warnings are intentionally disabled. Native
    # compaction and handoff own continuity; the footer remains informational.
    # Preserve the JSON field for consumers that already parse it.
    alert_line = None
    warnings, previous, current_record, cumulative_context_tokens = anomaly_warnings(
        key,
        context_tokens,
        context_limit,
        token_source,
        status_meta,
        args,
        entry,
    )
    context_label = "本輪 Context" if args.no_history else "累積 Context"
    displayed_context_tokens = context_tokens if args.no_history else cumulative_context_tokens
    context_limit_mismatch = bool(displayed_context_tokens is not None and context_limit is not None and displayed_context_tokens > context_limit)
    if context_limit_mismatch:
        context_line = f"📊 {context_label}：{format_k_tokens(displayed_context_tokens)}｜上限待核對"
    elif context_limit is not None:
        context_line = f"📊 {context_label}：{format_k_tokens(displayed_context_tokens)} / {format_k_tokens(context_limit)}"
    else:
        context_line = f"📊 {context_label}：{format_k_tokens(displayed_context_tokens)} tokens"

    lines = []
    lines.extend(warnings)
    if transcript_line:
        lines.append(transcript_line)
    lines.append(context_line)
    lines.append(compaction_line)

    payload = {
        "sessionKey": key,
        "sessionFile": session_file,
        "transcriptSource": transcript_source if size_bytes is not None else None,
        "sizeBytes": size_bytes,
        "sizeMb": round(size_mb, 2) if size_mb is not None else None,
        "level": level,
        "contextTokens": displayed_context_tokens,
        "observedContextTokens": context_tokens,
        "cumulativeContextTokens": cumulative_context_tokens,
        "contextLimit": context_limit,
        "contextLimitMismatch": context_limit_mismatch,
        "tokenSource": token_source,
        "statusMeta": status_meta,
        "previous": previous,
        "warnings": warnings,
        "transcriptLine": transcript_line,
        "contextLine": context_line,
        "compactions": compactions,
        "compactionSource": compaction_source,
        "compactionLine": compaction_line,
        "alertLine": alert_line,
        "lines": lines,
    }
    append_audit(args.audit_log, current_record | {"warnings": warnings, "sizeBytes": size_bytes})
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an OpenClaw transcript/context footer.")
    stores = parser.add_mutually_exclusive_group()
    stores.add_argument("--sessions-json", help="Explicit legacy/offline JSON store (no SQLite lookup)")
    stores.add_argument("--sessions-db", help="Canonical SQLite store (read-only, exact --session-key required)")
    parser.add_argument("--session-key")
    parser.add_argument("--to")
    parser.add_argument("--channel-id")
    parser.add_argument("--context-tokens", help="Current context tokens from runtime/session status")
    parser.add_argument("--context-limit", help="Context window limit from runtime/session status")
    parser.add_argument("--token-source", help="Override token source label when passing explicit context values")
    parser.add_argument("--status-text", help="Raw runtime/session status text containing '📚 Context: used/limit'")
    parser.add_argument("--status-file", help="File containing raw runtime/session status text")
    parser.add_argument("--model", help="Active model label for anomaly diagnostics")
    parser.add_argument("--runtime", help="Runtime label for audit diagnostics")
    parser.add_argument("--history-json", default=DEFAULT_HISTORY_JSON, help="State file used to compare previous footer values")
    parser.add_argument("--no-history", action="store_true", help="Do not read/write previous footer state")
    parser.add_argument("--audit-log", help="Optional JSONL audit log path for diagnostics")
    parser.add_argument("--drop-ratio", type=float, default=0.5, help="Warn if tokens drop below this ratio of previous value")
    parser.add_argument("--drop-min-previous", type=int, default=20_000, help="Minimum previous context tokens before drop warning is enabled")
    parser.add_argument("--allow-latest", action="store_true", help="Allow fallback to most recently updated session")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail if the transcript file cannot be inspected")
    args = parser.parse_args()

    try:
        payload = build_payload(args)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("\n".join(payload["lines"]))
        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[session footer unavailable: {exc}]", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
