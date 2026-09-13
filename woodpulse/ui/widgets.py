"""800×480 触摸屏的自绘控件（PRD §3.2、§3.3、§4）。

这里的每个控件都刻意**不用图表库**：
  · 响应序列图是逐列追加的二维强度图，用 QPainter 直接画最快，
    也不引入 pyqtgraph/matplotlib 这类在树莓派上偏重的依赖；
  · 相机预览严格等比缩放，不做 setScaledContents 拉伸（PRD §2、§13 H16）；
  · 曲线与序列图都带"来源标识"，界面上永远能看出这段数据从哪来。

所有控件都只读数据、不改数据 —— 采集与状态由 woodpulse.app 负责。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .qt import HAVE_QT, QtCore, QtGui, QtWidgets
from . import theme


# --------------------------------------------------------------------------- #
# 顶部状态条（PRD §4.3：约 44px，当前柱、测区与平台连接）
# --------------------------------------------------------------------------- #

class StatusStrip(QtWidgets.QFrame if HAVE_QT else object):
    """一行式状态条。左侧是工单/柱/测区，右侧是平台连接与设备健康摘要。

    刻意只放"当前任务相关"的信息：完整 CPU、内存与上传队列在设备状态页
    （PRD §4.2：扫描时首屏保留主要信息，设备信息放细状态条）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Header")
        self.setFixedHeight(theme.HEADER_H)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 10, 2)
        layout.setSpacing(12)

        self.order_label = QtWidgets.QLabel("工单 —")
        self.order_label.setObjectName("Hint")
        self.pillar_label = QtWidgets.QLabel("柱 — / 测区 —")
        self.pillar_label.setObjectName("Strong")
        self.task_label = QtWidgets.QLabel("待开始")
        self.task_label.setObjectName("Status")
        self.source_label = QtWidgets.QLabel("")
        self.source_label.setObjectName("Hint")
        self.link_label = QtWidgets.QLabel("平台 —")
        self.link_label.setObjectName("Strong")
        self.device_label = QtWidgets.QLabel("—")

        layout.addWidget(self.order_label)
        layout.addWidget(self.pillar_label)
        layout.addWidget(self.task_label)
        layout.addWidget(self.source_label)
        layout.addStretch(1)
        layout.addWidget(self.device_label)
        layout.addWidget(self.link_label)
        # 状态条只有 44px：贴顶/贴底会被拉成竖条，这里给所有文字一个确定高度。
        # 同时显式指定字号——面板内的 QLabel 走的是小字号规则，状态条要读得清。
        for label in (
            self.order_label,
            self.pillar_label,
            self.task_label,
            self.source_label,
            self.device_label,
            self.link_label,
        ):
            label.setFixedHeight(26)
            label.setAlignment(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft)
        self.order_label.setStyleSheet(f"color: {theme.INK_SOFT}; font-size: {theme.FONT_TINY}px;")
        self.source_label.setStyleSheet(f"color: {theme.INK_SOFT}; font-size: {theme.FONT_TINY}px;")
        self.device_label.setStyleSheet(f"color: {theme.INK_SOFT}; font-size: {theme.FONT_TINY}px;")
        self.pillar_label.setStyleSheet(f"color: {theme.INK}; font-size: {theme.FONT_SMALL}px;")
        self.task_label.setStyleSheet(f"color: {theme.INK}; font-size: {theme.FONT_STRONG}px;")
        self.link_label.setStyleSheet(f"color: {theme.INK}; font-size: {theme.FONT_SMALL}px;")

    def update_from(self, snapshot: Dict[str, Any]) -> None:
        assignment = (snapshot.get("task") or {}).get("assignment") or {}
        task = snapshot.get("task") or {}
        batch = snapshot.get("batch") or {}
        connection = snapshot.get("connection") or {}
        config = snapshot.get("config") or {}

        order = assignment.get("order_id") or "—"
        component = assignment.get("component_id") or "—"
        zone = assignment.get("zone_id") or "—"
        round_name = {"initial": "初扫", "rescan": "复扫", "reference": "参考样本"}.get(assignment.get("round"), assignment.get("round") or "")
        self.order_label.setText(f"工单 {order}")
        self.pillar_label.setText(f"{component} · {zone}" + (f" · {round_name}" if round_name else ""))

        state = task.get("state", "idle")
        self.task_label.setText(task.get("stateLabel") or state)
        color = theme.STATE_COLORS.get(state, theme.INK)
        self.task_label.setStyleSheet(f"color: {color}; font-size: {theme.FONT_STATUS}px;")

        sources = []
        if batch:
            sources.append("检测样例" if batch.get("radarSourceMode") == "replay" else "雷达实采")
            sources.append("相机实拍" if batch.get("cameraSourceMode") == "live" else "无相机画面")
        if config:
            sources.append(f"配置 {config.get('configVersion') or '未下发'}")
        self.source_label.setText(" · ".join(sources))

        conn_state = connection.get("state", "offline")
        conn_label = connection.get("label") or conn_state
        latency = connection.get("latencyMs")
        text = f"平台 {conn_label}"
        if latency is not None and conn_state == "online":
            text += f" {latency:.0f}ms"
        self.link_label.setText(text)
        self.link_label.setStyleSheet(f"color: {theme.STATE_COLORS.get(conn_state, theme.INK)}; font-size: {theme.FONT_BODY}px;")

        telemetry = snapshot.get("telemetry") or {}
        bits = []
        cpu = telemetry.get("cpuPercent")
        bits.append(f"CPU {cpu:.0f}%" if isinstance(cpu, (int, float)) else "CPU —")
        temp = telemetry.get("socTempC")
        bits.append(f"SoC {temp:.1f}℃" if isinstance(temp, (int, float)) else "SoC —")
        upload = snapshot.get("upload") or {}
        pending = upload.get("pendingBytes")
        if pending:
            from ..telemetry import format_bytes

            bits.append(f"待传 {format_bytes(pending)}")
        self.device_label.setText(" | ".join(bits))
        self.device_label.setObjectName("Hint")


