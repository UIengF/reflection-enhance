from __future__ import annotations

import json
import subprocess

import event_recorder
import inject_context
import review_worker
from common import DEFAULT_CONFIG, append_jsonl, session_dir


class Completed:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_full_event_review_injection_flow(isolated_data_root, monkeypatch):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["trigger"]["min_tool_iterations"] = 1
    config["trigger"]["min_duration_seconds"] = 0
    config["review_mode"] = "split"

    event = event_recorder.build_event(
        {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "C:/repo/session-1",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_output": {"exit_code": 1, "stderr": "failed"},
            "error": {"message": "test failed"},
        },
        config,
    )
    append_jsonl(session_dir(event["session_id"]) / "events.jsonl", event)

    transcript = isolated_data_root / "transcript.txt"
    transcript.write_text("The test failed; next time verify focused tests.", encoding="utf-8")

    calls = []

    def fake_run(args, **kwargs):
        calls.append({"args": args, "input": kwargs["input"]})
        if len(calls) == 1:
            assert args == ["claude", "-p", "--model", "haiku"]
            return Completed(
                json.dumps(
                    {
                        "should_record": True,
                        "trigger_signals": ["tool_failure"],
                        "filter_matches": [],
                        "lesson": "Run focused tests after failures.",
                        "avoid_next_time": "Do not finish before rerunning the failing test.",
                        "evidence_refs": [{"type": "event", "ref": "event:1", "summary": "pytest failed"}],
                        "confidence": 0.9,
                        "fingerprint_source": "focused tests after failures",
                    }
                )
            )
        assert args == ["claude", "-p", "--model", "sonnet"]
        return Completed(
            json.dumps(
                {
                    "candidates": [
                        {
                            "name": "focused-test-verification",
                            "dedupe_key": "focused-test-verification",
                            "failure_pattern": "Failure fixed without rerunning focused tests",
                            "reuse_scope": "Coding tasks with failing tests",
                            "confidence": 0.8,
                            "proposed_action": "create_skill",
                            "do_not_create_reason": None,
                            "evidence_refs": [{"type": "reflection", "ref": "reflection:r", "summary": "test"}],
                            "skill_markdown": "# Focused test verification",
                        }
                    ]
                }
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(review_worker, "read_stdin_json", lambda: {"session_id": event["session_id"], "transcript_path": str(transcript)})
    monkeypatch.setattr(review_worker, "load_config", lambda: config)

    assert review_worker.main() == 0

    context = inject_context.build_context(config, "C:/repo")
    assert "Run focused tests after failures." in context
    assert "focused-test-verification" in context
    assert len(calls) == 2
