# -*- coding: utf-8 -*-
"""天宫ERP 安卓版入口（python-for-android · webview bootstrap）

工作原理：本进程在手机本地起一个 http 服务，APK 自带的 Android WebView
再去加载 http://127.0.0.1:<PORT>。因此界面与网页版完全一致，
业务层（db / services）与桌面版共用同一份代码。

与桌面版的三点差异：
  1. 数据目录指向 app 私有目录（ANDROID_PRIVATE），卸载随应用清除，不需要存储权限
  2. 不弹系统浏览器，界面由 APK 自带 WebView 承载
  3. 关闭市场对接定时同步：单机离线场景拉不到平台订单，避免空跑耗电
"""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# 端口必须与 buildozer.spec 里的 p4a.port 一致，否则 WebView 加载不到页面
PORT = int(os.environ.get("ERP_PORT", "8765"))

# 1) 数据目录：安卓取 app 私有目录；本机调试时落在入口文件旁的 data/
_private = (os.environ.get("ANDROID_PRIVATE")
            or os.environ.get("ANDROID_APP_PATH"))
os.environ["ERP_DATA_DIR"] = str(
    Path(_private) / "data" if _private else _HERE / "data")

sys.path.insert(0, str(_HERE))

from erp import config                            # noqa: E402
config.MARKETPLACE["sync_interval_min"] = 0       # 单机离线：关闭定时拉单

from erp import db                                # noqa: E402
from erp.webui import serve                       # noqa: E402


def main():
    # 首次启动：建表并确保存在管理员账号（db.init_db 内部含 _ensure_admin）
    db.init_db()
    print(f"[ERP] 安卓版本地服务启动：127.0.0.1:{PORT}")
    serve(port=PORT, open_browser=False, host="127.0.0.1")


if __name__ == "__main__":
    main()