# --------------------------------------------------------------------------- #
# 相机预览（严格等比，不拉伸）
# --------------------------------------------------------------------------- #

class CameraPreview(QtWidgets.QWidget if HAVE_QT else object):
    """16:9 相机预览。画面按比例缩放居中，周围留灰底。

    没有相机时**不画占位假画面**，只显示一行说明：未接入 / 原因。
    """

    def __init__(self, parent=None, *, width: int = theme.CAMERA_W) -> None:
        super().__init__(parent)
        self._image: Optional[Any] = None
        self._frame_info: Dict[str, Any] = {}
        self._message = "相机未接入"
        self._submessage = ""
        self._paused = False
        # 预览按 16:9 给高度，且**不设最小高度**：设了会把左列顶高，
        # 任务摘要就只剩几十像素，文字会叠在一起。
        self.setMinimumWidth(width)
        self.setMaximumHeight(int(width * 9 / 16))
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

    def set_image(self, image, info: Optional[Dict[str, Any]] = None) -> None:
        self._image = image
        if info:
            self._frame_info = dict(info)
        self.update()

    def set_unavailable(self, message: str, submessage: str = "") -> None:
        self._image = None
        self._message = message
        self._submessage = submessage
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(theme.WAVE_BG))

        if self._image is None:
            painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
            font = painter.font()
            font.setPixelSize(theme.FONT_SMALL)
            painter.setFont(font)
            painter.drawText(
                rect.adjusted(8, 8, -8, -8),
                int(QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap),
                self._message + (f"\n{self._submessage}" if self._submessage else ""),
            )
            self._draw_badge(painter, "无画面", theme.INK_SOFT)
            return

        # 等比缩放：只用可用区域里能放下的最大 16:9 或原比例矩形
        source_w = max(1, self._image.width())
        source_h = max(1, self._image.height())
        scale = min(rect.width() / source_w, rect.height() / source_h)
        draw_w = max(1, int(source_w * scale))
        draw_h = max(1, int(source_h * scale))
        target = QtCore.QRect(
            rect.x() + (rect.width() - draw_w) // 2,
            rect.y() + (rect.height() - draw_h) // 2,
            draw_w,
            draw_h,
        )
        painter.drawImage(target, self._image)
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.DIVIDER), 1))
        painter.drawRect(target.adjusted(0, 0, -1, -1))

        if self._paused:
            self._draw_badge(painter, "已暂停（末帧）", theme.AMBER)
        else:
            mode = self._frame_info.get("sourceMode", "live")
            self._draw_badge(painter, "相机实拍" if mode == "live" else "无画面", theme.GREEN if mode == "live" else theme.INK_SOFT)

    def _draw_badge(self, painter, text: str, color: str) -> None:
        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 2)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        height = metrics.height() + 4
        box = QtCore.QRect(6, self.height() - height - 6, width, height)
        painter.fillRect(box, QtGui.QColor(0, 0, 0, 150))
        painter.setPen(QtGui.QColor(color))
        painter.drawText(box, int(QtCore.Qt.AlignCenter), text)


