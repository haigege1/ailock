# AiLock — 你的 AI 在跑，人可以离开的锁屏

为「AI 在跑，人不在」场景设计的 Windows 轻量锁屏。锁屏期间禁止休眠、支持多屏覆盖、自定义密码与背景，AI 任务完成后还能自动关机 / 解锁 / 通知。

## 主要功能

- **多屏全覆盖**：每个显示器一个无边框置顶窗口，副屏也显示时钟和提示
- **自定义密码**：PBKDF2-HMAC-SHA256 加盐哈希，配置用 Windows DPAPI 加密存储；带恢复码兜底
- **防休眠**：锁屏期间阻止系统睡眠，可选「屏幕常亮」或「允许息屏但主机不睡」
- **定时息屏**：锁屏后键鼠无操作达到设定时长（1~30 分钟）自动关闭显示器，主机不休眠、AI 任务照跑，动一下鼠标即点亮
- **自定义背景**：单图 / 文件夹轮播 / **在线 Bing 每日壁纸**（多市场拉取、自动下载最近 15 天、滚动只保留最新 30 张、增量更新、断网回落本地缓存、锁屏起点随机），5 种填充方式、可调高斯模糊与暗化遮罩
- **锁屏动效与壁纸切换**：入场滑入、密码错误卡片摇晃、Ken Burns 背景缓慢平移（仅图片/文件夹模式）；壁纸切换带 5 种过渡动效（无 / 淡入淡出 / 幻灯滑动 / 缩放淡入 / 擦除扫过，`background.transition` 可选）；文件夹与在线模式按设定间隔自动轮播，锁屏界面也可按 `←` / `→` 或点两侧箭头手动切换（单图模式自动扩展为同目录全部图片，仅本次锁屏会话生效，不写回配置）
- **自定义锁屏提示语**：一句话显示在时钟下方，超长自动跑马灯滚动；文字颜色可自定义（`message.color`，留空跟随主题），设置页点色块即可取色
- **系统键屏蔽**：Win、Alt+Tab、Alt+Esc、Ctrl+Esc、Alt+F4、Alt+Space、PrintScreen
- **看门狗自恢复**：主进程被杀后 3 秒内自动拉起并重新锁定
- **空闲自动上锁**、**最短锁定时长**、**启动即锁**、**开机自启**
- **AI 任务联动**（关键）：
  - 状态显示：锁屏上实时显示任务名 / 进度条 / 自定义文本行
  - 完成动作：任务跑完自动关机 / 休眠 / 解锁 / 提示
  - 倒计时取消：关机/休眠前默认 90 秒倒计时，期间输入密码即取消
  - 失联判定：状态文件多久没更新就视为任务卡死，触发你设定的动作

## 与 AI 工具联动

三种方式，从省事到灵活：

### 方式 1：`--run` 包一层（最省事，不需要装 Python）

原来怎么跑脚本，前面加 `AiLock.exe --run` 就行。开始自动标 running，
结束按退出码自动标 done / failed，锁屏上实时看得到，跑完按设置里的动作处理：

```cmd
AiLock.exe --run -t "模型微调 v3" -- python train.py --epochs 50
AiLock.exe --run -t "数据清洗" -- bash ./pipeline.sh
```

`--` 后面的东西原样交给你的命令，它自己的 `-t`、`--epochs` 之类参数不会被吃掉。
退出码会原样透传（成功 0，失败就是你的退出码），所以 CI 里也能用。
默认会为被包装命令弹一个控制台窗口方便你看日志；在 hooks / CI 里加 `--no-console` 关掉。

### 方式 2：`--notify` 主动上报（最灵活）

脚本内部想细粒度控制进度时用这个：

```bash
# 训练启动
AiLock.exe --notify --state running -t "Qwen2.5 微调" -p 0 -l "正在加载数据..."

# 训练中更新（进度可以是 0.24 或 24 或 24%）
AiLock.exe --notify --state running -p 24 -l "epoch 12/50" -l "loss 1.367"

# 训练完成（触发设置中的「任务完成时」动作）
AiLock.exe --notify --state done
```

源码形态下等价命令是 `python notify.py ...`。

### 方式 3：直接写状态文件（任何语言都能接）

