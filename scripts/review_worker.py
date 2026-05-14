from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from common import (
    append_jsonl,
    atomic_write_json,
    data_root,
    fingerprint,
    load_config,
    read_stdin_json,
    redact_text,
    safe_name,
    session_dir,
    truncate_text,
    truncate_transcript,
    utc_now,
    write_log,
)


REFLECTION_PROMPT = """You are the asynchronous reflection-enhancement reviewer for Claude Code.

Review the transcript and tool event log. Identify durable lessons only when the session contains
one or more meaningful trigger signals:
- tool failures
- user corrections
- wrong assumptions
- wrong tool ordering
- missed verification
- repeated failure patterns

Filter out lessons that are not worth recording:
- environment-only failures
- temporary outages or transient issues
- purely negative tool conclusions with no reusable behavior change
- one-off task-specific facts

Return only JSON with this schema:
{
  "should_record": true,
  "trigger_signals": ["tool_failure", "user_correction"],
  "filter_matches": [],
  "lesson": "Concrete reusable lesson.",
  "avoid_next_time": "Specific action to take next time.",
  "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "short evidence"}],
  "confidence": 0.8,
  "fingerprint_source": "stable text used for dedupe"
}

Use concise strings. If there is no durable lesson, set should_record=false and explain with filter_matches.
"""


SKILL_PROMPT = """You are extracting candidate Claude Code skills from a recorded reflection.

Follow this priority order:
1. Update an existing candidate if the same dedupe_key already exists.
2. Add a new reference to an existing candidate when the behavior is related.
3. Create a new class-level skill only when it is reusable across tasks.
4. Otherwise do not create a candidate and provide do_not_create_reason.

Return only JSON with this schema:
{
  "candidates": [
    {
      "name": "short-safe-name",
      "dedupe_key": "stable-key",
      "failure_pattern": "what goes wrong",
      "reuse_scope": "when this skill applies",
      "confidence": 0.75,
      "proposed_action": "create_skill",
      "do_not_create_reason": null,
      "evidence_refs": [{"type": "reflection", "ref": "reflection:<id>", "summary": "short evidence"}],
      "skill_markdown": "# Skill name\\n\\nInstructions..."
    }
  ]
}

Quality gates: include evidence_refs, failure_pattern, reuse_scope, confidence, dedupe_key, and
do_not_create_reason when no candidate should be created. Do not propose narrow one-off skills.
"""


def main() -> int:
    try:
        payload = read_stdin_json()
        config = load_config()
        session_id = _session_id(payload)
        events = _read_events(session_id, int(config.get("limits", {}).get("max_events_chars", 80000)))
        transcript = _read_transcript(payload, int(config.get("limits", {}).get("max_transcript_chars", 120000)))
        transcript, events = _redact_inputs(transcript, events, config)
        decision = _trigger_decision(payload, events, transcript, config)
        if not decision.get("should_review"):
            write_log(
                "review-worker",
                {
                    "timestamp": utc_now(),
                    "session_id": session_id,
                    "status": "skipped",
                    "decision": decision,
                },
            )
            return 0

        reflection_raw = _call_claude(
            _build_reflection_input(transcript, events, decision),
            config,
            model=config.get("model", {}).get("review_model", "haiku"),
        )
        reflection = _parse_llm_json(reflection_raw, "reflection", session_id)
        if not isinstance(reflection, dict) or not reflection.get("should_record"):
            write_log(
                "review-worker",
                {
                    "timestamp": utc_now(),
                    "session_id": session_id,
                    "status": "reflection_skipped",
                    "decision": decision,
                    "parsed": bool(reflection),
                    "filter_matches": reflection.get("filter_matches") if isinstance(reflection, dict) else None,
                },
            )
            return 0

        if _matches_ignore_patterns(reflection, config):
            write_log(
                "review-worker",
                {
                    "timestamp": utc_now(),
                    "session_id": session_id,
                    "status": "reflection_ignored",
                    "reason": "matches_ignore_patterns",
                },
            )
            return 0

        warnings = _validate_reflection(reflection)
        if warnings:
            write_log(
                "review-worker",
                {
                    "timestamp": utc_now(),
                    "session_id": session_id,
                    "status": "validation_warnings",
                    "warnings": warnings,
                },
            )

        stored_reflection = _store_reflection(reflection, session_id, decision)
        _auto_judge_reflection(stored_reflection)
        candidates_written = 0
        if decision.get("review_mode") == "split":
            skill_raw = _call_claude(
                _build_skill_input(transcript, events, stored_reflection),
                config,
                model=config.get("model", {}).get("synthesis_model", "sonnet"),
            )
            skill_result = _parse_llm_json(skill_raw, "skill", session_id)
            candidates_written = _store_candidates(skill_result, stored_reflection, config)

        write_log(
            "review-worker",
            {
                "timestamp": utc_now(),
                "session_id": session_id,
                "status": "completed",
                "decision": decision,
                "fingerprint": stored_reflection.get("fingerprint"),
                "candidates_written": candidates_written,
            },
        )
    except Exception as exc:
        try:
            write_log("review-worker", {"timestamp": utc_now(), "status": "error", "message": str(exc)})
        except Exception:
            pass
    return 0


