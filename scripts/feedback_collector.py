from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from common import append_jsonl, atomic_write_json, data_root, load_config, read_stdin_json, safe_name, truncate_text, utc_now


FEEDBACK_PROMPT = """You are synthesizing user feedback into reflection-enhancement rules.

Given feedback on reflections, derive concise rules for:
- which reflections should be kept
- which reflections should be dismissed
- why

Return only JSON with this schema:
{
  "rules": [
    {
      "action": "keep",
      "when": "condition",
      "reason": "why this feedback pattern matters"
    }
  ],
  "metadata": {
    "summary": "short summary"
  }
}
"""


AUTO_JUDGE_PROMPT = """You are judging whether a reflection is worth keeping.

Use the supplied rules to decide whether the reflection should be kept or dismissed.
Return only JSON with this schema:
{
  "action": "keep",
  "reason": "short reason"
}

The action must be "keep" or "dismiss".
"""


def collect_feedback(reflection_id: str, action: str, reason: str) -> None:
    action = _normalize_action(action)
    record = {
        "timestamp": utc_now(),
        "reflection_id": str(reflection_id),
        "action": action,
        "reason": str(reason or ""),
    }
    append_jsonl(_feedback_log_path(), record)
    _increment_judged_count()
    _clear_pending_feedback(str(reflection_id))
    _reset_auto_judgment_counter()


def decide_candidate(candidate_name: str, action: str) -> None:
    candidate_name = safe_name(str(candidate_name or ""))
    normalized = str(action or "").strip().lower()
    if normalized not in {"create", "skip"}:
        raise ValueError("action must be 'create' or 'skip'")
    metadata_path = data_root() / "candidate-skills" / candidate_name / "candidate.json"
    metadata = _read_json(metadata_path)
    if not metadata:
        raise FileNotFoundError(str(metadata_path))

    metadata["status"] = "pending" if normalized == "create" else "dismissed"
    metadata["updated_at"] = utc_now()
    atomic_write_json(metadata_path, metadata)
    append_jsonl(
        _feedback_log_path(),
        {
            "timestamp": utc_now(),
            "candidate_name": candidate_name,
            "action": normalized,
            "source": "user_decision",
        },
    )


