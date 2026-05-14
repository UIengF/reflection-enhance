from __future__ import annotations

import json
import sys
import uuid
from collections import deque
from typing import Any

from common import append_jsonl, load_config, redact_text, session_dir, truncate_text, utc_now

ROLLING_WINDOW_WARNING = (
    "Warning: Recent tool calls show a pattern of consecutive failures. "
    "Consider trying a different approach or asking the user for clarification."
)


def main() -> int:
    try:
        payload = _read_payload()
        config = load_config()
        event = build_event(payload, config)
        if not event["session_id"]:
            return 0
        events_path = session_dir(event["session_id"]) / "events.jsonl"
        max_bytes = int(config.get("limits", {}).get("max_event_file_bytes", 1048576))
        if events_path.exists() and events_path.stat().st_size >= max_bytes:
            return 0
        append_jsonl(events_path, event)
        warning = _check_rolling_window(event["session_id"], config)
        if warning and not _has_already_warned(event["session_id"]):
            _mark_warned(event["session_id"])
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": warning,
                        }
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    except Exception:
        return 0
    return 0


def build_event(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    hook = _first(payload, "hook_event_name", "hookEventName", "event", default="")
    status = "failure" if hook == "PostToolUseFailure" or _has_error(payload) else "success"
    session_id = _first(payload, "session_id", "sessionId", "conversation_id", "conversationId", default="unknown-session")
    tool_input = _first(payload, "tool_input", "toolInput", "input", "parameters", default={})
    tool_output = _first(payload, "tool_output", "toolOutput", "output", "result", default={})
    error = _first(payload, "error", "exception", default={})
    patterns = config.get("redaction", {}).get("patterns", [])
    redaction_enabled = bool(config.get("redaction", {}).get("enabled", True))

    def clean(value: Any, max_chars: int = 4000) -> str | None:
        text = truncate_text(value, max_chars=max_chars)
        return redact_text(text, patterns) if redaction_enabled else text

    return {
        "schema_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "session_id": str(session_id or "unknown-session"),
        "timestamp": _first(payload, "timestamp", "created_at", "createdAt", default=utc_now()),
        "hook": hook or ("PostToolUseFailure" if status == "failure" else "PostToolUse"),
        "tool_name": str(_first(payload, "tool_name", "toolName", "name", default="unknown")),
        "status": status,
        "duration_ms": _first(payload, "duration_ms", "durationMs", "elapsed_ms", "elapsedMs", default=None),
        "input_summary": {
            "command": clean(_find_key(tool_input, "command"), max_chars=1000),
            "path_refs": _path_refs(tool_input),
            "args_redacted": clean(tool_input, max_chars=3000),
        },
        "output_summary": {
            "exit_code": _find_key(tool_output, "exit_code", "exitCode", "code"),
            "stdout_excerpt": clean(_find_key(tool_output, "stdout"), max_chars=2000),
            "stderr_excerpt": clean(_find_key(tool_output, "stderr"), max_chars=2000),
            "result_excerpt": clean(tool_output, max_chars=3000),
        },
        "error_summary": {
            "type": clean(_find_key(error, "type", "name"), max_chars=200),
            "message": clean(_find_key(error, "message", "error"), max_chars=2000),
            "trace_excerpt": clean(_find_key(error, "trace", "traceback", "stack"), max_chars=3000),
        },
        "redactions_applied": ["secret", "token"] if redaction_enabled else [],
    }


def _read_payload() -> dict[str, Any]:
    try:
        from common import read_stdin_json

        return read_stdin_json()
    except Exception:
        return {}


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(mapping, dict) and key in mapping:
            return mapping[key]
    return default


def _find_key(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _find_key(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, *keys)
            if found is not None:
                return found
    return None


def _path_refs(value: Any) -> list[str]:
    refs: list[str] = []
    for key in ("path", "file_path", "filePath", "cwd"):
        found = _find_key(value, key)
        if found:
            refs.append(str(found))
    return refs[:10]


def _has_error(payload: dict[str, Any]) -> bool:
    return bool(_first(payload, "error", "exception", default=None))


def _check_rolling_window(session_id: str, config: dict[str, Any]) -> str | None:
    settings = config.get("rolling_window", {})
    window_size = int(settings.get("window_size", 5) or 5)
    threshold = int(settings.get("consecutive_failures_threshold", 3) or 3)
    if window_size <= 0 or threshold <= 0:
        return None

    events = _tail_events(session_dir(session_id) / "events.jsonl", window_size)
    if not events:
        return None

    recent = events[-window_size:]
    statuses = [event.get("status") for event in recent]
    if _has_consecutive_failures(statuses, threshold):
        return ROLLING_WINDOW_WARNING

    if len(recent) >= window_size:
        failure_count = sum(1 for status in statuses if status == "failure")
        if failure_count / window_size >= 0.8:
            return ROLLING_WINDOW_WARNING

    previous: dict[str, Any] | None = None
    for event in recent:
        if (
            previous
            and previous.get("status") == "failure"
            and event.get("status") == "failure"
            and previous.get("tool_name") == event.get("tool_name")
        ):
            return ROLLING_WINDOW_WARNING
        previous = event

    return None


def _has_already_warned(session_id: str) -> bool:
    return (session_dir(session_id) / ".window_warned").exists()


def _mark_warned(session_id: str) -> None:
    marker = session_dir(session_id) / ".window_warned"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _tail_events(path: Any, max_lines: int) -> list[dict[str, Any]]:
    lines: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _has_consecutive_failures(statuses: list[Any], threshold: int) -> bool:
    count = 0
    for status in statuses:
        if status == "failure":
            count += 1
            if count >= threshold:
                return True
        else:
            count = 0
    return False


if __name__ == "__main__":
    sys.exit(main())
