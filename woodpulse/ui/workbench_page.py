"""任务工作台（PRD §5.1）。

显示：设备身份、平台连接、本次工单、当前柱、待接收配置、下一项任务。
入口（在底部导航）：检测作业、参考样本、数据交付、更新管理、设备状态。

这里**不再有"成功扫描 / 异常扫描"**的选择 —— 旧版用 SplitButton 把情景选择
藏在点击位置里（PRD §2），本版情景属于排练控制，不放首屏。
"""

from __future__ import annotations

from typing import Any, Dict

from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets
from .state_view import format_round


class WorkbenchPage(QtWidgets.QWidget if HAVE_QT else object):
    """首页。左列任务与工单，右列连接与设备。"""

    prepareRequested = QtCore.pyqtSignal(str) if HAVE_QT else None      # (round)
    selfCheckRequested = QtCore.pyqtSignal() if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(8)

        # ---------------- 左列 ----------------
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)

        order_panel, order_layout = theme.panel("本次工单")
        self.order_grid = QtWidgets.QGridLayout()
        self.order_grid.setHorizontalSpacing(10)
        self.order_grid.setVerticalSpacing(3)
        self._order_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([
            ("orderId", "工单编号"),
            ("scope", "构件 / 测区"),
            ("round", "当前阶段"),
            ("revision", "任务版本"),
        ]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.order_grid.addWidget(name, index, 0)
            self.order_grid.addWidget(value, index, 1)
            self._order_labels[key] = value
        self.order_grid.setColumnStretch(1, 1)
        order_layout.addLayout(self.order_grid)

        self.next_label = QtWidgets.QLabel("")
        self.next_label.setObjectName("Strong")
        self.next_label.setWordWrap(True)
        order_layout.addWidget(self.next_label)

        round_row = QtWidgets.QHBoxLayout()
        round_row.setSpacing(6)
        for label, round_name in (("初扫", "initial"), ("复扫", "rescan"), ("参考样本", "reference")):
            button = theme.make_button(label, kind="ghost")
            button.setMinimumHeight(40)
            button.setToolTip(f"准备{label}批次（绑定所选样例包与当前配置版本）")
            button.clicked.connect(lambda _=False, r=round_name: self.prepareRequested.emit(r) if self.prepareRequested else None)
            round_row.addWidget(button)
        order_layout.addLayout(round_row)
        left.addWidget(order_panel)

        config_panel, config_layout = theme.panel("环境配置")
        self.config_grid = QtWidgets.QGridLayout()
        self.config_grid.setHorizontalSpacing(10)
        self.config_grid.setVerticalSpacing(3)
        self._config_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([("version", "配置版本"), ("state", "状态"), ("published", "发布时间")]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.config_grid.addWidget(name, index, 0)
            self.config_grid.addWidget(value, index, 1)
            self._config_labels[key] = value
        self.config_grid.setColumnStretch(1, 1)
        config_layout.addLayout(self.config_grid)
        left.addWidget(config_panel)

        samples_panel, samples_layout = theme.panel("检测样例包")
        self.samples_label = QtWidgets.QLabel("—")
        self.samples_label.setWordWrap(True)
        samples_layout.addWidget(self.samples_label)
        left.addWidget(samples_panel, 2)
        columns.addLayout(left, 3)

        # ---------------- 右列 ----------------
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)

        device_panel, device_layout = theme.panel("设备身份")
        self.device_grid = QtWidgets.QGridLayout()
        self.device_grid.setHorizontalSpacing(10)
        self.device_grid.setVerticalSpacing(3)
        self._device_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, label) in enumerate([
            ("deviceId", "设备号"),
            ("operator", "操作人"),
            ("appVersion", "应用版本"),
            ("bootId", "启动 ID"),
            ("host", "主机"),
        ]):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            self.device_grid.addWidget(name, index, 0)
            self.device_grid.addWidget(value, index, 1)
            self._device_labels[key] = value
        self.device_grid.setColumnStretch(1, 1)
        device_layout.addLayout(self.device_grid)
        right.addWidget(device_panel)

        link_panel, link_layout = theme.panel("平台连接")
        self.link_state = QtWidgets.QLabel("离线")
        self.link_state.setObjectName("Status")
        self.link_state.setWordWrap(True)
        self.link_state.setMaximumHeight(30)
        link_layout.addWidget(self.link_state)
        self.link_detail = QtWidgets.QLabel("")
        self.link_detail.setObjectName("Hint")
        self.link_detail.setWordWrap(True)
        self.link_detail.setMinimumHeight(30)
        link_layout.addWidget(self.link_detail)
        self.capability_label = QtWidgets.QLabel("")
        self.capability_label.setObjectName("Hint")
        self.capability_label.setWordWrap(True)
        link_layout.addWidget(self.capability_label)
        self.check_button = theme.make_button("开机自检", kind="ghost")
        self.check_button.setMinimumHeight(40)
        self.check_button.clicked.connect(lambda: self.selfCheckRequested.emit() if self.selfCheckRequested else None)
        link_layout.addWidget(self.check_button)
        right.addWidget(link_panel, 2)

        update_panel, update_layout = theme.panel("更新与恢复")
        self.update_label = QtWidgets.QLabel("无更新")
        self.update_label.setWordWrap(True)
        update_layout.addWidget(self.update_label)
        self.recovered_label = QtWidgets.QLabel("")
        self.recovered_label.setObjectName("Hint")
        self.recovered_label.setWordWrap(True)
        update_layout.addWidget(self.recovered_label)
        right.addWidget(update_panel, 1)
        columns.addLayout(right, 2)

        root.addLayout(columns, 1)

    # ------------------------------------------------------------------ #

    def update_view(self, snapshot: Dict[str, Any], cards: Dict[str, Any]) -> None:
        order = cards["order"]
        self._order_labels["orderId"].setText(str(order.get("orderId") or "—"))
        self._order_labels["scope"].setText(f"{order.get('componentId') or '—'} / {order.get('zoneId') or '—'}")
        self._order_labels["round"].setText(format_round(order.get("round")))
        self._order_labels["revision"].setText(
            f"rev {order.get('taskRevision')}（来源：{'平台下发' if order.get('source') == 'platform' else '本机默认'}）"
        )
        self.next_label.setText(cards["nextTask"]["hint"])

        config = cards["config"]
        self._config_labels["version"].setText(str(config.get("version") or "—"))
        state = str(config.get("state") or "")
        self._config_labels["state"].setText(state)
        self._config_labels["state"].setStyleSheet(
            f"color: {theme.AMBER if '待' in state else theme.GREEN if state else theme.INK};"
        )
        self._config_labels["published"].setText(str(config.get("publishedAt") or "平台尚未发布"))

        samples = cards["samples"]
        self.samples_label.setText(
            f"{samples['ready']} / {samples['total']} 套就绪\n目录：{samples['root']}\n"
            "响应序列全部来自固定检测样例包（replay），不是雷达实采。"
        )
        color = theme.GREEN if samples["ready"] == samples["total"] and samples["total"] else theme.AMBER if samples["ready"] else theme.RED
        self.samples_label.setStyleSheet(f"color: {color};")

        device = cards["device"]
        self._device_labels["deviceId"].setText(str(device.get("deviceId") or "—"))
        self._device_labels["operator"].setText(str(device.get("operatorId") or "—"))
        self._device_labels["appVersion"].setText(str(device.get("appVersion") or "—"))
        self._device_labels["bootId"].setText(str(device.get("bootId") or "—"))
        self._device_labels["host"].setText(str(device.get("host") or "—"))

        connection = cards["connection"]
        state = connection.get("state", "offline")
        self.link_state.setText(str(connection.get("label") or state))
        self.link_state.setStyleSheet(f"color: {theme.STATE_COLORS.get(state, theme.INK)}; font-size: {theme.FONT_STATUS}px;")
        detail = connection.get("detail") or ""
        latency = connection.get("latencyMs")
        if latency is not None and state == "online":
            detail = f"心跳延迟 {latency:.0f} ms；" + detail
        if state != "online":
            detail = (detail + "　").strip() + "可离线采集，恢复后自动补传。"
        self.link_detail.setText(detail or "尚未建立连接")
        flags = cards["capabilities"]
        self.capability_label.setText(
            "相机 {} · 雷达 样例回放 · IMU {} · 电量 {} · GPU {} · 遥测 {}".format(
                "实采" if flags.get("cameraLive") else "未接入",
                "实采" if flags.get("imu") == "live" else "未接入",
                "可读" if flags.get("battery") == "live" else "未接入",
                "可读" if flags.get("gpu") == "live" else "未接入",
                "实采" if flags.get("telemetryLive") else "受限",
            )
        )

        update = cards["update"]
        state_label = {
            "idle": "无更新",
            "received": "已接收更新通知（待下载）",
            "downloaded": "已下载（待校验）",
            "verified": "校验通过（待暂存/切换）",
            "staged": "已暂存（等当前批次结束）",
            "applied": "已生效并回验",
            "failed": "更新失败",
            "rolled_back": "已回退旧版本",
        }.get(update.get("state"), update.get("state"))
        self.update_label.setText(
            f"{state_label}" + (f"：{update.get('version')}" if update.get("version") else "")
            + "\n演示包不可烧录，不执行任何烧录动作。"
        )
        recovered = snapshot.get("recoveredBatches") or []
        if recovered:
            self.recovered_label.setText(
                "上次未正常退出，已恢复为中断态的批次："
                + "、".join(item["batchId"] for item in recovered)
                + "（数据保留，未自动续扫）"
            )
            self.recovered_label.setStyleSheet(f"color: {theme.AMBER};")
        else:
            self.recovered_label.setText("没有需要恢复的中断批次")
            self.recovered_label.setStyleSheet(f"color: {theme.INK_SOFT};")