往 `%APPDATA%\AiLock\status.json` 原子写一个 JSON 即可：

```json
{
  "task": "模型微调 v3",
  "state": "running",
  "progress": 0.24,
  "lines": ["epoch 12/50", "预计剩余 23 分钟"],
  "updated": 1756789012.3,
  "watch_pid": 12345
}
```

`state` 取 `running / done / failed / idle`；`watch_pid` 可选，填了之后
**这个进程一消失就视为任务结束**（脚本自己崩了来不及上报也能兜住）。
写文件务必用「临时文件 + `os.replace`」，否则轮询端可能读到半截 JSON。

### 多个任务并行：一任务一个状态文件

同时跑多个任务时，改写 `%APPDATA%\AiLock\status.d\<会话名>.json`（目录自动创建），
内容同上，多一个 `sid` 字段（= 文件名去掉 `.json`）：

```
%APPDATA%\AiLock\status.d\train-v3.json    ← 任务 A
%APPDATA%\AiLock\status.d\claude-refactor.json  ← 任务 B
```

锁屏**右侧一会话一张卡**（最多 4 张，正在跑的排最上面）。完成动作以**最后一个
收尾的会话为准**：只要有别的会话还是 `running`，某个会话的 `done` / `failed`
就不会触发动作 —— 不会因为你开着的另一个任务还在跑就关机。

会话名只允许字母、数字、`-`、`_`（其余字符自动过滤）。收尾满 10 分钟的会话文件
会被自动清理。`--notify` 加 `--sid <会话名>` 可达到同样效果；`--run` 会自动按
进程号生成 sid。详见 `AI联动接入指南.md` §1.6。

### 在 WorkBuddy / Zcode / Claude Code 里使用

**WorkBuddy** —— 在 automation 的 prompt 末尾加一句：

```
...（你原本的任务描述）

全部完成后执行：C:\Tools\AiLock\AiLock.exe --notify --state done
```

**Claude Code / Zcode** —— 用 hooks，任务停下时自动上报：

```json
{
  "hooks": {
    "Stop": [
      { "command": "C:\\Tools\\AiLock\\AiLock.exe --notify --state done" }
    ]
  }
}
```

**Zcode / WorkBuddy / Codex 一键部署（推荐）** —— 三个 AI 工具都不用手写配置：

```cmd
python integrations/zcode/install.py       # ZCode：注册 6 个 hook 事件
python integrations/workbuddy/install.py   # WorkBuddy：注册 6 个 hook 事件
python integrations/codex/install.py       # Codex：notify 一变二中转
```

安装器自动完成：注册钩子（ZCode / WorkBuddy 各 6 个事件：SessionStart /
UserPromptSubmit / PostToolUse / PostToolUseFailure / PermissionRequest /
Stop；Codex 走 notify 中转，原命令如 computer-use 照常转发）、打开状态
检测开关、跑冒烟测试。已有配置只追加不覆盖，各 `uninstall.py` 一键干净
卸载（Codex 会把 notify 还原为原命令）。细节见各自目录的 README.md。

> 联动不只是"停下时上报"：锁屏会实时显示你的提问、AI 回复和工具调用
> 进度。Stop / notify 都采用"完成确认窗口"——默认 12 秒内没有新事件
> 才判定完成，避免多轮对话每轮都触发完成动作。

更稳的做法是用 `--run` 把整个命令包住，这样连 hooks 都不用配：

```cmd
AiLock.exe --run -t "Claude Code 长任务" -- claude -p "重构整个模块"
```

> 提醒：把「任务完成时」设为关机前，先在设置里选 `notify` 试一遍，
> 确认联动生效再改成 `shutdown`。关机前有 90 秒倒计时，
> 期间输入密码就能取消。

## 界面技术（PySide6 / Qt）

锁屏与设置界面用 **PySide6（Qt 6）** 原生渲染 —— 不依赖 WebView2、.NET 或任何浏览器运行时，
在老旧系统（如 Win10 LTSC + 旧版 WebView2 118）上也能稳定运行。

- 控件全部自绘（时钟卡片、胶囊开关、分段选择、壁纸 Ken Burns 缓移、密码错误摇晃），
  不同 DPI / 系统主题下观感一致；
