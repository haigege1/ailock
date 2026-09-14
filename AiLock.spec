# -*- mode: python ; coding: utf-8 -*-
# AiLock 打包配置 —— Qt(PySide6) 专用构建
#
# 冻结 exe 只内置 Qt 界面（默认 AILOCK_UI=qt）。
# 旧 UI（tkinter / pywebview）仅供源码运行调试，全部排除以控制体积：
#   - tkinter / _tkinter / tkinterdnd2 等 tk 全家
#   - webview / pywebview / pythonnet（WebView2 方案）
#   - ui.lock_window / ui.settings_panel / ui.webui（旧界面模块）
# 注意：冻结 exe 里设置 AILOCK_UI=web/tk 会因模块缺失而退出，属预期行为。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # assets：壁纸与图标；integrations/*：一键安装所需的钩子/中转脚本
    #（设置页「一键安装」按钮会把脚本复制到 %APPDATA%/AiLock/hooks/）
    datas=[
        ('assets', 'assets'),
        ('integrations/zcode/ailock_zcode_hook.py',
         'integrations/zcode'),
        ('integrations/workbuddy/ailock_workbuddy_hook.py',
         'integrations/workbuddy'),
        ('integrations/codex/ailock_codex_relay.py',
         'integrations/codex'),
    ],
    hiddenimports=['ailock_run'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 旧 UI：tkinter 方案
        'tkinter', '_tkinter', 'tkinterdnd2', 'ui.lock_window',
        'ui.settings_panel',
        # 旧 UI：WebView2 方案
        'webview', 'pywebview', 'pythonnet', 'clr', 'ui.webui',
        # 用不到的大块头
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebChannel', 'PySide6.QtQml', 'PySide6.QtQuick',
        'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtXml',
        'PySide6.QtDesigner', 'PySide6.QtNetwork',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AiLock',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # Qt DLL 加 UPX 易触发杀软误报，不压缩
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/ailock.ico'],
)
