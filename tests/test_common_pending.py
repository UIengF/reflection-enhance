from __future__ import annotations

import json

from common import (
    format_pending_reflection_context,
    load_pending_reflections,
    load_staging_candidates,
)


def _write_index(root, data):
    path = root / "reflections" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_pending_reflections_returns_only_needs_feedback(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "fp1": {
                    "fingerprint": "fp1",
                    "lesson": "Pending lesson",
                    "confidence": 0.9,
                    "needs_user_feedback": True,
                },
                "fp2": {
                    "fingerprint": "fp2",
                    "lesson": "No flag",
                    "confidence": 0.9,
                },
            },
        },
    )

    result = load_pending_reflections()
    assert len(result) == 1
    assert result[0]["lesson"] == "Pending lesson"


def test_load_pending_reflections_empty_without_index(isolated_data_root):
    assert load_pending_reflections() == []


def test_load_pending_reflections_sorted_by_confidence(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "low": {"fingerprint": "low", "lesson": "Low", "confidence": 0.3, "needs_user_feedback": True},
                "high": {"fingerprint": "high", "lesson": "High", "confidence": 0.9, "needs_user_feedback": True},
            },
        },
    )

    result = load_pending_reflections()
    assert result[0]["lesson"] == "High"
    assert result[1]["lesson"] == "Low"


def test_load_pending_reflections_ignores_malformed_confidence(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "bad": {"fingerprint": "bad", "lesson": "Bad confidence", "confidence": "not-a-number", "needs_user_feedback": True},
                "good": {"fingerprint": "good", "lesson": "Good confidence", "confidence": 0.9, "needs_user_feedback": True},
            },
        },
    )

    result = load_pending_reflections()
    assert [item["lesson"] for item in result] == ["Good confidence", "Bad confidence"]


def test_load_pending_reflections_dedupes_fingerprint(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "one": {"fingerprint": "same", "lesson": "First", "confidence": 0.8, "needs_user_feedback": True},
                "two": {"fingerprint": "same", "lesson": "Second", "confidence": 0.9, "needs_user_feedback": True},
            },
        },
    )

    result = load_pending_reflections()
    assert len(result) == 1
    assert result[0]["lesson"] == "First"


def test_load_staging_candidates(isolated_data_root):
    candidate_dir = isolated_data_root / "candidate-skills" / "test-skill"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.json").write_text(
        json.dumps({"name": "test-skill", "status": "staging", "confidence": 0.7}),
        encoding="utf-8",
    )

    result = load_staging_candidates()
    assert len(result) == 1
    assert result[0]["name"] == "test-skill"


def test_load_staging_candidates_skips_non_staging(isolated_data_root):
    candidate_dir = isolated_data_root / "candidate-skills" / "installed"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.json").write_text(
        json.dumps({"name": "installed", "status": "installed"}),
        encoding="utf-8",
    )

    assert load_staging_candidates() == []


def test_format_pending_reflection_context(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "fp1": {
                    "fingerprint": "fp1",
                    "reflection_id": "r-1",
                    "lesson": "Test lesson",
                    "avoid_next_time": "Test avoid",
                    "confidence": 0.9,
                    "needs_user_feedback": True,
                },
            },
        },
    )

    context = format_pending_reflection_context()
    assert "Test lesson" in context
    assert "r-1" in context
    assert "keep:<reflection_id>" in context


def test_format_pending_reflection_context_filters_by_cwd(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "reflections": {
                "repo": {
                    "fingerprint": "repo",
                    "reflection_id": "r-repo",
                    "session_ids": ["C:/repo/session"],
                    "lesson": "Repo lesson",
                    "confidence": 0.8,
                    "needs_user_feedback": True,
                },
                "other": {
                    "fingerprint": "other",
                    "reflection_id": "r-other",
                    "session_ids": ["C:/other/session"],
                    "lesson": "Other lesson",
                    "confidence": 0.9,
                    "needs_user_feedback": True,
                },
            },
        },
    )

    context = format_pending_reflection_context(cwd="C:/repo")
    assert "Repo lesson" in context
    assert "Other lesson" not in context


def test_format_pending_reflection_context_empty(isolated_data_root):
    assert format_pending_reflection_context() == ""
