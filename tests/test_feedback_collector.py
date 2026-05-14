from __future__ import annotations

import json
import subprocess

import feedback_collector
from common import atomic_write_json


class Completed:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_collect_feedback_writes_log(isolated_data_root):
    feedback_collector.collect_feedback("reflection-1", "keep", "durable lesson")

    path = isolated_data_root / "reflections" / "feedback_log.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert rows[0]["reflection_id"] == "reflection-1"
    assert rows[0]["action"] == "keep"
    assert rows[0]["reason"] == "durable lesson"
    assert rows[0]["timestamp"]


def test_should_synthesize_rules_at_threshold(isolated_data_root):
    for index in range(5):
        feedback_collector.collect_feedback(f"r-{index}", "keep", "reason")

    assert feedback_collector.should_synthesize_rules() is True


def test_should_not_synthesize_below_threshold(isolated_data_root):
    for index in range(4):
        feedback_collector.collect_feedback(f"r-{index}", "dismiss", "reason")

    assert feedback_collector.should_synthesize_rules() is False


def test_should_synthesize_counts_since_last_synthesis(isolated_data_root):
    rules_path = isolated_data_root / "reflections" / "rules.json"
    atomic_write_json(
        rules_path,
        {"rules": [], "metadata": {"last_synthesis_at": "2026-05-14T00:00:00Z", "total_feedbacks_at_last_synthesis": 2}},
    )
    for index in range(6):
        feedback_collector.collect_feedback(f"r-{index}", "keep", "reason")

    assert feedback_collector.should_synthesize_rules() is False

    feedback_collector.collect_feedback("r-6", "keep", "reason")
    assert feedback_collector.should_synthesize_rules() is True


def test_synthesize_rules_creates_file(isolated_data_root, monkeypatch):
    def fake_run(args, **kwargs):
        assert args == ["claude", "-p", "--model", "sonnet"]
        return Completed(
            json.dumps(
                {
                    "rules": [{"action": "keep", "when": "has reusable verification lesson", "reason": "generalizes"}],
                    "metadata": {"summary": "Prefer durable lessons."},
                }
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    feedbacks = [{"reflection_id": "r1", "action": "keep", "reason": "useful"}]

    result = feedback_collector.synthesize_rules(feedbacks)

    rules_path = isolated_data_root / "reflections" / "rules.json"
    stored = json.loads(rules_path.read_text(encoding="utf-8"))
    assert stored == result
    assert stored["rules"][0]["action"] == "keep"
    assert stored["metadata"]["total_feedbacks_at_last_synthesis"] == 1


def test_decide_candidate_create(isolated_data_root):
    candidate_dir = isolated_data_root / "candidate-skills" / "verify"
    candidate_dir.mkdir(parents=True)
    candidate_path = candidate_dir / "candidate.json"
    candidate_path.write_text(json.dumps({"name": "verify", "status": "staging"}), encoding="utf-8")

    feedback_collector.decide_candidate("verify", "create")

    metadata = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "pending"
    rows = [
        json.loads(line)
        for line in (isolated_data_root / "reflections" / "feedback_log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["candidate_name"] == "verify"
    assert rows[0]["action"] == "create"
    assert rows[0]["source"] == "user_decision"


def test_decide_candidate_skip(isolated_data_root):
    candidate_dir = isolated_data_root / "candidate-skills" / "verify"
    candidate_dir.mkdir(parents=True)
    candidate_path = candidate_dir / "candidate.json"
    candidate_path.write_text(json.dumps({"name": "verify", "status": "staging"}), encoding="utf-8")

    feedback_collector.decide_candidate("verify", "skip")

    metadata = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "dismissed"
