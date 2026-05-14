# Reflection Framework: Ours vs Hermes Agent

## Architecture Overview

| Dimension | Ours (reflection-enhance) | Hermes Agent |
|-----------|--------------------------|--------------|
| **Trigger** | Async at session end (Stop hook) | Real-time during session (agent self-judgment) |
| **Granularity** | Whole session → distilled lessons | Single task/interaction → direct skill creation |
| **Storage** | index.json (structured) + rules.json + feedback_log.jsonl | MEMORY.md + USER.md + skills/ directory |
| **Token budget** | 2000 chars injection, 5 reflections | ~1300 tokens (MEMORY.md 800 + USER.md 500) |
| **Cross-session search** | None (inject latest 5 only) | SQLite FTS5 full-text search + Gemini Flash summarization |

## Reflection Trigger Mechanism

| Aspect | Ours | Hermes |
|--------|------|--------|
| **Conditions** | Tool iterations >= 5 or duration >= 60s AND error/correction signal | 5+ tool calls, dead ends, user corrections, non-trivial workflows |
| **Judgment model** | Haiku (low-cost async review) | Agent itself (built-in system prompt rules) |
| **Filtering** | Multi-layer: environment failures, transient issues, non-reusable lessons | No explicit filtering, relies on agent judgment |
| **Signal source** | PostToolUse/PostToolUseFailure hook events + transcript keyword matching | Agent's real-time awareness during session |

**Assessment**: Our framework is more conservative (multi-layer filtering + signal validation). Hermes is more aggressive (agent self-judges, no explicit filtering). Conservative = less noise but may miss valuable lessons.

## Skill Generation

| Aspect | Ours | Hermes |
|--------|------|--------|
| **Generation** | Reflection → candidate skill → human/auto review → staging → pending | Agent calls `skill_manage create` directly |
| **Candidate improvement** | dedupe_key match → seen_count++ + evidence merge + SKILL.md rewrite | `skill_manage patch` for incremental updates |
| **Lifecycle** | staging → pending (human review) → install. Candidates aggregate before approval | Agent creates/modifies directly, no staging |
| **Quality gates** | Confidence threshold, dedupe_key, evidence_refs required, staging state machine | No explicit quality gates |
| **Discovery** | No progressive disclosure (candidates need manual approval) | `skills_list()` L0 names/descriptions → L1 full content |
| **Runtime improvement** | Candidate aggregation across sessions (evidence accumulation) | `skill_manage patch` during active use |

**Key distinction**: Our candidate skills improve through cross-session aggregation (same failure pattern recurring → evidence accumulates, seen_count increases, confidence rises). Installed skills are not auto-modified. Hermes' skills improve through in-session patching.

## Memory / Lesson Persistence

| Aspect | Ours | Hermes |
|--------|------|--------|
| **Storage model** | Reflection index.json (unlimited capacity) + inject latest 5 | MEMORY.md (2200 chars) + USER.md (1375 chars), hard limits |
| **Injection** | UserPromptSubmit hook auto-injects | System prompt frozen snapshot (changes take effect next session) |
| **Management** | Auto + human review for first 5 | Agent self-manages (add/replace/remove), consolidate at >80% |
| **Security** | Redaction patterns (token/password/cookie/private_key) | Intercepts prompt injection, credential exfiltration, SSH backdoors |
| **User profile** | None | USER.md stores user preferences, role, skill level |
| **Dedup** | Fingerprint-based dedup | Exact duplicate rejection |

**Assessment**: Our framework has unlimited capacity but lacks security scanning and user profiling. Hermes' hard limits force higher information density but may lose historical lessons.

## Human Review

| Aspect | Ours | Hermes |
|--------|------|--------|
| **Mechanism** | First 5 reflections need keep/dismiss, then auto-review | No explicit human review |
| **Feedback loop** | User feedback → rules.json → influences future auto-review decisions | None |
| **State tracking** | judged_count, needs_user_feedback, auto_judgments_since_user_feedback | None |

**Assessment**: This is our unique advantage. Hermes relies entirely on agent self-judgment without human calibration. Our feedback → rules → auto-judge chain forms a tunable quality control system.

## Summary

| Advantage | Ours | Hermes |
|-----------|------|--------|
| **Reflection quality control** | Multi-layer filtering + human review + auto-review rules | - |
| **Skill runtime improvement** | - | Patch mechanism for incremental updates |
| **Cross-session retrieval** | - | FTS5 full-text search + LLM summarization |
| **User profiling** | - | USER.md dedicated modeling |
| **Token efficiency** | - | Progressive disclosure + frozen snapshot |
| **Reflection capacity** | Unlimited (index.json) | Hard limit (2200 chars) |
| **Tunability** | feedback → rules → auto-judge chain | - |
| **Security** | - | Built-in prompt injection interception |
| **Candidate aggregation** | Evidence accumulation across sessions drives confidence | - |

## What We Can Learn from Hermes

1. **Skill runtime improvement**: Add `patch` ability to candidate skills so in-use skills can incrementally update
2. **Progressive disclosure**: Show reflection titles/fingerprints first, expand full content on demand to save tokens
3. **Security scanning**: Check for prompt injection and sensitive data leaks before storing reflections
4. **User profiling**: Add a USER.md-style lightweight user model for personalized reflections
5. **Cross-session search**: Consider adding FTS index to events.jsonl for "did we discuss X" queries

## What Hermes Can Learn from Us

1. **Human review gates**: Calibration loop where human feedback shapes auto-review rules
2. **Multi-layer filtering**: Conservative approach reduces noise in reflection storage
3. **Candidate lifecycle**: Staging → approval prevents premature skill installation
4. **Evidence aggregation**: Cross-session dedup_key matching strengthens candidate confidence over time
5. **Two-phase async review**: Reflection (Haiku) and skill synthesis (Sonnet) can use different models
