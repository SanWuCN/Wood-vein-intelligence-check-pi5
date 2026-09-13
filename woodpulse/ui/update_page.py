"""更新管理页（PRD §5.6、§13 H11/H12/H13）。

按包类型区分：配置包、模型包、应用包、控制器固件包。
首版演示主线只走"模型演示包"这条：

    接收通知 → 实际下载 → 实际摘要检查 → 切换本地演示模型版本 → 重启对应软件任务 → 回验

界面上刻意做到三件事：
  · 四步流程各自显示状态与真实细节（字节数、实测摘要、目标核对结果）；
  · 实际控制器版本与演示模型版本**分栏显示**，演示回执不覆盖真机状态；
  · 没有任何"已检测供电""总线自动移交""Hash 通过"这类虚构事件 ——
    摘要通过只可能来自本机重新计算的结果（H12）。

采集进行中收到更新时只允许"暂存"，并在界面上写明原因（H13）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..telemetry import format_bytes
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets

STEP_ORDER = [
    ("received", "1 接收通知"),
    ("download", "2 下载"),
    ("verify", "3 摘要与目标检查"),
    ("stage", "4 暂存"),
    ("apply", "5 切换并回验"),
    ("receipt", "6 提交回执"),
]


class UpdatePage(QtWidgets.QWidget if HAVE_QT else object):
    downloadRequested = QtCore.pyqtSignal() if HAVE_QT else None
    verifyRequested = QtCore.pyqtSignal() if HAVE_QT else None
    applyRequested = QtCore.pyqtSignal() if HAVE_QT else None
    receiptRequested = QtCore.pyqtSignal() if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._step_rows: Dict[str, QtWidgets.QLabel] = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)

        # ---- 左：四步流程 ----
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)
        flow_panel, flow_layout = theme.panel("更新流程（每步都是真实动作与真实结果）")
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for index, (key, label) in enumerate(STEP_ORDER):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("待执行")
            value.setWordWrap(True)
            grid.addWidget(name, index, 0)
            grid.addWidget(value, index, 1)
            self._step_rows[key] = value
        grid.setColumnStretch(1, 1)
        flow_layout.addLayout(grid)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.download_button = theme.make_button("下载更新包", kind="ghost")
        self.verify_button = theme.make_button("校验摘要与目标", kind="ghost")
        self.apply_button = theme.make_button("切换并回验", kind="ghost")
        self.receipt_button = theme.make_button("提交回执", kind="ghost")
        self.download_button.clicked.connect(lambda: self.downloadRequested.emit() if self.downloadRequested else None)
        self.verify_button.clicked.connect(lambda: self.verifyRequested.emit() if self.verifyRequested else None)
        self.apply_button.clicked.connect(lambda: self.applyRequested.emit() if self.applyRequested else None)
        self.receipt_button.clicked.connect(lambda: self.receiptRequested.emit() if self.receiptRequested else None)
        for button in (self.download_button, self.verify_button, self.apply_button, self.receipt_button):
            buttons.addWidget(button)
        flow_layout.addLayout(buttons)
        left.addWidget(flow_panel)

        checks_panel, checks_layout = theme.panel("检查明细（本机重新计算，点空白处不会产生通过事件）")
        self.check_table = QtWidgets.QTableWidget(0, 3)
        self.check_table.setHorizontalHeaderLabels(["检查项", "结果", "说明"])
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.check_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        self.check_table.setMinimumHeight(theme.TABLE_MIN_H)
        checks_layout.addWidget(self.check_table, 1)
        left.addWidget(checks_panel, 1)
        top.addLayout(left, 3)

        # ---- 右：包信息与版本 ----
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        info_panel, info_layout = theme.panel("包信息")
        self.info_grid = QtWidgets.QGridLayout()
        self.info_grid.setHorizontalSpacing(10)
        self.info_grid.setVerticalSpacing(2)
        self._info_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([
            ("artifactId", "产物 ID"),
            ("artifactKind", "包类型"),
            ("version", "包版本"),
            ("target", "目标设备"),
            ("size", "声明大小"),
            ("declaredSha", "平台声明摘要"),
            ("actualSha", "本机实测摘要"),
            ("staging", "暂存目录"),
        ]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.info_grid.addWidget(name, index, 0)
            self.info_grid.addWidget(value, index, 1)
            self._info_labels[key] = value
        self.info_grid.setColumnStretch(1, 1)
        info_layout.addLayout(self.info_grid)
        right.addWidget(info_panel)

        version_panel, version_layout = theme.panel("版本（实机与演示分栏，不互相覆盖）")
        self.version_grid = QtWidgets.QGridLayout()
        self.version_grid.setHorizontalSpacing(10)
        self.version_grid.setVerticalSpacing(2)
        self._version_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([
            ("live", "实际控制器版本"),
            ("demo", "演示模型版本"),
            ("config", "配置版本"),
            ("fallback", "可恢复的旧版本"),
        ]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.version_grid.addWidget(name, index, 0)
            self.version_grid.addWidget(value, index, 1)
            self._version_labels[key] = value
        self.version_grid.setColumnStretch(1, 1)
        version_layout.addLayout(self.version_grid)
        right.addWidget(version_panel)

        notes_panel, notes_layout = theme.panel("口径说明")
        self.notes_label = QtWidgets.QLabel("")
        self.notes_label.setObjectName("Hint")
        self.notes_label.setWordWrap(True)
        notes_layout.addWidget(self.notes_label)
        right.addWidget(notes_panel, 1)
        top.addLayout(right, 2)

        root.addLayout(top, 1)

    # ------------------------------------------------------------------ #

    def update_view(self, summary: Optional[Dict[str, Any]], snapshot: Dict[str, Any]) -> None:
        summary = summary or {}
        state = summary.get("state", "idle")
        steps: List[Dict[str, Any]] = summary.get("steps") or []
        latest: Dict[str, Dict[str, Any]] = {}
        for step in steps:
            latest[step.get("key")] = step

        for key, label in STEP_ORDER:
            value_label = self._step_rows[key]
            step = latest.get(key)
            if step is None:
                if key == "receipt" and state == "applied":
                    value_label.setText("可以提交（设备回执未提交）")
                    value_label.setStyleSheet(f"color: {theme.AMBER};")
                else:
                    value_label.setText("待执行")
                    value_label.setStyleSheet(f"color: {theme.INK_SOFT};")
                continue
            state_key = step.get("state")
            color = {"ok": theme.GREEN, "failed": theme.RED, "running": theme.AMBER, "skipped": theme.INK_SOFT}.get(state_key, theme.INK)
            value_label.setText(f"{step.get('stateLabel') or state_key}：{step.get('detail') or ''}")
            value_label.setStyleSheet(f"color: {color};")

        checks = summary.get("checks") or []
        self.check_table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            cells = [
                check.get("label") or check.get("key") or "",
                "通过" if check.get("ok") else "异常",
                check.get("detail") or "",
            ]
            for column, text in enumerate(cells):
                cell = QtWidgets.QTableWidgetItem(str(text))
                if column == 1:
                    cell.setForeground(QtGui.QColor(theme.GREEN if check.get("ok") else theme.RED))
                self.check_table.setItem(row, column, cell)

        info = summary.get("artifact") or {}
        labels = self._info_labels
        labels["artifactId"].setText(str(summary.get("version") and (info.get("artifactId") or info.get("artifact_id") or "—") or "—"))
        labels["artifactKind"].setText(str(info.get("artifactKind") or info.get("artifact_kind") or "（未收到通知）"))
        labels["version"].setText(str(summary.get("version") or "—"))
        labels["target"].setText(str(summary.get("target") or "—"))
        size = info.get("size") or info.get("bytes")
        labels["size"].setText(format_bytes(size) if size else "—")
        declared = str(summary.get("declaredSha256") or "")
        actual = str(summary.get("downloadedSha256") or "")
        labels["declaredSha"].setText((declared[:24] + "…") if declared else "平台未声明")
        labels["actualSha"].setText((actual[:24] + "…") if actual else "尚未下载")
        if declared and actual:
            match = declared.lower() == actual.lower()
            labels["actualSha"].setStyleSheet(f"color: {theme.GREEN if match else theme.RED};")
        labels["staging"].setText(str(summary.get("packageDir") or "—"))

        vlabels = self._version_labels
        vlabels["live"].setText(f"{snapshot.get('controllerVersion') or '未接入（无串口链路，不填估计值）'}")
        vlabels["demo"].setText(str(summary.get("demoModelVersion") or summary.get("activeModelVersion") or "—"))
        vlabels["config"].setText(str((snapshot.get("config") or {}).get("configVersion") or snapshot.get("modelVersion") or "—"))
        vlabels["fallback"].setText("旧演示版本记录保留在本地库，切换失败时自动回退")

        state_label = {
            "idle": "无更新",
            "received": "已接收通知",
            "downloading": "下载中",
            "downloaded": "已下载",
            "verified": "摘要与目标检查通过",
            "staged": "已暂存（等当前批次结束）",
            "applying": "切换中",
            "applied": "已生效并回验",
            "failed": "更新失败",
            "rolled_back": "已回退旧版本",
        }.get(state, state)
        notes = summary.get("notes") or []
        self.notes_label.setText(f"当前状态：{state_label}\n" + "\n".join(f"· {note}" for note in notes))

        self.download_button.setEnabled(state in ("received", "failed"))
        self.verify_button.setEnabled(state == "downloaded")
        self.apply_button.setEnabled(state == "staged")
        self.receipt_button.setEnabled(state in ("applied", "failed", "rolled_back"))