- 壁纸预处理（裁剪 / 高斯模糊 / 暗化）在后台线程用 PIL 完成，不卡锁屏动画；
- 后端（配置 / 口令 / 电源 / 看门狗 / 状态监控 / 托盘）与界面层完全解耦。

源码运行时可切换 UI 实现（冻结 exe 只内置 Qt）：

```cmd
set AILOCK_UI=qt && python main.py     :: 默认，Qt 原生界面
set AILOCK_UI=web && python main.py    :: 旧 WebView2 方案（依赖 WebView2 运行时）
set AILOCK_UI=tk && python main.py     :: 最旧 tkinter 界面（调试用）
```

> 历史说明：v1 曾用 WebView2（pywebview）渲染 Web 界面，但在「data URI 超长导致初始化失败」
> 「旧版运行时崩溃」「跨线程 JS 求值死锁」三类问题上不可控，故整体迁移到 Qt。

## 安装与运行

### 方式 A：直接打包好的 exe（推荐）

```cmd
build.bat
```

会在 `dist\AiLock.exe` 生成单文件（Qt 版约 50 MB，含 PySide6 运行时）。放到任意位置，建议固定目录，例如 `C:\Tools\AiLock\`。

### 方式 B：源码运行

```cmd
cd lockscreen
run.bat
```

（依赖项目根目录的 venv，即 `lockscreen\..\.venv`；缺失时先在项目根执行 `python -m venv .venv` + `.venv\Scripts\pip install pillow pywin32 pyside6`）

## 命令行

| 命令 | 说明 |
|---|---|
| `AiLock` | 后台常驻（托盘图标） |
| `AiLock --lock` | 立即锁定 |
| `AiLock --settings` | 打开设置面板 |
| `AiLock --notify --state done` | 状态上报（等价于 `notify.py`） |
| `AiLock --run -t 任务名 -- 命令` | 包装一条命令，自动上报 running / done / failed |
| `AiLock --selftest 6` | 自检：铺满所有屏幕 6 秒后自动退出 |

> **锁屏界面操作**：锁定后按 `←` / `→` 或点击屏幕左右两侧的箭头可切换壁纸（底部 toast 提示「壁纸 3 / 12 · 文件名」）；输错密码时卡片会左右摇晃并红边提示。摇晃动效可在「设置 → 安全 → 错误反馈动画」关闭，壁纸缓移可在「设置 → 壁纸」关闭。

## 设置要点

打开设置后建议调整：

1. **安全**：第一次进来设密码，抄下恢复码
2. **壁纸**：选锁屏壁纸来源（纯色 / 单图 / 文件夹轮播 / 在线 Bing），调模糊与暗化；「壁纸缓移（Ken Burns）」开关可按需关（笔记本省电场景建议关）；「切换动效」可选壁纸切换时的过渡动画（无 / 淡入淡出 / 幻灯滑动 / 缩放淡入 / 擦除扫过）。选「在线 Bing」会立即后台下载最近 8 天壁纸到 `%APPDATA%\AiLock\wallpapers\`，之后每次锁屏自动增量更新，按「壁纸轮播间隔」（config 里 `background.slideshow_seconds`）自动切换
3. **常规**：「屏幕常亮」开启时可配合「锁屏后自动息屏」——锁屏后键鼠无操作达到设定分钟数自动关显示器（主机不休眠，动鼠标即唤醒）；「空闲自动锁屏」可省心，但设置后用密码解锁才不会被频繁弹；「锁屏提示语」可写一句话，「提示语颜色」点色块取色（默认跟随主题）
4. **AI 联动**：选择「任务完成时」执行的动作（先试 `notify` 看效果，再换成 `shutdown`）
5. **常规 → 开机自启**：勾上即可

## 诚实的能力边界

- **能拦住的键**：Win、Alt+Tab、Alt+Esc、Ctrl+Esc、Alt+F4、Alt+Space、PrintScreen
- **拦不住的键**：`Ctrl+Alt+Del` 与 `Ctrl+Shift+Esc` —— 这是 Windows 的安全序列，由内核直接交给 Winlogon，用户态程序无权截获
- **拦不住时的兜底**：看门狗进程，主程序被杀 3 秒内自动重启并重新锁定
- **定位**：防误触 / 防他人随手乱动 / 防 AI 跑完没人管，不是防专业攻击者；真正安全请用 `Win+L`

## 故障排查

| 现象 | 处理 |
|---|---|
| 双击 exe 没反应 | 现版本启动后会弹「AiLock 设置」窗口（首次）+ 托盘气泡；若都没有，看 `%APPDATA%\AiLock\ailock.log`。托盘图标可能折叠在通知区 `^` 里 |
| 有托盘图标但点菜单没反应 | 确认用的是最新版 exe（早前版本有托盘菜单跨线程失效 bug，已修复）；重启一次即可 |
| 托盘找不到 | 看 `C:\Users\<你>\AppData\Roaming\AiLock\ailock.log` |
| 忘记密码 | 在登录界面用「恢复码」解锁（在设置面板「安全」页生成）；或者彻底重置：删除 `C:\Users\<你>\AppData\Roaming\AiLock\vault.dat` |
| 热键没生效 | 大概率被其它程序占用（日志里会有 `tray.warn`），换一个组合 |
| exe 里设 AILOCK_UI=web/tk | 冻结版只内置 Qt 界面，旧 UI 仅供源码运行调试 |
| 双屏缩放下窗口漏边 | 已在 `set_dpi_awareness` 中处理 System DPI Aware；如还出问题告诉我你的缩放比例 |

## 文件结构

```
lockscreen/
├── main.py              主程序入口（含 --run 分发）
├── notify.py            状态上报 CLI
├── ailock_run.py        命令包装器
├── assets/              图标与默认壁纸
├── core/
│   ├── winapi.py        ctypes Win32 公共封装
│   ├── config.py        配置 / 运行时状态 / 心跳
│   ├── security.py      口令哈希、DPAPI 保险箱、恢复码
│   ├── power.py         防休眠心跳
│   ├── input_hook.py    低层键盘钩子
│   ├── monitors.py      多显示器枚举
│   ├── idle.py          空闲检测
│   ├── wallpaper_fetch.py Bing 在线壁纸下载（增量 / 断网容错）
│   ├── watchdog.py      看门狗
│   ├── autostart.py     开机自启（注册表）
│   ├── statusfile.py    状态文件协议与轮询
│   └── actions.py       完成动作（关机/休眠/解锁）
├── ui/
│   ├── qt/              Qt 界面（默认）
│   │   ├── theme.py     设计系统：颜色 / 字体 / QSS 样式
│   │   ├── widgets.py   自绘控件：开关 / 分段按钮 / 滑块 / 卡片行
│   │   ├── wallpaper.py 壁纸服务：后台线程 PIL 预处理 + 缓存
│   │   ├── lock_window.py 锁屏窗口（多屏 / 时钟 / 任务卡 / 密码卡）
│   │   ├── settings_window.py 设置面板（常规 / 安全 / 壁纸 / AI 联动）
│   │   └── app.py       Qt 主控（状态联动 / 动作倒计时 / IPC / 心跳）
│   ├── webui.py         WebView2 引擎（legacy，AILOCK_UI=web 时用）
│   ├── web/             Web UI 前端（legacy）
│   ├── lock_window.py   锁屏窗口（tk 回退路径，legacy）
│   ├── settings_panel.py 设置面板（tk 回退路径，legacy）
│   └── tray.py          托盘 + 全局热键（纯 win32，各界面共用）
├── tools/
│   ├── make_assets.py   图标 / 壁纸生成
│   ├── smoke_qt_lock.py    Qt 锁屏冒烟（铺屏 + 状态推送）
│   ├── smoke_qt_settings.py Qt 设置面板冒烟（页面切换 + 配置落盘）
│   ├── test_tray_hotkey.py 热键跨线程重注册回归
│   └── ...              其他回归 / 冒烟测试
├── run.bat              源码一键运行
└── build.bat            打包成 exe
```

配置、日志、状态文件全部在 `%APPDATA%\AiLock\`，删掉它就彻底重置。

---

## 许可证

本项目采用 [MIT License](LICENSE) 开源。