# --------------------------------------------------------------------------- #
# 当前响应曲线（PRD §3.2）
# --------------------------------------------------------------------------- #

class ResponseCurve(QtWidgets.QWidget if HAVE_QT else object):
    """一帧响应曲线。横轴是采样点／频点索引，纵轴是归一化幅值。

    刻度标注"采样点／频点索引（未标定距离轴）"——没有标定距离轴时
    **不把横坐标写成木柱深度毫米值**（PRD §3.2、§9.3）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._values: List[float] = []
        self._peaks: List[Dict[str, Any]] = []
        self._frame_index = -1
        self._source_mode = "replay"
        self._paused = False
        self.setMinimumHeight(96)

    def set_frame(self, values: Sequence[float], *, frame_index: int, peaks: Optional[List[Dict[str, Any]]] = None, source_mode: str = "replay") -> None:
        self._values = list(values)
        self._frame_index = frame_index
        self._peaks = list(peaks or [])
        self._source_mode = source_mode
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(theme.WAVE_BG))
        plot = rect.adjusted(40, 18, -10, -18)

        # 网格与坐标
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.WAVE_GRID), 1, QtCore.Qt.DotLine))
        for row in range(1, 4):
            y = plot.top() + plot.height() * row / 4
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.WAVE_GRID), 1))
        painter.drawRect(plot)

        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 3)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
        painter.drawText(QtCore.QRect(rect.left() + 2, plot.top() - 14, plot.width(), 14), int(QtCore.Qt.AlignLeft), "幅值")
        painter.drawText(
            QtCore.QRect(plot.left(), rect.bottom() - 14, plot.width(), 14),
            int(QtCore.Qt.AlignLeft),
            "采样点／频点索引（未标定距离轴，不表示内部深度）",
        )

        if not self._values:
            painter.drawText(plot, int(QtCore.Qt.AlignCenter), "等待有效响应段")
            self._badge(painter, rect, "无数据", theme.INK_SOFT)
            return

        count = len(self._values)
        peak = max(self._values) or 1.0
        ceiling = max(0.25, min(1.0, (int(peak * 20) + 1) / 20.0))
        painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
        for step in range(5):
            value = ceiling * step / 4
            y = plot.bottom() - plot.height() * (value / ceiling)
            painter.drawText(QtCore.QRect(2, int(y) - 8, 36, 16), int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter), f"{value:.2f}")

        # 曲线：用折线而不是"平滑动画值"，避免把平滑后的曲线当原始值保存（PRD §3.4）
        points = []
        for index, value in enumerate(self._values):
            x = plot.left() + plot.width() * index / max(1, count - 1)
            y = plot.bottom() - plot.height() * min(1.0, max(0.0, value / ceiling))
            points.append(QtCore.QPointF(x, y))
        painter.setPen(QtGui.QPen(QtGui.QColor(theme.WAVE_CURVE), 2))
        painter.drawPolyline(QtGui.QPolygonF(points))

        # 峰值标记
        for marker in self._peaks:
            x = marker.get("normalizedX")
            if x is None:
                sample_index = marker.get("sampleIndex")
                x = (sample_index / max(1, count - 1)) if sample_index is not None else None
            if x is None:
                continue
            px = plot.left() + plot.width() * min(1.0, max(0.0, float(x)))
            painter.setPen(QtGui.QPen(QtGui.QColor(theme.AMBER), 1, QtCore.Qt.DashLine))
            painter.drawLine(int(px), plot.top(), int(px), plot.bottom())
            label = str(marker.get("label") or "")
            if label:
                painter.setPen(QtGui.QColor(theme.AMBER))
                painter.drawText(int(px) + 3, plot.top() + 12, label)

        badge = f"检测样例 · 帧 {self._frame_index}" if self._source_mode == "replay" else f"实采 · 帧 {self._frame_index}"
        if self._paused:
            badge += " · 已暂停"
        self._badge(painter, rect, badge, theme.AMBER if self._paused else theme.WAVE_CURVE_DIM)

    def _badge(self, painter, rect, text: str, color: str) -> None:
        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 3)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(color))
        painter.drawText(QtCore.QRect(rect.right() - 260, rect.top() + 2, 256, 14), int(QtCore.Qt.AlignRight), text)


# --------------------------------------------------------------------------- #
# 响应序列图（PRD §3.2 的核心呈现）
# --------------------------------------------------------------------------- #

class ResponseSequence(QtWidgets.QWidget if HAVE_QT else object):
    """响应序列图：每收到一帧有效回波就追加一列。

    横轴 = 采集帧序号（回放帧序号）
    纵轴 = 采样点／频点索引
    颜色 = 该帧的响应幅值（灰阶 → 琥珀连续色带）

    交互：
      · 点历史列 → 回看对应曲线（`columnClicked` 信号）
      · 标记列画白色竖线并编号
      · 暂停时停止追加（由外部不再喂数据实现），右上角标"已暂停"
    """

    columnClicked = QtCore.pyqtSignal(int) if HAVE_QT else None

    def __init__(self, parent=None, *, max_columns: int = 300, rows: int = 96) -> None:
        super().__init__(parent)
        self._columns: List[Tuple[int, List[float]]] = []
        self._marks: Dict[int, List[int]] = {}
        self._max_columns = max_columns
        self._rows = rows
        self._paused = False
        self._source_mode = "replay"
        self._legend_value = 0.9
        self._hover_column = -1
        #: 显示色带的映射下限/上限。**只影响显示**：落盘与上传始终是原始幅值。
        #: 固定用 0—1 会让基线附近的响应几乎看不出差异，所以按本批数据的
        #: 量级自动取一个显示区间（在图例上写明"显示范围"），而不是给每列
        #: 单独归一化 —— 那会让弱帧看起来和强帧一样，属于误导性可视化。
        self._floor = 0.0
        self._ceiling = 1.0
        self._observed_max = 0.0
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)

    # ---- 数据 ----

    def clear(self) -> None:
        self._columns = []
        self._marks = {}
        self._hover_column = -1
        self._observed_max = 0.0
        self._floor = 0.0
        self._ceiling = 1.0
        self.update()

    def set_display_range(self, floor: float, ceiling: float) -> None:
        """手动指定显示色带区间（例如按样例包的已知量级）。"""
        self._floor = max(0.0, float(floor))
        self._ceiling = max(self._floor + 0.05, float(ceiling))
        self.update()

    def _update_display_range(self, frame_max: float) -> None:
        """按已收到的帧自动定显示区间。

        取"观测到的最大值"作为上限，最低不低于 0.35，避免一开始几帧就把
        色带压扁；下限跟着基线的量级走，这样基线与异常的分界在图上看得见。
        """
        self._observed_max = max(self._observed_max, float(frame_max))
        ceiling = max(0.35, min(1.0, self._observed_max * 1.12))
        self._ceiling = ceiling
        self._floor = min(0.08, ceiling * 0.25)

    def reset_marks(self) -> None:
        self._marks = {}
        self.update()

    def append_frame(self, frame, *, rows: Optional[int] = None) -> None:
        """追加一帧。`frame` 需要有 `frame_index` 与 `downsample(rows)`。"""
        count = rows or self._rows
        values = frame.downsample(count)
        self._columns.append((frame.frame_index, values))
        if values:
            self._update_display_range(max(values))
        if len(self._columns) > self._max_columns:
            del self._columns[: len(self._columns) - self._max_columns]
        self.update()

    def add_mark(self, frame_index: int, ordinal: int) -> None:
        self._marks.setdefault(frame_index, []).append(ordinal)
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def set_source_mode(self, mode: str) -> None:
        self._source_mode = mode
        self.update()

    @property
    def column_count(self) -> int:
        return len(self._columns)

    def column_at(self, x: int) -> int:
        """屏幕 x → 序列图里有几列（返回 -1 表示没有）。"""
        plot_left, plot_width = self._plot_geometry()
        if not self._columns or plot_width <= 0:
            return -1
        if x < plot_left or x > plot_left + plot_width:
            return -1
        ratio = (x - plot_left) / plot_width
        index = int(ratio * len(self._columns))
        return max(0, min(len(self._columns) - 1, index))

    def frame_index_at(self, x: int) -> int:
        index = self.column_at(x)
        if index < 0:
            return -1
        return self._columns[index][0]

    def amplitudes_at(self, x: int) -> List[float]:
        index = self.column_at(x)
        if index < 0:
            return []
        return list(self._columns[index][1])

    # ---- 几何 ----

    def _plot_geometry(self) -> Tuple[int, int]:
        left = 34
        right = self.width() - 10
        return left, max(1, right - left)

    # ---- 事件 ----

    def mousePressEvent(self, event) -> None:  # noqa: N802
        frame_index = self.frame_index_at(int(event.x()))
        if frame_index >= 0 and self.columnClicked is not None:
            self.columnClicked.emit(frame_index)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._hover_column = self.column_at(int(event.x()))
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_column = -1
        self.update()

    # ---- 绘制 ----

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(theme.WAVE_BG))

        plot_left, plot_width = self._plot_geometry()
        # 顶部留 22px 放右上角来源标识；底部留 40px 放图例与横轴说明，
        # 两者必须各占一条带，否则标签会叠在一起（小屏上格外明显）
        plot_top = 22
        bottom_band = 40
        plot_height = max(10, rect.height() - plot_top - bottom_band)

        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 3)
        painter.setFont(font)

        # 纵轴标签（采样点索引）
        painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
        rows = self._rows
        for step in range(3):
            value = rows - 1 - int((rows - 1) * step / 2)
            y = plot_top + plot_height * step / 2
            painter.drawText(QtCore.QRect(0, int(y) - 7, 30, 14), int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter), str(value))
        painter.drawText(QtCore.QRect(2, 4, 46, 12), int(QtCore.Qt.AlignLeft), "采样点")

        if not self._columns:
            painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
            painter.drawText(
                QtCore.QRect(plot_left, plot_top, plot_width, plot_height),
                int(QtCore.Qt.AlignCenter),
                "等待采集：每收到一帧有效响应就在这里追加一列",
            )
        else:
            column_width = plot_width / len(self._columns)
            cell_height = plot_height / max(1, rows)
            span = max(1e-6, self._ceiling - self._floor)
            for column_index, (frame_index, values) in enumerate(self._columns):
                x = plot_left + column_index * column_width
                width = max(1.0, column_width + 0.6)
                for row_index, value in enumerate(values):
                    normalized = (float(value) - self._floor) / span
                    painter.fillRect(
                        QtCore.QRectF(x, plot_top + (rows - 1 - row_index) * cell_height, width, cell_height + 0.6),
                        theme.heat_color(normalized),
                    )

            # 标记线
            for column_index, (frame_index, _values) in enumerate(self._columns):
                ordinals = self._marks.get(frame_index)
                if not ordinals:
                    continue
                x = plot_left + column_index * column_width + column_width / 2
                painter.setPen(QtGui.QPen(QtGui.QColor(theme.WAVE_MARK), 1))
                painter.drawLine(int(x), plot_top, int(x), plot_top + plot_height)
                painter.setPen(QtGui.QColor(theme.INK))
                for offset, ordinal in enumerate(ordinals):
                    painter.drawText(int(x) - 5, plot_top + 11 + offset * 11, f"{ordinal:02d}")

            # 轴框
            painter.setPen(QtGui.QPen(QtGui.QColor(theme.WAVE_GRID), 1))
            painter.drawRect(QtCore.QRect(plot_left, plot_top, plot_width, plot_height))

            # 当前/历史列游标
            if self._hover_column >= 0:
                x = plot_left + self._hover_column * column_width
                painter.setPen(QtGui.QPen(QtGui.QColor(theme.AMBER), 1, QtCore.Qt.DashLine))
                painter.drawLine(int(x), plot_top, int(x), plot_top + plot_height)

        # 横轴标签
        painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
        painter.drawText(
            QtCore.QRect(plot_left, rect.bottom() - 15, plot_width, 14),
            int(QtCore.Qt.AlignLeft),
            "采集帧序号（回放帧序号；时间前进不等于绕柱角度或空间位移）",
        )

        # 右上角：来源标识 + 暂停状态
        badge_parts = ["检测样例" if self._source_mode == "replay" else "雷达实采"]
        if self._columns:
            badge_parts.append(f"{len(self._columns)} 列 / 末帧 {self._columns[-1][0]}")
        if self._paused:
            badge_parts.append("已暂停（停止追加）")
        painter.setPen(QtGui.QColor(theme.AMBER if self._paused else theme.WAVE_CURVE_DIM))
        painter.drawText(
            QtCore.QRect(plot_left, 4, plot_width, 14),
            int(QtCore.Qt.AlignRight),
            " · ".join(badge_parts),
        )

        self._draw_legend(painter, rect)

    def _draw_legend(self, painter, rect) -> None:
        """数值图例：色带 + 显示范围（PRD §4.1 要求响应图提供数值图例）。

        图例标签写的是**显示范围**而不是 0/1：显示色带可能被自动收窄，
        不写清楚会让人以为颜色对应的就是 0—1 的绝对幅值。
        标签放在色带上方的独立一行，横轴说明在最底下，互不遮挡。
        """
        width = 96
        height = 7
        right = rect.right() - 12
        band_top = rect.bottom() - 24
        for index in range(24):
            value = index / 23
            painter.fillRect(
                QtCore.QRectF(right - width + width * index / 24, band_top, width / 24 + 1, height),
                theme.heat_color(value),
            )
        painter.setPen(QtGui.QColor(theme.WAVE_CURVE_DIM))
        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 5)
        painter.setFont(font)
        painter.drawText(QtCore.QRect(right - width - 60, band_top - 12, 56, 12), int(QtCore.Qt.AlignRight), "显示幅值")
        painter.drawText(QtCore.QRect(right - width, band_top - 12, 40, 12), int(QtCore.Qt.AlignLeft), f"{self._floor:.2f}")
        painter.drawText(QtCore.QRect(right - width // 2 - 20, band_top - 12, 40, 12), int(QtCore.Qt.AlignCenter), f"{(self._floor + self._ceiling) / 2:.2f}")
        painter.drawText(QtCore.QRect(right - 34, band_top - 12, 34, 12), int(QtCore.Qt.AlignRight), f"{self._ceiling:.2f}")


# --------------------------------------------------------------------------- #
# 数值图例 / 刻度条
# --------------------------------------------------------------------------- #

class ValueBar(QtWidgets.QWidget if HAVE_QT else object):
    """一条带数值的水平刻度条，用于置信度、进度这类"数值 + 文字"的地方。

    状态同时用文字与颜色（PRD §15：不能只靠红绿）。
    """

    def __init__(self, parent=None, *, label: str = "", height: int = 20) -> None:
        super().__init__(parent)
        self._label = label
        self._value: Optional[float] = None
        self._text = ""
        self._color = theme.INK_SOFT
        self.setFixedHeight(height)

    def set_value(self, value: Optional[float], text: str = "", color: Optional[str] = None) -> None:
        self._value = value
        self._text = text
        self._color = color or theme.INK
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(theme.PANEL_ALT))
        if self._value is not None:
            ratio = max(0.0, min(1.0, float(self._value)))
            painter.fillRect(
                QtCore.QRectF(rect.left(), rect.top(), rect.width() * ratio, rect.height()),
                QtGui.QColor(self._color),
            )
        painter.setPen(QtGui.QColor(theme.INK if self._value is None or self._value < 0.6 else theme.PANEL))
        font = painter.font()
        font.setPixelSize(theme.FONT_SMALL - 2)
        painter.setFont(font)
        text = f"{self._label} {self._text}".strip()
        painter.drawText(rect.adjusted(6, 0, -6, 0), int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft), text)


# --------------------------------------------------------------------------- #
# 标记列表（点某标记回到该位置记录）
# --------------------------------------------------------------------------- #

class MarkList(QtWidgets.QListWidget if HAVE_QT else object):
    """标记列表。每行显示编号、人工方位、帧号与"缺图"状态。

    点一行 → `markSelected(markId, frameIndex)`，界面回到该位置的记录
    （相机截图与同一时刻的样例片段并排显示）。方向没选就写"未选方向"，
    **不猜方位**（PRD §3.3）。
    """

    markSelected = QtCore.pyqtSignal(str, int) if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setUniformItemSizes(True)
        if HAVE_QT:
            self.itemClicked.connect(self._on_item_clicked)

    def refresh(self, marks: List[Dict[str, Any]]) -> None:
        self.clear()
        for mark in marks:
            label = mark.get("operatorLabel") or "未选方向"
            frame_index = mark.get("frameIndex")
            suffix = "" if mark.get("imageOk", True) else " · 缺图"
            item = QtWidgets.QListWidgetItem(f"{mark.get('markId', '')[-2:]}  {label}  帧 {frame_index}{suffix}")
            item.setData(QtCore.Qt.UserRole, (mark.get("markId"), frame_index))
            if not mark.get("imageOk", True):
                item.setForeground(QtGui.QColor(theme.AMBER))
            self.addItem(item)

    def _on_item_clicked(self, item) -> None:
        payload = item.data(QtCore.Qt.UserRole) or ("", -1)
        if self.markSelected is not None:
            self.markSelected.emit(str(payload[0]), int(payload[1] or -1))


# --------------------------------------------------------------------------- #
# 自检行 / 状态行
# --------------------------------------------------------------------------- #

class SelfCheckRow(QtWidgets.QFrame if HAVE_QT else object):
    """自检的一行：状态色块 + 项目名，第二行放详细说明。

    为什么分两行：800×480 里这一列只有约 210px 宽，状态块 + 项目名 + 说明挤在
    一行时说明只剩几十像素，文字会被截成两三个字。分成两行后说明能占满整宽。

    未接入项是灰的，不是绿的 —— 这是 PRD §5.2 的硬要求。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(1)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        self.chip = QtWidgets.QLabel("—")
        self.chip.setFixedWidth(52)
        self.chip.setFixedHeight(16)
        self.chip.setAlignment(QtCore.Qt.AlignCenter)
        self.name = QtWidgets.QLabel("")
        self.name.setStyleSheet(f"color: {theme.INK}; font-size: {theme.FONT_TINY}px;")
        head.addWidget(self.chip)
        head.addWidget(self.name, 1)
        outer.addLayout(head)

        self.detail = QtWidgets.QLabel("")
        self.detail.setObjectName("Hint")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {theme.INK_SOFT}; font-size: {theme.FONT_TINY - 1}px;")
        outer.addWidget(self.detail)

    def update_from(self, item: Dict[str, Any]) -> None:
        state = item.get("state", "unavailable")
        color = theme.STATE_COLORS.get(state, theme.INK_SOFT)
        self.chip.setText(item.get("stateLabel") or theme.STATE_LABELS.get(state, state))
        self.chip.setStyleSheet(
            f"background-color: {color}; color: {theme.PANEL}; border-radius: 2px; "
            f"font-size: {theme.FONT_TINY - 2}px;"
        )
        self.name.setText(item.get("label") or "")
        source = item.get("source") or ""
        detail = item.get("detail") or ""
        self.detail.setText(f"{detail}" + (f"（来源：{source}）" if source else ""))


