# Claude Code Reflection-Enhance Plugin PRD

## 1. 产品概述

### Problem Statement
Claude Code 在执行复杂开发任务时，会遇到工具失败、错误假设、验证遗漏、用户纠正、重复踩坑等问题。这些问题通常只存在于单轮 transcript 中，任务结束后不会被系统性沉淀，导致同类错误在后续会话中重复发生。

当前缺口包括：
- 工具调用事实缺少结构化记录，难以复盘失败路径。
- 反思依赖人工总结，缺少自动触发和稳定落盘机制。
- 可复用工作流不能从错误中系统提取为候选 skill。
- 已有反思不能在下一轮任务开始前低成本注入上下文。
- 自动生成 skill 如果直接安装，存在污染用户技能库和放大错误结论的风险。

### Target Users
- 使用 Claude Code 进行长期软件开发、调试、代码审查、文档编写的个人开发者。
- 维护 Claude Code 插件、skills、hooks 工作流的高级用户。
- 希望把失败经验沉淀为可复用流程的团队或多代理协作用户。

### Value Proposition
该插件通过事件采集、异步反思、候选 skill 提取、下一轮上下文注入和人工审核闭环，把一次性错误转化为可复用经验，同时避免自动修改 conversation history 或未经确认地安装生成内容。

核心价值：
- 降低重复错误率。
- 提升长任务后的复盘质量。
- 将稳定经验沉淀为候选 skill。
- 保留用户对最终知识库的审核权。
- 使用 JSONL 事实层增强 transcript 复盘可靠性。

## 2. 功能需求

### P0 必须实现

#### P0.1 实时事件采集
插件必须通过 `PostToolUse` 和 `PostToolUseFailure` hook 实时记录工具事件到当前 session 的 `events.jsonl`。

要求：
- 每个事件一行 JSON。
- 仅记录白名单字段。
- 对命令、路径、错误信息、工具参数进行基础脱敏。
- 写入失败不得阻塞 Claude Code 主流程。
- 支持 Windows 路径和 PowerShell 环境。

#### P0.2 Stop Hook 异步反思
插件必须通过 `Stop` hook 以 `async: true` 异步触发 `review_worker.py`。

要求：
- 只在满足触发门槛时运行：出现错误、用户纠正、长任务、明显验证遗漏或重复失败信号。
- 使用 transcript 作为主证据，`events.jsonl` 作为事实层补充。
- 调用 `claude -p` 生成结构化反思。
- 将反思追加或合并到 `reflections/reflections.md`。
- 将去重索引写入 `reflections/index.json`。

#### P0.3 工作流候选提取
反思完成后，插件必须用第二个 `claude -p` 调用提取候选 skill。

要求：
- 默认 review 模式为 `split`，即反思和 skill 提取使用两步调用。
- 支持配置为 `single` 或 `off`。
- 候选 skill 写入 `candidate-skills/<name>/`。
- 候选必须包含 `candidate.json` 和 `SKILL.md`。
- 不得自动安装候选 skill。

#### P0.4 上下文注入
插件必须通过 `UserPromptSubmit` hook 注入反思摘要到下一轮对话上下文。

要求：
- 注入内容应简短、相关、可操作。
- 不改写历史 conversation。
- 已加载 skill 本轮不会自动重读，因此注入必须面向下一轮用户请求。
- 注入失败时静默降级，不影响用户提交。

#### P0.5 候选人工审核
插件必须提供 `review-candidates` skill 或等效命令，让用户手动审核候选。

要求：
- 展示候选名称、触发证据、失败模式、复用范围、置信度和建议操作。
- 支持安装、跳过、合并、要求重写候选。
- 用户确认前不得写入正式 skills 目录。

#### P0.6 去重机制
插件必须对反思和候选 skill 进行去重。

要求：
- 反思 fingerprint 使用 `lesson + avoid_next_time`。
- 候选 skill 使用 `dedupe_key`。
- 索引保存在 `reflections/index.json`。
- 重复项应更新已有记录的证据或计数，而不是创建重复文件。

