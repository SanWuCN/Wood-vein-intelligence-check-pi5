"""逐项自检（PRD §5.2、剧本 S09）。

旧版是"定时显示雷达、相机、IMU 均正常"——纯表演。本版每一项都必须对应
真实的驱动或能力探测结果，并且三态分明：

    ok           能读到真实数据
    warn         能读到但有问题（例如磁盘快满、样例包缺一套）
    fail         本应可用却读不到（例如相机节点存在但打不开）
    unavailable  **本机根本没有这个能力**（IMU、GPU、电量计、真实雷达）

最后一项是关键：未接入项显示"未接入"，不全绿。回放单独标注"检测样例"。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .adapters.camera import STATE_STREAMING, STATE_UNAVAILABLE, CameraSource, probe_devices
from .adapters.replay import ReplayError, ReplayLibrary
from .app_state import AppState, SelfCheckItem
from .logging_setup import get_logger
from .storage import Storage

log = get_logger("selfcheck")

#: 自检项的固定顺序（界面按这个顺序排，方便对着剧本念）
CHECK_ORDER = (
    "platform",
    "camera",
    "sample_packages",
    "storage",
    "config",
    "controller",
    "imu",
    "battery",
    "gpu",
    "app_version",
)

#: 磁盘剩余空间的告警阈值
DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024      # 低于 2GB 提醒
DISK_FAIL_BYTES = 300 * 1024 * 1024           # 低于 300MB 直接判故障


@dataclass
class SelfCheckReport:
    items: List[SelfCheckItem]
    started_at: str
    finished_at: str
    elapsed_ms: float

    @property
    def summary(self) -> Dict[str, int]:
        counts = {"ok": 0, "warn": 0, "fail": 0, "unavailable": 0}
        for item in self.items:
            counts[item.state] = counts.get(item.state, 0) + 1
        return counts

    @property
    def blockers(self) -> List[SelfCheckItem]:
        """阻断项：fail 与 warn。unavailable 不算阻断（本机没有的能力不该拦人）。"""
        return [item for item in self.items if item.state in ("fail", "warn")]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "elapsedMs": round(self.elapsed_ms, 1),
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }


class SelfCheckService:
    """跑一遍自检并返回逐项结果。不弹窗、不改状态，只回答"现在到底行不行"。"""

    def __init__(
        self,
        app: AppState,
        storage: Storage,
        library: ReplayLibrary,
        camera: Optional[CameraSource] = None,
        platform_client: Any = None,
    ) -> None:
        self.app = app
        self.storage = storage
        self.library = library
        self.camera = camera
        self.platform = platform_client

    def run(self) -> SelfCheckReport:
        import time
        from datetime import datetime

        started = time.monotonic()
        started_at = datetime.now().strftime("%H:%M:%S")
        items: List[SelfCheckItem] = []
        for key in CHECK_ORDER:
            method = getattr(self, f"_check_{key}", None)
            if method is None:
                continue
            try:
                items.append(method())
            except Exception as exc:  # noqa: BLE001 - 单项异常不能中断整轮自检
                log.exception("自检项 %s 异常", key)
                items.append(SelfCheckItem(key, key, "fail", f"自检执行异常：{exc}", source="本机"))
        elapsed = (time.monotonic() - started) * 1000.0
        report = SelfCheckReport(items, started_at, datetime.now().strftime("%H:%M:%S"), elapsed)
        self.app.set_self_checks(items)
        self.storage.log_event("selfcheck.completed", "", report.to_dict())
        log.info(
            "自检完成：正常 %d / 注意 %d / 故障 %d / 未接入 %d（%.0f ms）",
            report.summary["ok"],
            report.summary["warn"],
            report.summary["fail"],
            report.summary["unavailable"],
            elapsed,
        )
        return report

    # ---- 平台连接 ----

    def _check_platform(self) -> SelfCheckItem:
        if self.platform is None:
            return SelfCheckItem("platform", "平台连接", "unavailable", "本次未启用平台客户端", source="未接入")
        state = self.platform.state
        detail = self.platform.state_detail or "无附加说明"
        if state == "online":
            latency = self.app.platform_latency_ms
            text = f"地址 {self.platform.cfg.platform.platform_url}，延迟 {latency} ms" if latency else f"地址 {self.platform.cfg.platform.platform_url}"
            return SelfCheckItem("platform", "平台连接", "ok", text, source="实测心跳")
        if state == "degraded":
            return SelfCheckItem("platform", "平台连接", "warn", f"{detail}（可离线采集，恢复后补传）", source="实测心跳")
        return SelfCheckItem(
            "platform",
            "平台连接",
            "warn",
            f"{detail}；已缓存任务仍可离线采集，事件进 outbox 等待补传",
            source="实测探测",
        )

    # ---- 相机 ----

    def _check_camera(self) -> SelfCheckItem:
        if self.camera is None:
            return SelfCheckItem("camera", "相机可读", "unavailable", "本次未启用相机适配器", source="未接入")
        stats = self.camera.stats
        if stats.state == STATE_STREAMING:
            return SelfCheckItem(
                "camera",
                "相机可读",
                "ok",
                f"{stats.device} {stats.width}×{stats.height}，采集 {stats.capture_fps:.1f} fps"
                f"（成功读帧计数 / 滑动窗口，非标称值）",
                source="实测采集",
            )
        if stats.state == STATE_UNAVAILABLE:
            return SelfCheckItem("camera", "相机可读", "unavailable", stats.reason or "本机无可用相机", source="能力探测")
        devices = probe_devices()
        detail = stats.reason or "相机不可用"
        if devices:
            detail += "；已探测到的 V4L2 节点：" + "、".join(item["device"] for item in devices)
        return SelfCheckItem("camera", "相机可读", "fail", detail, source="实测探测")

    # ---- 检测样例包 ----

    def _check_sample_packages(self) -> SelfCheckItem:
        status = self.library.status()
        missing = [item for item in status["items"] if not item["present"]]
        if status["ready"] == status["total"]:
            hashes = "，".join(
                f"{item['scenarioId']}={str(item.get('datasetHash') or '')[:10]}" for item in status["items"]
            )
            return SelfCheckItem(
                "sample_packages",
                "检测样例包",
                "ok",
                f"{status['ready']}/{status['total']} 套就绪；datasetHash {hashes}",
                source="检测样例（replay）",
            )
        if status["ready"] == 0:
            return SelfCheckItem(
                "sample_packages",
                "检测样例包",
                "fail",
                f"一套都没找到。根目录 {status['root']}；请先运行 python tools/make_samples.py --out samples",
                source="检测样例（replay）",
            )
        return SelfCheckItem(
            "sample_packages",
            "检测样例包",
            "warn",
            f"{status['ready']}/{status['total']} 套就绪，缺少：" + "、".join(item["scenarioId"] for item in missing),
            source="检测样例（replay）",
        )

    # ---- 存储 ----

    def _check_storage(self) -> SelfCheckItem:
        path = Path(self.storage.batches_dir)
        try:
            usage = shutil.disk_usage(str(path))
        except OSError as exc:
            return SelfCheckItem("storage", "存储空间", "fail", f"数据目录不可用：{exc}", source="实测磁盘")
        free = usage.free
        text = f"{path} 所在分区剩余 {free / (1024 ** 3):.1f} GB / 共 {usage.total / (1024 ** 3):.1f} GB"
        writable = _probe_writable(path)
        if not writable:
            return SelfCheckItem("storage", "存储空间", "fail", f"{text}；目录不可写", source="实测磁盘")
        if free < DISK_FAIL_BYTES:
            return SelfCheckItem("storage", "存储空间", "fail", f"{text}；低于 300MB，无法继续采集", source="实测磁盘")
        if free < DISK_WARN_BYTES:
            return SelfCheckItem("storage", "存储空间", "warn", f"{text}；建议清理历史批次", source="实测磁盘")
        return SelfCheckItem("storage", "存储空间", "ok", text, source="实测磁盘")

    # ---- 配置 ----

    def _check_config(self) -> SelfCheckItem:
        config = self.app.config
        if config is None or not config.has_data:
            return SelfCheckItem(
                "config",
                "环境配置版本",
                "warn",
                f"尚未收到平台环境配置，将使用任务默认 {self.app.assignment.config_version}",
                source="本机状态",
            )
        state = self.app.config_machine.state
        detail = (
            f"{config.config_version}（来源 {config.source or '未注明'}，发布 {config.published_at or '未注明'}）；"
            f"环境温度 {config.air_temp_c if config.air_temp_c is not None else '—'} ℃、"
            f"相对湿度 {config.relative_humidity_pct if config.relative_humidity_pct is not None else '—'} %、"
            f"风速 {config.wind_speed_ms if config.wind_speed_ms is not None else '—'} m/s"
        )
        if state == "applied":
            return SelfCheckItem("config", "环境配置版本", "ok", detail + "；已回传 ack", source="平台下发")
        if state == "confirmed":
            return SelfCheckItem("config", "环境配置版本", "warn", detail + "；已确认但还没回传 ack", source="平台下发")
        return SelfCheckItem("config", "环境配置版本", "warn", detail + "；待操作者确认差异", source="平台下发")

    # ---- 控制器（ESP32-S3）----

    def _check_controller(self) -> SelfCheckItem:
        """没有真实控制器链路时明确写"未接入"，不拿演示模型版本冒充固件版本（PRD §5.6）。"""
        version = self.app.controller_version
        if version:
            return SelfCheckItem("controller", "控制器能力", "ok", f"读取到实际控制器版本 {version}", source="实机读取")
        return SelfCheckItem(
            "controller",
            "控制器能力",
            "unavailable",
            "未探测到 ESP32-S3 串口链路，控制器版本留空（不用演示模型版本顶替）",
            source="未接入",
        )

    # ---- IMU ----

    def _check_imu(self) -> SelfCheckItem:
        return SelfCheckItem(
            "imu",
            "IMU / 姿态",
            "unavailable",
            "本机未配置 IMU：不显示枪体倾斜、航向、轨迹或自动绕柱角度，方向靠人工标记",
            source="未接入",
        )

    # ---- 电池 ----

    def _check_battery(self) -> SelfCheckItem:
        value, reason = self.app.capability.get("battery"), self.app.capability.reason("battery")
        if value == "live":
            return SelfCheckItem("battery", "电池 / 续航", "ok", reason or "检测到电量计", source="实机读取")
        return SelfCheckItem(
            "battery",
            "电池 / 续航",
            "unavailable",
            reason or "无电量计或 UPS 接口，普通供电接入不能推算剩余续航",
            source="未接入",
        )

    # ---- GPU ----

    def _check_gpu(self) -> SelfCheckItem:
        value, reason = self.app.capability.get("gpu"), self.app.capability.reason("gpu")
        if value == "live":
            return SelfCheckItem("gpu", "GPU 利用率", "ok", "可读取 GPU 忙率", source="实测")
        return SelfCheckItem(
            "gpu",
            "GPU 利用率",
            "unavailable",
            reason or "本机没有可读的 GPU 利用率接口，界面隐藏该字段而不显示假零值",
            source="未接入",
        )

    # ---- 应用版本 ----

    def _check_app_version(self) -> SelfCheckItem:
        from .contracts import ADAPTER_VERSION, APP_VERSION

        return SelfCheckItem(
            "app_version",
            "应用版本",
            "ok",
            f"终端 {APP_VERSION}；适配器 {ADAPTER_VERSION}；演示模型 {self.app.model_version}；"
            f"启动 ID {self.app.boot_id}",
            source="本机",
        )


def _probe_writable(path: Path) -> bool:
    probe = path / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False
