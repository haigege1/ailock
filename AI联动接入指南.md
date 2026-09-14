# AiLock × AI 工具接入指南

> 本机实测版 · 2026-09-03 · 针对 Claude Code 2.1.71 与 Codex CLI 0.148.0

---

## 0. 先搞清楚「AI 联动」到底是什么

设置页那一页叫「AI 联动」，但它**不连接任何 AI 的 API**：不调 Claude 的接口、不读聊天记录、不做任何语义判断。

它是一条**单向管道**，只做三件事：

| 环节 | 谁负责 | 说明 |
|---|---|---|
| 写状态 | **外部（你 / AI 工具 / 脚本）** | 往 `%APPDATA%\AiLock\status.json` 写 JSON；多个任务并行时改写 `status.d\<会话名>.json`（见 §1.6） |
| 读状态 | AiLock | 每 2 秒轮询一次，把 `task` / `progress` / `lines` 画到锁屏**右侧的会话卡**上 |
| 执行动作 | AiLock | 只在 `state` **跃迁**到 `done` / `failed` 时触发一次；多会话时以**最后一个收尾的会话**为准（见 §1.6） |

所以答案有两层：

- **能联动什么 AI** —— 理论上任何能写文件或执行命令的东西（Claude Code、Codex、训练脚本、甚至批处理）。它跟"AI"没关系，是个通用任务状态协议。
- **为什么你感觉没作用** —— 因为**写状态那一端默认是空的**。AiLock 只负责读，没人往里写，你设的"任务完成时关机"就永远等不到信号。这是设计如此，不是坏了。

---

## 1. 你机器上的实际情况

> 本节已修正：早前判断"status.json 从未生成"是错的 —— 当时 Shell 里 `$APPDATA` 未展开，导致读到了错误路径。

