"""AiLock Qt 版 · 设置面板

四个页面：常规 / 安全 / 壁纸 / AI 联动。
所有改动即改即存：控件变化 → cfg.set(...) → on_saved(cfg)。

接口契约（app.py 依赖）：
    SettingsWindow(cfg, vault, on_saved=None, on_lock_now=None, parent=None)
    closed = Signal()      # 窗口关闭时发射，主控据此清理引用
    show() / close()

注意：theme.T.font() 依赖 QFontDatabase，必须在 QApplication 实例化之后才能
调用，所以本模块顶层不创建任何字体/控件。
"""

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIntValidator
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox,
                               QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea,
                               QStackedWidget, QVBoxLayout, QWidget)

from core.actions import ACTION_LABELS
from core.autostart import sync as autostart_sync
from core.config import default_status_path
from core.logger import log
from core import zcode_bridge
from core import workbuddy_bridge
from core import codex_bridge

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIntValidator
from PySide6.QtWidgets import (QApplication, QColorDialog, QComboBox,
                               QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout,
                               QWidget)

from . import theme as T
from .widgets import CardBlock, CardRow, SegButton, SliderRow, Switch

# ---------------------------------------------------------------- 选项表
IDLE_OPTIONS = [("关闭", 0), ("5 分钟", 5), ("10 分钟", 10), ("30 分钟", 30)]
MIN_LOCK_OPTIONS = [("无限制", 0), ("1 分钟", 1), ("2 分钟", 2), ("5 分钟", 5)]
MODE_OPTIONS = [("纯色", "color"), ("单图", "image"), ("文件夹", "folder"),
                ("在线 Bing", "online")]
OFF_AFTER_OPTIONS = [("关闭", 0), ("1 分钟", 1), ("3 分钟", 3), ("5 分钟", 5),
                     ("10 分钟", 10), ("15 分钟", 15), ("30 分钟", 30)]
TRANSITION_OPTIONS = [("无（直接切换）", "none"), ("淡入淡出", "fade"),
                      ("幻灯滑动", "slide"), ("缩放淡入", "zoom"),
                      ("擦除扫过", "wipe")]
ACTION_KEYS = ("none", "shutdown", "hibernate", "sleep", "unlock", "notify")
ACTION_OPTIONS = [(ACTION_LABELS[k], k) for k in ACTION_KEYS]

IMG_FILTER = "图片 (*.png *.jpg *.jpeg *.bmp *.webp)"


