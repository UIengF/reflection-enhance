from __future__ import annotations

import io
import json

import event_recorder
from common import DEFAULT_CONFIG, session_dir


def test_build_event_from_normal_payload():
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "session-1",
        "tool_name": "Bash",
        "duration_ms": 42,
        "tool_input": {"command": "pytest", "cwd": "C:/repo"},
        "tool_output": {"exit_code": 0, "stdout": "ok", "stderr": ""},
    }

    event = event_recorder.build_event(payload, DEFAULT_CONFIG)

    assert event["session_id"] == "session-1"
    assert event["hook"] == "PostToolUse"
    assert event["tool_name"] == "Bash"
    assert event["status"] == "success"
    assert event["duration_ms"] == 42
    assert event["input_summary"]["command"] == "pytest"
    assert event["input_summary"]["path_refs"] == ["C:/repo"]
    assert event["output_summary"]["exit_code"] == 0


def test_build_event_missing_fields_uses_defaults():
    event = event_recorder.build_event({}, DEFAULT_CONFIG)

    assert event["session_id"] == "unknown-session"
    assert event["hook"] == "PostToolUse"
    assert event["tool_name"] == "unknown"
    assert event["status"] == "success"
    assert event["input_summary"]["args_redacted"] == "{}"


def test_build_event_post_tool_use_failure():
    payload = {
        "hookEventName": "PostToolUseFailure",
        "sessionId": "s",
        "toolName": "Edit",
        "exception": {"type": "RuntimeError", "message": "failed", "trace": "stack"},
    }

    event = event_recorder.build_event(payload, DEFAULT_CONFIG)

    assert event["status"] == "failure"
    assert event["hook"] == "PostToolUseFailure"
    assert event["error_summary"]["type"] == "RuntimeError"
    assert event["error_summary"]["message"] == "failed"


def test_build_event_redacts_input_output_and_errors():
    payload = {
        "session_id": "s",
        "tool_input": {"command": "curl -H 'Authorization: Bearer abc123'", "token": "secret-token"},
        "tool_output": {"stdout": "password=super-secret", "stderr": "cookie: sid=abc"},
        "error": {"message": "api_key=hidden"},
    }

    event = event_recorder.build_event(payload, DEFAULT_CONFIG)
    rendered = str(event)

    assert "abc123" not in rendered
    assert "secret-token" not in rendered
    assert "super-secret" not in rendered
    assert "sid=abc" not in rendered
    assert "hidden" not in rendered


def test_rolling_window_consecutive_3_failures(isolated_data_root):
    _write_events(
        "rolling-s",
        [
            _event("Bash", "failure"),
            _event("Edit", "failure"),
            _event("Read", "failure"),
        ],
    )

    assert event_recorder._check_rolling_window("rolling-s", DEFAULT_CONFIG)


def test_rolling_window_high_failure_rate(isolated_data_root):
    _write_events(
        "rolling-s",
        [
            _event("Bash", "failure"),
            _event("Edit", "success"),
            _event("Read", "failure"),
            _event("Write", "failure"),
            _event("Test", "failure"),
        ],
    )

    assert event_recorder._check_rolling_window("rolling-s", DEFAULT_CONFIG)


def test_rolling_window_same_tool_2_failures(isolated_data_root):
    _write_events(
        "rolling-s",
        [
            _event("Read", "success"),
            _event("Bash", "failure"),
            _event("Bash", "failure"),
        ],
    )

    assert event_recorder._check_rolling_window("rolling-s", DEFAULT_CONFIG)


def test_rolling_window_success_no_trigger(isolated_data_root):
    _write_events(
        "rolling-s",
        [
            _event("Bash", "success"),
            _event("Edit", "success"),
            _event("Read", "success"),
            _event("Write", "success"),
            _event("Test", "success"),
        ],
    )

    assert event_recorder._check_rolling_window("rolling-s", DEFAULT_CONFIG) is None


def test_rolling_window_warn_once(isolated_data_root, monkeypatch, capsys):
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Bash", "error": {"message": "failed-1"}},
    )
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Edit", "error": {"message": "failed-2"}},
    )
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Read", "error": {"message": "failed-3"}},
    )
    first_output = capsys.readouterr().out

    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Write", "error": {"message": "failed-4"}},
    )
    second_output = capsys.readouterr().out

    assert first_output
    assert second_output == ""
    assert (session_dir("rolling-s") / ".window_warned").exists()


