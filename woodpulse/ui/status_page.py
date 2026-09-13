"""设备状态页（PRD §5.7、§6）。

只显示**真实遥测**与**连接分层**，不显示随机值或假零值：
  · 本机网络（接口、地址、链路、收发速率）
  · 平台服务（连接状态、心跳延迟、最后更新时间）
  · 摄像头、样例播放器
  · 控制器能力（没有就是未接入）
  · 上传队列（队列数、待传字节、确认字节、最近失败原因）

无能力的字段（GPU 利用率、电池电量）**隐藏或标为不可用 + 原因**，
历史欠压标志与当前欠压标志分开显示，不把"历史上发生过"渲染成"现在正在欠压"。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..telemetry import format_bytes, format_duration, format_rate
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets


class DeviceStatusPage(QtWidgets.QWidget if HAVE_QT else object):
    diagnosticsRequested = QtCore.pyqtSignal() if HAVE_QT else None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._value_labels: Dict[str, QtWidgets.QLabel] = {}
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(8)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._build_group("本机网络与平台连接", [
            ("interface", "通信接口"),
            ("address", "接口地址"),
            ("link", "链路状态"),
            ("txRate", "发送速率（接口流量）"),
            ("rxRate", "接收速率（接口流量）"),
            ("wifi", "Wi-Fi 信号"),
            ("connection", "平台连接"),
            ("latency", "平台应用延迟"),
            ("lastSeen", "最近收到平台消息"),
            ("pending", "待补传事件"),
        ]))
        left.addWidget(self._build_group("计算资源", [
            ("cpu", "CPU 利用率"),
            ("cpuCount", "CPU 核数"),
            ("processCpu", "本进程 CPU"),
            ("processRss", "本进程内存 RSS"),
            ("memory", "内存"),
            ("disk", "数据目录磁盘"),
            ("cpuFreq", "CPU 频率（不等于负载）"),
            ("uptime", "进程运行时长"),
        ]))
        left.addStretch(1)
        columns.addLayout(left, 1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        right.addWidget(self._build_group("温度 / 供电 / 能力", [
            ("socTemp", "SoC 温度"),
            ("throttleNow", "当前降频与欠压"),
            ("throttleHistory", "历史发生过的标志"),
            ("camera", "摄像头"),
            ("replay", "样例播放器"),
            ("controller", "控制器能力"),
            ("battery", "电池 / 续航"),
            ("gpu", "GPU 利用率"),
            ("preview", "预览图上传"),
        ]))
        right.addWidget(self._build_group("版本", [
            ("appVersion", "终端应用"),
            ("adapterVersion", "适配器"),
            ("controllerVersion", "实际控制器"),
            ("demoModelVersion", "演示模型"),
            ("configVersion", "配置版本"),
            ("bootId", "本次启动 ID"),
            ("fingerprint", "主机指纹"),
        ]))
        right.addStretch(1)
        columns.addLayout(right, 1)

        root.addLayout(columns, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)
        self.upload_label = QtWidgets.QLabel("上传队列：—")
        self.upload_label.setObjectName("Hint")
        self.upload_label.setWordWrap(True)
        footer.addWidget(self.upload_label, 1)
        # 按钮放左侧：触摸屏上右侧边缘不好按，主操作尽量靠中间偏左
        self.diag_button = theme.make_button("查看诊断包", kind="ghost")
        self.diag_button.setMinimumHeight(40)
        self.diag_button.clicked.connect(lambda: self.diagnosticsRequested.emit() if self.diagnosticsRequested else None)
        footer.insertWidget(0, self.diag_button)
        root.addLayout(footer)

    def _build_group(self, title: str, rows: List[tuple]) -> QtWidgets.QFrame:
        frame, layout = theme.panel(title)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        for index, (key, label) in enumerate(rows):
            name = QtWidgets.QLabel(label)
            name.setObjectName("Hint")
            value = QtWidgets.QLabel("—")
            value.setWordWrap(True)
            grid.addWidget(name, index, 0)
            grid.addWidget(value, index, 1)
            self._value_labels[key] = value
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        return frame

    # ------------------------------------------------------------------ #

    def _set(self, key: str, text: str, *, color: str = "", soft: bool = False) -> None:
        label = self._value_labels.get(key)
        if label is None:
            return
        label.setText(text)
        if color:
            label.setStyleSheet(f"color: {color};")
        elif soft:
            label.setStyleSheet(f"color: {theme.INK_SOFT};")
        else:
            label.setStyleSheet(f"color: {theme.INK};")

    def update_telemetry(self, telemetry: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
        telemetry = telemetry or {}
        network = telemetry.get("network") or {}
        connection = snapshot.get("connection") or {}
        camera = telemetry.get("camera") or {}
        replay = telemetry.get("replay") or {}
        versions = telemetry.get("versions") or {}
        upload = telemetry.get("upload") or {}
        memory = telemetry.get("memory") or {}
        disk = telemetry.get("disk") or {}
        throttled = telemetry.get("throttled") or {}
        wifi = telemetry.get("wifi") or {}
        capabilities = snapshot.get("capabilities") or {}

        # ---- 网络 ----
        self._set("interface", _or_dash(network.get("interface")) + ("" if network.get("isWireless") in (None, False) else "（无线）"))
        self._set("address", _or_dash(network.get("address")))
        link = network.get("linkUp")
        self._set("link", "已连接" if link else ("未连接" if link is False else "—"), color=theme.GREEN if link else theme.RED if link is False else "")
        tx = network.get("txBytesPerSec")
        rx = network.get("rxBytesPerSec")
        note = network.get("reason") or ""
        self._set("txRate", f"{format_rate(tx)}" + ("（接口流量，不是本应用上传速率）" if tx is not None else f"（{note or '不可用'}）"), soft=tx is None)
        self._set("rxRate", format_rate(rx), soft=rx is None)
        if wifi.get("rssiDbm") is not None:
            self._set("wifi", f"{wifi['rssiDbm']:.0f} dBm（{wifi.get('interface')}）")
        else:
            self._set("wifi", f"不可用：{wifi.get('reason') or '未读取到 RSSI'}", soft=True)

        # ---- 平台 ----
        state = connection.get("state", "offline")
        self._set(
            "connection",
            f"{connection.get('label') or state}" + (f"（{connection.get('platformDetail')}）" if connection.get("platformDetail") else ""),
            color=theme.STATE_COLORS.get(state, theme.INK),
        )
        latency = connection.get("latencyMs")
        self._set("latency", f"{latency:.0f} ms（心跳往返，未用未校准设备时间做时延）" if isinstance(latency, (int, float)) else "—")
        self._set("lastSeen", _or_dash(connection.get("lastSeenAt")))
        self._set("pending", f"{snapshot.get('pendingEvents', 0)} 条（关键事件至少一次投递，平台按 messageId 去重）")

        # ---- 计算 ----
        cpu = telemetry.get("cpuPercent")
        quality = telemetry.get("cpuQuality")
        if isinstance(cpu, (int, float)):
            self._set("cpu", f"{cpu:.1f} %（psutil 非阻塞差分，采样窗口 {telemetry.get('sampleWindowMs', 1000)} ms）")
        else:
            self._set("cpu", f"不可用：{telemetry.get('cpuReason') or quality or '未采集到'}", soft=True)
        self._set("cpuCount", str(telemetry.get("cpuCount") or "—"))
        process_cpu = telemetry.get("processCpuPercent")
        rss = telemetry.get("processRssBytes")
        self._set(
            "processCpu",
            f"{process_cpu:.1f} %（{telemetry.get('processCpuScope') or '单进程口径'}）" if isinstance(process_cpu, (int, float)) else "—",
        )
        self._set("processRss", format_bytes(rss) if rss else "—")
        if memory:
            self._set(
                "memory",
                f"{format_bytes(memory.get('usedBytes'))} / {format_bytes(memory.get('totalBytes'))}"
                f"（{memory.get('percent', 0):.1f}%，可用 {format_bytes(memory.get('availableBytes'))}）",
            )
        else:
            self._set("memory", f"不可用：{telemetry.get('memoryReason') or '未采集到'}", soft=True)
        if disk:
            self._set(
                "disk",
                f"{disk.get('path')}：已用 {format_bytes(disk.get('usedBytes'))} / 共 {format_bytes(disk.get('totalBytes'))}"
                f"（剩余 {format_bytes(disk.get('freeBytes'))}）",
            )
        else:
            self._set("disk", f"不可用：{telemetry.get('diskReason') or '未采集到'}", soft=True)
        freq = telemetry.get("cpuFreqMhz")
        self._set(
            "cpuFreq",
            f"{freq:.0f} MHz（{telemetry.get('cpuFreqSource')}）" if isinstance(freq, (int, float)) else f"不可用：{telemetry.get('cpuFreqReason') or '无接口'}",
            soft=not isinstance(freq, (int, float)),
        )
        self._set("uptime", format_duration(telemetry.get("uptimeSeconds")))

        # ---- 温度 / 供电 / 能力 ----
        temperature = telemetry.get("socTempC")
        if isinstance(temperature, (int, float)):
            self._set("socTemp", f"{temperature:.1f} ℃（来源 {telemetry.get('socTempSource') or '未知'}；SoC 温度不等于木材周围环境温度）")
        else:
            self._set("socTemp", f"不可用：{telemetry.get('socTempReason') or '未探测到接口'}", soft=True)

        if telemetry.get("throttledSupported"):
            now_flags = [name for name in ("underVoltageNow", "freqCappedNow", "throttledNow", "softTempLimitNow") if throttled.get(name)]
            history_flags = [
                name for name in ("underVoltageOccurred", "freqCappedOccurred", "throttledOccurred", "softTempLimitOccurred") if throttled.get(name)
            ]
            self._set("throttleNow", _flag_text(now_flags, {"underVoltageNow": "欠压", "freqCappedNow": "频率限制", "throttledNow": "降频", "softTempLimitNow": "软温度限制"}), color=theme.AMBER if now_flags else theme.GREEN)
            self._set(
                "throttleHistory",
                _flag_text(history_flags, {
                    "underVoltageOccurred": "历史欠压",
                    "freqCappedOccurred": "历史频率限制",
                    "throttledOccurred": "历史降频",
                    "softTempLimitOccurred": "历史软温度限制",
                })
                + "（历史标志不代表当前状态）",
                soft=True,
            )
        else:
            self._set("throttleNow", f"不可用：{telemetry.get('throttledReason') or 'get_throttled 不可用'}", soft=True)
            self._set("throttleHistory", "—", soft=True)

        if camera.get("state") == "streaming":
            details = [f"{camera.get('device')} {camera.get('width')}×{camera.get('height')}"]
            if camera.get("captureFps") is not None:
                details.append(f"采集 {camera['captureFps']:.1f} fps")
            if camera.get("displayFps") is not None:
                details.append(f"显示 {camera['displayFps']:.1f} fps")
            if camera.get("lastFrameAgeMs") is not None:
                details.append(f"帧龄 {camera['lastFrameAgeMs']:.0f} ms")
            if camera.get("droppedFrames"):
                details.append(f"丢帧 {camera['droppedFrames']}")
            if camera.get("reconnects"):
                details.append(f"重连 {camera['reconnects']} 次")
            self._set("camera", "；".join(details), color=theme.GREEN)
        else:
            self._set(
                "camera",
                f"{camera.get('stateLabel') or '不可用'}：{camera.get('reason') or '未采集到画面'}",
                color=theme.STATE_COLORS.get("unavailable" if camera.get("state") == "unavailable" else "fail", theme.INK_SOFT),
            )

        if replay.get("scenarioId"):
            self._set(
                "replay",
                f"{replay.get('scenarioId')} · {replay.get('frameIndex')}/{replay.get('frameCount')} 帧"
                f" · 播放 {replay.get('playbackFps') or 0:.1f} fps（样例播放率，不是雷达实测帧率）"
                + (f" · 未采集 {replay.get('missingFrames')} 帧" if replay.get("missingFrames") else ""),
            )
        else:
            self._set("replay", "本机当前没有回放任务", soft=True)

        self._set(
            "controller",
            f"{capabilities.get('radar') and '雷达：' + _capability_label(capabilities.get('radar'))}"
            f"；控制器版本 {snapshot.get('controllerVersion') or '未接入'}",
        )
        battery_reason = (snapshot.get("capabilityReasons") or {}).get("battery", "")
        battery_value = capabilities.get("battery")
        self._set(
            "battery",
            "可读取电量计" if battery_value == "live" else f"不可用：{battery_reason or '无电量计或 UPS 接口'}",
            soft=battery_value != "live",
        )
        gpu_reason = (snapshot.get("capabilityReasons") or {}).get("gpu", "")
        self._set(
            "gpu",
            "可读取" if capabilities.get("gpu") == "live" else f"不可用：{gpu_reason or '无 GPU 利用率接口（隐藏而不显示假零值）'}",
            soft=capabilities.get("gpu") != "live",
        )
        preview_value = capabilities.get("preview")
        self._set(
            "preview",
            "按 1—2 fps 上传 640 宽 JPEG（不改原始照片尺寸）" if preview_value == "live" else f"不可用：{(snapshot.get('capabilityReasons') or {}).get('preview', '无相机时不产生预览图')}",
            soft=preview_value != "live",
        )

        # ---- 版本 ----
        self._set("appVersion", _or_dash(versions.get("appVersion")))
        self._set("adapterVersion", _or_dash(versions.get("adapterVersion")))
        self._set("controllerVersion", _or_dash(versions.get("controllerVersion")) + "（未接入时不填估计值）", soft=not versions.get("controllerVersion"))
        self._set("demoModelVersion", _or_dash(versions.get("demoModelVersion")) + "（演示模型，与真机固件分开记录）")
        self._set("configVersion", _or_dash(versions.get("configVersion")))
        self._set("bootId", _or_dash(snapshot.get("bootId")))
        fingerprint = snapshot.get("fingerprint") or {}
        self._set("fingerprint", f"{fingerprint.get('hostname')} / {fingerprint.get('machine')} / Python {fingerprint.get('python')}")

        # ---- 上传队列 ----
        queued = upload.get("queued")
        pending = upload.get("pendingBytes")
        confirmed = upload.get("confirmedBytes")
        last_error = upload.get("lastError")
        text = (
            f"上传队列：{queued or 0} 项 · 待传 {format_bytes(pending or 0)} · 已确认 {format_bytes(confirmed or 0)}"
            f" · 当前文件 {upload.get('activeFile') or '无'}"
        )
        if last_error:
            text += f" · 最近失败：{last_error}"
        self.upload_label.setText(text)


def _or_dash(value: Any) -> str:
    return "—" if value in (None, "") else str(value)


def _flag_text(flags: List[str], mapping: Dict[str, str]) -> str:
    if not flags:
        return "无"
    return "、".join(mapping.get(name, name) for name in flags)


def _capability_label(value: Any) -> str:
    return {"live": "实采", "replay": "样例回放", "unavailable": "未接入", "preset": "预制结果"}.get(str(value), str(value))
