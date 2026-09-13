"""界面读取用的状态视图。

界面不直接拼各种内部对象，只读这一个 dict。这样做有两个好处：
  · 刷新只发生在一次组装里，不会出现"半新半旧"的界面；
  · 组装逻辑不依赖 Qt，可以在测试里直接断言字段（H15 要求两端字段一致）。

来源优先级：`platform.status_snapshot()` 为骨架，遥测的实时值覆盖它，
再补上界面专用字段（能力文案、配置差异、上传统计等）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..contracts import Capability, ConnectionState, TaskState


def build_snapshot(app) -> Dict[str, Any]:
    """把 WoodPulseApp 的当前状态整理成界面可直接渲染的 dict。"""
    snapshot: Dict[str, Any] = app.status_snapshot()
    telemetry = app.app.telemetry or {}
    snapshot["telemetry"] = telemetry

    # 遥测里的版本信息比骨架更即时（演示模型版本可能在更新页刚被切换）
    versions = telemetry.get("versions") or {}
    if versions:
        snapshot["modelVersion"] = versions.get("demoModelVersion") or snapshot.get("modelVersion")

    # 界面文案：能力的中文标签
    capability = app.app.capability
    snapshot["capabilityLabels"] = {
        name: capability.label(name) for name in sorted(capability.values)
    }

    # 自检摘要（没有跑过就是 None，界面显示"尚未自检"）
    snapshot["selfCheck"] = app.last_self_check.to_dict() if app.last_self_check else None

    # 配置差异：优先用当前快照与上一版的差异
    diffs: List[Dict[str, Any]] = []
    if app.app.config is not None and app.app.previous_config is not None:
        diffs = [row.__dict__ for row in app.app.config.diff(app.app.previous_config)]
    elif app.app.config is not None:
        diffs = [row.__dict__ for row in app.app.config.diff(None)]
    snapshot["configDiff"] = diffs

    # 能力可用性快捷判断，界面到处都要问"这个能力是真的吗"
    snapshot["capabilityFlags"] = {
        "cameraLive": capability.is_live("camera"),
        "radarReplay": capability.get("radar") == Capability.REPLAY,
        "telemetryLive": capability.is_live("telemetry"),
        "imu": capability.get("imu", Capability.UNAVAILABLE),
        "battery": capability.get("battery", Capability.UNAVAILABLE),
        "gpu": capability.get("gpu", Capability.UNAVAILABLE),
    }

    # 样例包状态（检测样例包这一项每次刷新都要真实读盘）
    try:
        snapshot["samplePackages"] = app.library.status()
    except Exception as exc:  # noqa: BLE001 - 读盘失败不能拖垮界面刷新
        snapshot["samplePackages"] = {"root": "", "ready": 0, "total": 0, "items": [], "error": str(exc)}

    snapshot["logs"] = {"count": app.log_handler.last_seq}
    snapshot["recoveredBatches"] = [item.to_dict() for item in app.recovered_batches]
    snapshot["delivery"] = app.delivery_history[-1].to_dict() if app.delivery_history else None
    snapshot["ui"] = {
        "screenWidth": app.cfg.ui.width,
        "screenHeight": app.cfg.ui.height,
        "mainView": app.cfg.ui.main_view,
    }
    snapshot["paths"] = {
        "dataDir": str(app.cfg.data_path),
        "batchesDir": str(app.cfg.batches_path),
        "logsDir": str(app.cfg.logs_path),
        "scenarioRoot": str(app.library.root),
        "dbPath": str(app.cfg.db_path),
    }
    if app.capture is not None and app.capture.package is not None:
        snapshot["activePackage"] = app.capture.package.summary()
    else:
        snapshot["activePackage"] = None
    return snapshot


def workbench_cards(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """任务工作台要显示的那几块信息（PRD §5.1）。"""
    assignment = (snapshot.get("task") or {}).get("assignment") or {}
    connection = snapshot.get("connection") or {}
    batch = snapshot.get("batch") or {}
    config = snapshot.get("config") or {}
    update = snapshot.get("update") or {}
    packages = snapshot.get("samplePackages") or {}
    flags = snapshot.get("capabilityFlags") or {}
    return {
        "device": {
            "deviceId": snapshot.get("deviceId"),
            "bootId": snapshot.get("bootId"),
            "operatorId": snapshot.get("operatorId"),
            "appVersion": snapshot.get("appVersion"),
            "host": (snapshot.get("fingerprint") or {}).get("hostname"),
        },
        "connection": {
            "state": connection.get("state", ConnectionState.OFFLINE),
            "label": connection.get("label", "离线"),
            "detail": connection.get("platformDetail") or "",
            "latencyMs": connection.get("latencyMs"),
            "lastSeenAt": connection.get("lastSeenAt") or "",
            "offlineCaptureAllowed": True,
        },
        "order": {
            "orderId": assignment.get("order_id"),
            "componentId": assignment.get("component_id"),
            "zoneId": assignment.get("zone_id"),
            "round": assignment.get("round"),
            "source": assignment.get("source"),
            "taskRevision": assignment.get("task_revision"),
        },
        "config": {
            "version": config.get("configVersion") or assignment.get("config_version") or "未下发",
            "state": (snapshot.get("configState") if isinstance(snapshot.get("configState"), str) else None)
            or ("待确认" if config and not batch else "已生效" if config else "未下发"),
            "publishedAt": config.get("publishedAt") or "",
        },
        "nextTask": {
            "batchId": batch.get("batchId") or "",
            "state": batch.get("state") or "",
            "round": batch.get("round") or assignment.get("round"),
            "hint": _next_hint(snapshot, batch),
        },
        "update": {
            "state": update.get("state", "idle"),
            "version": update.get("version") or "",
            "artifactKind": (update.get("artifact") or {}).get("artifactKind") or "",
        },
        "samples": {
            "ready": packages.get("ready", 0),
            "total": packages.get("total", 0),
            "root": packages.get("root", ""),
        },
        "capabilities": flags,
    }


def _next_hint(snapshot: Dict[str, Any], batch: Dict[str, Any]) -> str:
    task_state = ((snapshot.get("task") or {}).get("state")) or TaskState.IDLE
    if task_state == TaskState.RUNNING:
        return "正在采集：必要时点“标记位置”，结束后点“结束并交付”"
    if task_state == TaskState.PAUSED:
        return "已暂停：确认原因后点“继续”，或直接结束本段"
    if task_state in TaskState.TERMINAL:
        return "本批已结束：到“数据交付”页上传并提交给平台"
    if batch.get("batchId"):
        return "批次已就绪：回到“检测作业”页点“开始本次扫描”"
    return "在“检测作业”页选择构件与测区，准备本次批次"


def format_round(round_name: Optional[str]) -> str:
    return {"initial": "初扫", "rescan": "复扫", "reference": "参考样本采集"}.get(str(round_name), str(round_name or "—"))
