"""AiLock · Qt (PySide6) 界面层

锁屏与设置界面用原生 Qt 渲染，不依赖 WebView2 / 任何浏览器运行时。
后端（配置 / 口令 / 电源 / 看门狗 / 状态监控）完全复用 core/。
"""

__all__ = ["theme", "widgets", "wallpaper", "lock_window"]
