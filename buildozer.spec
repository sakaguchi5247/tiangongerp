[app]

# ---------------- 基本信息 ----------------
title = 天宫ERP系统
package.name = tiangongerp
package.domain = com.tiangong
version = 1.0.0

# ---------------- 源码 ----------------
# 目录名用英文，避免 Buildozer 在 Linux 下处理中文路径出现编码问题
source.dir = app
# 只打 Python 源码与徽标 PNG；桌面端 tkinter 代码已在本包内剔除
source.include_exts = py,png

# ---------------- 依赖（本项目零第三方依赖） ----------------
# 只要 Python 运行时本身，不引入 Kivy / Flask / 任何库，
# 因此无需任何 recipe、无需交叉编译 C 扩展
requirements = python3

# ---------------- 屏幕 ----------------
# 竖屏（手机开单竖着更好用）；想跟随系统旋转就把它改回 sensor
orientation = portrait
fullscreen = 0

# ---------------- 安卓 ----------------
# INTERNET：WebView 即便只访问本机 localhost，Android 也要求此权限
# WAKE_LOCK：录入单据时保持屏幕常亮，避免中途休眠
android.permissions = INTERNET,WAKE_LOCK
android.wakelock = 1
# 覆盖目前几乎所有在用手机（64 位 + 32 位）
android.archs = arm64-v8a, armeabi-v7a

# ---------------- 界面承载方式 ----------------
# webview：用 Android 原生 WebView 承载界面，不需要 Kivy / SDL2
p4a.bootstrap = webview
# 必须与 app/main.py 里的 PORT 一致，否则 WebView 加载不到页面
p4a.port = 8765

# ---------------- 图标 ----------------
# 复用天宫徽标（128px 宫殿徽标）
icon.filename = %(source.dir)s/erp/ui/assets/icon.png
presplash.filename = %(source.dir)s/erp/ui/assets/icon.png

[buildozer]
log_level = 2
warn_on_root = 1
