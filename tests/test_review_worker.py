from __future__ import annotations

import json
import subprocess

import review_worker
from common import DEFAULT_CONFIG


def _config(**overrides):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def _events(count: int, status: str = "success") -> str:
    return "\n".join(json.dumps({"status": status, "timestamp": f"2026-05-14T00:00:0{i}Z"}) for i in range(count))


def test_trigger_decision_review_mode_off():
    decision = review_worker._trigger_decision({}, _events(10, "failure"), "", _config(review_mode="off"))
    assert decision == {"should_review": False, "reason": "review_mode_off"}


def test_trigger_decision_split_requires_signal_by_default():
    decision = review_worker._trigger_decision({}, _events(5), "", _config(review_mode="split"))
    assert decision["should_review"] is False
    assert decision["reason"] == "no_error_or_correction_signal"


def test_trigger_decision_error_and_tool_threshold_reviews():
    decision = review_worker._trigger_decision({}, _events(5, "failure"), "", _config(review_mode="single"))
    assert decision["should_review"] is True
    assert decision["review_mode"] == "single"
    assert decision["has_error"] is True
    assert decision["tool_iterations"] == 5


def test_trigger_decision_correction_and_duration_reviews():
    decision = review_worker._trigger_decision(
        {"duration_seconds": 60},
        _events(1),
        "Actually, that was wrong.",
        _config(trigger={"min_tool_iterations": 5, "min_duration_seconds": 60, "require_error_or_correction": True}),
    )
    assert decision["should_review"] is True
    assert decision["has_correction"] is True


def test_trigger_decision_without_required_signal_uses_complexity_only():
    decision = review_worker._trigger_decision(
        {"duration_seconds": 10},
        _events(5),
        "",
        _config(trigger={"min_tool_iterations": 5, "min_duration_seconds": 60, "require_error_or_correction": False}),
    )
    assert decision["should_review"] is True


def test_read_transcript_preserves_tail_correction():
    transcript = ("start " * 50) + ("middle " * 200) + "actually, 不对"

    result = review_worker._read_transcript({"transcript": transcript}, max_chars=120)

    assert "...[truncated]..." in result
    assert "actually" in result
    assert "不对" in result


def test_parse_llm_json_five_fallback_paths(isolated_data_root):
    assert review_worker._parse_llm_json('{"should_record": true}', "reflection", "s") == {"should_record": True}
    assert review_worker._parse_llm_json('```json\n{"a": 1}\n```', "reflection", "s") == {"a": 1}
    assert review_worker._parse_llm_json('prefix {"b": [1, 2]} suffix', "reflection", "s") == {"b": [1, 2]}
    assert review_worker._parse_llm_json('{"c": 3,}', "reflection", "s") == {"c": 3}
    assert review_worker._parse_llm_json("lesson: Use tmp dirs\nconfidence: 0.7", "reflection", "s")["lesson"] == "Use tmp dirs"
    assert review_worker._parse_llm_json("not json", "reflection", "s") is None
    assert list((isolated_data_root / "diagnostics").glob("reflection-s-*.raw.txt"))


def test_validate_reflection_empty_lesson():
    warnings = review_worker._validate_reflection({"lesson": "", "avoid_next_time": "Run focused tests."})

    assert any("lesson" in warning for warning in warnings)


def test_validate_reflection_high_confidence_no_evidence():
    warnings = review_worker._validate_reflection(
        {
            "lesson": "Run focused tests after changing behavior.",
            "avoid_next_time": "Avoid finishing without verification.",
            "confidence": 0.8,
            "evidence_refs": [],
        }
    )

    assert any("evidence_refs" in warning for warning in warnings)


def test_validate_reflection_duplicate_content():
    warnings = review_worker._validate_reflection(
        {
            "lesson": "Run focused tests after changing behavior.",
            "avoid_next_time": "Run focused tests after changing behavior.",
            "confidence": 0.5,
            "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "failure"}],
        }
    )

    assert any("identical" in warning for warning in warnings)


def test_validate_reflection_valid_passes():
    warnings = review_worker._validate_reflection(
        {
            "lesson": "Run focused tests after changing behavior.",
            "avoid_next_time": "Avoid finishing before verification completes.",
            "confidence": 0.8,
            "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "failure"}],
        }
    )

    assert warnings == []


