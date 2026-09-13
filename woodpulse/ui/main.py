"""界面入口：创建 QApplication、套灰银主题、起主窗口。

实机（树莓派触摸屏）推荐用 `--fullscreen`；开发机上用 `--no-fullscreen`
开一个 800×480 的窗口，效果与实机一致（PRD §4.2）。
"""

from __future__ import annotations

import signal
import sys
from typing import Optional

from ..app import WoodPulseApp
from ..logging_setup import get_logger
from .qt import HAVE_QT, QtCore, QtWidgets, binding_info, high_dpi_setup, require_qt
from .theme import apply_theme

log = get_logger("ui")


def run_gui(app: WoodPulseApp, argv: Optional[list] = None) -> int:
    """起界面并进入事件循环；返回进程退出码。"""
    require_qt()
    # 高分屏属性必须在 QApplication 构造前设置
    high_dpi_setup()
    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or sys.argv)
    apply_theme(qt_app)
    info = binding_info()
    log.info("Qt 绑定：%s %s", info["binding"], info["qtVersion"])

    from .shell import Shell

    shell = Shell(app)

    # Ctrl+C 能优雅退出（实机上多是 systemd 停服务，也会走到同一个收尾流程）
    signal.signal(signal.SIGINT, lambda *_: qt_app.quit())
    # 让 Python 有机会处理信号：Qt 事件循环默认不让解释器跑信号处理
    keepalive = QtCore.QTimer()
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)

    handshake = app.start()
    if not handshake.get("ok"):
        log.warning("首次握手未成功：%s（终端仍可离线采集，恢复后自动补传）", handshake.get("detail"))

    if app.cfg.self_check_on_start:
        # 启动自检放到界面起来之后跑，避免开机黑屏等待
        QtCore.QTimer.singleShot(400, shell._on_self_check)
    if app.recovered_batches:
        QtCore.QTimer.singleShot(700, lambda: shell._toast(
            "恢复中断批次",
            "上次未正常退出，已恢复为中断态：" + "、".join(item.batch_id for item in app.recovered_batches),
        ))

    try:
        code = qt_app.exec_() if hasattr(qt_app, "exec_") else qt_app.exec()
    finally:
        app.shutdown("界面退出")
    return int(code or 0)


def headless_report(app: WoodPulseApp) -> int:
    """没有图形环境时的降级：打印状态并给出安装提示。

    这样"程序能不能跑"与"有没有屏幕"解耦：CI 与预检可以直接跑这一段。
    """
    from .qt import binding_info

    info = binding_info()
    print("=" * 68)
    print("木脉智检手持终端 · 无图形环境模式")
    print("=" * 68)
    print(f"Qt 绑定：{info['binding'] or '未找到'}")
    print(f"原因：{info['error']}")
    if info["binding"] is None:
        print("安装：树莓派 sudo apt install -y python3-pyqt5 python3-psutil python3-opencv")
        print("      开发机 pip install PyQt5")
    status = app.status_snapshot()
    print(f"设备：{status['deviceId']}（启动 ID {status['bootId']}）")
    print(f"能力：{status['capabilities']}")
    print(f"任务：{status['task']['stateLabel']}")
    packages = app.library.status()
    print(f"样例包：{packages['ready']}/{packages['total']} 就绪（{packages['root']}）")
    print(f"数据目录：{app.cfg.data_path}")
    print("=" * 68)
    app.shutdown("无图形环境，直接退出")
    return 0 if info["binding"] else 2