### P1 应该实现

#### P1.1 配置文件
提供插件配置项，文件位置：`${CLAUDE_PLUGIN_DATA}/config.json`。

配置 schema：

```json
{
  "review_mode": "split",
  "trigger": {
    "min_tool_iterations": 5,
    "min_duration_seconds": 60,
    "require_error_or_correction": true
  },
  "limits": {
    "max_transcript_chars": 120000,
    "max_events_chars": 80000,
    "max_injection_chars": 2000,
    "max_injection_items": 5,
    "max_candidate_confidence_threshold": 0.5,
    "max_event_file_bytes": 1048576
  },
  "redaction": {
    "enabled": true,
    "patterns": ["api_key", "token", "password", "authorization", "cookie", "private_key"]
  },
  "data_retention": {
    "session_events_days": 7,
    "candidate_pending_days": 30
  }
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `review_mode` | string | `"split"` | `split`（两步）/ `single`（一步）/ `off`（禁用） |
| `trigger.min_tool_iterations` | int | `5` | 触发 review 的最少 tool iteration 数 |
| `trigger.min_duration_seconds` | int | `60` | 触发 review 的最少任务耗时 |
| `trigger.require_error_or_correction` | bool | `true` | 是否要求有错误或纠正信号才触发 |
| `limits.max_transcript_chars` | int | `120000` | transcript 截断长度 |
| `limits.max_events_chars` | int | `80000` | events JSONL 截断长度 |
| `limits.max_injection_chars` | int | `2000` | 注入上下文最大字符数 |
| `limits.max_injection_items` | int | `5` | 注入反思条目最大数量 |
| `limits.max_candidate_confidence_threshold` | float | `0.5` | 低于此置信度不生成 candidate |
| `limits.max_event_file_bytes` | int | `1048576` | 单个 session events 文件上限（1MB） |
| `redaction.enabled` | bool | `true` | 是否启用脱敏 |
| `redaction.patterns` | list | `[...]` | 脱敏关键词列表 |
| `data_retention.session_events_days` | int | `7` | session events 保留天数 |
| `data_retention.candidate_pending_days` | int | `30` | 候选 skill 待审核保留天数 |

#### P1.2 证据引用
反思和候选 skill 必须引用 evidence refs，例如 transcript 片段编号、事件序号、工具名和时间戳。

#### P1.3 候选更新策略
当新证据匹配已有候选时，优先更新已有候选；其次添加 reference；最后才创建新的 class-level skill。

#### P1.4 容错 JSON 解析
`claude -p` 输出 JSON 不稳定时，系统应支持从 Markdown fenced block、前后缀文本、部分字段缺失中恢复。

#### P1.5 数据保留策略
支持限制每个 session 的事件文件大小、候选数量和 reflection 历史长度，避免长期运行后数据无限增长。

### P2 可以实现

#### P2.1 候选评分界面增强
提供更友好的候选列表、diff 预览、批量操作和排序。

#### P2.2 跨 Session 聚合
支持跨 session 聚合相同 fingerprint 的反思，识别高频错误模式。

#### P2.3 自动建议修改已有 Skill
当候选与已有 skill 高度相关时，生成 patch 建议而不是新 skill。

#### P2.4 可观测性
提供简单统计：触发次数、跳过原因、候选生成数、安装数、去重命中数。

## 3. 非功能需求

### 性能
- `PostToolUse` 和 `PostToolUseFailure` hook 单次开销目标小于 50ms。
- Stop hook 必须异步执行，不阻塞 Claude Code 结束当前轮。
- 单次 review 默认限制 transcript 和 events 输入大小，避免超长上下文导致高延迟和高成本。
- 文件写入采用 append JSONL，避免每次重写大文件。

### 安全
- 事件记录必须进行字段白名单过滤。
- 默认脱敏 API key、token、password、authorization header、cookie、私钥块、本地敏感路径片段。
- 不记录完整环境变量。
- 不自动安装或启用生成的 skill。
- 不执行 candidate skill 中的脚本或命令。
- 反思和候选内容必须保存在插件数据目录，避免污染项目仓库。

### 可维护性
- 公共路径、配置、脱敏、JSON 解析逻辑集中在 `scripts/common.py`。
- hook 入口脚本保持薄封装，复杂逻辑放入可测试函数。
- 数据 schema 必须版本化。
- Prompt 文本应集中定义，便于迭代和测试。
- 所有写文件操作应使用原子写或临时文件替换，避免并发 Stop hook 造成损坏。

## 4. 用户故事

1. 作为开发者，我希望 Claude Code 在工具失败后自动记录事实，这样任务结束时能准确复盘失败原因。
2. 作为开发者，我希望 Claude Code 在我纠正它之后记住这类错误，下次遇到相似任务时先提醒自己。
3. 作为插件维护者，我希望候选 skill 先进入待审核目录，而不是自动安装，以免低质量经验污染正式技能库。
4. 作为重度用户，我希望普通成功任务不会触发昂贵反思，以控制延迟和 token 成本。
5. 作为 Windows 用户，我希望插件路径、脚本和 hook 在 PowerShell 环境下稳定工作。
6. 作为团队用户，我希望每条反思都有证据引用，方便判断它是否值得沉淀为流程。
7. 作为用户，我希望下一轮任务开始时看到精简反思摘要，而不是大量历史日志。

## 5. 接口设计

### Hook 接口

#### PostToolUse / PostToolUseFailure
入口：`scripts/event_recorder.py`

输入：Claude Code hook payload。

输出：追加到：
`${CLAUDE_PLUGIN_DATA}/sessions/<session_id>/events.jsonl`

行为：
- 解析 payload。
- 提取 session、tool、timestamp、status、duration、input 摘要、output 摘要、error 摘要。
- 脱敏。
- JSONL append。

#### Stop
入口：`scripts/review_worker.py`

配置：`async: true`

行为：
- 读取 transcript 和本 session events。
- 判断是否满足触发门槛。
- 运行 reflection prompt。
- 运行 skill prompt。
- 合并 index 和文件。
- 记录跳过原因或失败原因。

#### UserPromptSubmit
入口：`scripts/inject_context.py`

行为：
- 读取 `reflections/index.json` 和 `reflections/reflections.md`。
- 匹配算法（按优先级）：
  1. 按 cwd 匹配：筛选 `session_ids` 中与当前 cwd 相同的反思条目。
  2. 按 recency 匹配：取最近 7 天内的反思条目。
  3. 合并去重后，按 `confidence` 降序排列，取前 5 条。
  4. 如果当前 cwd 下无匹配，fallback 到全局最近 3 条高置信度反思。
- 构造短上下文块（总长度不超过 2000 字符）。
- 注入到当前提交的额外上下文中。
- 不修改历史 conversation。

### 数据格式

#### events.jsonl
每行一个事件对象，schema 见第 6 节。

#### reflections.md
Markdown 追加式日志，按日期和 fingerprint 分组。

建议格式：

```markdown
## 2026-05-14 - <fingerprint>