def _session_id(payload: dict[str, Any]) -> str:
    return str(payload.get("session_id") or payload.get("sessionId") or "unknown-session")


def _read_events(session_id: str, max_chars: int) -> str:
    path = session_dir(session_id) / "events.jsonl"
    if not path.exists():
        return ""
    return truncate_text(path.read_text(encoding="utf-8", errors="replace"), max_chars=max_chars) or ""


def _read_transcript(payload: dict[str, Any], max_chars: int) -> str:
    transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
    if transcript_path:
        path = Path(str(transcript_path))
        if path.exists():
            return truncate_transcript(path.read_text(encoding="utf-8", errors="replace"), max_chars=max_chars) or ""
    transcript = payload.get("transcript") or payload.get("conversation") or ""
    return truncate_transcript(transcript, max_chars=max_chars) or ""


def _trigger_decision(payload: dict[str, Any], events: str, transcript: str, config: dict[str, Any]) -> dict[str, Any]:
    mode = str(config.get("review_mode", "split") or "split").lower()
    if mode == "off":
        return {"should_review": False, "reason": "review_mode_off"}
    if mode not in {"split", "single"}:
        return {"should_review": False, "reason": "invalid_review_mode", "review_mode": mode}

    parsed_events = _parse_events(events)
    has_error = any(str(event.get("status", "")).lower() == "failure" for event in parsed_events)
    has_error = has_error or "\"status\": \"failure\"" in events or "PostToolUseFailure" in events
    correction_markers = ("wrong", "incorrect", "no,", "actually", "不是", "不对", "错了")
    has_correction = any(marker in transcript.lower() for marker in correction_markers)
    tool_iterations = len(parsed_events) if parsed_events else len([line for line in events.splitlines() if line.strip()])
    duration_seconds = _duration_seconds(payload, parsed_events)
    min_tools = int(config.get("trigger", {}).get("min_tool_iterations", 5))
    min_duration = int(config.get("trigger", {}).get("min_duration_seconds", 60))
    require_signal = bool(config.get("trigger", {}).get("require_error_or_correction", True))
    signal_ok = has_error or has_correction
    enough_tools = tool_iterations >= min_tools
    long_enough = duration_seconds >= min_duration
    complexity_ok = enough_tools or long_enough
    should_review = complexity_ok and (signal_ok or not require_signal)
    if should_review:
        reason = "eligible"
    elif require_signal and not signal_ok:
        reason = "no_error_or_correction_signal"
    elif not complexity_ok:
        reason = "below_tool_iteration_and_duration_threshold"
    else:
        reason = "below_trigger_threshold"
    return {
        "should_review": should_review,
        "reason": reason,
        "review_mode": mode,
        "tool_iterations": tool_iterations,
        "duration_seconds": duration_seconds,
        "has_error": has_error,
        "has_correction": has_correction,
        "min_tool_iterations": min_tools,
        "min_duration_seconds": min_duration,
        "require_error_or_correction": require_signal,
    }


def _redact_inputs(transcript: str, events: str, config: dict[str, Any]) -> tuple[str, str]:
    redaction = config.get("redaction", {})
    if not bool(redaction.get("enabled", True)):
        return transcript, events
    patterns = redaction.get("patterns", [])
    return redact_text(transcript, patterns) or "", redact_text(events, patterns) or ""


