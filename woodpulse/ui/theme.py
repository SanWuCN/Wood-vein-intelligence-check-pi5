"""灰银色工业主题（PRD §4）。

颜色表直接照 PRD §4.1 给定值，不做"更有科技感"的自由发挥：
    外层背景 #D8DADC / 工作面板 #F3F4F4 / 次级面板 #E7E9EA
    主文字 #202427 / 次级文字 #566068 / 分隔线 #B7BDC1
    主按钮 #343A40 / 注意 #A76500 / 故障 #B83B35 / 完成 #34704C

取消蓝色导航、青色描边、发光日志和高饱和边框；不使用金属纹理或大面积渐变
（小屏反光与文字对比度问题）。

尺寸按 800×480 约 5 英寸屏：
    主要触摸按钮高度 64px（≈8.7mm）、次级至少 48px、间隔 8—12px
    正文 18—20px、关键状态 22—26px、辅助文字 16px
    顶部状态条约 44px、主体约 344px、底部操作条约 68px
"""

from __future__ import annotations

from .qt import HAVE_QT, QtCore, QtGui, QtWidgets

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #

BG = "#D8DADC"            # 外层背景，银灰底色
PANEL = "#F3F4F4"         # 工作面板，浅灰阅读区
PANEL_ALT = "#E7E9EA"     # 次级面板，配置与状态分区
INK = "#202427"           # 主文字，深石墨
INK_SOFT = "#566068"      # 次级文字
DIVIDER = "#B7BDC1"       # 分隔线
BUTTON = "#343A40"        # 主按钮
BUTTON_TEXT = "#F3F4F4"
BUTTON_PRESSED = "#20242A"
BUTTON_DISABLED = "#9AA1A6"
AMBER = "#A76500"         # 注意
RED = "#B83B35"           # 故障
GREEN = "#34704C"         # 完成

#: 波形区：深灰背景 + 银白曲线（PRD §4.1）
WAVE_BG = "#2A2E32"
WAVE_GRID = "#414750"
WAVE_CURVE = "#E8EAEC"
WAVE_CURVE_DIM = "#8E969D"
WAVE_MARK = "#C9CED3"

#: 响应序列图色带：灰阶 → 琥珀的连续色带（PRD §4.1），用于幅值映射与数值图例
HEAT_STOPS = (
    (0.00, (42, 46, 50)),
    (0.22, (86, 96, 104)),
    (0.45, (140, 152, 160)),
    (0.62, (186, 190, 190)),
    (0.78, (206, 154, 62)),
    (0.90, (183, 110, 24)),
    (1.00, (167, 101, 0)),
)

#: 状态色（同时用文字与颜色，不靠红绿单独表意，PRD §15）
STATE_COLORS = {
    "ok": GREEN,
    "warn": AMBER,
    "fail": RED,
    "unavailable": INK_SOFT,
    "running": GREEN,
    "paused": AMBER,
    "finished": INK_SOFT,
    "interrupted": RED,
    "idle": INK_SOFT,
    "ready": INK,
    "online": GREEN,
    "offline": RED,
    "connecting": AMBER,
    "degraded": AMBER,
    "done": GREEN,
    "uploading": AMBER,
}

STATE_LABELS = {
    "ok": "正常",
    "warn": "注意",
    "fail": "故障",
    "unavailable": "未接入",
}

# --------------------------------------------------------------------------- #
# 尺寸
# --------------------------------------------------------------------------- #

SCREEN_W = 800
SCREEN_H = 480

HEADER_H = 44            # 顶部状态条
FOOTER_H = 68            # 底部三项大操作
BODY_H = SCREEN_H - HEADER_H - FOOTER_H   # 344 主体（工具条浮在主体内，不占额外高度）

PRIMARY_BUTTON_H = 64
SECONDARY_BUTTON_H = 48
GAP = 10
#: 800×480 下最紧的一页（环境与自检）也要放得下，所以表格类统一用这个最小高度
TABLE_MIN_H = 86

