from __future__ import annotations

import json

import common


def test_safe_name_boundaries():
    assert common.safe_name("") == "unknown"
    assert common.safe_name("   ") == "unknown"
    assert common.safe_name("...---") == "unknown"
    assert common.safe_name("abc XYZ/?:*中文") == "abc-XYZ"
    assert common.safe_name("a" * 200) == "a" * 120


def test_fingerprint_is_stable_and_short():
    assert common.fingerprint("same") == common.fingerprint("same")
    assert common.fingerprint("same") != common.fingerprint("different")
    assert len(common.fingerprint("same")) == 16


def test_redact_text_redacts_common_secret_shapes():
    text = (
        "api_key=abc123 token: secret password='pw' "
        "Authorization: Bearer token123\nCookie: sid=abc\n"
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    )
    redacted = common.redact_text(text)
    assert "abc123" not in redacted
    assert "secret" not in redacted.lower()
    assert "pw" not in redacted
    assert "sid=abc" not in redacted
    assert "[REDACTED" in redacted


def test_truncate_text_handles_none_objects_and_long_text():
    assert common.truncate_text(None) is None
    assert common.truncate_text({"a": 1}, 100) == '{"a": 1}'
    assert common.truncate_text("abcdef", 3) == "abc...[truncated]"


def test_truncate_transcript_short_text_unchanged():
    assert common.truncate_transcript("short text", 100) == "short text"


def test_truncate_transcript_head_tail_preservation():
    text = "0123456789" * 20

    truncated = common.truncate_transcript(text, 10)

    assert truncated == text[:3] + "...[truncated]..." + text[-7:]


def test_append_jsonl_writes_sorted_json_lines(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    common.append_jsonl(path, {"b": 2, "a": 1})
    common.append_jsonl(path, {"c": 3})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1, "b": 2}', '{"c": 3}']


def test_atomic_write_json_replaces_existing_file(tmp_path):
    path = tmp_path / "state" / "index.json"
    common.atomic_write_json(path, {"old": True})
    common.atomic_write_json(path, {"new": {"value": 1}})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": {"value": 1}}
    assert not path.with_suffix(".json.tmp").exists()