def _parse_events(events: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in events.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed


def _duration_seconds(payload: dict[str, Any], events: list[dict[str, Any]]) -> float:
    for key in ("duration_seconds", "durationSeconds", "elapsed_seconds", "elapsedSeconds"):
        if key in payload:
            try:
                return float(payload[key])
            except Exception:
                pass
    for key in ("duration_ms", "durationMs", "elapsed_ms", "elapsedMs"):
        if key in payload:
            try:
                return float(payload[key]) / 1000.0
            except Exception:
                pass

    timestamps = [str(event.get("timestamp") or "") for event in events if event.get("timestamp")]
    if len(timestamps) >= 2:
        try:
            from datetime import datetime

            def parse(value: str) -> datetime:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))

            return max(0.0, (parse(timestamps[-1]) - parse(timestamps[0])).total_seconds())
        except Exception:
            pass

    duration_ms = 0.0
    for event in events:
        try:
            duration_ms += float(event.get("duration_ms") or 0)
        except Exception:
            pass
    return duration_ms / 1000.0


def _build_reflection_input(transcript: str, events: str, decision: dict[str, Any]) -> str:
    return (
        REFLECTION_PROMPT
        + "\n\nTrigger decision:\n"
        + json.dumps(decision, ensure_ascii=False, indent=2)
        + "\n\nTranscript:\n"
        + transcript
        + "\n\nEvents JSONL:\n"
        + events
    )


def _build_skill_input(transcript: str, events: str, reflection: dict[str, Any]) -> str:
    return (
        SKILL_PROMPT
        + "\n\nStored reflection:\n"
        + json.dumps(reflection, ensure_ascii=False, indent=2)
        + "\n\nTranscript:\n"
        + transcript
        + "\n\nEvents JSONL:\n"
        + events
    )


