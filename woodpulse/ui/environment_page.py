"""环境配置与自检页（PRD §5.2、§4.1）。

这一页要同时回答三件事：
  1. 平台发布了什么配置（温度、湿度、风速、配置版本、来源、发布时间）；
  2. 新配置与上一版**差在哪里**，操作者确认后才生效并回传 ack；
  3. 逐项自检结果 —— 未接入项显示"未接入"，**不全绿**。

温度分栏的用意（PRD §5.2）：树莓派 SoC 温度不能代替木材周围环境温度，
所以这里把"环境温度（平台仪表）"和"SoC 温度（本机传感器）"分成两栏显示，
并分别标注来源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..contracts import ConfigState
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets
from .widgets import SelfCheckRow


class EnvironmentPage(QtWidgets.QWidget if HAVE_QT else object):
    """环境配置 + 自检。左侧配置差异（宽），右侧自检清单（窄）。"""

    confirmRequested = QtCore.pyqtSignal() if HAVE_QT else None
    selfCheckRequested = QtCore.pyqtSignal() if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[SelfCheckRow] = []
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(8)

        # ---------------- 左：环境配置 ----------------
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        env_panel, env_layout = theme.panel("环境记录（平台发布 / 来源与发布时间）")
        self.env_grid = QtWidgets.QGridLayout()
        self.env_grid.setHorizontalSpacing(10)
        self.env_grid.setVerticalSpacing(3)
        self._env_labels: Dict[str, QtWidgets.QLabel] = {}
        # 5 行以内：左侧一栏总高约 300px，行数再多网格就会超出可用高度、
        # Qt 把行压到一起造成文字重叠（800×480 上很显眼）。长信息放 Tooltip。
        env_rows = [
            ("configVersion", "配置版本"),
            ("source", "来源与时间"),
            ("airTempC", "环境温度"),
            ("relativeHumidityPct", "相对湿度"),
            ("socTempC", "SoC 温度"),
        ]
        for index, (key, label) in enumerate(env_rows):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            name.setMinimumWidth(74)
            name.setMaximumWidth(74)
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.env_grid.addWidget(name, index, 0)
            self.env_grid.addWidget(value, index, 1)
            self._env_labels[key] = value
        self.env_grid.setColumnStretch(1, 1)
        env_layout.addLayout(self.env_grid)
        self.hh_label = QtWidgets.QLabel("")
        self.hh_label.setObjectName("Hint")
        self.hh_label.setWordWrap(True)
        env_layout.addWidget(self.hh_label)
        left.addWidget(env_panel)

        diff_panel, diff_layout = theme.panel("与上一版的差异（确认后才保存快照并回传 ack）")
        self.diff_table = QtWidgets.QTableWidget(0, 4)
        self.diff_table.setHorizontalHeaderLabels(["字段", "原值", "新值", "说明"])
        self.diff_table.verticalHeader().setVisible(False)
        self.diff_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.diff_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        header = self.diff_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.diff_table.setMinimumHeight(theme.TABLE_MIN_H)
        diff_layout.addWidget(self.diff_table, 1)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(6)
        self.confirm_button = theme.make_button("确认差异并生效", kind="ok")
        self.confirm_button.clicked.connect(lambda: self.confirmRequested.emit() if self.confirmRequested else None)
        self.state_label = QtWidgets.QLabel("等待平台下发配置")
        self.state_label.setObjectName("Hint")
        actions.addWidget(self.confirm_button)
        actions.addWidget(self.state_label, 1)
        diff_layout.addLayout(actions)
        left.addWidget(diff_panel, 1)
        columns.addLayout(left, 2)

        # ---------------- 右：自检 ----------------
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        check_panel, check_layout = theme.panel("开机自检（未接入项显示未接入，不全绿）")
        self.check_scroll = QtWidgets.QScrollArea()
        self.check_scroll.setWidgetResizable(True)
        self.check_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.check_host = QtWidgets.QWidget()
        self.check_layout = QtWidgets.QVBoxLayout(self.check_host)
        self.check_layout.setContentsMargins(0, 0, 0, 0)
        self.check_layout.setSpacing(3)
        self.check_layout.addStretch(1)
        self.check_scroll.setWidget(self.check_host)
        check_layout.addWidget(self.check_scroll, 1)
        self.summary_label = QtWidgets.QLabel("尚未自检")
        self.summary_label.setObjectName("Hint")
        self.summary_label.setWordWrap(True)
        check_layout.addWidget(self.summary_label)
        self.run_button = theme.make_button("重新自检", kind="ghost")
        self.run_button.clicked.connect(lambda: self.selfCheckRequested.emit() if self.selfCheckRequested else None)
        check_layout.addWidget(self.run_button)
        right.addWidget(check_panel, 1)
        columns.addLayout(right, 3)

        root.addLayout(columns, 1)

    # ------------------------------------------------------------------ #

    def update_config(self, snapshot: Dict[str, Any], *, state: str, diffs: List[Dict[str, Any]]) -> None:
        labels = self._env_labels
        config = snapshot or {}
        labels["configVersion"].setText(str(config.get("configVersion") or "—"))
        published = str(config.get("publishedAt") or "—")
        received = str(config.get("receivedAt") or "—")
        labels["source"].setText(f"{config.get('source') or '—'} · 发布 {published.split('T')[0] if 'T' in published else published}")
        labels["source"].setToolTip(f"发布：{published}\n接收：{received}")
        labels["airTempC"].setText(_fmt(config.get("airTempC"), " ℃") + "（仪表实测，不是 SoC 温度）")
        labels["relativeHumidityPct"].setText(_fmt(config.get("relativeHumidityPct"), " %"))
        wind = _fmt(config.get("windSpeedMs"), " m/s")
        instrument = str(config.get("instrumentId") or "—")
        position = str(config.get("position") or "—")
        labels["relativeHumidityPct"].setToolTip(f"风速 {wind}（仅作采集稳定性记录，不代入 HH 模型）\n仪表 {instrument}\n位置 {position}")
        labels["airTempC"].setToolTip(f"仪表 {instrument}\n位置 {position}")

        state_label = ConfigState.LABEL.get(state, state)
        self.state_label.setText(f"配置状态：{state_label}")
        self.confirm_button.setEnabled(state == ConfigState.RECEIVED)

        self.diff_table.setRowCount(len(diffs))
        for row, item in enumerate(diffs):
            for column, key in enumerate(("field", "before", "after", "note")):
                cell = QtWidgets.QTableWidgetItem(str(item.get(key) or ""))
                if key == "after":
                    cell.setForeground(QtGui.QColor(theme.AMBER))
                self.diff_table.setItem(row, column, cell)

    def update_telemetry(self, telemetry: Dict[str, Any]) -> None:
        """SoC 温度单独取实时值；采集不到就显示"未接入"和原因（PRD §6）。"""
        temperature = telemetry.get("socTempC")
        source = telemetry.get("socTempSource") or ""
        quality = telemetry.get("socTempQuality")
        if isinstance(temperature, (int, float)):
            self._env_labels["socTempC"].setText(f"{temperature:.1f} ℃（{source}）")
            self._env_labels["socTempC"].setStyleSheet(f"color: {theme.INK};")
        else:
            reason = telemetry.get("socTempReason") or "未探测到温度接口"
            self._env_labels["socTempC"].setText(f"未接入：{reason}")
            self._env_labels["socTempC"].setStyleSheet(f"color: {theme.INK_SOFT};")
        _ = quality

    def set_hh_note(self, text: str) -> None:
        self.hh_label.setText(text)

    def update_self_check(self, report: Dict[str, Any]) -> None:
        items = report.get("items") or []
        # 复用已有的行控件，数量变化时重建
        if len(self._rows) != len(items):
            while self.check_layout.count():
                entry = self.check_layout.takeAt(0)
                widget = entry.widget()
                if widget is not None:
                    widget.deleteLater()
            self._rows = []
            for _ in items:
                row = SelfCheckRow()
                self.check_layout.addWidget(row)
                self._rows.append(row)
            self.check_layout.addStretch(1)
        for row, item in zip(self._rows, items):
            row.update_from(item)
        summary = report.get("summary") or {}
        self.summary_label.setText(
            f"正常 {summary.get('ok', 0)} · 注意 {summary.get('warn', 0)} · "
            f"故障 {summary.get('fail', 0)} · 未接入 {summary.get('unavailable', 0)}"
            f"（耗时 {report.get('elapsedMs', 0):.0f} ms）"
        )


def _fmt(value: Any, unit: str) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)):
        return f"{value:g}{unit}"
    return f"{value}{unit}"
