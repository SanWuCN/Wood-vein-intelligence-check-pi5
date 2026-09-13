"""数据交付与结果页（PRD §5.5、§9）。

三件事必须分开显示，不能合成一句"完成"：
    结束采集  ≠  上传完成  ≠  平台分析完成
所以页面上是三条独立状态：批次状态 / 上传状态 / 平台回执。

断网后进入"待上传"，恢复后继续同步（真正的续传在 platform_client 里，
这里只反映它上报的字节数）。

复扫结果沿用剧本口径：疑似受潮 0.71、两处疑似虫蛀响应 0.84 / 0.87，
并明确写"待平台复核"；**不标注异常响应的深度与形状**（当前硬件资料未给出
可验证的内部成像分辨率）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..telemetry import format_bytes
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets

RESULT_CONCLUSION_LABEL = {
    "preliminary": "端侧初筛（待平台复核）",
    "withheld": "不出结论（适用域待核验）",
    "none": "无结论",
}


class DeliveryPage(QtWidgets.QWidget if HAVE_QT else object):
    uploadRequested = QtCore.pyqtSignal(str) if HAVE_QT else None
    submitRequested = QtCore.pyqtSignal(str) if HAVE_QT else None
    verifyRequested = QtCore.pyqtSignal(str) if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._batch_id = ""
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)

        # ---- 左：批次与上传 ----
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)
        batch_panel, batch_layout = theme.panel("批次与上传状态")
        self.batch_grid = QtWidgets.QGridLayout()
        self.batch_grid.setHorizontalSpacing(10)
        self.batch_grid.setVerticalSpacing(2)
        self._labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([
            ("batchId", "批次"),
            ("round", "轮次"),
            ("zone", "构件 / 测区"),
            ("state", "批次状态"),
            ("files", "文件与字节"),
            ("integrity", "完整性检查"),
            ("upload", "上传状态"),
            ("platform", "平台回执"),
            ("hash", "样例标识 datasetHash"),
        ]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.batch_grid.addWidget(name, index, 0)
            self.batch_grid.addWidget(value, index, 1)
            self._labels[key] = value
        self.batch_grid.setColumnStretch(1, 1)
        batch_layout.addLayout(self.batch_grid)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self.upload_button = theme.make_button("上传本批文件", kind="ghost")
        self.submit_button = theme.make_button("提交批次清单", kind="ghost")
        self.verify_button = theme.make_button("校验本地摘要", kind="ghost")
        self.upload_button.clicked.connect(lambda: self.uploadRequested.emit(self._batch_id) if self.uploadRequested else None)
        self.submit_button.clicked.connect(lambda: self.submitRequested.emit(self._batch_id) if self.submitRequested else None)
        self.verify_button.clicked.connect(lambda: self.verifyRequested.emit(self._batch_id) if self.verifyRequested else None)
        for button in (self.upload_button, self.submit_button, self.verify_button):
            buttons.addWidget(button)
        batch_layout.addLayout(buttons)
        left.addWidget(batch_panel)

        # ---- 文件清单 ----
        files_panel, files_layout = theme.panel("批次文件清单（本地批次目录）")
        self.file_table = QtWidgets.QTableWidget(0, 4)
        self.file_table.setHorizontalHeaderLabels(["角色", "文件", "大小", "上传状态"])
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.file_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.file_table.setMinimumHeight(theme.TABLE_MIN_H)
        files_layout.addWidget(self.file_table, 1)
        left.addWidget(files_panel, 1)
        top.addLayout(left, 3)

        # ---- 右：端侧结果 ----
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        result_panel, result_layout = theme.panel("端侧初筛结果")
        self.conclusion_label = QtWidgets.QLabel("—")
        self.conclusion_label.setObjectName("Strong")
        self.conclusion_label.setWordWrap(True)
        result_layout.addWidget(self.conclusion_label)
        self.result_note = QtWidgets.QLabel("")
        self.result_note.setObjectName("Hint")
        self.result_note.setWordWrap(True)
        result_layout.addWidget(self.result_note)

        self.finding_table = QtWidgets.QTableWidget(0, 3)
        self.finding_table.setHorizontalHeaderLabels(["编号", "结论", "分数"])
        self.finding_table.verticalHeader().setVisible(False)
        self.finding_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        fheader = self.finding_table.horizontalHeader()
        fheader.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        fheader.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        fheader.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        result_layout.addWidget(self.finding_table, 1)
        self.boundary_label = QtWidgets.QLabel(
            "边界说明：端侧不输出异常深度与形状；响应序列为预制样例，不代表木柱内部真实结构。"
            "详细证据与多模态结论由平台返回。"
        )
        self.boundary_label.setObjectName("Hint")
        self.boundary_label.setWordWrap(True)
        result_layout.addWidget(self.boundary_label)
        right.addWidget(result_panel, 1)

        history_panel, history_layout = theme.panel("历史批次（本地）")
        self.history_table = QtWidgets.QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(["批次", "构件 / 测区", "状态"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        hheader = self.history_table.horizontalHeader()
        hheader.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hheader.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hheader.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.history_table.setMinimumHeight(theme.TABLE_MIN_H)
        history_layout.addWidget(self.history_table, 1)
        right.addWidget(history_panel, 1)
        top.addLayout(right, 2)

        root.addLayout(top, 1)

    # ------------------------------------------------------------------ #

    def show_batch(
        self,
        batch: Optional[Dict[str, Any]],
        *,
        files: List[Dict[str, Any]],
        integrity: Optional[Dict[str, Any]],
        result: Optional[Dict[str, Any]],
        upload_summary: Dict[str, Any],
        delivery: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not batch:
            for label in self._labels.values():
                label.setText("—")
            self.file_table.setRowCount(0)
            self.finding_table.setRowCount(0)
            self.conclusion_label.setText("还没有批次")
            self.result_note.setText("先在“检测作业”页准备并完成一次采集。")
            self._batch_id = ""
            self.upload_button.setEnabled(False)
            self.submit_button.setEnabled(False)
            self.verify_button.setEnabled(False)
            return

        self._batch_id = str(batch.get("batchId") or "")
        labels = self._labels
        labels["batchId"].setText(self._batch_id)
        labels["round"].setText({"initial": "初扫", "rescan": "复扫", "reference": "参考样本"}.get(batch.get("round"), str(batch.get("round") or "—")))
        labels["zone"].setText(f"{batch.get('componentId')} / {batch.get('zoneId')}")
        state = batch.get("state") or ""
        labels["state"].setText(
            {
                "open": "记录中",
                "sealed": "已封存（manifest 已原子提交，允许上传）",
                "uploading": "上传中",
                "uploaded": "平台已接收",
                "partial": "部分接收",
                "failed": "失败",
            }.get(state, state)
        )
        labels["files"].setText(f"{len(files)} 个文件 · {format_bytes(sum(item.get('size') or 0 for item in files))}")
        if integrity:
            missing = integrity.get("missingRequired") or []
            broken = integrity.get("broken") or []
            if integrity.get("complete"):
                labels["integrity"].setText("完整：必需文件齐全，逐文件摘要可校验")
                labels["integrity"].setStyleSheet(f"color: {theme.GREEN};")
            else:
                labels["integrity"].setText(
                    "不完整：" + "、".join([*(f"缺 {name}" for name in missing), *(f"{item['path']} {item['reason']}" for item in broken)])
                )
                labels["integrity"].setStyleSheet(f"color: {theme.AMBER};")
        labels["upload"].setText(
            f"{upload_summary.get('queued', 0)} 项待传 · 待传 {format_bytes(upload_summary.get('pendingBytes') or 0)}"
            f" · 已确认 {format_bytes(upload_summary.get('confirmedBytes') or 0)}"
        )
        if delivery:
            ack = delivery.get("platformAck") or {}
            if delivery.get("error"):
                labels["platform"].setText(f"提交失败：{delivery['error']}")
            else:
                labels["platform"].setText(
                    ("完整接收" if ack.get("complete") else "部分接收")
                    + (f"，缺少：{'、'.join(delivery.get('missing') or [])}" if delivery.get("missing") else "")
                    + "（平台已接收 ≠ 平台分析完成）"
                )
        else:
            labels["platform"].setText("尚未提交给平台")
        dataset_hash = str(batch.get("datasetHash") or "")
        labels["hash"].setText((dataset_hash[:24] + "…") if dataset_hash else "未封存（未提交 manifest）")

        self.file_table.setRowCount(len(files))
        role_labels = {
            "frames": "响应序列",
            "segments": "响应分段",
            "marks": "人工标记",
            "marks_csv": "标记人读副本",
            "quality": "质量记录",
            "result": "端侧结果",
            "config": "配置快照",
            "dataset": "数据集分组",
            "plan": "采集计划",
            "events": "阶段事件",
            "image": "标记截图",
            "image_index": "图像索引",
            "batch": "批次元数据",
        }
        for row, item in enumerate(files):
            upload_state = {
                "queued": "待上传",
                "active": "上传中",
                "done": "已确认",
                "failed": "失败",
                "paused_offline": "待上传（离线）",
            }.get(item.get("upload_state"), item.get("upload_state") or "—")
            cells = [
                role_labels.get(item.get("role"), item.get("role") or "—"),
                item.get("rel_path") or item.get("name"),
                format_bytes(item.get("size") or 0),
                upload_state,
            ]
            for column, text in enumerate(cells):
                cell = QtWidgets.QTableWidgetItem(str(text))
                if column == 3 and item.get("upload_state") == "failed":
                    cell.setForeground(QtGui.QColor(theme.RED))
                if column == 3 and item.get("upload_state") == "done":
                    cell.setForeground(QtGui.QColor(theme.GREEN))
                self.file_table.setItem(row, column, cell)

        self.upload_button.setEnabled(bool(self._batch_id))
        self.submit_button.setEnabled(bool(self._batch_id) and int(batch.get("manifest_committed") or 0) == 1)
        self.verify_button.setEnabled(bool(self._batch_id))

        # ---- 端侧结果 ----
        if not result:
            self.conclusion_label.setText("本批没有结果文件")
            self.result_note.setText("结果只在结束采集时生成；中断批次会明确写明原因。")
            self.finding_table.setRowCount(0)
            return
        conclusion = str(result.get("conclusion") or "none")
        self.conclusion_label.setText(RESULT_CONCLUSION_LABEL.get(conclusion, conclusion))
        self.conclusion_label.setStyleSheet(
            f"color: {theme.AMBER if conclusion == 'preliminary' else theme.RED if conclusion == 'withheld' else theme.INK};"
        )
        note = result.get("reason") or result.get("note") or ""
        model = result.get("modelVersion") or batch.get("modelVersion") or ""
        self.result_note.setText(f"模型 {model}；{note}")
        findings = result.get("findings") or []
        self.finding_table.setRowCount(len(findings))
        for row, finding in enumerate(findings):
            score = finding.get("score")
            cells = [
                str(finding.get("id") or ""),
                str(finding.get("label") or ""),
                f"{float(score):.2f}" if isinstance(score, (int, float)) else "—",
            ]
            for column, text in enumerate(cells):
                cell = QtWidgets.QTableWidgetItem(text)
                if column == 2:
                    cell.setForeground(QtGui.QColor(theme.RED if isinstance(score, (int, float)) and score >= 0.8 else theme.AMBER))
                self.finding_table.setItem(row, column, cell)

    def update_history(self, batches: List[Dict[str, Any]]) -> None:
        self.history_table.setRowCount(len(batches))
        for row, item in enumerate(batches):
            cells = [
                str(item.get("batch_id") or ""),
                f"{item.get('component_id')} / {item.get('zone_id')}",
                {
                    "open": "记录中",
                    "sealed": "已封存",
                    "uploading": "上传中",
                    "uploaded": "已接收",
                    "partial": "部分接收",
                    "failed": "失败",
                }.get(item.get("state"), item.get("state") or ""),
            ]
            for column, text in enumerate(cells):
                self.history_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(text)))