- Lesson: ...
- Avoid next time: ...
- Evidence: event:42, transcript:turn-7
- Confidence: 0.82
```

#### index.json
用于去重、检索和注入。

#### candidate.json
候选 skill 元数据和审核状态。

### Prompt 规范

#### REFLECTION_PROMPT
必须覆盖 6 类触发信号：
- 工具失败。
- 用户纠正。
- 错误假设。
- 工具顺序错误。
- 遗漏验证。
- 重复踩坑。

必须覆盖 4 类过滤条件：
- 环境错误。
- 临时问题。
- 负面工具结论。
- 一次性问题。

输出字段必须包括：
- `should_record`
- `trigger_signals`
- `filter_matches`
- `lesson`
- `avoid_next_time`
- `evidence_refs`
- `confidence`
- `fingerprint_source`

#### SKILL_PROMPT
必须遵循优先级链：
1. 更新已有候选。
2. 添加 reference。
3. 创建新的 class-level skill。
4. 明确不创建并给出 `do_not_create_reason`。

质量门槛字段：
- `evidence_refs`
- `failure_pattern`
- `reuse_scope`
- `confidence`
- `dedupe_key`
- `do_not_create_reason`

## 6. 数据模型

### Event Schema

```json
{
  "schema_version": "1.0",
  "event_id": "uuid",
  "session_id": "string",
  "timestamp": "ISO-8601",
  "hook": "PostToolUse | PostToolUseFailure",
  "tool_name": "string",
  "status": "success | failure",
  "duration_ms": 1234,
  "input_summary": {
    "command": "string|null",
    "path_refs": ["string"],
    "args_redacted": "object|string|null"
  },
  "output_summary": {
    "exit_code": 0,
    "stdout_excerpt": "string|null",
    "stderr_excerpt": "string|null",
    "result_excerpt": "string|null"
  },
  "error_summary": {
    "type": "string|null",
    "message": "string|null",
    "trace_excerpt": "string|null"
  },
  "redactions_applied": ["secret", "path", "token"]
}
```

### Reflection Schema

```json
{
  "schema_version": "1.0",
  "reflection_id": "uuid",
  "fingerprint": "sha256",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "session_ids": ["string"],
  "should_record": true,
  "trigger_signals": ["tool_failure", "user_correction"],
  "filter_matches": [],
  "lesson": "string",
  "avoid_next_time": "string",
  "evidence_refs": [
    {
      "type": "event | transcript",
      "ref": "event:42",
      "summary": "string"
    }
  ],
  "confidence": 0.8,
  "seen_count": 1
}
```

### Candidate Schema

```json
{
  "schema_version": "1.0",
  "candidate_id": "uuid",
  "name": "string",
  "dedupe_key": "sha256-or-stable-string",
  "status": "pending | installed | skipped | merged | needs_rewrite",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "source_reflection_ids": ["uuid"],
  "evidence_refs": [
    {
      "type": "event | transcript | reflection",
      "ref": "string",
      "summary": "string"
    }
  ],
  "failure_pattern": "string",
  "reuse_scope": "string",
  "confidence": 0.75,
  "proposed_action": "create_skill | update_candidate | add_reference | do_not_create",
  "do_not_create_reason": "string|null",
  "skill_path": "candidate-skills/<name>/SKILL.md",
  "references_path": "candidate-skills/<name>/references"
}
```

## 7. 错误处理与降级策略

### Hook Payload 不完整
- 缺少 session id 时使用 fallback session 名称或跳过写入。
- 缺少工具输出时写入可用字段。
- payload 解析失败时记录最小错误日志，不抛出到 Claude Code 主流程。

### 文件写入失败
- event append 失败时静默降级并写 debug 日志。
- index 写入使用临时文件加原子替换。
- candidate 写入失败不得影响 reflections 写入。

### `claude -p` 调用失败
- reflection 调用失败：记录 review failure，不生成候选。
- skill 调用失败：保留 reflection，跳过候选生成。
- 超时：终止子进程并记录 skip reason。

### JSON 输出不稳定
解析顺序：
1. 直接 JSON parse。
2. 提取 Markdown fenced JSON。
3. 提取首个 `{...}` 或 `[...]` 块。
4. 尝试字段级修复。
5. 仍失败则保存 raw output 到诊断文件并跳过该结果。

### 触发门槛不满足
- 普通成功任务跳过 review。
- 跳过原因应可追踪，例如 `no_error_signal`、`short_successful_task`、`low_confidence`。

### 数据损坏
- `index.json` 损坏时备份为 `.corrupt.<timestamp>`，从 `reflections.md` 和候选目录尽力重建。
- 单个 candidate 损坏时跳过该候选，不影响其他候选审核。

## 8. 验收标准

### P0 Acceptance Criteria
- 触发一次成功工具调用后，`events.jsonl` 新增一行 `status=success` 的事件。
- 触发一次失败工具调用后，`events.jsonl` 新增一行 `status=failure` 的事件，且错误信息经过脱敏。
- 包含工具失败或用户纠正的 session 结束后，Stop hook 异步启动 review，不阻塞主流程。
- review 成功后，`reflections/reflections.md` 和 `reflections/index.json` 被创建或更新。
- 当反思满足 skill 门槛时，`candidate-skills/<name>/candidate.json` 和 `SKILL.md` 被创建。
- 默认情况下，候选 skill 不会自动安装到正式 skills 目录。
- 下一轮 `UserPromptSubmit` 能注入不超过配置上限的反思摘要。
- 相同 `lesson + avoid_next_time` 不会生成重复 reflection 条目。

### P1 Acceptance Criteria
- 配置 `review_mode=off` 时 Stop hook 不调用 `claude -p`。
- 配置 `review_mode=single` 时最多执行一次 `claude -p`。
- `claude -p` 输出带 Markdown 包裹时仍能解析。
- 新证据匹配已有 candidate 时更新其 evidence，而不是创建重复 candidate。
- 事件、反思、候选 schema 均包含 `schema_version`。

### P2 Acceptance Criteria
- 用户可以查看候选排序和批量状态变更。
- 可输出跨 session 高频错误模式统计。
- 对已有 skill 的修改以 patch 建议形式呈现。

## 9. 里程碑与排期

### M1: 插件骨架与事件采集（1-2 天）
- 创建 plugin manifest 和 hooks 配置。
- 实现 `common.py` 路径、配置、脱敏工具。
- 实现 `event_recorder.py`。
- 手动验证成功和失败工具事件写入 JSONL。

### M2: 异步反思闭环（2-3 天）
- 实现 Stop hook 触发门槛判断。
- 实现 transcript + events 输入构造。
- 实现 reflection prompt 和容错 JSON 解析。
- 写入 `reflections.md` 和 `index.json`。

### M3: 候选 Skill 生成（2-3 天）
- 实现 split 模式第二次 `claude -p` 调用。
- 实现 candidate schema 写入。
- 实现 dedupe 和候选更新策略。
- 生成 `candidate-skills/<name>/SKILL.md`。

### M4: 上下文注入与审核流程（1-2 天）
- 实现 `inject_context.py`。
- 实现 `skills/review-candidates/SKILL.md`。
- 支持候选查看、安装、跳过、合并、重写状态。

### M5: 稳定性与验收（2 天）
- 增加 Windows PowerShell 验证。
- 测试 JSON 解析降级。
- 测试 index 损坏恢复。
- 编写使用说明和验收记录。

## 10. 风险与缓解

### 风险：反思质量不稳定
缓解：使用明确 schema、证据引用、置信度阈值和过滤条件；低置信度只记录诊断，不生成候选。

### 风险：`claude -p` JSON 输出不稳定
缓解：实现多阶段解析和 raw output 保存；prompt 中要求只输出 JSON，但实现不依赖完美遵守。

### 风险：泄露敏感信息
缓解：字段白名单、默认脱敏、禁止完整环境变量记录、候选人工审核、诊断文件同样脱敏。

### 风险：事件记录影响主流程性能
缓解：hook 入口只做轻量提取和 append；失败静默降级；Stop hook 执行重任务且异步。

### 风险：生成低质量或过窄 skill
缓解：SKILL_PROMPT 要求 class-level reuse scope、evidence refs、confidence、do_not_create_reason；默认写入 candidate-skills，人工确认后安装。

### 风险：重复候选堆积
缓解：fingerprint 和 dedupe_key 双层去重；新证据优先更新已有候选或添加 reference。

### 风险：Hook 不能改写 conversation history
缓解：只在 `UserPromptSubmit` 注入下一轮简短上下文；不依赖修改历史消息。

### 风险：已加载 skill 本轮不会自动重读
缓解：审核安装后的 skill 明确从下一轮生效；当前轮通过 prompt 注入反思摘要提供轻量补偿。

### 风险：Windows 路径和并发写入问题
缓解：统一使用 `pathlib` 和 UTF-8；原子写 index；append JSONL；避免 shell-specific 路径拼接。
