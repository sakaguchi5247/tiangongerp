[app]

# ---------------- 基本信息 ----------------
title = 天宫ERP
package.name = tiangongerp
package.domain = com.tiangong
version = 1.0.0

# ---------------- 源码 ----------------
# 目录名用英文，避免 Buildozer 在 Linux 下处理中文路径出现编码问题
source.dir = app
# 只打 Python 源码与徽标 PNG；桌面端 tkinter 代码已在本包内剔除
source.include_exts = py,png,sql

# ---------------- 依赖（本项目零第三方依赖） ----------------
# 只要 Python 运行时本身，不引入 Kivy / Flask / 任何库，
# 因此无需任何 recipe、无需交叉编译 C 扩展
requirements = python3

# ---------------- 屏幕 ----------------
# 强制竖屏（单手录入单据更顺手；如想横屏看表格可改回 sensor）
orientation = portrait
fullscreen = 0

# ---------------- 安卓 ----------------
# INTERNET：WebView 即便只访问本机 localhost，Android 也要求此权限
# WAKE_LOCK：录入单据时保持屏幕常亮，避免中途休眠
android.permissions = INTERNET,WAKE_LOCK
android.wakelock = 1
# 只编 arm64-v8a 一个架构。
# 为什么：双架构会把 Python 运行时编译两遍，APK 体积 13MB → 21MB，
# 构建时间也翻倍。arm64 覆盖 2018 年之后几乎所有在用手机，
# 也是 Google Play 上架的硬性要求（2019 年起必须提供 64 位版本）。
# 如果确实要照顾 32 位老机型，把下面这行改回：
#     android.archs = arm64-v8a, armeabi-v7a
android.archs = arm64-v8a

# ---------------- 界面承载方式 ----------------
# webview：用 Android 原生 WebView 承载界面，不需要 Kivy / SDL2
p4a.bootstrap = webview
# 必须与 app/main.py 里的 PORT 一致，否则 WebView 加载不到页面
p4a.port = 8765

# ---------------- 图标 ----------------
# 复用天宫徽标（128px 宫殿徽标）
icon.filename = %(source.dir)s/erp/ui/assets/icon.png
presplash.filename = %(source.dir)s/erp/ui/assets/icon.png

# 自适应图标（Android 8.0+ 生效）：透明前景 + 纯黑背景

# 自适应图标（Android 8.0+ 生效）：透明前景 + 纯黑背景
icon.adaptive_foreground.filename = %(source.dir)s/erp/ui/assets/icon_fg.png
icon.adaptive_background.filename = %(source.dir)s/erp/ui/assets/icon_bg.png

p4a.branch = develop


[buildozer]
log_level = 2
warn_on_root = 1
