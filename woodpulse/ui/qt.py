"""Qt 兼容层：同一份界面代码跑在 PyQt5 或 PySide6 上。

为什么要有这一层：
    PRD §10 明确"保留 PyQt5 和现有 QStackedWidget 路线"，树莓派上
    `apt install python3-pyqt5` 也最省事；但开发机上常常只有 PySide6。
    把差异收在这一个文件里，界面代码只 `from .qt import QtWidgets, QtCore, QtGui`，
    两边都能跑，将来整体换绑也只需要改这里。

用法：

    from .qt import QtCore, QtGui, QtWidgets, Signal, Slot, HAVE_QT

`HAVE_QT=False` 时导入本模块不会抛异常，只是把名字设为 None —— 这样
"没有图形环境也要能跑验收脚本"这件事在同一套代码里成立。
"""

from __future__ import annotations

import os
from typing import Any

#: 实际绑定的名字（pyqt5 / pyside6 / None）
BINDING = ""
QT_VERSION = ""
HAVE_QT = False

QtCore: Any = None
QtGui: Any = None
QtWidgets: Any = None
Signal: Any = None
Slot: Any = None
Property: Any = None
pyqtSignal: Any = None
pyqtSlot: Any = None
QPointF: Any = None
QPoint: Any = None
QRectF: Any = None
QSize: Any = None
QColor: Any = None
QFont: Any = None
QPixmap: Any = None
QImage: Any = None
QPainter: Any = None
QPen: Any = None
QBrush: Any = None
QLinearGradient: Any = None
QPolygonF: Any = None

_IMPORT_ERROR = ""


def _try_pyqt5() -> bool:
    global BINDING, QT_VERSION, QtCore, QtGui, QtWidgets, Signal, Slot, Property
    global pyqtSignal, pyqtSlot, QPointF, QPoint, QRectF, QSize, QColor, QFont
    global QPixmap, QImage, QPainter, QPen, QBrush, QLinearGradient, QPolygonF
    try:
        from PyQt5 import QtCore as _QtCore, QtGui as _QtGui, QtWidgets as _QtWidgets
    except Exception:  # noqa: BLE001
        return False
    BINDING = "PyQt5"
    QT_VERSION = getattr(_QtCore, "QT_VERSION_STR", "")
    QtCore, QtGui, QtWidgets = _QtCore, _QtGui, _QtWidgets
    Signal = _QtCore.pyqtSignal
    Slot = _QtCore.pyqtSlot
    Property = _QtCore.pyqtProperty
    pyqtSignal, pyqtSlot = _QtCore.pyqtSignal, _QtCore.pyqtSlot
    QPointF, QPoint, QRectF, QSize = _QtCore.QPointF, _QtCore.QPoint, _QtCore.QRectF, _QtCore.QSize
    QColor, QFont, QPixmap, QImage, QPainter = _QtGui.QColor, _QtGui.QFont, _QtGui.QPixmap, _QtGui.QImage, _QtGui.QPainter
    QPen, QBrush, QLinearGradient, QPolygonF = _QtGui.QPen, _QtGui.QBrush, _QtGui.QLinearGradient, _QtGui.QPolygonF
    return True


def _try_pyside6() -> bool:
    global BINDING, QT_VERSION, QtCore, QtGui, QtWidgets, Signal, Slot, Property
    global pyqtSignal, pyqtSlot, QPointF, QPoint, QRectF, QSize, QColor, QFont
    global QPixmap, QImage, QPainter, QPen, QBrush, QLinearGradient, QPolygonF
    try:
        from PySide6 import QtCore as _QtCore, QtGui as _QtGui, QtWidgets as _QtWidgets
    except Exception:  # noqa: BLE001
        return False
    BINDING = "PySide6"
    QT_VERSION = getattr(_QtCore, "__version__", "")
    QtCore, QtGui, QtWidgets = _QtCore, _QtGui, _QtWidgets
    Signal = _QtCore.Signal
    Slot = _QtCore.Slot
    Property = _QtCore.Property
    # PySide 的名字不同，界面代码里统一用 Signal/Slot
    pyqtSignal, pyqtSlot = _QtCore.Signal, _QtCore.Slot
    QPointF, QPoint, QRectF, QSize = _QtCore.QPointF, _QtCore.QPoint, _QtCore.QRectF, _QtCore.QSize
    QColor, QFont, QPixmap, QImage, QPainter = _QtGui.QColor, _QtGui.QFont, _QtGui.QPixmap, _QtGui.QImage, _QtGui.QPainter
    QPen, QBrush, QLinearGradient, QPolygonF = _QtGui.QPen, _QtGui.QBrush, _QtGui.QLinearGradient, _QtGui.QPolygonF
    return True