| 项目 | 实测结果 |
|---|---|
| AiLock.exe | 正在运行（3 个进程：主程序 + 看门狗），配置目录 `%APPDATA%\AiLock\` 已就绪 |
| `status.json` | **存在且有内容**（是 `--run` 测试写出的 failed 记录） |
| **`status.enabled`** | **`false` —— 这才是"完全没反应"的直接原因** |
| `stale_minutes` | `0`（失联判定关闭，原来 `on_stale_action=notify` 等于永不触发） |
| `claude` | 2.1.71，`C:\nvm4w\nodejs\claude` |
| `codex` | 0.148.0，`C:\nvm4w\nodejs\codex` |

**结论**：数据链路其实只差两处开关 —— **状态检测没开**，以及**没有东西持续往 status.json 写**。

已修好的两项（2026-09-03 15:20）：

- AiLock `config.json` → `status.enabled: true`、`stale_minutes: 15`
- ZCode 钩子已装好，见下一节

> 注意：AiLock 是"即改即存"，**正在运行的实例不会热加载**配置文件。要么在设置页手动打开「状态检测」开关（即时生效），要么退出托盘再重启 AiLock。

---

## 1.5 ZCode 接入（已配好，可直接用）

### 为什么必须写在用户级配置

ZCode 的 Hook 有三个来源，但**项目级当前不执行** —— 写在 `<工作区>\.zcode\config.json` 里的 hooks 会被整体忽略（日志记为 `config_project_hooks_ignored`）。所以只能写 `~/.zcode/cli/config.json`，且**必须带 `hooks.enabled: true`**。

### 已生成的文件

| 文件 | 作用 |
|---|---|
| `C:\Users\admin\.zcode\hooks\ailock-notify.mjs` | 桥接脚本：解析事件 → 原子写 status.json |
| `C:\Users\admin\.zcode\cli\config.json` | 注册了 5 个事件（原 plugins / mcp 配置已保留） |
| `...config.json.bak.20260903-151441` | 改之前的备份 |

### 为什么不调 AiLock.exe

AiLock.exe 是 53 MB 的 PyInstaller 打包程序，冷启动约 1~3 秒。`PostToolUse` 是**每调用一次工具就触发一次**，那样会把 ZCode 拖垮。所以脚本直接按协议写 status.json，单次开销在毫秒级。

### 各事件各自的职责

| 事件 | 锁屏上发生什么 |
|---|---|
| `SessionStart` | 显示「ZCode 已连接 / 等待任务」，状态 idle（不触发任何动作） |
| `UserPromptSubmit` | 任务名换成你的提问（截前 40 字），进度归零，转 running |
| `PermissionRequest` | 转 **waiting**，卡片亮「等待授权」+ 即将执行的动作；批准后 PostToolUse 拉回 running |
| `PostToolUse` | 追加一行「N. 编辑 OrderService.php」这类动作，最多保留 3 行 |
| `PostToolUseFailure` | 追加一行「N. 失败：xxx」 |
| `Stop` | 只刷新时间戳，追加「本轮结束，等待下一步」 |

**Stop 不标 done**。`Stop` 在**每一轮**结束就触发，标 done 会让第一轮说完就触发"任务完成时"的动作。任务真正结束靠的是**失联判定**：超过 15 分钟没有新事件 = 人走了 / 任务停了。

进度条用已执行步数算：`step / (step + 6)`，封顶 0.9 —— 缓慢推进但永远到不了 100%，避免"看着像快完了其实没有"。

### 让改动生效

Hook 配置在 **session 启动时形成快照**，改完必须**新建一个 session** 才生效，已启动的会话不保证热更新。

排查看日志：`~/.zcode/cli/log/zcode-<日期>.jsonl`，搜 `hook.run.failed`。脚本自身的诊断写 stderr，也会进这个日志。

---

## 1.55 WorkBuddy / Codex 一键接入（2026-09-05 新增）

### WorkBuddy

hooks 配置在 `~\.workbuddy\settings.json` 的 `hooks` 字段，格式与 Claude
Code 同构。**实测会触发**：SessionStart / UserPromptSubmit / PreToolUse /
PostToolUse / PermissionRequest / Stop（payload 自带 `session_id`、
`prompt`、`tool_input`、`last_assistant_message`，不用解析 transcript）。

**Windows 上钩子经 Git Bash 执行**（不支持 cmd / PowerShell），注册的
命令必须是 bash 兼容写法——安装器已处理（正斜杠 + 引号包裹）。

```cmd
python integrations/workbuddy/install.py      # 一键安装
python integrations/workbuddy/uninstall.py    # 一键卸载
```

装完：新建 WorkBuddy 会话生效；AiLock 重启一次。

### Codex

`~\.codex\config.toml` 的 notify 是根级单值且已被 codex-computer-use.exe
占用。安装器装的是**中转脚本**：Codex 的 JSON 先原样转发原命令（插件
不受影响），再写 AiLock 状态；原命令存 sidecar，卸载时把 notify 原样还原。
改写只动 notify 一行，其余 TOML 内容不碰。

```cmd
python integrations/codex/install.py          # 一键安装
python integrations/codex/uninstall.py        # 还原原 notify 并删除脚本
```

Codex 只有「每轮结束」一种信号（无逐工具事件），会话卡不显示工具明细；
任务名取本轮输入的第一条消息，AI 行取 last-assistant-message。
固定 sid=`codex`，一台机器同时只认一个 Codex 会话。

装完：重启 Codex；AiLock 重启一次。

---

## 1.6 多会话：同时开几个 AI 任务

### 为什么需要它

你很可能一边开 ZCode 改 bug、一边跑着 `claude -p` 的长任务、后台还挂个训练脚本。
旧版三个任务共用一个 `status.json`，后写的覆盖先写的：锁屏上只看到最后一个说话的，
**而且任何一个任务标 `done` 都会触发「任务完成时关机」** —— 另外两个还在跑，机器就关了。

多会话把每个任务分开：一会话一个文件、锁屏一会话一张卡，**最后一个收尾的才触发动作**。

### 协议：往 status.d 里写

```
%APPDATA%\AiLock\status.json            ← 单会话（老写法，继续有效）
%APPDATA%\AiLock\status.d\claude-1.json ← 会话 A
%APPDATA%\AiLock\status.d\codex-2.json  ← 会话 B
%APPDATA%\AiLock\status.d\train.json    ← 会话 C
```

文件内容跟原来一模一样，多一个 `sid` 字段（等于文件名去掉 `.json`）：

```json
{
  "sid": "claude-1",
  "task": "重构订单模块",
  "state": "running",
  "progress": 0.24,
  "lines": ["改完 Service 层", "单测 12 / 15 通过"],
  "updated": 1756789012.3,
  "watch_pid": 12345
}
```

`sid` 只允许 `字母 / 数字 / - / _`，最长 64 字符，其余字符自动过滤（防路径穿越）；
留空或非法会回落到 `default`。**目录不用手工建**，写入时自动创建。

### 锁屏上长什么样

- 屏幕**右侧**竖排会话卡，一会话一卡，最多 4 张（再多只显示最活跃的 4 个）
- **正在跑的排最上面**，其余按最后更新时间倒序
- 每张卡：状态点（跑动脉动）+ 任务名 + 状态（运行中 / 已完成 / 失败）+ 进度条 + 最近 2 行日志
- 收尾满 10 分钟的会话，文件和卡片一起清掉，不留垃圾
- 会话卡是覆盖层，**不会挤动中央的时钟和密码框**

### 关键规则：以最后一个会话为准

| 场景 | 会发生什么 |
|---|---|
| A、B 都在跑，A 先完成 | A 的卡片变「已完成」，**不触发任何动作** |
| 接着 B 也完成 | **这时**才触发「任务完成时」的动作 |
| A 完成后你又让它继续跑（回到 running） | 立即反映，不会被之前的完成状态抑制 |
| A 失联（超时没更新）但 B 还在跑 | 不触发；等 B 收尾后 A 的失联会补发一次判定 |

一句话：**只要有别的会话还在跑，收尾事件就压着不触发。**

### 三种接入方式怎么带会话

| 方式 | 用法 | sid 从哪来 |
|---|---|---|
| `--run` 包裹 | `AiLock.exe --run -t "长任务" -- claude -p "..."` | **自动**，用 `run-<进程号>`，天然不冲突 |
| `--notify` 上报 | `AiLock.exe --notify --sid claude-1 --state running -t "重构" -m` | 你用 `--sid` 指定 |
| 直接写文件 | 写 `status.d\<sid>.json` | 你定文件名 |
| ZCode 钩子 | 装好即用 | **自动**，用 ZCode 的 `session_id` |

不传 `--sid` 时行为跟旧版完全一致（写主 `status.json`），老脚本不用改。

### 验证

```cmd
:: 两个会话同时跑
C:\Tools\AiLock\AiLock.exe --notify --sid demo-a --state running -t "会话 A：重构" -p 30 -l "改 Service"
C:\Tools\AiLock\AiLock.exe --notify --sid demo-b --state running -t "会话 B：训练" -p 60 -l "epoch 3/10"

