from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PLUGIN_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = Path.home() / ".claude" / "plugin-data" / "reflection-enhance"

DEFAULT_CONFIG: dict[str, Any] = {
    "review_mode": "split",
    "trigger": {
        "min_tool_iterations": 5,
        "min_duration_seconds": 60,
        "require_error_or_correction": True,
    },
    "model": {
        "review_model": "haiku",
        "synthesis_model": "sonnet",
    },
    "rolling_window": {
        "window_size": 5,
        "consecutive_failures_threshold": 3,
    },
    "limits": {
        "max_transcript_chars": 120000,
        "max_events_chars": 80000,
        "max_injection_chars": 2000,
        "max_injection_items": 5,
        "max_candidate_confidence_threshold": 0.5,
        "max_event_file_bytes": 1048576,
    },
    "redaction": {
        "enabled": True,
        "patterns": ["api_key", "token", "password", "authorization", "cookie", "private_key"],
    },
    "ignore_patterns": [
        "gh auth",
        "gh: not logged in",
        "GitHub CLI not authenticated",
    ],
    "data_retention": {
        "session_events_days": 7,
        "candidate_pending_days": 30,
    },
}


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {"payload": parsed}


def data_root() -> Path:
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        return Path(raw)
    return DEFAULT_DATA_ROOT


def session_dir(session_id: str) -> Path:
    return data_root() / "sessions" / safe_name(session_id or "unknown-session")


def safe_name(s: str) -> str:
    value = str(s or "unknown").strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = value.strip(".-")
    return value[:120] or "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def load_config() -> dict[str, Any]:
    config = _deepcopy(DEFAULT_CONFIG)
    path = data_root() / "config.json"
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                _merge_dict(config, loaded)
    except Exception:
        return config
    return config


def redact_text(text: Any, patterns: list[str] | None = None) -> str | None:
    if text is None:
        return None
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    redactions = patterns or DEFAULT_CONFIG["redaction"]["patterns"]
    value = re.sub(r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*)bearer\s+[^\s,'\";]+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*)[^\s,'\";]+", r"\1[REDACTED]", value)
    for pattern in redactions:
        escaped = re.escape(str(pattern))
        value = re.sub(
            rf"(?i)(['\"]?{escaped}['\"]?\s*[:=]\s*)(['\"]?)[^\s,'\";}}]+",
            r"\1\2[REDACTED]",
            value,
        )
    value = re.sub(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", value)
    value = re.sub(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+", r"\1[REDACTED]", value)
    return value


def atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def write_log(name: str, obj: dict[str, Any]) -> None:
    try:
        append_jsonl(data_root() / "logs" / f"{safe_name(name)}.jsonl", obj)
    except Exception:
        pass


def truncate_text(text: Any, max_chars: int = 4000) -> str | None:
    if text is None:
        return None
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "...[truncated]"


def truncate_transcript(text: Any, max_chars: int = 120000) -> str | None:
    if text is None:
        return None
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(value) <= max_chars:
        return value
    head_chars = int(max_chars * 0.3)
    tail_chars = max_chars - head_chars
    return value[:head_chars] + "...[truncated]..." + value[-tail_chars:]


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dict(base[key], value)
        else:
            base[key] = value


def _deepcopy(value: Any) -> Any:
    return json.loads(json.dumps(value))
