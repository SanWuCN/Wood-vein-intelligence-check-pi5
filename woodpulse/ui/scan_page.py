"""检测作业页 —— 800×480 首屏（PRD §4.3、§3.2、§3.3）。

首屏结构严格照 PRD §4.3：
    顶部 44px  当前柱、测区与平台连接          （在 Shell 里）
    主体 344px 左侧 240px 相机预览 + 任务摘要；右侧 524px 响应序列图
    底部 68px  开始／暂停、标记位置、结束三项大操作

呈现口径（PRD §3.2）：
  · 相机预览 = 相机实拍，标"相机实拍"，表面画面不代表内部结构；
  · 序列图 = 检测样例（replay），标"检测样例"，时间前进不等于绕柱角度或空间位移；
  · 测区进度只写"已完成测区数 / 已保存帧数 / 标记数量"，**不显示按时间估算的绕柱百分比**；
  · 异常候选与指定帧、测区、版本关联，不随运行时间随机提高置信度。

交互口径（PRD §3.3）：标记**不暂停**当前任务，一键保存；方向可选，没选就不猜。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..contracts import OperationLabel, TaskState
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets
from .widgets import CameraPreview, MarkList, ResponseCurve, ResponseSequence

ROUND_LABELS = {"initial": "初扫", "rescan": "复扫", "reference": "参考样本采集"}


class ScanPage(QtWidgets.QWidget if HAVE_QT else object):
    """检测作业主页面。"""

    #: 请求开始 / 暂停 / 继续 / 结束 / 标记（由 Shell 接上 app）
    startRequested = QtCore.pyqtSignal() if HAVE_QT else None
    pauseRequested = QtCore.pyqtSignal() if HAVE_QT else None
    resumeRequested = QtCore.pyqtSignal() if HAVE_QT else None
    finishRequested = QtCore.pyqtSignal() if HAVE_QT else None
    markRequested = QtCore.pyqtSignal(str) if HAVE_QT else None
    prepareRequested = QtCore.pyqtSignal(str, str) if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._marks_cache: List[Dict[str, Any]] = []
        self._frame_index = -1
        self._last_clicked_column = -1
        #: 已经画进序列图的帧号；批次开始时清空（H06：不同批次无残留数据）
        self._sequence_seen: set = set()
        #: 帧号 → 帧对象，用于点历史列回看曲线
        self._frames_by_index: Dict[int, Any] = {}

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 0)
        root.setSpacing(4)

        # ---- 主体：左（相机 + 任务摘要） / 右（响应图） ----
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)

        # ---------------- 左：相机 + 任务摘要 ----------------
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        camera_panel, camera_layout = theme.panel("相机实拍（表面画面，不代表内部结构）")
        # 预览高度取 16:9 上限的 90%：给任务摘要留出 5 行文本的位置。
        # 800×480 上左列总高约 330px，这两块必须一起算，不能各自"尽量大"。
        preview_height = int(theme.CAMERA_H * 0.9)
        self.camera_preview = CameraPreview()
        self.camera_preview.setFixedHeight(preview_height)
        camera_layout.addWidget(self.camera_preview)
        camera_panel.setFixedWidth(theme.CAMERA_W + 22)
        camera_panel.setFixedHeight(preview_height + 28)
        # 相机预览固定高度不参与拉伸，多余高度留给任务摘要
        left.addWidget(camera_panel, 0)

        summary_panel, summary_layout = theme.panel("任务摘要")
        self.summary_panel = summary_panel
        # 用一行"标签 值"的富文本代替 8 行网格：800×480 上左列只有约 150px 高，
        # 8 行键值对会超出可用高度，Qt 会把行压到一起导致文字重叠。
        self.summary_label = QtWidgets.QLabel("—")
        self.summary_label.setTextFormat(QtCore.Qt.RichText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
        self.summary_label.setStyleSheet(
            f"font-size: {theme.FONT_TINY}px; color: {theme.INK}; line-height: 108%;"
        )
        summary_layout.addWidget(self.summary_label, 1)
        self.frozen_label = QtWidgets.QLabel("")
        self.frozen_label.setWordWrap(True)
        self.frozen_label.setStyleSheet(f"color: {theme.RED}; font-size: {theme.FONT_TINY}px;")
        summary_layout.addWidget(self.frozen_label)
        left.addWidget(summary_panel, 1)

        body.addLayout(left)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(4)
        tabs = QtWidgets.QHBoxLayout()
        tabs.setSpacing(6)
        self.tab_sequence = theme.make_button("响应序列图", kind="tab", checkable=True)
        self.tab_curve = theme.make_button("当前曲线", kind="tab", checkable=True)
        self.tab_marks = theme.make_button("标记列表", kind="tab", checkable=True)
        self.tab_sequence.setChecked(True)
        for button in (self.tab_sequence, self.tab_curve, self.tab_marks):
            button.setMinimumHeight(34)
            tabs.addWidget(button)
        tabs.addStretch(1)
        self.progress_label = QtWidgets.QLabel("0 帧")
        self.progress_label.setObjectName("Hint")
        tabs.addWidget(self.progress_label)
        right.addLayout(tabs)

        self.stack = QtWidgets.QStackedWidget()
        self.sequence = ResponseSequence()
        self.stack.addWidget(self.sequence)

        curve_wrap = QtWidgets.QWidget()
        curve_layout = QtWidgets.QVBoxLayout(curve_wrap)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        curve_layout.setSpacing(4)
        self.curve = ResponseCurve()
        curve_layout.addWidget(self.curve, 1)
        self.curve_note = QtWidgets.QLabel("")
        self.curve_note.setObjectName("Hint")
        self.curve_note.setWordWrap(True)
        curve_layout.addWidget(self.curve_note)
        self.stack.addWidget(curve_wrap)

        marks_wrap = QtWidgets.QWidget()
        marks_layout = QtWidgets.QVBoxLayout(marks_wrap)
        marks_layout.setContentsMargins(0, 0, 0, 0)
        marks_layout.setSpacing(4)
        self.mark_list = MarkList()
        self.mark_list.setMinimumHeight(120)
        marks_layout.addWidget(self.mark_list, 1)
        self.mark_hint = QtWidgets.QLabel("点某条标记可回到该位置记录（相机截图与同一时刻的样例片段并排显示）")
        self.mark_hint.setObjectName("Hint")
        self.mark_hint.setWordWrap(True)
        marks_layout.addWidget(self.mark_hint)
        self.stack.addWidget(marks_wrap)

        right.addWidget(self.stack, 1)

        # 三个页签互斥
        self.tab_sequence.clicked.connect(lambda: self._select_tab(0))
        self.tab_curve.clicked.connect(lambda: self._select_tab(1))
        self.tab_marks.clicked.connect(lambda: self._select_tab(2))
        self.sequence.columnClicked.connect(self._on_column_clicked)
        self.mark_list.markSelected.connect(self._on_mark_selected)

        body.addLayout(right, 1)
        root.addLayout(body, 1)

        # ---- 底部三项大操作 ----
        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 4)
        footer.setSpacing(theme.GAP)
        self.btn_start = theme.make_button("▶ 开始本次扫描", primary=True)
        self.btn_pause = theme.make_button("⏸ 暂停", primary=True, kind="warn")
        self.btn_mark = theme.make_button("⚑ 标记位置", primary=True)
        self.btn_finish = theme.make_button("■ 结束并交付", primary=True, kind="danger")
        self.btn_start.clicked.connect(lambda: self.startRequested.emit() if self.startRequested else None)
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_mark.clicked.connect(lambda: self.markRequested.emit(self._current_label) if self.markRequested else None)
        self.btn_finish.clicked.connect(lambda: self.finishRequested.emit() if self.finishRequested else None)

        # 方向选择：默认"未选方向"，一键标记不被打断（PRD §3.3）
        self.label_selector = QtWidgets.QComboBox()
        self.label_selector.addItems([OperationLabel.LABEL[value] for value in OperationLabel.CHOICES])
        self.label_selector.setFixedHeight(theme.PRIMARY_BUTTON_H)
        self.label_selector.setFixedWidth(120)
        self._current_label = ""
        self.label_selector.currentIndexChanged.connect(self._on_label_changed)

        footer.addWidget(self.btn_start, 2)
        footer.addWidget(self.btn_pause, 1)
        footer.addWidget(self.label_selector, 1)
        footer.addWidget(self.btn_mark, 2)
        footer.addWidget(self.btn_finish, 2)
        root.addLayout(footer)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def _select_tab(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.tab_sequence.setChecked(index == 0)
        self.tab_curve.setChecked(index == 1)
        self.tab_marks.setChecked(index == 2)

    def _on_pause_clicked(self) -> None:
        if self.btn_pause.text().startswith("⏸"):
            if self.pauseRequested:
                self.pauseRequested.emit()
        else:
            if self.resumeRequested:
                self.resumeRequested.emit()

    def _on_label_changed(self, index: int) -> None:
        self._current_label = OperationLabel.CHOICES[index] if 0 <= index < len(OperationLabel.CHOICES) else ""

    def _on_column_clicked(self, frame_index: int) -> None:
        """点历史列 → 回看对应曲线与（有的话）标记。"""
        self._last_clicked_column = frame_index
        info = self._frames_by_index.get(frame_index) if hasattr(self, "_frames_by_index") else None
        if info is not None:
            self.curve.set_frame(info.amplitudes, frame_index=info.frame_index, peaks=self._peaks_for(frame_index))
        self._select_tab(1)

    def _on_mark_selected(self, mark_id: str, frame_index: int) -> None:
        info = self._frames_by_index.get(frame_index) if hasattr(self, "_frames_by_index") else None
        if info is not None:
            self.curve.set_frame(info.amplitudes, frame_index=info.frame_index, peaks=self._peaks_for(frame_index))
            self._select_tab(1)
            self.curve_note.setText(f"正在回看 {mark_id} 所在位置：样例帧 {frame_index}")
        else:
            self.curve_note.setText(
                f"{mark_id} 指向样例帧 {frame_index}，该帧已不在显示窗口内（显示窗口只保留最近若干列）"
            )
            self._select_tab(1)

    def _peaks_for(self, frame_index: int) -> List[Dict[str, Any]]:
        for mark in self._marks_cache:
            if mark.get("frameIndex") == frame_index:
                return [{"normalizedX": None, "label": mark.get("operatorLabel") or "标记"}]
        return []

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #

    def set_frames(self, frames: List[Any], *, paused: bool) -> None:
        """把新到的帧追加到序列图并更新当前曲线与进度。

        `frames` 是采集控制器环形缓冲里的帧对象（含 `downsample` 与 `segment`）。
        每帧只会被追加一次（用 `_sequence_seen` 去重），所以重复调用是安全的。
        """
        self._frames_by_index = {frame.frame_index: frame for frame in frames}
        limit = self.sequence._max_columns
        for frame in frames[-limit:]:
            if frame.frame_index in self._sequence_seen:
                continue
            self._sequence_seen.add(frame.frame_index)
            self.sequence.append_frame(frame)
        self.sequence.set_paused(paused)
        if frames:
            latest = frames[-1]
            if self.stack.currentIndex() != 1 or self._last_clicked_column < 0:
                peaks = latest.segment.get("peaks") if latest.segment else None
                self.curve.set_frame(latest.amplitudes, frame_index=latest.frame_index, peaks=peaks)
            self._frame_index = latest.frame_index
            self.progress_label.setText(f"{len(frames)} 帧 · 末帧 {latest.frame_index}")
        self.curve.set_paused(paused)

    def begin_batch(self, batch: Dict[str, Any]) -> None:
        """新批次开始：清空序列图与标记（H06：不同批次无残留数据）。"""
        self._sequence_seen = set()
        self._frames_by_index = {}
        self.sequence.clear()
        self.mark_list.clear()
        self._marks_cache = []
        self.curve.set_frame([], frame_index=-1)
        self.curve_note.setText("")
        self.progress_label.setText("0 帧")
        self.frozen_label.setText("")
        self._last_clicked_column = -1
        self.sequence.set_source_mode(batch.get("radarSourceMode", "replay"))

    def add_mark(self, mark: Dict[str, Any]) -> None:
        self._marks_cache.append(mark)
        ordinal = len(self._marks_cache)
        self.sequence.add_mark(int(mark.get("frameIndex") or -1), ordinal)
        self.mark_list.refresh(self._marks_cache)

    def update_summary(self, snapshot: Dict[str, Any]) -> None:
        assignment = (snapshot.get("task") or {}).get("assignment") or {}
        batch = snapshot.get("batch") or {}
        muted = theme.INK_SOFT
        strong = theme.INK

        def row(label: str, value: str, color: str = "") -> str:
            return (
                f'<span style="color:{muted}">{label}</span> '
                f'<span style="color:{color or strong}">{value}</span>'
            )

        dataset = str(batch.get("datasetHash") or "")
        sample_hash = dataset[:10] + "…" if dataset else "未封存"
        title = self.summary_panel.findChild(QtWidgets.QLabel, "PanelTitle")
        if title is not None:
            title.setText(f"任务摘要 · 样例 {sample_hash}")
        # 4 行：左列总高有限，5 行会被截断最后一行。样例标识放在面板标题里，
        # 既不丢信息，也不跟这几行抢高度。
        self.summary_panel.setToolTip(
            f"样例包 datasetHash：{dataset or '（样例包未封存）'}\n"
            f"轮次：{ROUND_LABELS.get(assignment.get('round'), assignment.get('round') or '—')}"
        )
        lines = [
            row("工单", str(assignment.get("order_id") or "—")),
            row(
                "批次",
                f"{batch.get('batchId') or '尚未建立'} · {ROUND_LABELS.get(assignment.get('round'), '')}",
            ),
            row(
                "配置/模型",
                f"{batch.get('configVersion') or (snapshot.get('config') or {}).get('configVersion') or '—'}"
                f" / {batch.get('modelVersion') or snapshot.get('modelVersion') or '—'}",
            ),
            row(
                "帧 / 标记",
                f"{batch.get('returnedFrames', 0)}/{batch.get('frameCount', 0)}"
                f" · {batch.get('markCount', 0)} 个",
                theme.AMBER if batch.get("returnedFrames") else strong,
            ),
        ]
        self.summary_label.setText("<br/>".join(lines))

        if batch.get("diagnosisFrozen"):
            self.frozen_label.setText(f"诊断输出已冻结：{batch.get('freezeReason') or '适用域待核验'}")
        elif batch.get("interruptReason"):
            self.frozen_label.setText(f"批次中断：{batch.get('interruptReason')}")
        else:
            self.frozen_label.setText("")

    def update_buttons(self, task_state: str, *, camera_live: bool) -> None:
        running = task_state == TaskState.RUNNING
        paused = task_state == TaskState.PAUSED
        terminal = task_state in TaskState.TERMINAL
        ready = task_state in (TaskState.IDLE, TaskState.READY)
        self.btn_start.setEnabled(ready)
        self.btn_pause.setEnabled(running or paused)
        self.btn_pause.setText("▶ 继续" if paused else "⏸ 暂停")
        self.btn_mark.setEnabled(running or paused)
        self.btn_finish.setEnabled(running or paused)
        # 没有相机不是致命问题：标记会记录"缺图"，但按钮仍要可点（PRD §9）
        self.btn_mark.setToolTip(
            "保存当前相机截图与样例帧号" if camera_live else "当前无相机画面：标记会保留但标注“缺图”"
        )

    def set_camera_unavailable(self, reason: str) -> None:
        self.camera_preview.set_unavailable("相机画面不可用", reason)

    def set_camera_image(self, image, *, paused: bool, info: Optional[Dict[str, Any]] = None) -> None:
        self.camera_preview.set_paused(paused)
        self.camera_preview.set_image(image, info)
