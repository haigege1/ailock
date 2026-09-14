# AiLock 白屏修复 · Qt 重写交付总结

**日期**：2026-09-03 · **状态**：✅ 全部完成并通过验证

## TL;DR

锁屏/设置界面白屏的根因是 WebView2 方案在本机环境不可控（data URI 超 .NET 65,519 字符上限导致 CoreWebView2 初始化失败 + pywebview `evaluate_js` 在旧版运行时上永久死锁）。经用户确认，界面层整体迁移到 **PySide6 (Qt 6) 原生渲染**，后端逻辑全部复用。exe 已重新打包并实测通过。

## 交付内容

### 新增（Qt 界面层）
| 文件 | 职责 |
|---|---|
| `ui/qt/theme.py` | 设计系统：颜色 token / 字体工厂 / 锁屏与设置两套 QSS |
| `ui/qt/widgets.py` | 自绘控件：胶囊开关 / 分段按钮 / 滑块行 / 卡片行 / 状态点 |
| `ui/qt/wallpaper.py` | 壁纸服务：后台线程 PIL 预处理（裁剪/模糊/暗化）+ 缓存 |
| `ui/qt/lock_window.py` | 锁屏窗口：多屏覆盖 / 时钟 / 任务卡 / 密码卡 / Ken Burns / 摇晃动画 |
| `ui/qt/settings_window.py` | 设置面板：常规 / 安全 / 壁纸 / AI 联动四页，即改即存 |
| `ui/qt/app.py` | Qt 主控：状态联动 / 动作倒计时 / IPC / 心跳 / 托盘接线 |

### 修改
- `main.py`：三态 UI 路由（`AILOCK_UI=qt|web|tk`，默认 qt）；Qt 模式不再拖入 tkinter；修复 web 模式 `tk.Tk` 注解 NameError；修复 tk 回退路径 `on_close/on_cancel` 签名断裂
- `ui/tray.py`：**热键跨线程修复** —— `reregister_hotkey` 改为 PostMessage 转交托盘线程执行（此前主线程直接调用报 1408，改设置后热键失效）
- `ui/qt/widgets.py`：**SegButton 修复** —— 直接实例化抽象类 `QAbstractButton` 会崩溃，改用扁平 QPushButton + 重复点击恢复勾选态
- `AiLock.spec`：Qt-only 构建（排除 tkinter / pywebview / 旧 UI 模块），UPX 关闭防杀软误报
- `build.bat`：改用 spec 构建，依赖加 pyside6
- `README.md`：界面技术 / 设置要点 / 故障排查 / 文件结构全部更新

### 回归测试（全部通过）
- `tools/smoke_qt_lock.py`：双屏 2560×1440 置顶覆盖 + 任务卡状态推送 ✓
- `tools/smoke_qt_settings.py`：12 项检查（页面切换 / 配置落盘 / closed 信号幂等）✓
- `tools/test_tray_hotkey.py`：跨线程热键重注册，无 1408 ✓
- `dist/AiLock.exe --selftest 4`：冻结版自检 ✓
- `dist/AiLock.exe --settings`：真实配置目录端到端 ✓

## 已知事项