def _call_claude(prompt: str, config: dict[str, Any], model: str | None = None) -> str:
    timeout = int(config.get("review_timeout_seconds", 300))
    selected_model = model or config.get("model", {}).get("review_model", "haiku")
    completed = subprocess.run(
        ["claude", "-p", "--model", selected_model],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 and not output:
        raise RuntimeError(truncate_text(completed.stderr or f"claude exited {completed.returncode}", 1000) or "claude failed")
    return output or (completed.stderr or "")


def _parse_llm_json(raw: str, kind: str, session_id: str) -> Any:
    raw = raw or ""
    attempts = [raw.strip(), _extract_fenced_json(raw), _extract_first_json_block(raw)]
    for candidate in attempts:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            repaired = _repair_json(candidate)
            if repaired != candidate:
                try:
                    return json.loads(repaired)
                except Exception:
                    pass

    repaired_fields = _field_level_repair(raw)
    if repaired_fields:
        return repaired_fields
    _save_raw_output(raw, kind, session_id)
    return None


def _extract_fenced_json(raw: str) -> str | None:
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_first_json_block(raw: str) -> str | None:
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return raw[start : index + 1]
    return None


def _repair_json(value: str) -> str:
    repaired = value.strip().replace("\ufeff", "")
    repaired = repaired.replace("“", '"').replace("”", '"').replace("’", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


def _field_level_repair(raw: str) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    fields = (
        "lesson",
        "avoid_next_time",
        "fingerprint_source",
        "failure_pattern",
        "reuse_scope",
        "dedupe_key",
        "do_not_create_reason",
    )
    for field in fields:
        match = re.search(rf"(?im)^\s*[-*]?\s*{re.escape(field)}\s*[:=-]\s*(.+)$", raw)
        if match:
            result[field] = match.group(1).strip().strip('"')
    bool_match = re.search(r"(?im)^\s*[-*]?\s*should_record\s*[:=-]\s*(true|false|yes|no)\s*$", raw)
    if bool_match:
        result["should_record"] = bool_match.group(1).lower() in {"true", "yes"}
    conf_match = re.search(r"(?im)^\s*[-*]?\s*confidence\s*[:=-]\s*([0-9.]+)", raw)
    if conf_match:
        try:
            result["confidence"] = float(conf_match.group(1))
        except Exception:
            pass
    if result.get("lesson") or result.get("avoid_next_time"):
        result.setdefault("should_record", True)
        result.setdefault("trigger_signals", [])
        result.setdefault("filter_matches", [])
        result.setdefault("evidence_refs", [])
        result.setdefault("confidence", 0.5)
        return result
    return None


def _save_raw_output(raw: str, kind: str, session_id: str) -> None:
    try:
        path = data_root() / "diagnostics" / f"{safe_name(kind)}-{safe_name(session_id)}-{safe_name(utc_now())}.raw.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(truncate_text(redact_text(raw) or "", 20000) or "", encoding="utf-8", newline="\n")
    except Exception:
        pass


def _matches_ignore_patterns(reflection: dict[str, Any], config: dict[str, Any]) -> bool:
    patterns = config.get("ignore_patterns") or []
    if not patterns:
        return False
    text = " ".join(
        str(reflection.get(key) or "")
        for key in ("lesson", "avoid_next_time", "fingerprint_source")
    ).lower()
    return any(pattern.lower() in text for pattern in patterns)


def _validate_reflection(reflection: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    lesson = str(reflection.get("lesson") or "").strip()
    avoid = str(reflection.get("avoid_next_time") or reflection.get("avoid") or "").strip()
    lesson_lower = lesson.lower()

    if len(lesson) < 10 or lesson_lower in {"test", "todo", "n/a", "na", "none", "null"}:
        warnings.append("lesson is empty, too short, or placeholder-like")

    if lesson and avoid and lesson_lower == avoid.lower():
        warnings.append("lesson and avoid_next_time are identical")

    confidence = _float_between(reflection.get("confidence"), default=0.0)
    if confidence >= 0.8 and not _normalize_evidence(reflection.get("evidence_refs")):
        warnings.append("high confidence reflection has no evidence_refs")

    return warnings


def _store_reflection(reflection: dict[str, Any], session_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    lesson = str(reflection.get("lesson") or "").strip()
    avoid = str(reflection.get("avoid_next_time") or reflection.get("avoid") or "").strip()
    source = str(reflection.get("fingerprint_source") or f"{lesson}\n{avoid}")
    fp = str(reflection.get("fingerprint") or fingerprint(source))
    reflection_id = str(reflection.get("reflection_id") or uuid.uuid4())
    confidence = _float_between(reflection.get("confidence"), default=0.5)
    evidence_refs = _normalize_evidence(reflection.get("evidence_refs"))

    stored = {
        "schema_version": "1.0",
        "reflection_id": reflection_id,
        "fingerprint": fp,
        "created_at": now,
        "updated_at": now,
        "session_ids": [session_id],
        "should_record": bool(reflection.get("should_record", True)),
        "trigger_signals": _string_list(reflection.get("trigger_signals")),
        "filter_matches": _string_list(reflection.get("filter_matches")),
        "lesson": lesson,
        "avoid_next_time": avoid,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "seen_count": 1,
    }

    root = data_root() / "reflections"
    index_path = root / "index.json"
    index_doc, index = _load_index_parts(index_path)
    existing = index.get(fp)
    if isinstance(existing, dict):
        stored["reflection_id"] = existing.get("reflection_id") or reflection_id
        stored["created_at"] = existing.get("first_seen") or existing.get("created_at") or now
        stored["session_ids"] = _merge_unique(_string_list(existing.get("session_ids")), [session_id])
        stored["seen_count"] = int(existing.get("seen_count") or 1) + 1
        stored["evidence_refs"] = _merge_evidence(_normalize_evidence(existing.get("evidence_refs")), evidence_refs)

    index[fp] = {
        "schema_version": "1.0",
        "first_seen": stored["created_at"],
        "updated_at": now,
        "title": truncate_text(lesson, 80) or fp,
        "session_id": session_id,
        "session_ids": stored["session_ids"],
        "reflection_id": stored["reflection_id"],
        "lesson": lesson,
        "avoid_next_time": avoid,
        "confidence": confidence,
        "seen_count": stored["seen_count"],
        "evidence_refs": stored["evidence_refs"],
    }
    _write_index_parts(index_path, index_doc, index)
    _append_reflection_markdown(root / "reflections.md", stored, decision)
    return stored


def _auto_judge_reflection(reflection: dict[str, Any]) -> None:
    rules_path = data_root() / "reflections" / "rules.json"
    if not rules_path.exists():
        return
    try:
        with rules_path.open("r", encoding="utf-8") as handle:
            rules = json.load(handle)
    except Exception as exc:
        write_log("review-worker", {"timestamp": utc_now(), "status": "auto_judge_skipped", "reason": str(exc)})
        return
    if not isinstance(rules, dict):
        return

    metadata = rules.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        auto_count = int(metadata.get("auto_judgments_since_user_feedback") or 0)
    except Exception:
        auto_count = 0

    if auto_count >= 9:
        _mark_reflection_needs_user_feedback(reflection)
        metadata["auto_judgments_since_user_feedback"] = 0
        metadata["validation_requested_at"] = utc_now()
        rules["metadata"] = metadata
        atomic_write_json(rules_path, rules)
        return

    try:
        import feedback_collector

        action, reason = feedback_collector.auto_judge(reflection, rules)
    except Exception as exc:
        write_log("review-worker", {"timestamp": utc_now(), "status": "auto_judge_error", "message": str(exc)})
        return

    append_jsonl(
        data_root() / "reflections" / "feedback_log.jsonl",
        {
            "timestamp": utc_now(),
            "reflection_id": str(reflection.get("reflection_id") or ""),
            "action": action,
            "reason": reason,
            "source": "auto_judge",
        },
    )

    metadata["auto_judgments_since_user_feedback"] = auto_count + 1
    metadata["last_auto_judgment_at"] = utc_now()
    rules["metadata"] = metadata
    atomic_write_json(rules_path, rules)


def _mark_reflection_needs_user_feedback(reflection: dict[str, Any]) -> None:
    index_path = data_root() / "reflections" / "index.json"
    index_doc, index = _load_index_parts(index_path)
    fp = str(reflection.get("fingerprint") or "")
    if not fp or fp not in index or not isinstance(index.get(fp), dict):
        return
    index[fp]["needs_user_feedback"] = True
    _write_index_parts(index_path, index_doc, index)


def _load_index_parts(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _load_index_document(path)
    reflections = document.get("reflections")
    if isinstance(reflections, dict):
        return document, reflections
    entries = {k: v for k, v in document.items() if isinstance(v, dict)}
    meta = {k: v for k, v in document.items() if not isinstance(v, dict)}
    meta["reflections"] = entries
    return meta, entries


def _write_index_parts(path: Path, document: dict[str, Any], reflections: dict[str, Any]) -> None:
    if document:
        document["reflections"] = reflections
        atomic_write_json(path, document)
        return
    atomic_write_json(path, reflections)


def _load_index_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        try:
            backup = path.with_suffix(path.suffix + f".corrupt.{safe_name(utc_now())}")
            path.replace(backup)
        except Exception:
            pass
    return {}


def _load_index(path: Path) -> dict[str, Any]:
    return _load_index_parts(path)[1]


def _append_reflection_markdown(path: Path, reflection: dict[str, Any], decision: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence = ", ".join(_evidence_label(item) for item in reflection.get("evidence_refs", [])) or "n/a"
    now = utc_now()
    block = (
        f"\n## {now[:10]} - {reflection.get('fingerprint')}\n\n"
        f"- Date: {now}\n"
        f"- Session: {', '.join(reflection.get('session_ids') or [])}\n"
        f"- Trigger: {decision.get('reason')} "
        f"(error={decision.get('has_error')}, correction={decision.get('has_correction')}, "
        f"tools={decision.get('tool_iterations')}, duration={decision.get('duration_seconds')})\n"
        f"- Lesson: {reflection.get('lesson')}\n"
        f"- Next time: {reflection.get('avoid_next_time')}\n"
        f"- Evidence: {evidence}\n"
        f"- Confidence: {reflection.get('confidence')}\n"
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)


def _store_candidates(skill_result: Any, reflection: dict[str, Any], config: dict[str, Any]) -> int:
    if not isinstance(skill_result, dict):
        return 0
    candidates = skill_result.get("candidates")
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return 0

    written = 0
    threshold = float(config.get("limits", {}).get("max_candidate_confidence_threshold", 0.5))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        confidence = _float_between(candidate.get("confidence"), default=0.0)
        if confidence < threshold:
            continue
        if candidate.get("do_not_create_reason") or candidate.get("proposed_action") == "do_not_create":
            continue
        dedupe_key = str(candidate.get("dedupe_key") or fingerprint(str(candidate.get("failure_pattern") or candidate.get("name") or "")))
        name = safe_name(str(candidate.get("name") or dedupe_key))
        candidate_dir = data_root() / "candidate-skills" / name
        existing_dir = _find_candidate_by_dedupe(dedupe_key)
        if existing_dir is not None:
            candidate_dir = existing_dir
        candidate_dir.mkdir(parents=True, exist_ok=True)
        refs_dir = candidate_dir / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)

        metadata_path = candidate_dir / "candidate.json"
        existing = _load_candidate(metadata_path)
        now = utc_now()
        evidence = _merge_evidence(
            _normalize_evidence(existing.get("evidence_refs") if existing else []),
            _normalize_evidence(candidate.get("evidence_refs"))
            + [
                {
                    "type": "reflection",
                    "ref": f"reflection:{reflection.get('reflection_id')}",
                    "summary": reflection.get("lesson") or "",
                }
            ],
        )
        metadata = {
            "schema_version": "1.0",
            "candidate_id": (existing or {}).get("candidate_id") or str(uuid.uuid4()),
            "name": name,
            "dedupe_key": dedupe_key,
            "status": (existing or {}).get("status") or "staging",
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "source_reflection_ids": _merge_unique(
                _string_list((existing or {}).get("source_reflection_ids")),
                [str(reflection.get("reflection_id") or "")],
            ),
            "evidence_refs": evidence,
            "failure_pattern": str(candidate.get("failure_pattern") or (existing or {}).get("failure_pattern") or ""),
            "reuse_scope": str(candidate.get("reuse_scope") or (existing or {}).get("reuse_scope") or ""),
            "confidence": confidence,
            "proposed_action": str(candidate.get("proposed_action") or "create_skill"),
            "do_not_create_reason": candidate.get("do_not_create_reason"),
            "skill_path": str(candidate_dir / "SKILL.md"),
            "references_path": str(refs_dir),
            "seen_count": int((existing or {}).get("seen_count") or 0) + 1,
        }
        atomic_write_json(metadata_path, metadata)
        skill_markdown = str(candidate.get("skill_markdown") or "").strip() or _default_skill_markdown(metadata)
        (candidate_dir / "SKILL.md").write_text(skill_markdown + "\n", encoding="utf-8", newline="\n")
        _write_candidate_reference(refs_dir, reflection, metadata)
        written += 1
    return written


def _find_candidate_by_dedupe(dedupe_key: str) -> Path | None:
    root = data_root() / "candidate-skills"
    if not root.exists():
        return None
    for metadata_path in root.glob("*/candidate.json"):
        loaded = _load_candidate(metadata_path)
        if loaded.get("dedupe_key") == dedupe_key:
            return metadata_path.parent
    return None


def _load_candidate(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}
    return {}


def _write_candidate_reference(refs_dir: Path, reflection: dict[str, Any], metadata: dict[str, Any]) -> None:
    path = refs_dir / f"{safe_name(str(reflection.get('reflection_id') or utc_now()))}.md"
    text = (
        f"# Reference {reflection.get('reflection_id')}\n\n"
        f"- Candidate: {metadata.get('name')}\n"
        f"- Date: {utc_now()}\n"
        f"- Reflection fingerprint: {reflection.get('fingerprint')}\n"
        f"- Lesson: {reflection.get('lesson')}\n"
        f"- Next time: {reflection.get('avoid_next_time')}\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _default_skill_markdown(metadata: dict[str, Any]) -> str:
    title = str(metadata.get("name") or "reflection-enhancement-candidate")
    return (
        f"# {title}\n\n"
        "Use this candidate when the same failure pattern appears again.\n\n"
        f"Failure pattern: {metadata.get('failure_pattern')}\n\n"
        f"Reuse scope: {metadata.get('reuse_scope')}\n\n"
        "Instructions:\n"
        "- Before acting, check whether this task matches the reuse scope.\n"
        "- Avoid the recorded failure pattern.\n"
        "- Verify the outcome before finishing.\n"
    )


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(
                {
                    "type": str(item.get("type") or "unknown"),
                    "ref": str(item.get("ref") or ""),
                    "summary": str(item.get("summary") or ""),
                }
            )
        elif item:
            normalized.append({"type": "unknown", "ref": str(item), "summary": ""})
    return normalized


def _merge_evidence(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, Any]] = []
    for item in left + right:
        key = (str(item.get("type")), str(item.get("ref")), str(item.get("summary")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[:50]


def _evidence_label(item: dict[str, Any]) -> str:
    label = str(item.get("ref") or item.get("type") or "evidence")
    summary = str(item.get("summary") or "")
    if summary:
        return f"{label} ({truncate_text(summary, 80)})"
    return label


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    if value:
        return [str(value)]
    return []


def _merge_unique(left: list[str], right: list[str]) -> list[str]:
    result: list[str] = []
    for item in left + right:
        if item and item not in result:
            result.append(item)
    return result


def _float_between(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, parsed))


if __name__ == "__main__":
    sys.exit(main())