def should_synthesize_rules() -> bool:
    feedbacks = [item for item in _read_feedbacks() if item.get("source") != "auto_judge"]
    rules = _read_json(_rules_path())
    metadata = rules.get("metadata") if isinstance(rules, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    try:
        last_total = int(metadata.get("total_feedbacks_at_last_synthesis") or 0)
    except Exception:
        last_total = 0
    return len(feedbacks) - last_total >= 5


def synthesize_rules(feedbacks: list[dict[str, Any]]) -> dict[str, Any]:
    config = load_config()
    model = config.get("model", {}).get("synthesis_model", "sonnet")
    raw = _call_claude(
        FEEDBACK_PROMPT + "\n\nFeedback JSON:\n" + json.dumps(feedbacks, ensure_ascii=False, indent=2),
        config,
        model=model,
    )
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        parsed = {"rules": [], "metadata": {"summary": "No parseable rules returned."}}

    rules = parsed.get("rules")
    if isinstance(rules, dict):
        rules = [rules]
    if not isinstance(rules, list):
        rules = []

    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(
        {
            "last_synthesis_at": utc_now(),
            "total_feedbacks_at_last_synthesis": len(feedbacks),
        }
    )

    result = {"rules": rules, "metadata": metadata}
    atomic_write_json(_rules_path(), result)
    return result


def auto_judge(reflection: dict[str, Any], rules: dict[str, Any]) -> tuple[str, str]:
    config = load_config()
    model = config.get("model", {}).get("review_model", "haiku")
    raw = _call_claude(
        AUTO_JUDGE_PROMPT
        + "\n\nRules JSON:\n"
        + json.dumps(rules, ensure_ascii=False, indent=2)
        + "\n\nReflection JSON:\n"
        + json.dumps(reflection, ensure_ascii=False, indent=2),
        config,
        model=model,
    )
    parsed = _parse_json(raw)
    if not isinstance(parsed, dict):
        return "keep", "Auto-judge returned no parseable decision."
    try:
        action = _normalize_action(str(parsed.get("action") or "keep"))
    except ValueError:
        action = "keep"
    reason = str(parsed.get("reason") or "").strip() or "Auto-judged from feedback rules."
    return action, reason


def main() -> int:
    payload = read_stdin_json()
    action = str(payload.get("action") or "").strip()
    if action == "collect":
        collect_feedback(
            str(payload.get("reflection_id") or payload.get("reflectionId") or ""),
            str(payload.get("feedback_action") or payload.get("decision") or payload.get("feedback") or payload.get("action_value") or ""),
            str(payload.get("reason") or ""),
        )
        sys.stdout.write(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    if action == "decide_candidate":
        decide_candidate(
            str(payload.get("candidate_name") or payload.get("candidateName") or payload.get("name") or ""),
            str(payload.get("decision") or payload.get("candidate_action") or payload.get("candidateAction") or payload.get("action_value") or ""),
        )
        sys.stdout.write(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    if action == "synthesize":
        feedbacks = payload.get("feedbacks")
        if not isinstance(feedbacks, list):
            feedbacks = _read_feedbacks()
        sys.stdout.write(json.dumps(synthesize_rules(feedbacks), ensure_ascii=False))
        return 0
    if action == "auto_judge":
        reflection = payload.get("reflection")
        rules = payload.get("rules")
        if not isinstance(reflection, dict):
            reflection = {}
        if not isinstance(rules, dict):
            rules = _read_json(_rules_path())
        decision, reason = auto_judge(reflection, rules)
        sys.stdout.write(json.dumps({"action": decision, "reason": reason}, ensure_ascii=False))
        return 0
    raise ValueError(f"unknown feedback_collector action: {action}")


def _feedback_log_path():
    return data_root() / "reflections" / "feedback_log.jsonl"


def _rules_path():
    return data_root() / "reflections" / "rules.json"


def _normalize_action(action: str) -> str:
    normalized = str(action or "").strip().lower()
    if normalized not in {"keep", "dismiss"}:
        raise ValueError("action must be 'keep' or 'dismiss'")
    return normalized


def _read_feedbacks() -> list[dict[str, Any]]:
    path = _feedback_log_path()
    if not path.exists():
        return []
    feedbacks: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            feedbacks.append(parsed)
    return feedbacks


def _read_json(path) -> dict[str, Any]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        return {}
    return {}


def _increment_judged_count() -> None:
    path = data_root() / "reflections" / "index.json"
    index = _read_json(path)
    if not index:
        return
    try:
        index["judged_count"] = int(index.get("judged_count") or 0) + 1
    except Exception:
        index["judged_count"] = 1
    atomic_write_json(path, index)


def _clear_pending_feedback(reflection_id: str) -> None:
    if not reflection_id:
        return
    path = data_root() / "reflections" / "index.json"
    index = _read_json(path)
    if not index:
        return
    items = index.get("reflections") if isinstance(index.get("reflections"), dict) else index
    changed = False
    for item in items.values():
        if isinstance(item, dict) and str(item.get("reflection_id") or "") == reflection_id:
            if item.pop("needs_user_feedback", None) is not None:
                changed = True
    if changed:
        atomic_write_json(path, index)


def _reset_auto_judgment_counter() -> None:
    path = _rules_path()
    rules = _read_json(path)
    metadata = rules.get("metadata") if isinstance(rules, dict) else None
    if not isinstance(metadata, dict):
        return
    if metadata.get("auto_judgments_since_user_feedback") == 0:
        return
    metadata["auto_judgments_since_user_feedback"] = 0
    metadata["last_user_feedback_at"] = utc_now()
    atomic_write_json(path, rules)


def _call_claude(prompt: str, config: dict[str, Any], model: str | None = None) -> str:
    timeout = int(config.get("review_timeout_seconds", 300))
    completed = subprocess.run(
        ["claude", "-p", "--model", model or "haiku"],
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


def _parse_json(raw: str) -> Any:
    raw = (raw or "").strip()
    candidates = [raw, _extract_fenced_json(raw), _extract_first_json_block(raw)]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            repaired = candidate.replace("\ufeff", "")
            repaired = repaired.replace("“", '"').replace("”", '"')
            if repaired != candidate:
                try:
                    return json.loads(repaired)
                except Exception:
                    pass
    return None


def _extract_fenced_json(raw: str) -> str | None:
    import re

    match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_first_json_block(raw: str) -> str | None:
    start = raw.find("{")
    if start < 0:
        return None
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
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


if __name__ == "__main__":
    sys.exit(main())