def test_store_reflection_deduplicates_by_fingerprint(isolated_data_root):
    reflection = {
        "should_record": True,
        "lesson": "Check failures.",
        "avoid_next_time": "Run focused tests.",
        "fingerprint_source": "same source",
        "confidence": 0.8,
        "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "failure"}],
    }
    decision = {"reason": "eligible", "has_error": True, "has_correction": False, "tool_iterations": 5, "duration_seconds": 1}

    first = review_worker._store_reflection(reflection, "session-a", decision)
    second = review_worker._store_reflection(reflection, "session-b", decision)

    assert first["fingerprint"] == second["fingerprint"]
    assert second["session_ids"] == ["session-a", "session-b"]
    assert second["seen_count"] == 2

    index = json.loads((isolated_data_root / "reflections" / "index.json").read_text(encoding="utf-8"))
    reflections = index.get("reflections", index)
    assert len(reflections) == 1
    stored = next(iter(reflections.values()))
    assert stored["seen_count"] == 2


def test_store_reflection_preserves_index_metadata(isolated_data_root):
    index_path = isolated_data_root / "reflections" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "judged_count": 4,
                "reflections": {
                    "existing": {
                        "fingerprint": "existing",
                        "reflection_id": "old",
                        "lesson": "Existing lesson.",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    reflection = {
        "should_record": True,
        "lesson": "Check failures.",
        "avoid_next_time": "Run focused tests.",
        "fingerprint_source": "new source",
        "confidence": 0.8,
        "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "failure"}],
    }
    decision = {"reason": "eligible", "has_error": True, "has_correction": False, "tool_iterations": 5, "duration_seconds": 1}

    review_worker._store_reflection(reflection, "session-a", decision)

    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["judged_count"] == 4
    assert "existing" in index["reflections"]
    assert len(index["reflections"]) == 2


def test_store_candidates_deduplicates_by_key(isolated_data_root):
    reflection = {"reflection_id": "r1", "lesson": "Verify", "avoid_next_time": "Run tests", "fingerprint": "fp"}
    config = _config(limits={"max_candidate_confidence_threshold": 0.5})
    skill_result = {
        "candidates": [
            {
                "name": "verify-tests",
                "dedupe_key": "verify-tests",
                "failure_pattern": "Skipped verification",
                "reuse_scope": "Coding tasks",
                "confidence": 0.9,
                "proposed_action": "create_skill",
                "evidence_refs": [{"type": "reflection", "ref": "reflection:r1", "summary": "Verify"}],
                "skill_markdown": "# Verify tests",
            }
        ]
    }

    assert review_worker._store_candidates(skill_result, reflection, config) == 1
    reflection["reflection_id"] = "r2"
    assert review_worker._store_candidates(skill_result, reflection, config) == 1

    candidates = list((isolated_data_root / "candidate-skills").glob("*/candidate.json"))
    assert len(candidates) == 1
    metadata = json.loads(candidates[0].read_text(encoding="utf-8"))
    assert metadata["dedupe_key"] == "verify-tests"
    assert metadata["seen_count"] == 2
    assert metadata["source_reflection_ids"] == ["r1", "r2"]


def test_store_candidates_saves_with_staging_status(isolated_data_root):
    reflection = {"reflection_id": "r1", "lesson": "Verify", "avoid_next_time": "Run tests", "fingerprint": "fp"}
    config = _config(limits={"max_candidate_confidence_threshold": 0.5})
    skill_result = {
        "candidates": [
            {
                "name": "verify-tests",
                "dedupe_key": "verify-tests",
                "failure_pattern": "Skipped verification",
                "reuse_scope": "Coding tasks",
                "confidence": 0.9,
                "proposed_action": "create_skill",
                "evidence_refs": [{"type": "reflection", "ref": "reflection:r1", "summary": "Verify"}],
                "skill_markdown": "# Verify tests",
            }
        ]
    }

    assert review_worker._store_candidates(skill_result, reflection, config) == 1

    metadata_path = isolated_data_root / "candidate-skills" / "verify-tests" / "candidate.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "staging"


def test_auto_judge_reflection_writes_feedback(isolated_data_root, monkeypatch):
    rules_path = isolated_data_root / "reflections" / "rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(json.dumps({"rules": [], "metadata": {}}), encoding="utf-8")
    reflection = {
        "reflection_id": "r1",
        "fingerprint": "fp",
        "lesson": "Run focused tests.",
        "avoid_next_time": "Avoid unverified fixes.",
    }

    class Completed:
        stdout = json.dumps({"action": "keep", "reason": "matches keep rule"})
        stderr = ""
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Completed())

    review_worker._auto_judge_reflection(reflection)

    rows = [
        json.loads(line)
        for line in (isolated_data_root / "reflections" / "feedback_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["reflection_id"] == "r1"
    assert rows[0]["action"] == "keep"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    assert rules["metadata"]["auto_judgments_since_user_feedback"] == 1