:: A 完成 → 卡片变「已完成」，不应触发动作
C:\Tools\AiLock\AiLock.exe --notify --sid demo-a --state done

:: B 完成 → 这时才触发动作
C:\Tools\AiLock\AiLock.exe --notify --sid demo-b --state done
```

锁屏后应该看到右侧两张卡；中间那步 A 完成时，你设的关机/通知**不应该**被触发。

---

## 2. 最大的坑：Stop / notify 都是「每轮」触发，不是「任务全部完成」

这是照抄 README 最容易踩的雷：

| 事件 | 触发时机 | 能不能直接挂 `done` |
|---|---|---|
| Claude `Stop` | **每一轮回复结束** | 不行，第一轮说完就误报完成 |
| Claude `SessionEnd` | 整个会话退出 | 可以，但共享 1.5 秒执行预算 |
| Codex `notify` | **每一轮结束** | 不行，同上 |
| **`--run` 包裹进程** | **进程退出** | **最准，推荐** |

你在交互式会话里跟 Claude 聊十轮，`Stop` 就触发十次。如果挂的是"任务完成时关机"，第一轮结束机器就准备关机了。

---

## 3. 路线 A：`--run` 包裹（推荐，零配置，两个工具通吃）

原理：把整条命令交给 AiLock 包一层，进程启动标 `running`，进程退出按退出码标 `done` / `failed`。**完全不依赖 hook**，也就绕开了上面所有坑。

### 第一步：把 exe 放到固定位置

```cmd
mkdir C:\Tools\AiLock
copy "C:\Users\admin\WorkBuddy\2026-09-02-11-49-44\lockscreen\dist\AiLock.exe" C:\Tools\AiLock\
```

> 必须放固定位置，因为 hook / alias 里要写绝对路径。

### 第二步：直接用

```cmd
:: Claude Code 一次性长任务（推荐，跑完自动处理）
C:\Tools\AiLock\AiLock.exe --run -t "Claude 重构订单模块" -- claude -p "重构整个订单模块并跑通测试"
```

```cmd
:: Codex 一次性长任务
C:\Tools\AiLock\AiLock.exe --run -t "Codex 修登录 bug" -- codex exec "修复登录态失效的 bug 并补测试"
```

```cmd
:: 交互式会话：整个 claude 进程退出（你关掉会话）才算完成
C:\Tools\AiLock\AiLock.exe --run -t "Claude 会话" -- claude
```

要点：

| 行为 | 说明 |
|---|---|
| 退出码 | 0 → `done`，非 0 → `failed`；**原样透传**，CI 里也能用 |
| 崩溃兜底 | 自动注入 `watch_pid`，进程没了即视为结束，脚本崩了来不及上报也兜得住 |
| 参数隔离 | `--` 后面的东西原样交给你的命令，`-p`、`--epochs` 不会被吃掉 |
| 控制台 | 默认弹窗口看日志；在 hook 里加 `--no-console` 关掉 |
| 实例冲突 | 不会。这条路径不启动 UI，跟已在运行的托盘实例不冲突 |

### 可选：做成 alias 省事

```cmd
doskey claude-lock=C:\Tools\AiLock\AiLock.exe --run -t "Claude 长任务" -- claude $*
doskey codex-lock=C:\Tools\AiLock\AiLock.exe --run -t "Codex 长任务" -- codex $*
```

之后 `claude-lock -p "重构订单模块"` 即可。

---

## 4. 路线 B：Claude Code 追加 Stop hook（想要锁屏显示进度时才用）

### 别上报 done，用「running + 失联判定」

思路反过来：hook **每次只上报 `running`（刷新 `updated` 时间戳）**，然后在设置页配"多久没更新算结束"。

- 中途聊十轮 → 时间戳一直在刷新 → 不触发，安全
- 人走了 / Claude 卡住 → 超过阈值没新消息 → 判定为结束 → 触发动作

这正好是 AiLock「失联判定」字段的设计用途，比"完成"更适合长对话。

### 配置：追加，不要覆盖

你现有的 `Stop` 数组里已经有一条 tokentracker，**在它后面再加一条**（多条 hook 并行执行，互不干扰）：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/env node \"C:\\Users\\admin\\.tokentracker\\bin\\notify.cjs\" --source=claude"
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:\\Tools\\AiLock\\AiLock.exe --notify --state running -t \"Claude Code 任务\" -m"
          }
        ]
      }
    ]
  }
}
```

