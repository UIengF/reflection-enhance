from __future__ import annotations

import json

import feedback_collector
import inject_context
import review_worker
from common import DEFAULT_CONFIG, append_jsonl, atomic_write_json, session_dir


def _config(**overrides):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def test_reflection_lifecycle_feedback_and_candidate_dedupe(isolated_data_root, monkeypatch):
    config = _config(
        review_mode="split",
        trigger={"min_tool_iterations": 3, "min_duration_seconds": 0, "require_error_or_correction": True},
        limits={"max_candidate_confidence_threshold": 0.5, "max_injection_items": 5, "max_injection_chars": 4000},
    )
    session_id = "C:/repo/session-e2e"
    transcript = (
        "I assumed the fix was done too early. Actually, that was wrong: "
        "the failing test must be rerun after the tool error."
    )
    transcript_path = isolated_data_root / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    for event in [
        {"timestamp": "2026-05-14T00:00:00Z", "tool": "Read", "status": "success"},
        {"timestamp": "2026-05-14T00:00:02Z", "tool": "Bash", "status": "failure", "error": "pytest failed"},
        {"timestamp": "2026-05-14T00:00:04Z", "tool": "Bash", "status": "success"},
    ]:
        append_jsonl(session_dir(session_id) / "events.jsonl", event)

    index_path = isolated_data_root / "reflections" / "index.json"
    atomic_write_json(index_path, {"judged_count": 4, "reflections": {}})
    atomic_write_json(
        isolated_data_root / "reflections" / "rules.json",
        {"rules": [], "metadata": {"auto_judgments_since_user_feedback": 9}},
    )

    reflection_response = {
        "should_record": True,
        "trigger_signals": ["tool_failure", "user_correction"],
        "filter_matches": [],
        "lesson": "After a tool failure and user correction, rerun the focused verification before finishing.",
        "avoid_next_time": "Avoid treating a fix as complete until the failing command has been rerun successfully.",
        "evidence_refs": [
            {"type": "event", "ref": "event:2", "summary": "pytest command failed"},
            {"type": "transcript", "ref": "tail", "summary": "user corrected the assumption"},
        ],
        "confidence": 0.88,
        "fingerprint_source": "rerun focused verification after failure and correction",
    }
    candidate_response = {
        "candidates": [
            {
                "name": "focused-verification",
                "dedupe_key": "focused-verification",
                "failure_pattern": "A failed command is followed by a fix without rerunning focused verification",
                "reuse_scope": "Coding tasks where a tool failure or user correction changes the solution",
                "confidence": 0.82,
                "proposed_action": "create_skill",
                "do_not_create_reason": None,
                "evidence_refs": [{"type": "event", "ref": "event:2", "summary": "pytest failed"}],
                "skill_markdown": "# Focused verification\n\nRerun the focused failing command before finishing.",
            }
        ]
    }
    llm_outputs = [json.dumps(reflection_response), json.dumps(candidate_response)]

    def fake_call_claude(prompt, cfg, model=None):
        assert cfg == config
        assert model in {"haiku", "sonnet"}
        return llm_outputs.pop(0)

    monkeypatch.setattr(review_worker, "_call_claude", fake_call_claude)
    monkeypatch.setattr(review_worker, "read_stdin_json", lambda: {"session_id": session_id, "transcript_path": str(transcript_path)})
    monkeypatch.setattr(review_worker, "load_config", lambda: config)

    assert review_worker.main() == 0
    assert llm_outputs == []

    index_doc, reflections = review_worker._load_index_parts(index_path)
    assert index_doc["judged_count"] == 4
    assert len(reflections) == 1
    stored_fingerprint, stored_reflection = next(iter(reflections.items()))
    reflection_id = stored_reflection["reflection_id"]
    assert stored_reflection["lesson"] == reflection_response["lesson"]
    assert stored_reflection["needs_user_feedback"] is True

    context = inject_context.build_context(config, "C:/repo")
    assert f"[{reflection_id}] Lesson: {reflection_response['lesson']}" in context
    assert 'keep:<reflection_id>' in context
    assert "你的反馈将帮助系统学习什么值得记住" in context

    feedback_collector.collect_feedback(reflection_id, "keep", "durable verification habit")

    index_after_feedback = json.loads(index_path.read_text(encoding="utf-8"))
    reflection_after_feedback = index_after_feedback["reflections"][stored_fingerprint]
    assert index_after_feedback["judged_count"] == 5
    assert "needs_user_feedback" not in reflection_after_feedback

    first_candidate_path = isolated_data_root / "candidate-skills" / "focused-verification" / "candidate.json"
    first_candidate = json.loads(first_candidate_path.read_text(encoding="utf-8"))
    assert first_candidate["seen_count"] == 1

    second_candidate_response = json.loads(json.dumps(candidate_response))
    second_candidate_response["candidates"][0]["evidence_refs"] = [
        {"type": "transcript", "ref": "tail", "summary": "user correction repeated"}
    ]
    duplicate_reflection = dict(stored_reflection)
    duplicate_reflection["reflection_id"] = "reflection-second"
    duplicate_reflection["lesson"] = "Repeat focused verification after similar failures."

    assert review_worker._store_candidates(second_candidate_response, duplicate_reflection, config) == 1

    deduped_path = review_worker._find_candidate_by_dedupe("focused-verification")
    assert deduped_path == first_candidate_path.parent
    deduped_candidate = json.loads(first_candidate_path.read_text(encoding="utf-8"))
    assert deduped_candidate["seen_count"] == 2
    assert deduped_candidate["source_reflection_ids"] == [reflection_id, "reflection-second"]
    evidence_refs = {item["ref"] for item in deduped_candidate["evidence_refs"]}
    assert {"event:2", "tail", f"reflection:{reflection_id}", "reflection:reflection-second"} <= evidence_refs

    feedback_collector.decide_candidate("focused-verification", "create")

    decided_candidate = json.loads(first_candidate_path.read_text(encoding="utf-8"))
    assert decided_candidate["status"] == "pending"


def test_review_worker_skips_unparseable_reflection_without_writing_index(isolated_data_root, monkeypatch):
    config = _config(review_mode="single", trigger={"min_tool_iterations": 1, "min_duration_seconds": 0})
    session_id = "C:/repo/session-bad-json"
    append_jsonl(
        session_dir(session_id) / "events.jsonl",
        {"timestamp": "2026-05-14T00:00:00Z", "tool": "Bash", "status": "failure"},
    )

    monkeypatch.setattr(review_worker, "_call_claude", lambda *args, **kwargs: "not json")
    monkeypatch.setattr(review_worker, "read_stdin_json", lambda: {"session_id": session_id, "transcript": "Actually, wrong."})
    monkeypatch.setattr(review_worker, "load_config", lambda: config)

    assert review_worker.main() == 0
    assert not (isolated_data_root / "reflections" / "index.json").exists()
    assert list((isolated_data_root / "diagnostics").glob("reflection-C-repo-session-bad-json-*.raw.txt"))
