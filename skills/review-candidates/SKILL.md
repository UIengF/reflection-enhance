---
name: review-candidates
description: Review pending reflection-enhance candidate skills before installing, skipping, merging, or requesting rewrites.
---

# Review Candidates

Use this skill when the user asks to review reflection-enhance candidate skills or runs `/review-candidates`.

## Workflow

1. Scan `${CLAUDE_PLUGIN_DATA}/candidate-skills` (or `~/.claude/plugin-data/reflection-enhance/candidate-skills` if the env var is not set).
2. Filter to candidates with `status == "pending"`.
3. If no pending candidates, inform the user and stop.
4. For each pending candidate, present:
   - Name and description
   - Confidence score
   - Failure pattern and reuse scope
   - Evidence refs (transcript/event/reflection references)
   - SKILL.md preview (first 30 lines)
5. Ask the user to choose an action per candidate.

## Actions

| Action | Effect |
|--------|--------|
| **install** | Copy candidate directory to `~/.claude/skills/<name>/`. Update status to `installed`. |
| **skip** | Update status to `skipped`. Keep files in place. |
| **merge** | Show list of existing skills. User picks target. Copy SKILL.md content as new section in target. Update status to `merged`. |
| **rewrite** | Update status to `needs_rewrite`. User provides feedback; append to `candidate.json.notes`. |
| **delete** | Remove candidate directory entirely. |

## Batch Operations

- **install all** — Install all pending candidates with confidence >= 0.7.
- **skip all** — Skip all pending candidates.
- **review one by one** — Default mode, iterate through each candidate.

## Diff Preview

Before installing, show the SKILL.md content that will be written to `~/.claude/skills/`. Ask for confirmation.

## Post-Review

After processing all candidates:
1. Move processed candidates to `${CLAUDE_PLUGIN_DATA}/candidate-skills/.reviewed/<name>-<date>/`.
2. Report summary: installed N, skipped N, merged N, deleted N.

## Review Criteria

- Evidence should refer to concrete transcript, event, or reflection refs.
- The failure pattern should describe a reusable class of mistakes, not a one-off environment issue.
- The reuse scope should be broad enough to justify a skill.
- Confidence below the configured threshold should default to skip or needs_rewrite.
- Reject candidates that are session-specific, environment-only, or negative tool conclusions.

## Status Updates

When the user confirms an action, update the candidate's `candidate.json` status to one of:
- `installed`
- `skipped`
- `merged`
- `needs_rewrite`

Keep candidate files in place unless the user explicitly asks to delete them.
