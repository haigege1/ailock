# AiLock × Codex 联动（一键部署）

Codex（codex-cli / Codex 桌面版）每轮跑完时，锁屏显示本轮结果；
空闲确认后自动触发 AiLock 的「任务完成时」动作（通知 / 关机 / 解锁…）。

纯 Python 标准库实现，无第三方依赖，Python 3.8+ 即可。

## 原理：notify 是单值，所以「一变二」中转

`~\.codex\config.toml` 顶部的 `notify = ["命令", "参数"]` 是**根级单值**，
只能填一个命令——本机通常已被 `codex-computer-use.exe` 占用，直接覆盖会
废掉 computer-use 插件。

所以安装器装的是**中转脚本**：Codex 把 JSON 交给中转脚本，中转脚本
先原样转发给原命令（插件功能不受影响），再把本轮结果写进 AiLock 状态：

```
Codex 每轮结束 ──► python ailock_codex_relay.py '<json>'
                      ├─ 1. 原样转发原 notify 命令（computer-use 照常）
                      └─ 2. 写 %APPDATA%\AiLock\status.d\codex.json
```

原 notify 命令保存在 sidecar（`%APPDATA%\AiLock\hooks\codex_relay.json`），
卸载时把 `notify` 原样还原。改写只动 notify 这一行，config.toml
其余内容（注释、缩进、表顺序）一律不碰，改前自动备份。

## 一键安装

```cmd
python install.py
```

它会做四件事（全部自动、可逆）：

1. 复制中转脚本到 `%APPDATA%\AiLock\hooks\ailock_codex_relay.py`
2. 备份并改写 `~\.codex\config.toml` 的 notify 行（原命令存 sidecar）
3. 打开 AiLock 的「状态检测」开关（只动 `status.enabled` 一项）
4. 在临时目录跑一次冒烟测试，验证中转链路

常用参数：

```cmd
python install.py --dry-run     # 只看会做什么，不改文件
python uninstall.py             # 还原原 notify 并删除脚本
```

## 装完必须做的两步

1. **重启 Codex** —— notify 配置在启动时加载
2. **AiLock 正在运行就重启一次**，它不热加载配置

## 锁屏上会看到什么

```
修登录态失效的 bug                ← 本轮输入的第一条消息
AI: 已修复并补了回归测试           ← last-assistant-message
✓ 第 2 轮结束，等待确认…           ← 每轮刷新
```

Codex 的 notify 只有「每轮结束」一种信号，没有逐工具事件，所以会话卡
不显示工具调用明细，进度按轮数缓慢推进（封顶 90%）。

## 完成判定

notify 同样是**每轮**触发，不是整个任务结束。与 ZCode/WorkBuddy 一样走
「完成确认窗口」：一轮结束后等 **12 秒**，没有下一轮才判 done
（环境变量 `AILOCK_DONE_DELAY` 可调，`0` 立即判定）。

## 已知边界

- 一台机器同时只认一个 Codex 会话（固定 sid=`codex`，多开互相刷新）
- config.toml 里没有根级 `notify` 行时安装器会拒绝并提示先加一行
  （TOML 的 notify 必须写在任何 `[table]` 之前）

## 故障排查

| 现象 | 检查 |
|---|---|
| 锁屏完全不更新 | `%APPDATA%\AiLock\config.json` 里 `status.enabled`；AiLock 是否重启过 |
| 每轮没反应 | config.toml 的 notify 是否指向中转脚本；Codex 是否重启过 |
| computer-use 失灵 | sidecar 里的 `original` 是否完整；`uninstall.py` 还原后检查 |