def test_rolling_window_output_format(isolated_data_root, monkeypatch, capsys):
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Bash", "error": {"message": "failed-1"}},
    )
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Edit", "error": {"message": "failed-2"}},
    )
    _run_main(
        monkeypatch,
        {"session_id": "rolling-s", "tool_name": "Read", "error": {"message": "failed-3"}},
    )

    output = json.loads(capsys.readouterr().out)

    assert output["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "additionalContext" in output["hookSpecificOutput"]
    assert "consecutive failures" in output["hookSpecificOutput"]["additionalContext"]


def _event(tool_name: str, status: str) -> dict[str, str]:
    return {"tool_name": tool_name, "status": status}


def _write_events(session_id: str, events: list[dict[str, str]]) -> None:
    path = session_dir(session_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _run_main(monkeypatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert event_recorder.main() == 0


def _write_reflections(root, reflections_dict):
    path = root / "reflections" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reflections_dict), encoding="utf-8")


def test_pending_reflection_injected_on_post_tool_use(isolated_data_root, monkeypatch, capsys):
    _write_reflections(
        isolated_data_root,
        {
            "reflections": {
                "fp1": {
                    "fingerprint": "fp1",
                    "reflection_id": "r-1",
                    "lesson": "Always verify tests pass",
                    "avoid_next_time": "Skipping test verification",
                    "confidence": 0.9,
                    "needs_user_feedback": True,
                }
            },
            "judged_count": 0,
        },
    )

    _run_main(monkeypatch, {"session_id": "test-s", "tool_name": "Bash"})

    output = json.loads(capsys.readouterr().out)
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert "Always verify tests pass" in ctx
    assert "r-1" in ctx
    assert "keep:<reflection_id>" in ctx


def test_pending_reflection_only_shown_once(isolated_data_root, monkeypatch, capsys):
    _write_reflections(
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
                }
            },
            "judged_count": 0,
        },
    )

    _run_main(monkeypatch, {"session_id": "dup-s", "tool_name": "Bash"})
    first_output = capsys.readouterr().out
    assert "Test lesson" in first_output

    _run_main(monkeypatch, {"session_id": "dup-s", "tool_name": "Edit"})
    second_output = capsys.readouterr().out
    assert "Test lesson" not in second_output


def test_no_pending_reflections_no_injection(isolated_data_root, monkeypatch, capsys):
    _write_reflections(
        isolated_data_root,
        {
            "reflections": {
                "fp1": {
                    "fingerprint": "fp1",
                    "lesson": "No feedback flag",
                    "avoid_next_time": "",
                    "confidence": 0.9,
                }
            },
            "judged_count": 5,
        },
    )

    _run_main(monkeypatch, {"session_id": "no-pending", "tool_name": "Bash"})
    output = capsys.readouterr().out
    assert output == ""


def test_staging_candidates_injected(isolated_data_root, monkeypatch, capsys):
    candidate_dir = isolated_data_root / "candidate-skills" / "verify"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "candidate.json").write_text(
        json.dumps({
            "name": "verify",
            "status": "staging",
            "confidence": 0.8,
            "failure_pattern": "Skipped test rerun",
        }),
        encoding="utf-8",
    )

    _run_main(monkeypatch, {"session_id": "cand-s", "tool_name": "Bash"})

    output = json.loads(capsys.readouterr().out)
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert "verify" in ctx
    assert "create:{name}" in ctx


def test_pending_reflection_injection_filters_by_cwd(isolated_data_root, monkeypatch, capsys):
    _write_reflections(
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
            "judged_count": 0,
        },
    )

    _run_main(monkeypatch, {"session_id": "cwd-s", "tool_name": "Bash", "tool_input": {"cwd": "C:/repo"}})

    output = json.loads(capsys.readouterr().out)
    ctx = output["hookSpecificOutput"]["additionalContext"]
    assert "Repo lesson" in ctx
    assert "Other lesson" not in ctx
