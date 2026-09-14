# AiLock × ZCode 联动（一键部署）

在 ZCode 里干活时，锁屏实时显示：**你的提问 → AI 回复 → 工具调用进度**，
任务停止后自动触发 AiLock 的「任务完成时」动作（通知 / 关机 / 解锁…）。

纯 Python 标准库实现，无第三方依赖，Python 3.8+ 即可。

## 一键安装

```cmd
python install.py
```

它会做四件事（全部自动、可逆）：

1. 复制钩子脚本到 `%APPDATA%\AiLock\hooks\ailock_zcode_hook.py`
2. 备份并合并 `~\.zcode\cli\config.json` —— **只追加 AiLock 的 6 个 hook 事件，
   你已有的 plugins / MCP / 其他 hooks 原样保留**
3. 打开 AiLock 的「状态检测」开关（只动 `status.enabled` 一项）
4. 在临时目录跑一次冒烟测试，验证钩子链路

常用参数：

```cmd
python install.py --dry-run     # 只看会做什么，不改文件
python install.py --delay 0     # Stop 立即判定完成（默认 12 秒确认窗口）
python uninstall.py             # 干净卸载（同样只动自家条目）
```

## 装完必须做的两步

1. **ZCode 新建一个会话** —— hook 配置在会话启动时加载，旧会话不生效
2. **AiLock 正在运行就重启一次**（托盘右键退出再启动），它不热加载配置

## 锁屏上会看到什么

```
重构订单模块并跑通全部测试          ← 你的提问
AI: 已经重构完成，42 个测试全部通过   ← AI 回复（从会话 transcript 解析）
3. 执行 php artisan test --filter=Order   ← 最新工具调用
✓ 已完成                           ← Stop 确认后
```

内容显示在**屏幕右侧的会话卡**里 —— 同时开几个 ZCode 会话，就并排显示几张卡
（最多 4 张，正在跑的排在最上面）。中央的时钟和密码框不受影响。

## 多个会话同时跑怎么办

钩子自动用 ZCode 的 `session_id` 作会话标识，每个会话写自己的状态文件：

```
%APPDATA%\AiLock\status.d\zc-a1b2c3.json     ← 会话 A
%APPDATA%\AiLock\status.d\zc-9f3c1d.json     ← 会话 B
```

**完成动作以「最后一个收尾的会话」为准**：会话 A 先跑完，但会话 B 还在跑 ——
A 只是把卡片标成「已完成」，**不会触发关机**；等 B 也收尾了才触发。这样开着
三个会话改 bug，不会因为其中一个收工就把机器关了。

各会话的进度、日志行互不干扰；某个会话收尾满 10 分钟后它的文件和卡片一起消失。

## Stop 的「完成确认窗口」是什么

ZCode 的 `Stop` 事件在**每一轮对话结束**都会触发，不是整个会话结束。
如果一轮说完就标 done，多轮对话会反复触发「任务完成」动作。

所以默认行为是：Stop 后等 **12 秒**，期间你没有继续提问才判定完成。
环境变量 `AILOCK_DONE_DELAY`（秒）可调，`0` 表示立即判定。

## 故障排查

| 现象 | 检查 |
|---|---|
| 锁屏完全不更新 | `%APPDATA%\AiLock\config.json` 里 `status.enabled` 是否为 `true`；AiLock 是否重启过 |
| 状态文件更新了但锁屏没变 | AiLock 是否在运行；`status.path` 是否被改过 |
| 不触发完成动作 | 确认窗口 12 秒内是否又提问了（那会取消完成） |
| 看钩子日志 | ZCode 日志 `~\.zcode\cli\log\` 里搜 `[ailock-hook]` |

## 协议

钩子把状态写进状态文件（原子写），带 `session_id` 时写到会话目录：

| 情况 | 写入位置 |
|---|---|
| 有 `session_id`（正常情况） | `%APPDATA%\AiLock\status.d\<session_id>.json` |
| 没有 `session_id` | `%APPDATA%\AiLock\status.json`（旧的单文件行为） |

文件内容和旧协议完全一致，只是多一个 `sid` 字段（= 文件名去 `.json`）。
字段协议见仓库根 README 的「方式 3：直接写状态文件」——任何语言都能照此接入。