1. **exe 体积**：Qt 版约 50 MB（原 WebView2 版约 20 MB），含 PySide6 运行时，属正常水平
2. **`C:\Users\admin\AiLock\` 目录**：测试期间因 Bash 环境无 `APPDATA`，探针/测试进程的日志与配置回退写到了这里（内容均为测试数据，非真实配置）。确认无用后可手动删除
3. **冻结 exe 仅内置 Qt 界面**：`AILOCK_UI=web/tk` 仅对源码运行有效（spec 已排除旧模块）
4. **热键组合冲突**：本机 `Ctrl+Shift+K` 已被其他程序占用（1409），换组合即可

## 用户下一步建议

1. 双击 `dist\AiLock.exe` 体验：首次运行自动弹设置 → 设密码、抄恢复码、选壁纸
2. 托盘右键菜单：立即锁定 / 打开设置 / 退出；全局热键 `Ctrl+Alt+L`
3. AI 联动先用 `--run -t "任务名" -- 你的命令` 试跑，「任务完成时」动作先设为「仅通知」，确认无误再改关机
4. 开机自启在设置 → 常规页勾选

---

# AiLock 在线壁纸 + 定时息屏 · 交付总结

**日期**：2026-09-03（下午） · **状态**：✅ 全部完成，测试全绿，exe 已重新打包

## TL;DR

应用户两个需求：①锁屏背景接入第三方在线图源并自动切换；②锁屏后 N 分钟自动息屏但主机不休眠。图源选定 **Bing 每日壁纸官方接口**（免 key、国内直连）；息屏基于键鼠空闲判定，到点关显示器、系统永不睡眠。已在用户真实配置中启用（在线壁纸 + 5 分钟息屏 + 5 分钟轮播）。

## 交付内容

| 文件 | 改动 |
|---|---|
| `core/wallpaper_fetch.py` | **新增**：Bing 壁纸下载器（最近 8 天，优先 UHD 退 1080p，增量跳过，断网静默，meta.json 记录刷新时间） |
| `core/power.py` | PowerGuard 加 `off_after_minutes`：`_compute(idle, display_off)` 纯逻辑，无操作到点撤 `ES_DISPLAY_REQUIRED` + 广播 `SC_MONITORPOWER` 一次；键鼠活动恢复常亮重新计时；`ES_SYSTEM_REQUIRED` 恒留 |
| `core/config.py` | 新增 `background.online_days=8`、`display.off_after_minutes=0`；mode 支持 `online` |
| `ui/qt/wallpaper.py` | `collect_wallpapers` 支持 online 模式（收集 `%APPDATA%\AiLock\wallpapers\`） |
| `ui/qt/lock_window.py` | LockScreen 新增自动轮播定时器（folder/online 模式按 `slideshow_seconds`）+ `refresh_files()`（下载完成后热更新列表） |
| `ui/qt/app.py` | 锁定时触发 fetch；`wallpapersFetched = Signal(int)` 跨线程切回主线程刷新 |
| `ui/qt/settings_window.py` | 壁纸来源加「在线 Bing」档（选中即拉取）；常规页加「锁屏后自动息屏」档位（关闭/1/3/5/10/15/30 分钟） |
| `tools/test_power_display.py` | **新增**：息屏逻辑 + 配置 + 文件名单测，20 项全过 |
| `tools/smoke_wallpaper_fetch.py` | **新增**：真跑下载冒烟（真实拉 2 张合法 JPEG + 增量 0），通过 |
| `tools/smoke_online_wallpaper.py` | **新增**：在线模式端到端冒烟（收集→渲染→fetch→refresh→轮播→手动切），7 项全过 |
| `README.md` | 功能列表 / 设置要点 / 文件结构同步更新 |

## 用户当前生效配置

- 壁纸来源 = 在线 Bing；轮播间隔 300 秒（5 分钟/张）
- 锁屏后无操作 5 分钟自动息屏（主机不休眠，动鼠标即亮）
- 均可在设置里随时改

## 备注

- 在线壁纸缓存在 `%APPDATA%\AiLock\wallpapers\`，删掉即重置
- 息屏判定用 GetLastInputInfo（硬件级键鼠空闲），低层钩子不影响

---

# 追加交付：提示语颜色修复 + 壁纸切换动效

**日期**：2026-09-03 14:30 · **状态**：✅ 完成并验证，exe 已重打

## TL;DR

用户反馈两件事：①锁屏提示语变成黑色文字，最好能自定义颜色；②壁纸轮播切换太生硬。前者根因是 `QColor("rgba(...)")` 解析失败得到无效色（画出来即黑）；后者改为背景双层 + 过渡动画，提供 5 种切换动效。

## 交付内容

| 文件 | 改动 |
|---|---|
| `ui/qt/theme.py` | **新增 `parse_color()`**：正则解析 CSS `rgb()/rgba()`（百分比 + 0~1/0~255 alpha），解析失败回落 fallback，杜绝"无效色画成黑" |
| `ui/qt/widgets.py` | MarqueeLabel 默认色与 `setInk()` 全部改走 `parse_color()` |
| `ui/qt/lock_window.py` | 背景改**双层**：`bgLabel`（当前壁纸，Ken Burns）+ `bgOver`（过渡层）+ `WipeOverlay`（擦除层，自绘 `Property(frac)` 驱动裁剪）；新增 `_show_wallpaper` / `_run_transition` / `_finish_transition` / `_cancel_transition`；堆叠改用 `stackUnder()` 显式串联 |
| `ui/qt/settings_window.py` | 常规页加「提示语颜色」行（色块按钮 + 取值标签 + 默认）；壁纸页加「切换动效」下拉 |
| `core/config.py` | 新增 `message.color`（空=跟随主题）、`background.transition`（默认 `fade`） |
| `tools/test_msg_color.py` | **新增**：parse_color 10 项 + MarqueeLabel 默认色回归 + LockScreen 配置链路，19 项全过 |
| `tools/test_bg_transition.py` | **新增**：offscreen 真实 PIL 壁纸，5 种模式各切一次，10 项全过 |
| `README.md` | 功能列表 / 设置要点同步更新 |

## 动效一览

| 模式 | 效果 | 时长 |
|---|---|---|
| `none` | 直接替换（关「界面动效」时自动走这个） | — |
| `fade` | 透明度 0→1 淡入 | 650ms |
| `slide` | 从右滑入 | 650ms |
| `zoom` | 1.14→1 缩放 + 淡入 | 650ms |
| `wipe` | 左→右擦除扫过，前沿带高光 | 750ms |

过渡期间先停 Ken Burns，结束后把新图落底层再重启；首次铺图不做过渡。

## 回归

smoke_qt_lock / smoke_qt_settings(12) / smoke_qt_unlock(18) / test_lock_layout_fix(13) 全部通过，无回归。

## 坑（复用价值）

- **QColor 不认 `rgb()/rgba()`**（只认 #hex 与命名色）→ 无效色，QSS 里能用不代表 QPainter 能画，自绘控件必须显式解析
- **多背景层的 `lower()` 不可靠**：只保证"垫底"，顺序其实取决于创建顺序；用 `stackUnder()` 串联
- **`"got %s" % (a, b, c)` 会被当 3 个参数** → 断言信息用 f-string
- **打包时旧 exe 被占用**（正在运行的 AiLock）：PyInstaller 清理 dist 会失败，但 Windows 允许重命名运行中的 exe —— 先 `mv AiLock.exe AiLock.exe.old` 再打包，完成后可正常删除 .old，无需杀进程
- 本机 cmd.exe 被工具沙箱拦截，本次打包为直接复刻 build.bat 三步（make_assets → pip install → PyInstaller spec），产物与 build.bat 等价

---

# 追加交付：两个 P1 视觉 Bug 修复

**日期**：2026-09-03 14:36 · **状态**：✅ 修复完成 + 自动化测试通过

## TL;DR

用户截图反馈两个 bug，均有清晰根因，各 1 行修复 + 写一个 offscreen 测试防回归。

## 交付内容

| Bug | 根因 | 修复 | 测试 |
|---|---|---|---|
| 锁屏左上角灰块 | `_apply_bg_geometry` 给 bgLabel/bgOver/wipe 设全屏几何，唯独漏了 `self.veil`，QLabel 停在默认 (0,0) ~100x30，叠壁纸看着一坨暗灰 | `lock_window.py _apply_bg_geometry` 补 `self.veil.setGeometry(0, 0, w, h)` | `tools/test_veil_geometry.py`（10 项） |
| 设置页 Switch 点击漂移 | `widgets.py Switch` 自定义动画属性叫 `pos`，撞上 `QWidget.pos(QPoint)`，`QPropertyAnimation(self, b"pos", self)` 把整开关当成几何值插值 | Property 改名 `knob`（getKnob/setKnob），动画目标改 `b"knob"` | `tools/test_switch_anim.py`（12 项） |

## 验证

- 全部回归套件无影响：`smoke_qt_lock` / `smoke_qt_settings` (12) / `test_bg_transition` / `smoke_online_wallpaper` (7) / `test_msg_color` / `test_lock_layout_fix` (13) / `smoke_qt_unlock` (18) 全部仍通过
- 已知无关失败：`test_tray_hotkey` 仍 FAIL（Ctrl+Alt+F9 被本机其它程序占用，1409）

## 踩坑（通用价值）

- **QWidget 自带属性陷阱**：`pos` / `size` / `geometry` / `visible` / `enabled` / `text` 都已被 QObject 占用。给自绘控件写 Property 一定要用业务语义命名（knob / frac / progress / 等），否则 PySide6 元对象会优先撞到 C++ 的 QPoint/QLine 等，动画/绑定全部误中
- **offscreen + `app.exec()` 收尾踩坑**：本环境 Windows + PySide6 6.11.2，`app.quit()` 后再 `app.exec()` 会 SIGTERM。tests 断言完成后直接退出脚本，不再 exec
- **offscreen 默认屏幕 800x800**：构造 LockWindow 不必担心 2560x1440 真屏，`veil = QRect(0,0,w,h)` 用你设的尺寸断言，不要用 `window.size()`

## 待办

- 用户需要重新打包 `dist/AiLock.exe`（视觉 bug 需要在 exe 上验）

---

# 追加交付：AI 联动多会话（一会话一卡 + 最后会话收尾）

## TL;DR

同时开多个 AI 任务时，锁屏**右侧一会话一张卡**；完成动作不再被任何一个任务抢先触发，
而是**以最后一个收尾的会话为准**。存储层用 `status.d/<sid>.json` 分文件，
老的 `status.json` 单文件写法继续有效，老脚本一行不用改。

## 需求

用户提的两点，其余为配套设计：

1. 同时有多个会话 → 要有多个会话框，且**位置要挪**（不能放在原来那里）
2. 停止后执行 → **以最后一个会话为准**

两个默认确认：会话卡右侧竖排；旧 tkinter / web 界面只显示最新会话（完整体验只在 Qt 版）。

## 交付内容

### 新增

| 文件 | 说明 |
|---|---|
| `core/statusdir.py` | 多会话存储与聚合：`status.d` 目录、`sanitize_sid`（防路径穿越）、`SessionStatusFile`、`cleanup_finished`（收尾满 10 分钟清理）、`normalize_sessions` / `pick_primary`（UI 共用）、`_SessionTracker`、`MultiStatusMonitor` |
| `tools/test_session_stack_ui.py` | 多会话 UI 测试（23 项，offscreen） |

### 修改

| 文件 | 改动 |
|---|---|
| `core/statusfile.py` | `StatusFile` 加 `extra` 参数（sid 随同一次原子写落盘），抽出 `_dump` |
| `integrations/zcode/ailock_zcode_hook.py` | 按 `session_id` 分流到 `status.d/<sid>.json`；无 `session_id` 回落主文件 |
| `ailock_run.py` | 自动用 `run-<pid>` 作 sid，多个 `--run` 天然不打架 |
| `notify.py` | 新增 `--sid` |
| `ui/qt/lock_window.py` | 新增 `SessionCard` / `SessionStack`；**删除中央 `taskCard`**；`apply_status` 改为渲染会话列表；底部状态栏显示「X 等 N 个会话」 |
| `ui/qt/app.py` | `StatusMonitor` → `MultiStatusMonitor`；`_apply_status` 处理快照列表 |
| `main.py` | 同上；tk / web 旧界面取 `pick_primary` 单会话展示 |
| `tools/test_lock_layout_fix.py` | 重写：断言会话卡 0→4 张时**中央布局零位移** |
| `tools/smoke_qt_lock.py` | 冒烟改推两条会话 |
| `AI联动接入指南.md` | 新增 §1.6 多会话（协议 / 布局 / 触发规则 / 验证） |
| `integrations/zcode/README.md` | 多会话说明 + 写入位置表 |

## 关键设计决策

1. **分文件而不是塞进一个 JSON** —— 每个会话独立原子写，互不覆盖，任意语言照协议写文件即可接入；主 `status.json` 视作 `default` 会话，向后兼容。
2. **抑制规则看「有没有别人在跑」，不看时间戳先后** —— 会话 X 收尾时只要还有别的 running 会话，done/failed/stale 一律压住不触发；`restarted` 不抑制（人回来继续干活要立刻反映）。
3. **会话卡做成绝对定位覆盖层** —— 旧 `taskCard` 在中央 `QVBoxLayout` 里，显隐会让整列位移 ~110px 与入场动画撞车（历史 bug）。新堆叠层铺满窗口、内部按窗口坐标摆卡，增删卡片不触发中央布局重排。
4. **窄屏自适应** —— 卡片宽度按「右缘 → 居中密码卡右缘」的可用宽度收缩（300 → 下限 220），并把右缘让给壁纸切换箭头（右边距 84 = 箭头 44 + 边距 24 + 间隙 16）。

## 回归测试（全部通过）

| 套件 | 结果 |
|---|---|
| `tools/test_session_stack_ui.py` | 23 项通过（一会话一卡 / 截断 4 张 / 排序 / TTL 过滤 / 状态栏 / 不遮挡箭头与密码卡 / 多屏同步 / 窄屏收窄） |
| `tools/test_statusfile_multi.py` | 16 项通过（sid 净化 / 最后会话触发 / 重启不抑制 / stale 抑制与补发 / 过期清理） |
| `tools/test_lock_layout_fix.py` | 18 项通过（含关键回归：卡片 0→4 张，密码卡与时钟几何**完全不变**） |
| 其余既有套件 | `test_action_chain`(19) / `test_run_mode`(11) / `test_power_display`(20) / `test_status_priming` / `test_veil_geometry` / `test_switch_anim` / `test_bg_transition` / `test_msg_color` / `test_anim_stub` / `test_web_logic` / `test_live` / `test_startup` / `test_tray_hotkey` 全通过 |

两个例外，**均与本次改动无关**：

- `tools/test_settings_ui.py` 挂起 —— tkinter 面板需人工关闭窗口，无人值守环境必然超时
- `tools/test_unlock_flow.py` 失败 —— `main.py:196` 的 `App.lock()` 里 `WebLock` 未定义（`WebLock` 只在 `AILOCK_UI=web` 时导入），**tk 回退模式实际不可用**，历史遗留

## 踩坑（通用价值）

- **`threading.Event` 复用**：`MultiStatusMonitor.stop()` 后再 `start()` 必须先 `_stop.clear()`，否则新线程 `_stop.wait()` 立刻返回 True 直接退出（表现为「重启监控后收不到任何事件」）
- **抑制条件别用时间戳先后**：最初写成「其他会话 updated 比本会话新才抑制」，结果是先完成的会话（updated 更旧）反而不被抑制，恰好是主要场景。改成「有任何其他 running 会话就抑制」
- **被抑制的 stale 要重置标记**：`_SessionTracker.stale_reported` 置 True 后如果不复位，最后一个会话收尾时补发不了 stale，失联动作会丢
- **窄屏卡片会压住居中密码卡**：卡片宽度写死 300 时，1024 宽的窗口就会和 360 宽的密码卡重叠；按可用宽度收缩才稳
- **offscreen 下用 `isHidden()` 判断显隐意图**：`isVisible()` 在父窗口未真正显示时恒为 False，测试断言显隐要用 `isHidden()`

## 待办

- 重新打包 `dist/AiLock.exe`（多会话需要在 exe 上验）
- 可选：修 `main.py` tk 回退模式的 `WebLock` 未定义（该模式目前不可用）

# 追加交付：在线壁纸保鲜（随机起点 + 多市场 + 滚动窗口）

用户反馈在线壁纸「永远都是那几张」。诊断：Bing 官方接口单市场每天只出 1 张、
回溯硬上限 15 天（idx 再大也被 clamp），且从国内 IP 出去 `mkt` 参数被地理围栏
无视（cn.bing.com 与 www.bing.com 都强制返回中国区图，cc/cookie 绕不过）；
另外轮播起点固定从最旧一张开始，加重「没更新」观感。

## 改动

| 文件 | 内容 |
| --- | --- |
| `core/wallpaper_fetch.py` | 多市场拉取（zh-CN/en-US/en-GB/ja-JP/de-DE/fr-FR，按文件名天然去重，单市场失败只跳过）；滚动窗口：按日期倒序只下载最新 `keep` 张 + `cleanup_old()` 删除滑出窗口的旧图（默认 30 张 ≈ UHD 105MB 上限），meta.json 记录 markets/keep/downloaded/removed |
| `ui/qt/wallpaper.py` | online/folder 模式轮播起点随机化；online 列表上限提到 200 |
| `ui/qt/app.py`、`ui/qt/settings_window.py` | 透传 `background.online_days`（默认 15）与 `background.online_keep`（默认 30） |
| `tools/smoke_wallpaper_fetch.py` | 新增多市场（markets>=3）与滚动清理（临时目录真删）两步验证 |

## 测试

- `tools/smoke_wallpaper_fetch.py` 真跑通过：6/6 市场、二次增量 0 张、清理只删最旧且不碰 meta.json/用户自放文件
- days=15 端到端首填：池子 10 张（zh-CN 全量历史），与地理围栏结论一致
- `tools/test_qt_app_wiring.py` 8 项静态护栏全过

## 结论与边界

- 不挂代理时多市场退化为同图去重（无害）；挂代理/海外网络时每天可多收 4~6 张
- Bing 生态每天本质上只有 1~2 张新图（官方与第三方镜像同源），这是图源天性；
  第三方源 peapix 实测与 zh-CN 内容基本重合（仅一天偏移），未引入
- 沙箱环境注意：WorkBuddy 的 bash 无 APPDATA 且有代理白名单，测试需显式
  `APPDATA='C:/Users/admin/AppData/Roaming'`；曾误写 `C:/Users/admin/AiLock/`
  （app_dir 的 home 回退），属测试产物待清理

# 追加：按图去重 + idx 翻页（重复图修复）

用户反馈「会采集一堆重复的」。根因：同一张图在不同市场差一天上线（图名
slug 相同、日期和 OHR 编号不同），文件名去重挡不住；且接口单次 n 有上限，
15 天历史要 idx=0/8 两页才拿全。

- `wallpaper_fetch.py`：merged 按 slug（去日期+市场后缀）去重，zh-CN 先到
  先得；新增 `dedupe_existing()` 磁盘兜底清理（最早日期优先、同日优先
  ZH-CN）；`_fetch_meta` 支持 idx 翻页（days>8 时自动翻第二页）。
- 实测：池子 24→17 张全唯一（8/21~9/4 无缺口），并找回了此前莫名丢失的
  8/26、8/27 两张。smoke（含去重用例）+ 接线护栏全过。