class LogView(QtWidgets.QWidget if HAVE_QT else object):
    """折叠式日志。默认收起，展开后显示最近的日志（PRD §4.2：日志不能比检测画面更显眼）。"""

    def __init__(self, parent=None, *, max_lines: int = 400) -> None:
        super().__init__(parent)
        self._max_lines = max_lines
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        self.toggle = theme.make_button("日志", kind="ghost")
        self.toggle.setCheckable(True)
        self.toggle.setFixedHeight(24)
        self.toggle.setMinimumHeight(24)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setObjectName("Hint")
        header.addWidget(self.toggle)
        self.count_label.setVisible(False)
        layout.addLayout(header)

        self.body = QtWidgets.QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setMaximumBlockCount(max_lines)
        self.body.setVisible(False)
        layout.addWidget(self.body, 1)
        # 展开时占满底部条的可用高度（68px 减去外边距），不把导航按钮挤变形
        self.setFixedWidth(56)
        self.toggle.toggled.connect(self._on_toggle)

    def _on_toggle(self, checked: bool) -> None:
        self.body.setVisible(checked)
        self.count_label.setVisible(checked)
        self.updateGeometry()

    def append_line(self, line) -> None:
        level = getattr(line, "level", "INFO")
        color = {
            "WARNING": theme.AMBER,
            "ERROR": theme.RED,
            "CRITICAL": theme.RED,
            "DEBUG": theme.INK_SOFT,
        }.get(level, theme.INK)
        source = getattr(line, "source_label", "")
        stamp = getattr(line, "at", "")
        text = getattr(line, "text", "")
        self.body.appendHtml(
            f'<span style="color:{theme.INK_SOFT}">{stamp}</span> '
            f'<span style="color:{theme.INK_SOFT}">[{source}]</span> '
            f'<span style="color:{color}">{_escape(text)}</span>'
        )
        self.count_label.setText(f"{getattr(line, 'seq', 0)} 条")

    def set_lines(self, lines) -> None:
        self.body.clear()
        for line in lines:
            self.append_line(line)


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace(" ", "&nbsp;")
    )
