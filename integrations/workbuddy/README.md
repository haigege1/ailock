# AiLock × WorkBuddy 联动（一键部署）

在 WorkBuddy（腾讯 AI 办公工作台）里干活时，锁屏实时显示：
**你的提问 → 工具调用进度 → AI 回复**，
任务停止后自动触发 AiLock 的「任务完成时」动作（通知 / 关机 / 解锁…）。

纯 Python 标准库实现，无第三方依赖，Python 3.8+ 即可。

## 原理

WorkBuddy 桌面版的 hooks 配置在 `~\.workbuddy\settings.json` 的 `hooks`
字段，格式与 Claude Code 同构（实测触发：SessionStart / UserPromptSubmit /
PreToolUse / PostToolUse / PermissionRequest / Stop，payload 自带
`session_id` 与 `prompt` / `tool_input` / `last_assistant_message`）。

**Windows 上钩子经 Git Bash 执行**（不支持 cmd / PowerShell），
所以注册的命令是 bash 兼容写法（正斜杠 + 引号）：

```
"C:/.../python.exe" "C:/.../AiLock/hooks/ailock_workbuddy_hook.py"
```

## 一键安装

```cmd
python install.py
```

它会做四件事（全部自动、可逆）：

1. 复制钩子脚本到 `%APPDATA%\AiLock\hooks\ailock_workbuddy_hook.py`
2. 备份并合并 `~\.workbuddy\settings.json` —— **只追加 AiLock 的 6 个
   hook 事件，你已有的其他 hooks / 设置原样保留**
3. 打开 AiLock 的「状态检测」开关（只动 `status.enabled` 一项）
4. 在临时目录跑一次冒烟测试，验证钩子链路

常用参数：

```cmd
python install.py --dry-run     # 只看会做什么，不改文件
python uninstall.py             # 干净卸载（同样只动自家条目）
```

## 装完必须做的两步

1. **WorkBuddy 新建一个会话** —— hook 配置在会话启动时加载
2. **AiLock 正在运行就重启一次**，它不热加载配置

## 与 ZCode 版的差异

- 不解析 transcript：WorkBuddy 事件 payload 直接带所需字段，更稳
- AI 回复一行在 **Stop 时**取 `last_assistant_message` 更新
- 完成确认窗口与 ZCode 相同：Stop 每轮触发，默认等 12 秒无新事件才判
  done（环境变量 `AILOCK_DONE_DELAY` 可调，`0` 立即判定）

## 故障排查

| 现象 | 检查 |
|---|---|
| 锁屏完全不更新 | `%APPDATA%\AiLock\config.json` 里 `status.enabled` 是否为 `true`；AiLock 是否重启过 |
| 会话没动静 | 是否新开了会话；WorkBuddy 版本 hooks 是否可用（设置里的 hooks 面板可审查） |
| 看钩子日志 | 钩子诊断写 stderr；可在 settings.json 临时把命令换成 `... python.exe ... 2>>日志路径` 收集 |