FONT_BODY = 18
FONT_STRONG = 22
FONT_STATUS = 24
FONT_SMALL = 16
#: 面板内密集行（键值对）用的小字号。800×480 的高只有 480px，
#: 一页要塞下十几行键值对，这一档必须比正文小，否则整页放不下。
FONT_TINY = 14
#: 触控安全下限：40px ≈ 5.4mm，是"手指点得准"的最低限度（PRD §5.5 对触控目标的建议）

#: 相机预览约 240px 宽（PRD §4.3 左侧约 240px 放 16:9 预览）
CAMERA_W = 240
CAMERA_H = int(CAMERA_W * 9 / 16)   # 135


def heat_color(value: float):
    """幅值 → 颜色（灰阶到琥珀的连续色带）。"""
    from .qt import QColor

    clamped = max(0.0, min(1.0, float(value)))
    for index in range(len(HEAT_STOPS) - 1):
        low_pos, low_rgb = HEAT_STOPS[index]
        high_pos, high_rgb = HEAT_STOPS[index + 1]
        if low_pos <= clamped <= high_pos:
            span = max(1e-6, high_pos - low_pos)
            ratio = (clamped - low_pos) / span
            rgb = tuple(int(round(low_rgb[i] + (high_rgb[i] - low_rgb[i]) * ratio)) for i in range(3))
            return QColor(*rgb)
    return QColor(*HEAT_STOPS[-1][1])


def state_color(state: str):
    from .qt import QColor

    return QColor(STATE_COLORS.get(state, INK_SOFT))