class SettingsWindow(QWidget):
    """设置面板主窗口。"""

    closed = Signal()

    def __init__(self, cfg, vault, on_saved=None, on_lock_now=None,
                 parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.vault = vault
        self.on_saved = on_saved
        self.on_lock_now = on_lock_now

        self.setObjectName("SettingsRoot")
        self.setWindowTitle("AiLock 设置")
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.resize(880, 620)
        self.setMinimumSize(720, 520)

        self._closed = False
        self._debounce: dict = {}

        self._build()
        self.setStyleSheet(T.SETTINGS_QSS)
        self._switch_page(0)
        self._center()

    # ============================================================ 构建
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- 左侧导航 ----------------
        nav = QFrame(self)
        nav.setObjectName("nav")
        nav.setFixedWidth(176)
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(12, 20, 12, 20)
        nl.setSpacing(6)

        brand = QLabel("AiLock")
        brand.setFont(T.font(17, QFont.DemiBold))
        brand.setStyleSheet("color:%s;background:transparent;" % T.C_TEXT)
        brand.setContentsMargins(6, 0, 0, 10)
        nl.addWidget(brand)

        self._navBtns = []
        self._pageDefs = [
            ("常规", "锁定行为与开机启动", self._page_general),
            ("安全", "密码、冷却与恢复码", self._page_security),
            ("壁纸", "锁屏背景与滤镜", self._page_wallpaper),
            ("AI 联动", "任务状态检测与动作", self._page_status),
        ]
        for i, (name, _sub, _builder) in enumerate(self._pageDefs):
            b = QPushButton(name, nav)
            b.setObjectName("navItem")
            b.setFont(T.font(13, QFont.Medium))
            b.setFixedHeight(38)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("active", "false")
            b.clicked.connect(lambda _c=False, idx=i: self._switch_page(idx))
            self._navBtns.append(b)
            nl.addWidget(b)
        nl.addStretch(1)

        hint = QLabel("改动即时生效\n无需重启程序")
        hint.setFont(T.font(11))
        hint.setStyleSheet("color:%s;background:transparent;" % T.C_FAINT)
        nl.addWidget(hint)
        root.addWidget(nav)

        # ---------------- 右侧主体 ----------------
        main = QWidget(self)
        ml = QVBoxLayout(main)
        ml.setContentsMargins(26, 20, 26, 18)
        ml.setSpacing(14)

        # 顶部：标题 + 两个动作按钮
        head = QWidget(main)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        tc = QWidget(head)
        tl = QVBoxLayout(tc)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(2)
        self.pageTitle = QLabel("常规")
        self.pageTitle.setObjectName("pageTitle")
        self.pageTitle.setFont(T.font(19, QFont.DemiBold))
        self.pageSub = QLabel("")
        self.pageSub.setObjectName("pageSub")
        self.pageSub.setFont(T.font(12))
        tl.addWidget(self.pageTitle)
        tl.addWidget(self.pageSub)
        hl.addWidget(tc, 1)

        btnLog = QPushButton("打开日志", head)
        btnLog.setObjectName("btn")
        btnLog.setFont(T.font(12))
        btnLog.setCursor(Qt.PointingHandCursor)
        btnLog.setFixedHeight(34)
        btnLog.clicked.connect(self._open_log)

        btnLock = QPushButton("立即锁定", head)
        btnLock.setObjectName("btnPrimary")
        btnLock.setFont(T.font(12, QFont.Medium))
        btnLock.setCursor(Qt.PointingHandCursor)
        btnLock.setFixedHeight(34)
        btnLock.clicked.connect(self._lock_now)

        hl.addWidget(btnLog, 0, Qt.AlignVCenter)
        hl.addWidget(btnLock, 0, Qt.AlignVCenter)
        ml.addWidget(head)

        # 内容区：滚动 + 堆叠页
        self._stack = QStackedWidget(main)
        for name, sub, builder in self._pageDefs:
            page = QWidget(self._stack)
            pl = QVBoxLayout(page)
            pl.setContentsMargins(0, 0, 4, 12)
            pl.setSpacing(14)
            builder(page, pl)
            pl.addStretch(1)
            self._stack.addWidget(page)

        scroll = QScrollArea(main)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self._stack)
        ml.addWidget(scroll, 1)
        root.addWidget(main, 1)

    # ============================================================ 页面 1 常规
    def _page_general(self, page, lay):
        c = self.cfg

        block = CardBlock(page)
        sw = Switch(bool(c.get("lock.lock_on_start", False)))
        sw.toggled.connect(
            lambda v: self._apply("lock.lock_on_start", bool(v)))
        block.addRow(CardRow("开机自启后锁定", "电脑启动时立即进入锁屏", sw))

        cmb = self._combo(IDLE_OPTIONS, c.get("lock.auto_lock_idle_minutes", 0))
        cmb.currentIndexChanged.connect(
            lambda: self._apply("lock.auto_lock_idle_minutes", cmb.currentData()))
        block.addRow(CardRow("空闲自动锁屏", "无操作达到时长后自动锁屏", cmb))

        cmb2 = self._combo(MIN_LOCK_OPTIONS, c.get("lock.min_lock_minutes", 0))
        cmb2.currentIndexChanged.connect(
            lambda: self._apply("lock.min_lock_minutes", cmb2.currentData()))
        block.addRow(CardRow("最短锁定时长",
                             "倒计时内禁止解锁，防止手贱立刻解开", cmb2))
        lay.addWidget(block)

        block2 = CardBlock(page)
        sw2 = Switch(bool(c.get("display.keep_on", True)))
        sw2.toggled.connect(lambda v: self._apply("display.keep_on", bool(v)))
        block2.addRow(CardRow("屏幕常亮", "阻止系统息屏与休眠，保证 AI 任务不被打断",
                              sw2))

        cmb3 = self._combo(OFF_AFTER_OPTIONS,
                           c.get("display.off_after_minutes", 0))
        cmb3.currentIndexChanged.connect(
            lambda: self._apply("display.off_after_minutes",
                                cmb3.currentData()))
        block2.addRow(CardRow("锁屏后自动息屏",
                              "锁屏后键鼠无操作达到时长即熄灭画面，"
                              "主机不休眠、AI 任务照跑；动一下鼠标即点亮。"
                              "需开启「屏幕常亮」。现代待机机器以"
                              "「黑屏」方式实现（真关屏会触发系统待机）",
                              cmb3))

        msg = QLineEdit(page)
        msg.setObjectName("field")
        msg.setFixedWidth(240)
        msg.setFont(T.font(12))
        msg.setPlaceholderText("例如：AI 正在跑，请勿关机")
        msg.setText(str(c.get("message.text", "") or ""))
        msg.editingFinished.connect(
            lambda: self._apply("message.text", msg.text().strip()))
        block2.addRow(CardRow("锁屏提示语", "锁屏界面上显示的一句话", msg))

        # 提示语颜色：色块按钮 + 恢复默认
        colorBox = QWidget(page)
        cl = QHBoxLayout(colorBox)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        self.msgColorBtn = QPushButton(colorBox)
        self.msgColorBtn.setObjectName("btn")
        self.msgColorBtn.setFixedSize(56, 34)
        self.msgColorBtn.setCursor(Qt.PointingHandCursor)
        self.msgColorBtn.setToolTip("点击选择提示语颜色")
        self.msgColorBtn.clicked.connect(self._pick_msg_color)
        self.msgColorLbl = QLabel(colorBox)
        self.msgColorLbl.setObjectName("rowDesc")
        self.msgColorLbl.setFont(T.font(11))
        btnColorReset = QPushButton("默认", colorBox)
        btnColorReset.setObjectName("btn")
        btnColorReset.setFont(T.font(12))
        btnColorReset.setFixedHeight(34)
        btnColorReset.setCursor(Qt.PointingHandCursor)
        btnColorReset.clicked.connect(self._reset_msg_color)
        cl.addWidget(self.msgColorBtn)
        cl.addWidget(self.msgColorLbl)
        cl.addWidget(btnColorReset)
        cl.addStretch(1)
        block2.addRow(CardRow("提示语颜色",
                              "自定义锁屏提示语的文字颜色，留空跟随主题",
                              colorBox))
        self._sync_msg_color()

        # 开机自启：写注册表，失败要让用户看见
        autoBox = QWidget(page)
        al = QHBoxLayout(autoBox)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)
        self.autoFeedback = QLabel("")
        self.autoFeedback.setObjectName("feedback")
        self.autoFeedback.setFont(T.font(11))
        sw3 = Switch(bool(c.get("autostart", False)))
        sw3.toggled.connect(self._on_autostart)
        al.addWidget(self.autoFeedback)
        al.addWidget(sw3, 0, Qt.AlignVCenter)
        block2.addRow(CardRow("开机自启", "登录 Windows 后自动运行 AiLock", autoBox))
        lay.addWidget(block2)

    def _on_autostart(self, on: bool):
        self._apply("autostart", bool(on))
        ok = False
        try:
            ok = bool(autostart_sync(bool(on)))
        except Exception as exc:
            log.log("settings.autostart.error", repr(exc))
        self._feedback(self.autoFeedback,
                       "已开启 ✓" if (ok and on) else
                       "已关闭" if (ok and not on) else "写入注册表失败",
                       "ok" if ok else "err")

    # ---------------------------------------------------------- 提示语颜色
    def _sync_msg_color(self):
        color = str(self.cfg.get("message.color", "") or "").strip()
        if color:
            # 自定义色：实心色块
            self.msgColorBtn.setStyleSheet(
                "QPushButton#btn { background: %s; }" % color)
            self.msgColorLbl.setText(color)
        else:
            # 跟随主题：虚线框白底示意
            self.msgColorBtn.setStyleSheet(
                "QPushButton#btn { background: #F2F6FC; "
                "border: 1px dashed %s; }" % T.C_BORDER)
            self.msgColorLbl.setText("跟随主题")

    def _pick_msg_color(self):
        current = str(self.cfg.get("message.color", "") or "").strip()
        init = QColor(current) if current else QColor("#F2F6FC")
        try:
            color = QColorDialog.getColor(init, self, "选择提示语颜色")
        except Exception as exc:
            log.log("settings.msgcolor.error", repr(exc))
            return
        if not color.isValid():
            return
        self._apply("message.color", color.name())
        self._sync_msg_color()

    def _reset_msg_color(self):
        self._apply("message.color", "")
        self._sync_msg_color()

    # ============================================================ 页面 2 安全
    def _page_security(self, page, lay):
        v = self.vault

        block = CardBlock(page)
        ctl = QWidget(page)
        cl = QVBoxLayout(ctl)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)

        self.pwOld = QLineEdit(ctl)
        self.pwNew = QLineEdit(ctl)
        self.pwNew2 = QLineEdit(ctl)
        for e, ph in ((self.pwOld, "当前密码"),
                      (self.pwNew, "新密码（至少 4 位）"),
                      (self.pwNew2, "确认新密码")):
            e.setObjectName("field")
            e.setEchoMode(QLineEdit.Password)
            e.setPlaceholderText(ph)
            e.setFixedHeight(34)
            e.setFont(T.font(12))
            cl.addWidget(e)
        self.pwNew2.returnPressed.connect(self._save_password)
        self.pwNew.returnPressed.connect(self._save_password)

        act = QWidget(ctl)
        al = QHBoxLayout(act)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)
        btn = QPushButton("保存密码", act)
        btn.setObjectName("btnPrimary")
        btn.setFont(T.font(12, QFont.Medium))
        btn.setFixedHeight(34)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._save_password)
        self.pwFeedback = QLabel("")
        self.pwFeedback.setObjectName("feedback")
        self.pwFeedback.setFont(T.font(12))
        al.addWidget(btn)
        al.addWidget(self.pwFeedback, 1)
        cl.addWidget(act)

        has_pw = bool(v.is_set)
        self.pwOld.setVisible(has_pw)
        block.addRow(CardRow(
            "修改锁屏密码",
            "密码不以明文落盘：PBKDF2 哈希 + Windows DPAPI 加密存储"
            if has_pw else "尚未设置密码，直接填写新密码即可",
            ctl, vertical=True))
        lay.addWidget(block)

        block2 = CardBlock(page)
        max_att = int(self.cfg.get("security.max_attempts", 5) or 5)
        cool = int(self.cfg.get("security.cooldown_seconds", 30) or 30)
        ro = QLabel("%d 次 / %d 秒" % (max_att, cool))
        ro.setObjectName("rowDesc")
        ro.setFont(T.font(12, QFont.Medium))
        block2.addRow(CardRow("连续错误策略",
                              "输错达到次数后进入冷却，期间任何输入都无效", ro))

        sw = Switch(bool(self.cfg.get("ui.animations", True)))
        sw.toggled.connect(lambda val: self._apply("ui.animations", bool(val)))
        block2.addRow(CardRow("错误反馈动画", "密码错误时卡片摇晃 + 红边", sw))
        lay.addWidget(block2)

        block3 = CardBlock(page)
        box = QWidget(page)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        rowTop = QWidget(box)
        rtl = QHBoxLayout(rowTop)
        rtl.setContentsMargins(0, 0, 0, 0)
        rtl.setSpacing(10)
        self.recCode = QLabel("")
        self.recCode.setFont(T.font(16, QFont.DemiBold))
        self.recCode.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btnRe = QPushButton("重新生成", rowTop)
        btnRe.setObjectName("btn")
        btnRe.setFont(T.font(12))
        btnRe.setFixedHeight(32)
        btnRe.setCursor(Qt.PointingHandCursor)
        btnRe.clicked.connect(self._regen_recovery)
        rtl.addWidget(self.recCode, 1)
        rtl.addWidget(btnRe, 0, Qt.AlignVCenter)
        self.recTip = QLabel("")
        self.recTip.setObjectName("rowDesc")
        self.recTip.setFont(T.font(11))
        self.recTip.setWordWrap(True)
        bl.addWidget(rowTop)
        bl.addWidget(self.recTip)
        block3.addRow(CardRow("恢复码",
                              "忘记密码时用它在锁屏页紧急解锁",
                              box, vertical=True))
        lay.addWidget(block3)
        self._load_recovery()

    def _load_recovery(self, fresh: bool = False):
        try:
            if self.vault.has_recovery():
                self.recCode.setText("")
                self.recCode.setVisible(False)
                self.recTip.setText(
                    "已生成过恢复码。出于安全，它只在生成时显示一次；"
                    "若未抄下来或怀疑泄露，请点「重新生成」。" if not fresh else
                    "新的恢复码已生成，请立刻抄下并妥善保存 —— 关闭后无法再次查看。")
                if fresh:
                    self.recCode.setVisible(False)
                return
            code = self.vault.ensure_recovery()
            self.recCode.setText(code)
            self.recCode.setStyleSheet("color:%s;letter-spacing:2px;" % T.C_TEXT)
            self.recCode.setVisible(True)
            self.recTip.setText(
                "请立刻抄下并保存到安全的地方 —— 这是忘记密码时唯一的紧急通道，"
                "之后无法再次查看。")
            log.log("settings.recovery", "首次生成恢复码")
        except Exception as exc:
            log.log("settings.recovery.error", repr(exc))
            self.recCode.setVisible(False)
            self.recTip.setText("恢复码不可用：%r" % (exc,))

    def _regen_recovery(self):
        try:
            code = self.vault.regenerate_recovery()
        except Exception as exc:
            log.log("settings.recovery.error", repr(exc))
            self.recTip.setText("重新生成失败：%r" % (exc,))
            return
        self.recCode.setText(code)
        self.recCode.setStyleSheet("color:%s;letter-spacing:2px;" % T.C_TEXT)
        self.recCode.setVisible(True)
        self.recTip.setText(
            "已生成新的恢复码，旧的立即失效。请立刻抄下并妥善保存 —— "
            "关闭后无法再次查看。")
        log.log("settings.recovery", "重新生成恢复码")

    def _save_password(self):
        old = self.pwOld.text()
        new = self.pwNew.text()
        new2 = self.pwNew2.text()
        has_pw = bool(self.vault.is_set)

        if has_pw and not old:
            return self._pw_fail("请先输入当前密码")
        if len(new) < 4:
            return self._pw_fail("新密码至少 4 位")
        if new != new2:
            return self._pw_fail("两次输入的新密码不一致")
        if has_pw and not self.vault.verify(old):
            return self._pw_fail("当前密码不正确")

        try:
            self.vault.set_password(new)
        except Exception as exc:
            log.log("settings.password.error", repr(exc))
            return self._pw_fail("保存失败：%r" % (exc,))

        self.pwOld.clear()
        self.pwNew.clear()
        self.pwNew2.clear()
        was_unset = not has_pw
        self.pwOld.setVisible(True)
        self._feedback(self.pwFeedback,
                       "已保存 ✓" + ("（密码已设置）" if was_unset else ""),
                       "ok")
        log.log("settings.password", "口令已更新")

    def _pw_fail(self, text: str):
        self._feedback(self.pwFeedback, text, "err")

    # ============================================================ 页面 3 壁纸
    def _page_wallpaper(self, page, lay):
        c = self.cfg
        block = CardBlock(page)

        mode = str(c.get("background.mode", "color") or "color")
        self.bgSeg = SegButton(MODE_OPTIONS, mode, page)
        self.bgSeg.changed.connect(self._on_mode_changed)
        block.addRow(CardRow("壁纸来源",
                             "纯色 / 单张图片 / 文件夹轮播 / "
                             "在线（Bing 多市场每日图，滚动保留最新 30 张）",
                             self.bgSeg))

        # 路径行（纯色模式下整行隐藏）
        pathBox = QWidget(page)
        pl = QHBoxLayout(pathBox)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(8)
        self.bgPath = QLineEdit(page)
        self.bgPath.setObjectName("field")
        self.bgPath.setFixedWidth(300)
        self.bgPath.setFont(T.font(12))
        self.bgPath.setPlaceholderText("未选择")
        self.bgPath.setText(str(c.get("background.path", "") or ""))
        self.bgPath.editingFinished.connect(
            lambda: self._apply("background.path", self.bgPath.text().strip()))
        btnBrowse = QPushButton("浏览", page)
        btnBrowse.setObjectName("btn")
        btnBrowse.setFont(T.font(12))
        btnBrowse.setFixedHeight(34)
        btnBrowse.setCursor(Qt.PointingHandCursor)
        btnBrowse.clicked.connect(self._browse_path)
        pl.addWidget(self.bgPath)
        pl.addWidget(btnBrowse)
        self.pathRow = block.addRow(
            CardRow("路径", "单图选文件，文件夹模式选目录", pathBox))

        dim = int(round(float(c.get("background.dim", 0.45) or 0) * 100))
        self.dimSlider = SliderRow(0, 80, max(0, min(80, dim)), fmt="%d%%",
                                   parent=page)
        self.dimSlider.valueChanged.connect(
            lambda v: self._debounced_set("background.dim", round(v / 100.0, 2)))
        block.addRow(CardRow("暗化遮罩", "压暗壁纸，保证锁屏文字可读",
                             self.dimSlider))

        blur = int(c.get("background.blur", 0) or 0)
        self.blurSlider = SliderRow(0, 20, max(0, min(20, blur)), fmt="%d px",
                                    parent=page)
        self.blurSlider.valueChanged.connect(
            lambda v: self._debounced_set("background.blur", int(v)))
        block.addRow(CardRow("高斯模糊", "模糊半径，越大越朦胧",
                             self.blurSlider))

        sw = Switch(bool(c.get("background.kenburns", True)))
        sw.toggled.connect(lambda v: self._apply("background.kenburns", bool(v)))
        block.addRow(CardRow("壁纸缓移（Ken Burns）",
                             "锁屏时壁纸缓慢缩放平移", sw))

        cmbT = self._combo(TRANSITION_OPTIONS,
                           str(c.get("background.transition", "fade") or "fade"))
        cmbT.currentIndexChanged.connect(
            lambda: self._apply("background.transition", cmbT.currentData()))
        block.addRow(CardRow("切换动效",
                             "壁纸轮播 / 手动切换时的过渡动画；"
                             "关闭「界面动效」后不生效", cmbT))
        lay.addWidget(block)

        self.wpBlock = block
        self._sync_mode_ui(mode)

    def _on_mode_changed(self, mode: str):
        self._apply("background.mode", mode)
        self._sync_mode_ui(mode)
        if mode == "online":
            # 立即后台拉取一次，不用等下次锁屏
            try:
                from core.wallpaper_fetch import fetch_async
                days = int(self.cfg.get("background.online_days", 15) or 15)
                keep = int(self.cfg.get("background.online_keep", 30) or 30)
                fetch_async(days=days, keep=keep)
                log.log("settings.online", "已触发在线壁纸后台下载")
            except Exception as exc:
                log.log("settings.online.error", repr(exc))

    def _sync_mode_ui(self, mode: str):
        show = mode in ("image", "folder")
        self._set_row_visible(self.wpBlock, self.pathRow, show)
        self.bgPath.setPlaceholderText("选择图片文件" if mode == "image"
                                       else "选择图片文件夹" if mode == "folder"
                                       else "纯色模式无需路径")

    def _browse_path(self):
        mode = str(self.cfg.get("background.mode", "color") or "color")
        start = self.bgPath.text().strip() or ""
        try:
            if mode == "folder":
                path = QFileDialog.getExistingDirectory(
                    self, "选择壁纸文件夹", start)
            else:
                path, _f = QFileDialog.getOpenFileName(
                    self, "选择壁纸图片", start, IMG_FILTER)
        except Exception as exc:
            log.log("settings.browse.error", repr(exc))
            return
        if not path:
            return
        self.bgPath.setText(path)
        self._apply("background.path", path)

    # ============================================================ 页面 4 AI
    # 一键接入的三个 AI 工具：key → (bridge 模块, 展示文案)
    AI_BRIDGES = ("zcode", "workbuddy", "codex")
    AI_META = {
        "zcode": (zcode_bridge, "ZCode",
                  "锁屏实时显示 ZCode 对话与进度；新会话生效，改配置后需重启 ZCode 会话",
                  "没找到 ZCode 配置，请先安装并启动一次 ZCode",
                  "一键注册钩子：锁屏实时显示对话气泡与任务进度"),
        "workbuddy": (workbuddy_bridge, "WorkBuddy",
                      "锁屏实时显示 WorkBuddy 对话与进度；新会话生效，改配置后需重启会话",
                      "没找到 WorkBuddy 配置（~/.workbuddy/settings.json），请先安装并启动一次",
                      "一键注册钩子：锁屏实时显示对话气泡与任务进度"),
        "codex": (codex_bridge, "Codex",
                  "锁屏显示 Codex 每轮结果；原 notify 照常转发，computer-use 不受影响",
                  "没找到 Codex 配置（~/.codex/config.toml），请先安装并启动一次",
                  "一键接管 notify：原命令照常转发，锁屏显示每轮结果"),
    }

    def _ai_found(self, key: str, st: dict) -> bool:
        return bool(st.get("found", st.get(key + "_found")))

    def _page_status(self, page, lay):
        c = self.cfg

        # ---- AI 一键接入卡片（放最上面，ZCode / WorkBuddy / Codex）----
        self._aiBlocks = {}
        for key in self.AI_BRIDGES:
            blk = self._build_ai_block(page, key)
            self._aiBlocks[key] = blk
            lay.addWidget(blk)

        block = CardBlock(page)

        sw = Switch(bool(c.get("status.enabled", True)))
        sw.toggled.connect(lambda v: self._apply("status.enabled", bool(v)))
        block.addRow(CardRow("状态检测",
                             "周期性读取状态文件，在锁屏上显示任务进度", sw))

        cmb = self._combo(ACTION_OPTIONS, c.get("status.on_done_action", "none"))
        cmb.currentIndexChanged.connect(
            lambda: self._apply("status.on_done_action", cmb.currentData()))
        block.addRow(CardRow("任务完成时", "AI 任务标记为 done 后执行的动作", cmb))

        block.addRow(CardRow(
            "动作前倒计时",
            "关机 / 休眠 / 睡眠前留出的可取消时间，0 = 立即执行",
            self._num_box("status.on_done_countdown", 0, 3600, 90, "秒")))

        stPath = QLineEdit(page)
        stPath.setObjectName("field")
        stPath.setFixedWidth(280)
        stPath.setFont(T.font(12))
        stPath.setPlaceholderText(default_status_path())
        stPath.setText(str(c.get("status.path", "") or ""))
        stPath.editingFinished.connect(
            lambda: self._apply("status.path", stPath.text().strip()))
        block.addRow(CardRow("状态文件路径", "留空则使用上面的默认路径", stPath))

        block.addRow(CardRow(
            "轮询间隔", "读取状态文件的频率，越小越灵敏、开销略高",
            self._num_box("status.poll_seconds", 1, 60, 2, "秒")))

        block.addRow(CardRow(
            "失联判定", "状态文件多久没更新视为任务卡死，0 = 不判断",
            self._num_box("status.stale_minutes", 0, 1440, 0, "分钟")))

        cmb2 = self._combo(ACTION_OPTIONS,
                           c.get("status.on_stale_action", "none"))
        cmb2.currentIndexChanged.connect(
            lambda: self._apply("status.on_stale_action", cmb2.currentData()))
        block.addRow(CardRow("失联时的动作", "判定为卡死后执行的动作", cmb2))
        lay.addWidget(block)

    # ------------------------------------------------ AI 一键安装/卸载（通用）
    def _ai_bridge(self, key: str):
        return self.AI_META[key][0]

    def _ai_toggle(self, key: str):
        st = self._ai_bridge(key).installed_state()
        if st["configured"]:
            self._ai_uninstall(key)
        else:
            self._ai_install(key)

    def _ai_install(self, key: str):
        btn = self._aiButton(key)
        btn.setEnabled(False)
        btn.setText("安装中…")
        QApplication.processEvents()
        bridge = self._ai_bridge(key)
        try:
            report = bridge.install(dry=False)
        except bridge.BridgeError as err:
            self._ai_show_result("安装失败", str(err), error=True)
            self._ai_refresh(key)
            return
        except Exception as exc:      # 未知异常也要给用户交代
            log.log("ai.%s.install" % key, repr(exc))
            self._ai_show_result("安装失败", "意外错误：%r" % exc,
                                    error=True)
            self._ai_refresh(key)
            return
        self._ai_show_result("安装完成", report)
        self._ai_refresh(key)

    def _ai_uninstall(self, key: str):
        name = self.AI_META[key][1]
        ret = QMessageBox.question(
            self, "卸载 %s 联动" % name,
            "将从 %s 配置摘除 AiLock 的钩子并删除脚本文件。\n"
            "卸载前会自动备份配置，确定继续？" % name,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        bridge = self._ai_bridge(key)
        try:
            report = bridge.uninstall(dry=False)
        except Exception as exc:
            log.log("ai.%s.uninstall" % key, repr(exc))
            self._ai_show_result("卸载失败", "意外错误：%r" % exc,
                                    error=True)
            self._ai_refresh(key)
            return
        self._ai_show_result("卸载完成", report)
        self._ai_refresh(key)

    def _aiButton(self, key: str) -> QPushButton:
        return self._aiBlocks[key].findChild(QPushButton)

    def _ai_refresh(self, key: str):
        """重画某张 AI 卡片状态（安装/卸载后调用）。"""
        page = self._stack.widget(3)
        pl = page.layout()
        blk = self._aiBlocks.pop(key)
        pl.removeWidget(blk)
        blk.setParent(None)
        blk.deleteLater()
        # 在原位（三张卡片按 AI_BRIDGES 顺序排最前面）重建
        self._aiBlocks[key] = self._build_ai_block(page, key)
        pl.insertWidget(self.AI_BRIDGES.index(key), self._aiBlocks[key])

    def _build_ai_block(self, page, key: str) -> CardBlock:
        bridge, name, descInstalled, descMissing, descReady = self.AI_META[key]
        st = bridge.installed_state()
        found = self._ai_found(key, st)
        blk = CardBlock(page)
        if st["configured"] and st["hook_file"]:
            title, desc = "%s 已接入" % name, descInstalled
            btnText, objName = "卸载", "btn"
        elif not found:
            title = "%s 未检测到" % name
            desc = descMissing
            btnText, objName = "一键安装", "btnPrimary"
        else:
            title, desc = "%s 未接入" % name, descReady
            btnText, objName = "一键安装", "btnPrimary"
        btn = QPushButton(btnText)
        btn.setObjectName(objName)
        btn.setFont(T.font(12, QFont.Medium))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(34)
        btn.setFixedWidth(96)
        btn.setEnabled(found)
        btn.clicked.connect(lambda _c=False, k=key: self._ai_toggle(k))
        blk.addRow(CardRow("%s 联动" % name, desc, btn))

        # 状态细目行（只读展示）
        py = st["python"] or "未找到 Python"
        bits = ["钩子脚本：{}".format("就绪" if st["hook_file"] else "未部署"),
                "解释器：{}".format(
                    os.path.basename(py) if py != "未找到 Python" else py)]
        if key == "codex":
            bits.insert(0, "原 notify 备份：{}".format(
                "在" if st.get("sidecar") else "无"))
        lbl = QLabel("   ·   ".join(bits))
        lbl.setObjectName("rowDesc")
        lbl.setFont(T.font(11))
        lbl.setContentsMargins(18, 0, 18, 12)
        blk._lay.addWidget(lbl)
        return blk

    def _ai_show_result(self, title: str, text: str, error: bool = False):
        box = QMessageBox(self)
        box.setWindowTitle(("AiLock 设置" if not title else title))
        box.setIcon(QMessageBox.Critical if error else QMessageBox.Information)
        box.setText(title)
        box.setInformativeText(text)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setFixedWidth(560)
        box.exec()

    # ============================================================ 控件工厂
    def _combo(self, options, current) -> QComboBox:
        cmb = QComboBox()
        cmb.setObjectName("field")
        cmb.setFont(T.font(12))
        for text, val in options:
            cmb.addItem(text, val)
        idx = cmb.findData(current)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)
        return cmb

    def _num_box(self, key, minimum: int, maximum: int, fallback: int,
                 unit: str) -> QWidget:
        """带单位的整数输入框，非法值回落到 fallback。"""
        e = QLineEdit()
        e.setObjectName("field")
        e.setFixedWidth(80)
        e.setFont(T.font(12))
        e.setAlignment(Qt.AlignHCenter)
        e.setValidator(QIntValidator(minimum, maximum, e))
        try:
            cur = int(self.cfg.get(key, fallback))
        except Exception:
            cur = fallback
        if not (minimum <= cur <= maximum):
            cur = fallback
        e.setText(str(cur))
        e.editingFinished.connect(
            lambda: self._commit_num(e, key, minimum, maximum, fallback))

        box = QWidget()
        l = QHBoxLayout(box)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(6)
        l.addWidget(e)
        u = QLabel(unit)
        u.setObjectName("rowDesc")
        u.setFont(T.font(11))
        l.addWidget(u)
        return box

    def _commit_num(self, edit: QLineEdit, key: str, minimum: int,
                    maximum: int, fallback: int):
        raw = edit.text().strip()
        try:
            val = int(raw)
        except Exception:
            val = fallback
        if not (minimum <= val <= maximum):
            val = fallback
        if raw != str(val):
            edit.setText(str(val))
        self._apply(key, val)

    # ============================================================ 写入配置
    def _apply(self, key: str, value):
        try:
            self.cfg.set(key, value)
        except Exception as exc:
            log.log("settings.error", f"{key}: {exc!r}")
            return
        log.log("settings.set", f"{key} = {value}")
        self._notify_saved()

    def _debounced_set(self, key: str, value, ms: int = 350):
        """滑块这类高频改动攒一下再写盘，避免每像素都触发 on_saved。"""
        t = self._debounce.get(key)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            self._debounce[key] = t
        t.stop()
        try:
            t.timeout.disconnect()
        except Exception:
            pass
        t.timeout.connect(lambda k=key, v=value: self._apply(k, v))
        t.start(ms)

    def _notify_saved(self):
        if not self.on_saved:
            return
        try:
            self.on_saved(self.cfg)
        except Exception as exc:
            log.log("settings.saved_cb_error", repr(exc))

    # ============================================================ 小工具
    @staticmethod
    def _feedback(label: QLabel, text: str, state: str = ""):
        label.setText(text)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _set_row_visible(block: CardBlock, row: CardRow, visible: bool):
        """隐藏一行的同时把它上方的分隔线一起隐藏。"""
        lay = block.layout()
        prev = None
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is row:
                if prev is not None:
                    prev.setVisible(visible)
                break
            prev = w
        row.setVisible(visible)

    # ============================================================ 页面切换
    def _switch_page(self, idx: int):
        if not (0 <= idx < len(self._navBtns)):
            return
        for i, b in enumerate(self._navBtns):
            on = (i == idx)
            b.setProperty("active", "true" if on else "false")
            b.style().unpolish(b)
            b.style().polish(b)
        self._stack.setCurrentIndex(idx)
        name, sub, _b = self._pageDefs[idx]
        self.pageTitle.setText(name)
        self.pageSub.setText(sub)

    # ============================================================ 顶部动作
    def _open_log(self):
        try:
            p = log.path
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text("", encoding="utf-8")
            os.startfile(str(p))
        except Exception as exc:
            log.log("settings.open_log.error", repr(exc))

    def _lock_now(self):
        if not self.on_lock_now:
            return
        try:
            self.on_lock_now()
        except Exception as exc:
            log.log("settings.lock_now.error", repr(exc))

    # ============================================================ 生命周期
    def _center(self):
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        g = scr.availableGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def show(self) -> None:
        super().show()
        self.raise_()
        self.activateWindow()

    def close(self) -> None:
        super().close()
        # 兜底：窗口不可见时 Qt 可能不再派发 closeEvent
        self._emit_closed()

    def closeEvent(self, e):
        self._emit_closed()
        super().closeEvent(e)

    def _emit_closed(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.closed.emit()
        except Exception as exc:
            log.log("settings.closed.error", repr(exc))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)
