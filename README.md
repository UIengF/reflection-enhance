# reflection-enhance

从每次失败中自动提取教训，让 Claude 越用越聪明。

`reflection-enhance` 是一个 Claude Code 插件。它通过 hooks 记录失败、纠错和长回合上下文，在会话结束后异步复盘，并在后续提示提交时注入高相关性的经验提醒。

## 功能

- `PostToolUse`: 记录工具调用结果、耗时、路径和输出摘要，用于后续复盘。
- `PostToolUseFailure`: 记录失败事件，并对命令、输出和错误信息做敏感信息脱敏。
- `Stop`: 在会话结束时异步分析事件和 transcript，沉淀反思规则或候选技能。
- `UserPromptSubmit`: 在新提示提交时读取历史反思，并注入与当前工作目录和上下文相关的提醒。

## 安装

1. 将仓库克隆到 Claude Code 本地插件目录：

   ```powershell
   git clone https://github.com/UIengF/reflection-enhance.git "$env:USERPROFILE\.claude\plugins\local\reflection-enhance"
   ```

2. 确认插件 manifest 存在：

   ```powershell
   Get-Content "$env:USERPROFILE\.claude\plugins\local\reflection-enhance\.claude-plugin\plugin.json"
   ```

3. 在 Claude Code 中启用本地插件后重启会话，使 hooks 生效。

## 配置

插件默认把运行数据写入：

```text
~/.claude/plugin-data/reflection-enhance
```

也可以通过环境变量覆盖：

```powershell
$env:CLAUDE_PLUGIN_DATA = "$env:USERPROFILE\.claude\plugin-data\reflection-enhance"
```

可选配置文件为数据目录下的 `config.json`。常用配置项包括：

- `review_mode`: 复盘模式，默认 `split`。
- `trigger`: 控制何时触发复盘，例如最少工具迭代次数和最短会话时长。
- `model`: 配置复盘与规则合成使用的模型别名。
- `rolling_window`: 控制连续失败检测窗口。
- `limits`: 控制 transcript、事件文件和注入内容长度。
- `redaction`: 控制敏感信息脱敏开关和匹配模式。
- `data_retention`: 控制事件和候选技能保留时间。

## 工作流程

```text
User prompt
  -> UserPromptSubmit hook
  -> inject_context.py loads relevant reflections
  -> Claude Code works with injected reminders
  -> PostToolUse / PostToolUseFailure hooks
  -> event_recorder.py appends sanitized session events
  -> Stop hook
  -> review_worker.py reviews transcript and events
  -> reflections, feedback rules, or candidate skills are stored
  -> later sessions reuse those lessons
```

## 许可证

MIT License. See [LICENSE](LICENSE).
