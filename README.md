# reflection-enhance

从每次失败中自动提取教训，让 Claude 越用越聪明。

`reflection-enhance` 是一个 Claude Code 插件。它通过 hooks 记录失败、纠错和长回合上下文，在会话结束后异步复盘，并在后续提示提交时注入高相关性的经验提醒。

## 前置条件

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 已安装
- Python 3.10+
- `python` 命令在 PATH 中可用

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/UIengF/reflection-enhance.git ~/.claude/plugins/local/reflection-enhance
```

Windows (PowerShell):

```powershell
git clone https://github.com/UIengF/reflection-enhance.git "$env:USERPROFILE\.claude\plugins\local\reflection-enhance"
```

### 2. 启用插件

在 `~/.claude/settings.json` 中添加：

```json
{
  "enabledPlugins": {
    "reflection-enhance@local": true
  }
}
```

如果文件已有其他内容，只在 `enabledPlugins` 对象中加入这一行，不要替换整个文件。

### 3. 注册 Hooks

在 `~/.claude/settings.json` 中添加 `hooks` 部分（与 `enabledPlugins` 同级）：

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/scripts/event_recorder.py\""
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/scripts/event_recorder.py\""
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/scripts/review_worker.py\"",
            "async": true
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python \"${CLAUDE_PLUGIN_ROOT}/scripts/inject_context.py\""
          }
        ]
      }
    ]
  }
}
```

如果已有 `hooks` 部分，将上述条目合并进去，不要覆盖已有的 hooks。

### 4. 验证安装

重启 Claude Code 会话后：

1. 输入 `/hooks` 查看已注册的 hooks，确认 4 个 hook 都在列表中
2. 进行一轮包含工具调用的对话
3. 检查数据目录是否生成了事件文件：

```bash
ls ~/.claude/plugin-data/reflection-enhance/sessions/
```

## 功能

| Hook | 触发时机 | 作用 |
|------|---------|------|
| `PostToolUse` | 工具调用成功后 | 记录事件（工具名、输入摘要、输出摘要、耗时） |
| `PostToolUseFailure` | 工具调用失败后 | 记录失败事件，自动脱敏敏感信息 |
| `Stop` | 每轮对话结束 | 异步复盘：提取反思 → 生成候选 skill |
| `UserPromptSubmit` | 用户发送消息时 | 注入历史反思提醒和候选 skill 决策提示 |

## 工作流程

```text
用户发送消息
  -> UserPromptSubmit hook
  -> inject_context.py 加载相关反思注入上下文
  -> Claude 执行任务（带反思提醒）
  -> PostToolUse / PostToolUseFailure hooks
  -> event_recorder.py 记录脱敏事件
  -> Stop hook
  -> review_worker.py (Haiku) 提取反思教训
  -> review_worker.py (Sonnet) 生成候选 skill
  -> 候选 skill 暂存为 staging，等待用户决策
  -> 下次会话时用户可回复 create:name 或 skip:name
```

## 配置

插件默认把运行数据写入：

```text
~/.claude/plugin-data/reflection-enhance
```

可通过环境变量 `CLAUDE_PLUGIN_DATA` 覆盖。

可选配置文件为数据目录下的 `config.json`：

```json
{
  "review_mode": "split",
  "trigger": {
    "min_tool_iterations": 5,
    "min_duration_seconds": 60
  },
  "model": {
    "review_model": "haiku",
    "synthesis_model": "sonnet"
  },
  "rolling_window": {
    "window_size": 5,
    "consecutive_failures_threshold": 3
  },
  "limits": {
    "max_transcript_chars": 120000,
    "max_injection_chars": 2000,
    "max_injection_items": 5
  },
  "redaction": {
    "enabled": true,
    "patterns": ["api_key", "token", "password"]
  }
}
```

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `review_mode` | 复盘模式：`split`（反思+skill分离）、`single`、`off` | `split` |
| `trigger.min_tool_iterations` | 触发复盘的最少工具调用次数 | `5` |
| `trigger.min_duration_seconds` | 触发复盘的最短会话时长（秒） | `60` |
| `model.review_model` | 反思使用的模型 | `haiku` |
| `model.synthesis_model` | skill 生成使用的模型 | `sonnet` |
| `rolling_window.window_size` | 滑动窗口大小 | `5` |
| `rolling_window.consecutive_failures_threshold` | 连续失败告警阈值 | `3` |
| `redaction.enabled` | 是否启用敏感信息脱敏 | `true` |

## 许可证

MIT License. See [LICENSE](LICENSE).