def load_qt(prefer: str = "") -> bool:
    """绑定 Qt。返回是否成功。

    `prefer` 可取 "pyqt5" / "pyside6"；留空则按 环境变量 → PyQt5 → PySide6 顺序尝试。
    树莓派与 PRD 都以 PyQt5 为准，所以默认先试 PyQt5。
    """
    global HAVE_QT, _IMPORT_ERROR
    order = []
    env_pref = (prefer or os.environ.get("WOODPULSE_QT_BINDING", "")).strip().lower()
    if env_pref in ("pyqt5", "pyside6"):
        order.append(env_pref)
    order += ["pyqt5", "pyside6"]
    seen = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        ok = _try_pyqt5() if name == "pyqt5" else _try_pyside6()
        if ok:
            HAVE_QT = True
            _IMPORT_ERROR = ""
            return True
    _IMPORT_ERROR = "未找到 PyQt5 或 PySide6（树莓派：sudo apt install python3-pyqt5）"
    HAVE_QT = False
    return False


load_qt()


def require_qt() -> None:
    """界面入口调用：没有 Qt 就给出可执行的安装提示，而不是一堆 ImportError。"""
    if not HAVE_QT:
        raise RuntimeError(
            "没有可用的 Qt 绑定。\n"
            "  · 树莓派：sudo apt install -y python3-pyqt5 python3-pyqt5.qtsvg\n"
            "  · 开发机：pip install PyQt5\n"
            "  · 若已装 PySide6，会自动使用它。\n"
            f"详细错误：{_IMPORT_ERROR}"
        )


def binding_info() -> dict:
    return {"binding": BINDING or None, "qtVersion": QT_VERSION or None, "available": HAVE_QT, "error": _IMPORT_ERROR}


# --------------------------------------------------------------------------- #
# 绑定差异的补齐
# --------------------------------------------------------------------------- #

if HAVE_QT:

    def exec_dialog(dialog) -> int:
        """PyQt5 用 exec_()，Qt6 用 exec()。"""
        runner = getattr(dialog, "exec", None)
        if runner is not None:
            return runner()
        return dialog.exec_()

    def flush_events() -> None:
        QtWidgets.QApplication.processEvents()

    def high_dpi_setup() -> None:
        """高分屏属性必须在 QApplication 之前设置（PyQt5 才有这一段）。"""
        if BINDING == "PyQt5":
            QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
            QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    def fixed_size(widget, width: int, height: int) -> None:
        widget.setFixedSize(width, height)

    def align_flag(name: str):
        """`Qt.AlignCenter` 这类枚举在 PyQt5/PySide6 下的兼容取值。"""
        return getattr(QtCore.Qt, name)

    def image_from_bgr(data: bytes, width: int, height: int):
        """BGR 字节 → QImage。

        注意：QImage 不持有这块 Python 内存的所有权，所以这里**必须 copy()**，
        否则一旦 bytes 被回收，界面就会画出花屏（旧版跨线程传 QImage 的坑）。
        返回的 QImage 是深拷贝，可以安全跨线程传递。
        """
        image = QImage(data, width, height, width * 3, QImage.Format_BGR888)
        return image.copy()

else:

    def exec_dialog(dialog) -> int:  # type: ignore[no-redef]
        raise RuntimeError("Qt 不可用")

    def flush_events() -> None:  # type: ignore[no-redef]
        return None

    def high_dpi_setup() -> None:  # type: ignore[no-redef]
        return None

    def fixed_size(widget, width: int, height: int) -> None:  # type: ignore[no-redef]
        return None

    def align_flag(name: str):  # type: ignore[no-redef]
        raise RuntimeError("Qt 不可用")

    def image_from_bgr(data: bytes, width: int, height: int):  # type: ignore[no-redef]
        raise RuntimeError("Qt 不可用")