# --------------------------------------------------------------------------- #
# 样式表
# --------------------------------------------------------------------------- #

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {INK};
    font-family: "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Microsoft YaHei", sans-serif;
    font-size: {FONT_BODY}px;
}}
QFrame#Header {{
    background-color: {PANEL_ALT};
    border-bottom: 1px solid {DIVIDER};
}}
QFrame#Footer {{
    background-color: {PANEL_ALT};
    border-top: 1px solid {DIVIDER};
}}
QFrame#Panel {{
    background-color: {PANEL};
    border: 1px solid {DIVIDER};
    border-radius: 3px;
}}
QFrame#PanelAlt {{
    background-color: {PANEL_ALT};
    border: 1px solid {DIVIDER};
    border-radius: 3px;
}}
QFrame#Panel QLabel, QFrame#PanelAlt QLabel {{
    font-size: {FONT_TINY}px;
    background: transparent;
}}
QLabel#PanelTitle {{
    color: {INK_SOFT};
    font-size: {FONT_TINY}px;
}}
QLabel#Strong {{ font-size: {FONT_SMALL}px; color: {INK}; }}
QLabel#Status {{ font-size: {FONT_STRONG}px; color: {INK}; }}
QLabel#Hint {{ color: {INK_SOFT}; font-size: {FONT_TINY}px; }}
QPushButton {{
    background-color: {BUTTON};
    color: {BUTTON_TEXT};
    border: 1px solid {BUTTON};
    border-radius: 3px;
    padding: 4px 12px;
    font-size: {FONT_SMALL}px;
    min-height: {SECONDARY_BUTTON_H}px;
}}
QPushButton:pressed {{ background-color: {BUTTON_PRESSED}; }}
QPushButton:disabled {{ background-color: {BUTTON_DISABLED}; color: {PANEL}; border-color: {BUTTON_DISABLED}; }}
QPushButton#Primary {{ min-height: {PRIMARY_BUTTON_H}px; font-size: {FONT_STRONG}px; }}
QPushButton#Ghost {{
    background-color: {PANEL};
    color: {INK};
    border: 1px solid {DIVIDER};
}}
QPushButton#Ghost:pressed {{ background-color: {PANEL_ALT}; }}
QPushButton#Ghost:checked {{ background-color: {BUTTON}; color: {BUTTON_TEXT}; }}
QPushButton#Warn {{ background-color: {AMBER}; border-color: {AMBER}; }}
QPushButton#Danger {{ background-color: {RED}; border-color: {RED}; }}
QPushButton#Ok {{ background-color: {GREEN}; border-color: {GREEN}; }}
QPushButton#Tab {{
    background-color: {PANEL_ALT};
    color: {INK_SOFT};
    border: 1px solid {DIVIDER};
    min-height: 34px;
    padding: 2px 10px;
    font-size: {FONT_SMALL}px;
}}
QPushButton#Tab:checked {{ background-color: {BUTTON}; color: {BUTTON_TEXT}; border-color: {BUTTON}; }}
QListWidget, QTableWidget, QTreeWidget, QPlainTextEdit, QTextBrowser {{
    background-color: {PANEL};
    border: 1px solid {DIVIDER};
    selection-background-color: {BUTTON};
    selection-color: {BUTTON_TEXT};
    font-size: {FONT_SMALL}px;
}}
QHeaderView::section {{
    background-color: {PANEL_ALT};
    color: {INK_SOFT};
    border: none;
    border-right: 1px solid {DIVIDER};
    border-bottom: 1px solid {DIVIDER};
    padding: 5px 6px;
    font-size: {FONT_SMALL}px;
}}
QScrollBar:vertical {{ background: {PANEL_ALT}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {DIVIDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: {PANEL_ALT}; height: 12px; }}
QScrollBar::handle:horizontal {{ background: {DIVIDER}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QCheckBox, QRadioButton {{ font-size: {FONT_SMALL}px; spacing: 8px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {PANEL};
    border: 1px solid {DIVIDER};
    border-radius: 3px;
    min-height: {SECONDARY_BUTTON_H - 8}px;
    padding: 2px 8px;
    font-size: {FONT_SMALL}px;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QToolTip {{
    background-color: {INK};
    color: {PANEL};
    border: 1px solid {INK};
    padding: 4px 6px;
    font-size: {FONT_SMALL}px;
}}
"""


def apply_theme(app) -> None:
    """给 QApplication 套主题。同时把字体钉到 18px 起步（小屏可读性）。"""
    if not HAVE_QT:
        return
    app.setStyleSheet(STYLESHEET)
    font = QtGui.QFont()
    for family in ("Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Microsoft YaHei", "DejaVu Sans"):
        font.setFamily(family)
        if QtGui.QFontDatabase().families() and family in QtGui.QFontDatabase().families():
            break
    font.setPixelSize(FONT_BODY)
    app.setFont(font)


def make_button(text: str, *, primary: bool = False, kind: str = "", checkable: bool = False):
    """统一样式的按钮。`kind` 取 ghost / warn / danger / ok / tab。"""
    button = QtWidgets.QPushButton(text)
    if primary:
        button.setObjectName("Primary")
    elif kind:
        button.setObjectName({"ghost": "Ghost", "warn": "Warn", "danger": "Danger", "ok": "Ok", "tab": "Tab"}.get(kind, "Ghost"))
    if checkable:
        button.setCheckable(True)
    button.setMinimumHeight(PRIMARY_BUTTON_H if primary else SECONDARY_BUTTON_H)
    button.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
    return button


def panel(title: str = "", *, alt: bool = False, spacing: int = 4):
    """带标题的工作面板。返回 (frame, body_layout)。

    默认边距与间距压得比较紧：800×480 的主体只有 344px 高，
    面板内多留 4px 就可能把最后一行的触控目标挤到屏幕外。
    """
    frame = QtWidgets.QFrame()
    frame.setObjectName("PanelAlt" if alt else "Panel")
    layout = QtWidgets.QVBoxLayout(frame)
    layout.setContentsMargins(10, 6, 10, 6)
    layout.setSpacing(spacing)
    if title:
        label = QtWidgets.QLabel(title)
        label.setObjectName("PanelTitle")
        label.setFixedHeight(16)
        layout.addWidget(label)
    return frame, layout
