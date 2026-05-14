from __future__ import annotations

import json

import inject_context
from common import DEFAULT_CONFIG


def _config(max_items=5, max_chars=2000):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["limits"]["max_injection_items"] = max_items
    config["limits"]["max_injection_chars"] = max_chars
    return config


def _write_index(root, items):
    path = root / "reflections" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")


def test_build_context_returns_empty_without_index(isolated_data_root):
    assert inject_context.build_context(_config(), "C:/repo") == ""


def test_build_context_prioritizes_cwd_matches(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "fp1": {
                "fingerprint": "fp1",
                "session_ids": ["C:/repo/session"],
                "lesson": "Repo lesson",
                "avoid_next_time": "Use repo path",
                "confidence": 0.2,
                "updated_at": "2026-05-14T00:00:00Z",
            },
            "fp2": {
                "fingerprint": "fp2",
                "session_ids": ["other"],
                "lesson": "Other lesson",
                "avoid_next_time": "Other",
                "confidence": 0.9,
                "updated_at": "2026-05-14T00:00:00Z",
            },
        },
    )

    context = inject_context.build_context(_config(max_items=2), "C:/repo")

    assert "Repo lesson" in context
    assert "Other lesson" in context


def test_build_context_sorts_by_confidence_then_recency(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "old": {
                "fingerprint": "old",
                "lesson": "Older same confidence",
                "avoid_next_time": "",
                "confidence": 0.8,
                "updated_at": "2026-05-13T00:00:00Z",
            },
            "new": {
                "fingerprint": "new",
                "lesson": "Newer same confidence",
                "avoid_next_time": "",
                "confidence": 0.8,
                "updated_at": "2026-05-14T00:00:00Z",
            },
            "high": {
                "fingerprint": "high",
                "lesson": "Highest confidence",
                "avoid_next_time": "",
                "confidence": 0.9,
                "updated_at": "2026-05-12T00:00:00Z",
            },
        },
    )

    context = inject_context.build_context(_config(max_items=3), "")

    assert context.index("Highest confidence") < context.index("Newer same confidence")
    assert context.index("Newer same confidence") < context.index("Older same confidence")


def test_build_context_fallback_uses_high_confidence_old_items(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "low": {"fingerprint": "low", "lesson": "Low", "confidence": 0.5, "updated_at": "2020-01-01T00:00:00Z"},
            "high": {"fingerprint": "high", "lesson": "High", "confidence": 0.7, "updated_at": "2020-01-01T00:00:00Z"},
        },
    )

    context = inject_context.build_context(_config(), "")

    assert "High" in context
    assert "Low" not in context


def test_build_context_respects_max_items_and_max_chars(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            str(i): {
                "fingerprint": str(i),
                "lesson": f"Lesson {i} " + ("x" * 50),
                "avoid_next_time": "Avoid",
                "confidence": 1 - i / 10,
                "updated_at": "2026-05-14T00:00:00Z",
            }
            for i in range(5)
        },
    )

    context = inject_context.build_context(_config(max_items=2, max_chars=170), "")

    assert "Lesson 0" in context
    assert "Lesson 1" in context
    assert "Lesson 2" not in context
    assert len(context) <= 184
    assert context.endswith("...[truncated]")

def test_build_context_includes_staging_candidates(isolated_data_root):
    candidate_dir = isolated_data_root / "candidate-skills" / "verify"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.json").write_text(
        json.dumps(
            {
                "name": "verify",
                "status": "staging",
                "confidence": 0.8,
                "failure_pattern": "Skipped focused test rerun",
                "reuse_scope": "Coding tasks with test failures",
            }
        ),
        encoding="utf-8",
    )

    context = inject_context.build_context(_config(), "")

    assert "候选 skill 待创建决策" in context
    assert "verify" in context
    assert "confidence: 0.8" in context
    assert "失败模式: Skipped focused test rerun" in context
    assert "复用范围: Coding tasks with test failures" in context
    assert 'create:{name}' in context
    assert 'skip:{name}' in context
    assert context.count('create:{name}') == 1


def test_build_context_cold_start_prompt(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "judged_count": 4,
            "reflections": {
                "fp": {
                    "fingerprint": "fp",
                    "reflection_id": "reflection-1",
                    "lesson": "Keep reusable lessons.",
                    "avoid_next_time": "Avoid one-off facts.",
                    "confidence": 0.9,
                    "updated_at": "2026-05-14T00:00:00Z",
                }
            },
        },
    )

    context = inject_context.build_context(_config(), "")

    assert "你的反馈将帮助系统学习什么值得记住" in context
    assert "keep:<reflection_id>" in context


def test_build_context_no_prompt_after_judged(isolated_data_root):
    _write_index(
        isolated_data_root,
        {
            "judged_count": 5,
            "reflections": {
                "fp": {
                    "fingerprint": "fp",
                    "reflection_id": "reflection-1",
                    "lesson": "Keep reusable lessons.",
                    "avoid_next_time": "Avoid one-off facts.",
                    "confidence": 0.9,
                    "updated_at": "2026-05-14T00:00:00Z",
                }
            },
        },
    )

    context = inject_context.build_context(_config(), "")

    assert "Keep reusable lessons." in context
    assert "你的反馈将帮助系统学习什么值得记住" not in context
