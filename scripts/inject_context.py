from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import data_root, load_config, read_stdin_json, safe_name, truncate_text


def main() -> int:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "",
        }
    }
    try:
        payload = read_stdin_json()
        config = load_config()
        cwd = _extract_cwd(payload)
        context = build_context(config, cwd)
        output["hookSpecificOutput"]["additionalContext"] = context
    except Exception:
        pass
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0


def _extract_cwd(payload: dict[str, Any]) -> str:
    for key in ("cwd", "currentWorkingDirectory", "working_directory"):
        value = payload.get(key)
        if value:
            return str(value).replace("\\", "/").rstrip("/")
    return ""


def build_context(config: dict[str, Any], cwd: str = "") -> str:
    max_items = int(config.get("limits", {}).get("max_injection_items", 5))
    max_chars = int(config.get("limits", {}).get("max_injection_chars", 2000))

    reflections = _load_reflections(config, cwd)
    judged_count, has_pending_validation = _feedback_state()
    candidates = _load_staging_candidates()

    parts: list[str] = []
    if reflections:
        lines = ["Reflection-enhancement reminders from previous sessions:"]
        for item in reflections[:max_items]:
            lesson = item.get("lesson") or ""
            avoid = item.get("avoid_next_time") or ""
            if lesson or avoid:
                lines.append(f"- Lesson: {lesson} Avoid next time: {avoid}".strip())
        if len(lines) > 1:
            parts.append("\n".join(lines))

    if candidates:
        lines = ["候选 skill 待创建决策:"]
        for candidate in candidates[:10]:
            name = str(candidate.get("name") or "?")
            confidence = candidate.get("confidence")
            lines.append(f"- {name} (confidence: {confidence})")
            failure_pattern = str(candidate.get("failure_pattern") or "").strip()
            if failure_pattern:
                lines.append(f"  失败模式: {failure_pattern}")
            reuse_scope = str(candidate.get("reuse_scope") or "").strip()
            if reuse_scope:
                lines.append(f"  复用范围: {reuse_scope}")
        lines.append('→ 回复 "create:{name}" 或 "skip:{name}"')
        parts.append("\n".join(lines))

    if reflections and (judged_count < 5 or has_pending_validation):
        parts.append('→ 回复 "keep:<reflection_id> 原因" 或 "dismiss:<reflection_id> 原因"。你的反馈将帮助系统学习什么值得记住。')

    if not parts:
        return ""
    return truncate_text("\n\n".join(parts), max_chars=max_chars) or ""


def _load_reflections(config: dict[str, Any], cwd: str) -> list[dict[str, Any]]:
    index_path = data_root() / "reflections" / "index.json"
    if not index_path.exists():
        return []
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            index = json.load(handle)
    except Exception:
        return []

    if isinstance(index, dict):
        items = index.get("reflections", index)
    else:
        items = index if isinstance(index, list) else []
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return []

    valid = [item for item in items if isinstance(item, dict)]

    cwd_items: list[dict[str, Any]] = []
    if cwd:
        cwd_lower = cwd.lower()
        for item in valid:
            session_ids = item.get("session_ids") or []
            if any(cwd_lower in str(sid).lower() for sid in session_ids):
                cwd_items.append(item)

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent_items: list[dict[str, Any]] = []
    for item in valid:
        updated = item.get("updated_at") or item.get("first_seen") or ""
        if updated:
            try:
                dt = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                if dt >= cutoff:
                    recent_items.append(item)
            except Exception:
                recent_items.append(item)
        else:
            recent_items.append(item)

    seen_fps: set[str] = set()
    merged: list[dict[str, Any]] = []
    for item in cwd_items + recent_items:
        fp = item.get("fingerprint") or item.get("reflection_id") or id(item)
        if fp not in seen_fps:
            seen_fps.add(fp)
            merged.append(item)

    merged.sort(key=lambda x: (_confidence(x), _timestamp_seconds(x)), reverse=True)

    max_items = int(config.get("limits", {}).get("max_injection_items", 5))
    if merged:
        return merged[:max_items]

    fallback: list[dict[str, Any]] = []
    for item in valid:
        if float(item.get("confidence") or 0) >= 0.6:
            fallback.append(item)
    fallback.sort(key=lambda x: (_confidence(x), _timestamp_seconds(x)), reverse=True)
    return fallback[:3]


def _feedback_state() -> tuple[int, bool]:
    index_path = data_root() / "reflections" / "index.json"
    has_staging_candidates = bool(_load_staging_candidates())
    if not index_path.exists():
        return 0, has_staging_candidates
    try:
        with index_path.open("r", encoding="utf-8") as handle:
            index = json.load(handle)
    except Exception:
        return 0, has_staging_candidates
    if not isinstance(index, dict):
        return 0, has_staging_candidates

    try:
        judged_count = int(index.get("judged_count") or 0)
    except Exception:
        judged_count = 0

    items = index.get("reflections", index)
    if isinstance(items, dict):
        values = items.values()
    elif isinstance(items, list):
        values = items
    else:
        values = []
    has_pending_validation = any(isinstance(item, dict) and item.get("needs_user_feedback") for item in values)
    has_pending_validation = has_pending_validation or has_staging_candidates
    return judged_count, has_pending_validation


def _confidence(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence") or 0)
    except Exception:
        return 0.0


def _timestamp_seconds(item: dict[str, Any]) -> float:
    value = item.get("updated_at") or item.get("first_seen") or ""
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _load_staging_candidates() -> list[dict[str, Any]]:
    candidates_dir = data_root() / "candidate-skills"
    if not candidates_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for meta_path in candidates_dir.glob("*/candidate.json"):
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
            if isinstance(meta, dict) and meta.get("status") == "staging":
                results.append(meta)
        except Exception:
            continue
    return results


if __name__ == "__main__":
    sys.exit(main())