然后到设置页 → AI 联动：

| 字段 | 建议值 |
|---|---|
| 失联判定 | 5 分钟（按你实际节奏调） |
| 失联时的动作 | 先选 `notify`，验证通过再改 `shutdown` |
| 任务完成时 | 保持 `none`（这条路不靠它） |

### 两个必须注意的点

1. **必须带 `--state running`**。merge 模式下不传 state 时，代码会把 state 置成 `idle`（`notify.py` 里 `state=args.state or ""`，空串不在合法值里 → 回落 `idle`），正在跑的任务会被打成空闲。
2. **`-t` 可以不传，`-m` 不能省**。不传 `-t` 时 `notify.py` 会转成 `None`，`write()` 会保留原任务名；但没有 `-m` 就会整体覆盖，把其他字段清掉。

改完先跑 `claude -p "hi"` 验证一次，再看 `%APPDATA%\AiLock\status.json` 有没有出现。

---

## 5. 为什么 Codex 不能用 `notify` 字段

你的 `~/.codex/config.toml` 里已经有：

```toml
notify = [ "...\\codex-computer-use.exe", "turn-ended" ]
```

- `notify` 是**根级单值**，只能填一个命令，**没法追加第二条**
- 强行改成 AiLock 会破坏 computer-use 插件
- 而且它同样只在每轮结束时触发，挂 `done` 会误报

**所以 Codex 请走路线 A**（`--run` 包裹 `codex exec`）。想要锁屏显示 Codex
进度的话，现在有了安全的中转方案：`integrations/codex/install.py` 一键
安装——中转脚本先转发原 computer-use 命令再写 AiLock 状态，原命令存
sidecar，卸载还原（见 §1.55），不再是侵入式改法。

> 顺带一提：TOML 里 `notify` 必须写在任何 `[table]` 之前，否则会被当成表字段。

---

## 6. 三步验证（先做这个，别急着设关机）

```cmd
:: 1. 造一个信号
C:\Tools\AiLock\AiLock.exe --notify --state running -t "冒烟测试" -p 42 -l "hello AiLock"

:: 2. 确认文件出现了
type "%APPDATA%\AiLock\status.json"

:: 3. 触发完成
C:\Tools\AiLock\AiLock.exe --notify --state done
```

第 1 步之后锁定屏幕，**屏幕右侧的会话卡**上应该能看到"冒烟测试"和 42% 进度条。看不到就是 `status.enabled` 没开或路径不对。

第 3 步之后，你设的动作应该被触发。**这一步通了再考虑改成关机。**

要同时跑多个任务（每个任务一张卡），看 §1.6。

---

## 7. 安全提醒

- 「任务完成时 / 失联时的动作」**一律先用 `notify` 跑通**，确认信号链路没问题再改成 `shutdown` / `hibernate`
- 关机 / 休眠前有 **90 秒倒计时**，期间输入密码即可取消（倒计时时长在设置页可调）
- 用 `--run` 包裹 `claude -p` 时，注意 `-p` 模式会跳过工作区信任确认，**只在可信目录里用**
- `codex exec` 同理，审批策略建议保持默认，不要图省事加 `--dangerously-bypass-approvals-and-sandbox`

---

## 附：三种接入方式速查

| 方式 | 适用场景 | 配置成本 | 会不会误报完成 | 多会话 |
|---|---|---|---|---|
| `--run` 包裹 | 一次性长任务、交互式会话 | 零（命令行加前缀） | 不会 | 自动（按进程号） |
| `--notify` 上报 | 脚本内细粒度控进度 | 低（脚本里插几行） | 看你怎么调 | 加 `--sid <名字>` |
| 直接写 `status.json` | 任何语言 | 中（需原子写） | 看你怎么调 | 改写 `status.d\<名字>.json` |
