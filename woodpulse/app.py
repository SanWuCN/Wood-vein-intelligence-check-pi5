"""终端总装：把配置、存储、遥测、相机、平台客户端、采集流程接到一起。

分层关系（PRD §10 的模块表）：

    app.py  ← GUI 只跟它说话
      ├── app_state.py      状态机与全局状态
      ├── telemetry.py      真实系统指标
      ├── platform_client.py HTTP / WebSocket / 心跳 / 重连 / 回执
      ├── storage.py        SQLite、批次目录、outbox、恢复
      ├── capture.py        采集编排（回放 + 标记 + 落盘 + manifest）
      ├── selfcheck.py      逐项自检
      ├── update.py         演示模型包的接收、校验、切换、回验
      └── adapters/         camera（实采）、replay（固定样例）

**本文件不导入 Qt**：`WoodPulseApp` 可以在没有图形环境的机器上直接跑，
因此 H01/H02/H05/H06/H08/H14 这些验收用例能用普通 Python 脚本验证，
不需要真的在树莓派屏幕上点一遍。
"""

from __future__ import annotations

import json
import os
import platform as platform_module
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters.camera import CameraSource, encode_jpeg
from .adapters.replay import ReplayLibrary
from .app_state import AppState, SelfCheckItem, TaskAssignment, UploadJob
from .capture import CaptureController, CaptureTick, MarkResult
from .config import AppConfig, load_config
from .contracts import (
    ADAPTER_VERSION,
    APP_VERSION,
    Capability,
    CapabilityReport,
    Command,
    ConfigState,
    ConnectionState,
    ErrorCode,
    EventType,
    OperationLabel,
    ReceiptState,
    TaskState,
    UploadState,
    build_capabilities_payload,
    build_register_payload,
    new_id,
    utc_now_iso,
)
from .logging_setup import LogLine, get_logger, setup_logging
from .platform_client import ClientCallbacks, HttpClient, PlatformClient, upload_file
from .scenarios import PAUSE_SCOPE_NOTE, SCENARIOS, discover, resolve_scenario_root
from .selfcheck import SelfCheckReport, SelfCheckService
from .storage import RecoveredBatch, Storage
from .telemetry import TelemetryService, format_bytes, format_rate, system_fingerprint
from .update import UpdateService, UpdateStep

log = get_logger("app")


# --------------------------------------------------------------------------- #
# 配置与状态之间的一层便捷视图
# --------------------------------------------------------------------------- #

@dataclass
class DeliveryReport:
    """一次数据交付的结果（PRD §5.5：结束采集 ≠ 上传完成 ≠ 平台分析完成）。"""

    batch_id: str
    submitted_files: int = 0
    uploaded_files: int = 0
    failed_files: int = 0
    total_bytes: int = 0
    uploaded_bytes: int = 0
    complete: bool = False
    missing: List[str] = field(default_factory=list)
    platform_ack: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batchId": self.batch_id,
            "submittedFiles": self.submitted_files,
            "uploadedFiles": self.uploaded_files,
            "failedFiles": self.failed_files,
            "totalBytes": self.total_bytes,
            "uploadedBytes": self.uploaded_bytes,
            "complete": self.complete,
            "missing": list(self.missing),
            "platformAck": dict(self.platform_ack),
            "error": self.error,
        }


class WoodPulseApp:
    """终端应用核心。GUI 只调用这里的方法，不直接碰 socket、sqlite 或相机。"""

    def __init__(self, cfg: Optional[AppConfig] = None, *, on_ui_event: Optional[Callable[[str, Any], None]] = None) -> None:
        self.cfg = cfg or load_config()
        self.cfg.ensure_dirs()
        self.on_ui_event = on_ui_event

        # 日志：内存环形缓冲 + 轮转文件
        self.log_handler = setup_logging(
            self.cfg.log_level,
            self.cfg.logs_path,
            memory_capacity=self.cfg.log_max_lines,
            file_max_bytes=self.cfg.log_file_max_bytes,
            file_backups=self.cfg.log_file_backups,
            console=os.environ.get("WOODPULSE_CONSOLE_LOG", "1") != "0",
        )

        self.storage = Storage(self.cfg)
        self.app = AppState(
            device_id=self.cfg.effective_device_id,
            demo_session_id=self.cfg.platform.demo_session_id,
            operator_id=self.cfg.operator_id,
        )
        self.library = ReplayLibrary(configured=self.cfg.scenario_root)
        self.telemetry = TelemetryService(self.cfg, data_dir=self.cfg.data_path)

        self.camera: Optional[CameraSource] = None
        self.platform: Optional[PlatformClient] = None
        self.capture: Optional[CaptureController] = None
        self.selfcheck_service: Optional[SelfCheckService] = None
        self.update_service: Optional[UpdateService] = None

        self.last_tick: Optional[CaptureTick] = None
        self.last_self_check: Optional[SelfCheckReport] = None
        self.recovered_batches: List[RecoveredBatch] = []
        self.delivery_history: List[DeliveryReport] = []
        self.fingerprint = system_fingerprint()
        self._scenario_override = self.cfg.__dict__.get("_scenario_override")
        self._preview_last_at = 0.0
        self._started_at = time.monotonic()

        self._restore_state()
        self._build_camera()
        self._detect_capabilities()
        self._build_platform()
        self.capture = CaptureController(
            self.app,
            self.storage,
            self.library,
            self.camera,
            on_event=lambda level, source, text: self._emit_log(level, source, text),
        )
        self.selfcheck_service = SelfCheckService(self.app, self.storage, self.library, self.camera, self.platform)
        if self.platform is not None:
            self.update_service = UpdateService(
                self.app,
                self.storage,
                self.platform.http,
                staging_dir=Path(self.cfg.staging_path),
                on_step=lambda step: self._emit("update.step", step.to_dict()),
            )

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #

    def _restore_state(self) -> None:
        """恢复上次的配置快照与版本，并把未完成批次标为中断（PRD §8.2、H14）。"""
        self.recovered_batches = self.storage.recover_interrupted(self.app.boot_id)
        active = self.storage.all_active_versions()
        if active.get("config"):
            raw = self.storage.get_state("config_snapshot")
            if raw:
                try:
                    from .app_state import ConfigSnapshot

                    snapshot = ConfigSnapshot.from_dict(json.loads(raw))
                    self.app.config = snapshot
                    self.app.config_machine.reset(ConfigState.APPLIED, "从本地库恢复")
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    log.warning("本地配置快照不可解析：%s", exc)
        demo_model = active.get("demo_model")
        if demo_model:
            self.app.demo_model_version = demo_model
            self.app.model_version = demo_model
        controller = active.get("controller")
        self.app.controller_version = controller  # 没有就是 None，界面显示"未接入"
        self.storage.record_version("app", APP_VERSION, {"adapter": ADAPTER_VERSION}, active=True)
        self.storage.record_version("adapter", ADAPTER_VERSION, {}, active=True)

        # 默认任务绑定：本地兜底一份，平台 assign_task 会覆盖它
        self.app.assignment = TaskAssignment(
            scenario_id=self._scenario_override or "initial-anomaly-v1",
            round=SCENARIOS.get(self._scenario_override or "initial-anomaly-v1", SCENARIOS["initial-anomaly-v1"]).round,
            config_version=self.app.config.config_version if self.app.config else "CFG-02",
            model_version=self.app.model_version,
            source="local-default",
        )

    def _build_camera(self) -> None:
        self.camera = CameraSource(self.cfg.camera, on_state=lambda stats: self._emit("camera.state", stats.to_dict()))
        if self.cfg.camera.backend != "none":
            started = self.camera.start()
            if not started:
                log.warning("相机未能启动：%s", self.camera.stats.reason)

    def _detect_capabilities(self) -> None:
        """能力字段必须根据启动检查生成（PRD §7.2）。"""
        camera_available = bool(self.camera and self.camera.available)
        camera_reason = self.camera.stats.reason if self.camera else "未启用相机适配器"
        report = CapabilityReport()
        mapping = self.telemetry.probe_capabilities(camera_available=camera_available, camera_reason=camera_reason)
        for name, (value, reason) in mapping.items():
            report.set(name, value, reason)
        self.app.capability = report
        log.info(
            "能力声明：%s",
            "、".join(f"{name}={report.label(name)}" for name in sorted(report.values)),
        )

    def refresh_capabilities(self) -> List[str]:
        """运行时重新探测（相机插拔、平台连接变化后调用）。返回变化的字段。"""
        changed: List[str] = []
        if self.camera is not None:
            value, reason = self.camera.capability()
            if self.app.capability.get("camera") != value:
                self.app.set_capability("camera", value, reason)
                changed.append("camera")
        return changed

    def _build_platform(self) -> None:
        callbacks = ClientCallbacks(
            on_state=lambda state, detail: self._on_platform_state(state, detail),
            on_command=self.handle_command,
            on_config=self._on_platform_config,
            on_artifact=self._on_platform_artifact,
            on_task=self._on_platform_task,
            on_events_acked=self._on_events_acked,
            on_upload_progress=lambda data: self._emit("upload.progress", data),
            on_upload_done=self._on_upload_done,
            on_latency=lambda value: self._on_latency(value),
        )
        self.platform = PlatformClient(self.cfg, self.storage, callbacks)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> Dict[str, Any]:
        """启动平台客户端并做首次握手 + 注册。返回握手结果供界面显示。"""
        handshake: Dict[str, Any] = {"ok": False, "detail": "未启用平台客户端"}
        if self.platform is not None:
            payload = build_register_payload(
                device_id=self.app.device_id,
                boot_id=self.app.boot_id,
                capabilities=self.app.capability,
                app_version=APP_VERSION,
                adapter_version=ADAPTER_VERSION,
                model_version=self.app.model_version,
                operator_id=self.app.operator_id,
            )
            ok, body = self.platform.register_device(payload)
            handshake = {"ok": ok, "detail": "握手成功" if ok else f"握手失败：{'/'.join(str(v) for v in body.values())[:120]}", "body": body}
            # 平台下发的通道参数（心跳间隔、离线阈值）优先于本地默认值
            if ok:
                interval = body.get("heartbeatIntervalMs")
                if isinstance(interval, (int, float)) and interval > 0:
                    self.cfg.platform.heartbeat_interval_s = float(interval) / 1000.0
                offline = body.get("offlineAfterMs")
                if isinstance(offline, (int, float)) and offline > 0:
                    self.cfg.platform.offline_after_s = float(offline) / 1000.0
                version = body.get("configVersion")
                if version:
                    self.app.assignment.config_version = str(version)
                session = body.get("demoSessionId")
                if session:
                    self.app.demo_session_id = str(session)
            self.platform.start(self.app.boot_id)
        log.info("终端启动：设备 %s，启动 ID %s，数据目录 %s", self.app.device_id, self.app.boot_id, self.cfg.data_path)
        return handshake

    def shutdown(self, reason: str = "操作者退出程序") -> None:
        """退出顺序：停定时器（GUI 负责）→ 请求线程退出 → 等结束 → 释放相机。

        （PRD §2、§10：旧版没有正常停止与 release 流程。）
        """
        log.info("开始退出：%s", reason)
        if self.capture is not None and self.app.task.state in (TaskState.RUNNING, TaskState.PAUSED):
            self.capture.interrupt(reason)
        if self.platform is not None:
            if self.platform.pending_uploads:
                log.info("仍有 %d 项上传排队，退出前不再等待（数据已在本地批次目录，可下次续传）", self.platform.pending_uploads)
            self.platform.stop()
        if self.camera is not None:
            self.camera.stop()
        self.storage.set_state("last_shutdown", utc_now_iso())
        self.storage.close()
        log.info("退出完成")

    # ------------------------------------------------------------------ #
    # 采集控制（GUI 直接调）
    # ------------------------------------------------------------------ #

    def prepare_task(
        self,
        *,
        component_id: str = "Z04",
        zone_id: str = "Z04-lower",
        round_name: str = "initial",
        order_id: str = "SH-2026-0901",
        scenario_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        if self.capture is None:
            return False, "采集模块未初始化"
        scenario = scenario_id or self._scenario_for_round(round_name)
        if scenario is None:
            return False, f"没有与轮次 {round_name} 对应的样例包"
        assignment = TaskAssignment(
            order_id=order_id,
            component_id=component_id,
            zone_id=zone_id,
            round=round_name,
            scenario_id=scenario,
            # 批次号与所选样例包的 manifest 对齐：平台、终端与样例包必须指向同一个
            # batchId，否则 H15「两端查看结果一致」就无从核对
            config_version=self.app.active_config_version(),
            model_version=self.app.model_version,
            task_revision=self.app.assignment.task_revision,
            source="local-ui",
            received_at=utc_now_iso(),
        )
        self.app.bind_assignment(assignment)
        spec = SCENARIOS.get(scenario)
        ok, message = self.capture.prepare(assignment, batch_id=batch_id or (spec.batch_id if spec else None))
        if ok:
            self._emit("task.prepared", self.app.batch.to_manifest() if self.app.batch else {})
            self._send_event(
                EventType.CAPTURE_PROGRESS,
                {
                    "batchId": self.app.batch.batch_id if self.app.batch else "",
                    "componentId": component_id,
                    "zoneId": zone_id,
                    "stage": "prepared",
                    "round": round_name,
                },
            )
        return ok, message

    def _scenario_for_round(self, round_name: str) -> Optional[str]:
        from .scenarios import scenario_for_round

        if self._scenario_override:
            return self._scenario_override
        spec = scenario_for_round(round_name)
        if spec and self.library.has(spec.scenario_id):
            return spec.scenario_id
        # 该轮次的包缺失时，退回任意一套可用的，并明确记日志（不静默换数据）
        for entry in discover(self.library.root):
            if entry["present"]:
                log.warning("轮次 %s 的样例包缺失，临时改用 %s（自检会报 warn）", round_name, entry["scenarioId"])
                return entry["scenarioId"]
        return None

    def start_capture(self) -> Tuple[bool, str]:
        if self.capture is None:
            return False, "采集模块未初始化"
        if self.platform is not None and self.platform.state != ConnectionState.ONLINE and not self.cfg.platform.allow_offline_capture:
            return False, "平台离线且配置禁止离线采集"
        ok, message = self.capture.start()
        if ok:
            batch = self.app.batch
            self._send_event(
                EventType.CAPTURE_STARTED,
                {
                    "batchId": batch.batch_id if batch else "",
                    "componentId": batch.component_id if batch else "",
                    "zoneId": batch.zone_id if batch else "",
                    "configVersion": batch.config_version if batch else "",
                    "modelVersion": batch.model_version if batch else "",
                    "scenarioId": batch.scenario_id if batch else "",
                    "sourceMode": "replay",
                    "frameCount": batch.frame_count_expected if batch else 0,
                    "datasetId": self._dataset_id(),
                    "datasetHash": self._dataset_hash(),
                },
            )
        return ok, message

    def pause_capture(self, reason: str = "操作者暂停") -> Tuple[bool, str]:
        if self.capture is None:
            return False, "采集模块未初始化"
        ok = self.capture.pause(reason, source="local")
        if ok:
            self._send_event(
                EventType.CAPTURE_PAUSED,
                {
                    "batchId": self.app.batch.batch_id if self.app.batch else "",
                    "reason": reason,
                    "scope": self.app.command_scope(Command.PAUSE_CAPTURE),
                    "frameIndex": self.capture.session.frame_index if self.capture.session else None,
                    "savedFrames": self.app.batch.frames_returned if self.app.batch else 0,
                    "markCount": self.app.batch.mark_count if self.app.batch else 0,
                },
            )
        return ok, "" if ok else "当前状态不能暂停"

    def resume_capture(self, reason: str = "操作者继续") -> Tuple[bool, str]:
        if self.capture is None:
            return False, "采集模块未初始化"
        ok = self.capture.resume(reason, source="local")
        if ok:
            self._send_event(
                EventType.CAPTURE_RESUMED,
                {"batchId": self.app.batch.batch_id if self.app.batch else "", "reason": reason},
            )
        return ok, "" if ok else "当前状态不能继续"

    def finish_capture(self, reason: str = "操作者确认结束") -> Tuple[bool, Dict[str, Any]]:
        if self.capture is None:
            return False, {}
        ok, manifest = self.capture.finish(reason)
        if ok:
            batch = self.app.batch
            self._send_event(
                EventType.CAPTURE_FINISHED,
                {
                    "batchId": batch.batch_id if batch else "",
                    "savedFrames": batch.frames_returned if batch else 0,
                    "markCount": batch.mark_count if batch else 0,
                    "datasetHash": batch.dataset_hash if batch else "",
                    "state": "finished",
                },
            )
            self._send_event(
                EventType.BATCH_FINALIZED,
                {
                    "batchId": batch.batch_id if batch else "",
                    "manifest": manifest,
                },
            )
        return ok, manifest

    def add_mark(self, operator_label: str = "") -> MarkResult:
        if self.capture is None:
            return MarkResult(False, error="采集模块未初始化")
        result = self.capture.add_mark(operator_label)
        if result.ok and result.mark is not None:
            self._send_event(
                EventType.CAPTURE_MARK_CREATED,
                {
                    "batchId": result.mark.batch_id,
                    "markId": result.mark.mark_id,
                    "frameId": result.mark.frame_id,
                    "frameIndex": result.mark.frame_index,
                    "cameraAssetId": result.mark.camera_asset_id,
                    "deviceMonotonicNs": result.mark.device_monotonic_ns,
                    "operatorLabel": result.mark.operator_label,
                    "positionSource": result.mark.position_source,
                    "imageOk": result.mark.image_ok,
                    "note": result.mark.note,
                },
            )
            self._emit("mark.added", result.mark.to_dict())
        return result

    def set_last_mark_label(self, operator_label: str) -> bool:
        if self.capture is None:
            return False
        return self.capture.set_last_mark_label(operator_label)

    def tick(self, dt: float = 0.1) -> CaptureTick:
        """GUI 定时器调用。同时负责：遥测上报、预览上传、能力刷新。"""
        tick = CaptureTick()
        if self.capture is not None:
            tick = self.capture.tick(dt)
            self.last_tick = tick
            if tick.frames_added:
                self._emit("capture.frames", {"count": tick.frames_added, "frameIndex": tick.frame_index})
            for event in tick.events:
                self._send_event(
                    EventType.CAPTURE_ANOMALY if event.get("kind") == "finding" else EventType.CAPTURE_PROGRESS,
                    {"batchId": self.app.batch.batch_id if self.app.batch else "", **event},
                )

        # 遥测按 1 秒节奏推给平台（PRD §6）
        if self.platform is not None:
            payload = self.telemetry.snapshot(
                camera=self.camera.stats.to_dict() if self.camera else None,
                replay=self.capture.telemetry_stats() if self.capture else None,
                uploads=self.app.upload_summary(),
                versions={
                    "appVersion": APP_VERSION,
                    "adapterVersion": ADAPTER_VERSION,
                    "controllerVersion": self.app.controller_version,
                    "demoModelVersion": self.app.demo_model_version or self.app.model_version,
                    "configVersion": self.app.active_config_version(),
                },
            )
            self.app.telemetry = payload
            self.app.telemetry_updated_at = payload.get("sampledAt", "")
            self.platform.queue_telemetry(payload)
            self._emit("telemetry.updated", payload)
            self._maybe_upload_preview()
        return tick

    def _maybe_upload_preview(self) -> None:
        """低帧率预览图上传（PRD §4.3、§7.4）：1—2 fps，不改变原始照片尺寸。"""
        if not (self.cfg.upload_preview and self.cfg.platform.preview_upload):
            return
        if self.platform is None or self.camera is None or not self.camera.available:
            return
        fps = max(0.2, float(self.cfg.platform.preview_upload_fps))
        now = time.monotonic()
        if now - self._preview_last_at < 1.0 / fps:
            return
        frame = self.camera.latest()
        if frame is None or frame.age_ms > 2000:
            return
        data = encode_jpeg(frame, target_width=640, quality=self.cfg.camera.preview_jpeg_quality)
        if not data:
            return
        self._preview_last_at = now
        self.platform.queue_preview(data, frame.index)

    # ------------------------------------------------------------------ #
    # 数据交付
    # ------------------------------------------------------------------ #

    def upload_files(self, batch_id: str) -> List[Dict[str, Any]]:
        """把批次目录里的文件排进上传队列并登记到本地库（PRD §5.5、§9）。

        每个文件都带上**本机重算的摘要**：平台在 `/complete` 时会重算并比对，
        没有摘要的建单会被平台拒掉。这里对空摘要做一次补齐，避免"最后一个文件
        永远传不上去"这种只在联调时才暴露的问题。
        """
        files = self.storage.files_for_batch(batch_id)
        base = self.storage.batch_dir(batch_id)
        jobs: List[Dict[str, Any]] = []
        for item in files:
            path = base / item["rel_path"]
            sha256 = item["sha256"] or ""
            if not sha256 and path.is_file():
                from .contracts import hash_sample_file

                sha256 = hash_sample_file(str(path))
                log.warning("文件 %s 缺少登记摘要，已现场重算（%s…）", item["rel_path"], sha256[:12])
                self.storage.register_file(batch_id, item["role"], item["rel_path"], base_dir=base)
            job = {
                "path": str(path),
                "name": item["name"],
                "role": item["role"],
                "relPath": item["rel_path"],
                "size": item["size"],
                "sha256": sha256,
                "receivedOffset": item["received_offset"],
                "uploadId": "",
            }
            jobs.append(job)
            self.app.upsert_upload(
                UploadJob(
                    upload_id=f"up-{item['id']}",
                    batch_id=batch_id,
                    file_id=item["id"],
                    name=item["name"],
                    role=item["role"],
                    path=job["path"],
                    size=item["size"],
                    sha256=item["sha256"],
                    state=UploadState.QUEUED,
                )
            )
        if self.platform is not None:
            self.platform.request_upload(batch_id, jobs)
        if self.app.batch and self.app.batch.batch_id == batch_id:
            self.app.batch.upload_state = UploadState.ACTIVE
        self.storage.log_event("batch.upload_started", batch_id, {"files": len(jobs)})
        self._send_event(EventType.BATCH_UPLOAD_STARTED, {"batchId": batch_id, "fileCount": len(jobs)})
        return jobs

    def submit_batch(self, batch_id: str) -> Tuple[bool, Dict[str, Any]]:
        """把 manifest 与已上传的 fileId 提交给平台，拿到完整/部分接收的结论。"""
        if self.platform is None:
            return False, {"error": "未启用平台客户端"}
        record = self.storage.get_batch(batch_id)
        if not record:
            return False, {"error": f"本地没有批次 {batch_id}"}
        manifest_path = self.storage.batch_dir(batch_id) / "manifest.json"
        if not manifest_path.is_file():
            return False, {"error": "manifest 还没提交，按 PRD §8.2 不允许上传/交付"}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, {"error": f"manifest 不可读：{exc}"}
        files = self.storage.files_for_batch(batch_id)
        manifest["fileIds"] = [
            {"fileId": item["remote_id"], "role": item["role"], "path": item["rel_path"], "sha256": item["sha256"], "bytes": item["size"]}
            for item in files
            if item["remote_id"]
        ]
        manifest["missingRemote"] = [item["rel_path"] for item in files if not item["remote_id"]]
        result = self.platform.submit_batch(manifest)
        report = DeliveryReport(
            batch_id=batch_id,
            submitted_files=len(files),
            uploaded_files=len(manifest["fileIds"]),
            failed_files=len(manifest["missingRemote"]),
            total_bytes=sum(item["size"] for item in files),
            uploaded_bytes=sum(item["size"] for item in files if item["remote_id"]),
            complete=bool(result.body.get("complete")) if result.ok else False,
            missing=list(result.body.get("missing") or manifest["missingRemote"]),
            platform_ack=dict(result.body),
            error="" if result.ok else result.error,
        )
        self.delivery_history.append(report)
        self.storage.log_event("batch.upload_completed", batch_id, report.to_dict())
        self._send_event(EventType.BATCH_UPLOAD_COMPLETED, report.to_dict())
        return result.ok, report.to_dict()

    # ------------------------------------------------------------------ #
    # 平台回调
    # ------------------------------------------------------------------ #

    def _on_platform_state(self, state: str, detail: str) -> None:
        self.app.set_connection(state, detail)
        self.app.note_platform_seen()
        self._emit("platform.state", {"state": state, "detail": detail, "label": ConnectionState.LABEL.get(state, state)})

    def _on_latency(self, value: Optional[float]) -> None:
        self.app.platform_latency_ms = value
        self.telemetry.record_platform_latency(value)
        self._emit("platform.latency", {"latencyMs": value})

    def _on_events_acked(self, message_ids: List[str]) -> None:
        self.app.pending_events = self.storage.pending_event_count()
        self._emit("outbox.acked", {"messageIds": message_ids, "pending": self.app.pending_events})

    def _on_upload_done(self, data: Dict[str, Any]) -> None:
        job = data.get("job") or {}
        for item in self.app.uploads:
            if item.name == job.get("name") and item.batch_id == job.get("batchId"):
                item.state = UploadState.DONE if data.get("ok") else UploadState.FAILED
                item.remote_file_id = str(data.get("fileId") or "")
                item.last_error = str(data.get("error") or "")
                if data.get("ok"):
                    item.received_offset = item.size
                self.app.upsert_upload(item)
                break
        self.app.pending_events = self.storage.pending_event_count()
        self._emit("upload.done", data)

    def _on_platform_config(self, payload: Dict[str, Any]) -> None:
        """收到环境配置：算差异、进"待确认"、通知界面（PRD §5.2、H09）。"""
        from .app_state import ConfigSnapshot

        snapshot = ConfigSnapshot.from_dict(payload)
        if not snapshot.has_data:
            log.warning("收到的配置缺少 configVersion，已忽略：%s", json.dumps(payload, ensure_ascii=False)[:200])
            return
        diffs = self.app.apply_config(snapshot)
        self.storage.set_state("config_snapshot", json.dumps(snapshot.to_dict(), ensure_ascii=False))
        self.storage.record_version("config", snapshot.config_version, snapshot.to_dict(), active=False)
        self._send_event(
            EventType.CONFIG_RECEIVED,
            {
                "configVersion": snapshot.config_version,
                "source": snapshot.source,
                "publishedAt": snapshot.published_at,
                "receivedAt": snapshot.received_at,
                "diff": [row.__dict__ for row in diffs],
                "needsOperatorConfirm": True,
            },
        )
        self._emit("config.received", {"snapshot": snapshot.to_dict(), "diff": [row.__dict__ for row in diffs]})

    def confirm_config(self) -> Tuple[bool, str]:
        """操作者确认差异 → 保存快照 → 回传 ack（PRD §5.2）。"""
        if not self.app.confirm_config():
            return False, "当前没有待确认的配置"
        snapshot = self.app.config
        assert snapshot is not None
        self.storage.set_state("config_snapshot", json.dumps(snapshot.to_dict(), ensure_ascii=False))
        self.storage.record_version("config", snapshot.config_version, snapshot.to_dict(), active=True)
        self.app.assignment.config_version = snapshot.config_version
        self._send_event(
            EventType.CONFIG_APPLIED,
            {
                "configVersion": snapshot.config_version,
                "appliedAt": utc_now_iso(),
                "scope": self.app.command_scope(Command.APPLY_CONFIG),
                "snapshot": snapshot.to_dict(),
            },
        )
        if self.platform is not None:
            self.platform.http.request(
                "POST",
                f"/api/configs/{snapshot.config_version}/ack",
                body={
                    "deviceId": self.app.device_id,
                    "configVersion": snapshot.config_version,
                    "state": "applied",
                    "bootId": self.app.boot_id,
                    "appliedAt": utc_now_iso(),
                },
            )
        self.app.mark_config_applied()
        self._emit("config.applied", snapshot.to_dict())
        return True, snapshot.config_version

    def _on_platform_task(self, payload: Dict[str, Any]) -> None:
        """平台派发任务（assign_task）。接受后立刻回 executed（任务绑定是本地事实）。"""
        self.apply_assign_task(payload, source="platform")

    def apply_assign_task(self, payload: Dict[str, Any], *, source: str = "platform") -> Tuple[bool, str]:
        order_id = str(payload.get("orderId") or payload.get("order_id") or "SH-2026-0901")
        component_id = str(payload.get("componentId") or payload.get("component_id") or "Z04")
        zone_id = str(payload.get("zoneId") or payload.get("zone_id") or "Z04-lower")
        round_name = str(payload.get("round") or payload.get("phase") or "initial")
        scenario_id = payload.get("scenarioId") or payload.get("scenario_id") or self._scenario_for_round(round_name)
        revision = payload.get("taskRevision") or payload.get("revision") or self.app.assignment.task_revision
        self.app.bind_assignment(
            TaskAssignment(
                order_id=order_id,
                component_id=component_id,
                zone_id=zone_id,
                round=round_name,
                task_revision=int(revision or 1),
                scenario_id=scenario_id or "initial-anomaly-v1",
                config_version=str(payload.get("configVersion") or self.app.active_config_version()),
                model_version=str(payload.get("modelVersion") or self.app.model_version),
                source=source,
                received_at=utc_now_iso(),
            )
        )
        log.info("任务绑定已更新：%s / %s / %s（来源 %s）", order_id, component_id, zone_id, source)
        self._emit("task.assigned", self.app.assignment.__dict__)
        return True, f"已绑定 {order_id} {component_id} {zone_id}"

    def _on_platform_artifact(self, payload: Dict[str, Any]) -> None:
        if self.update_service is None:
            return
        notification = self.update_service.receive(payload)
        self._emit("update.received", notification.raw)

    # ------------------------------------------------------------------ #
    # 命令处理（PRD §8.1：accepted / executed / failed）
    # ------------------------------------------------------------------ #

    def handle_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """处理平台命令，返回回执报文。

        流程固定为：先做校验 → 立即回 accepted → 真正执行 → 回 executed 或 failed。
        暂停类命令必须先停止采集/回放推进，再回 executed（PRD §5.3）。
        """
        command_id = str(command.get("commandId") or command.get("command_id") or new_id("cmd"))
        action = str(command.get("action") or command.get("type") or "")
        command = {**command, "commandId": command_id, "action": action}

        accepted = self.app.command_receipt(command, ReceiptState.ACCEPTED, reason="命令已接收，正在执行")
        self._send_receipt(command, accepted)
        self.storage.save_receipt(command_id, accepted)

        ok, error_code, reason = self.app.validate_command(command)
        if not ok:
            log.warning("拒绝执行命令 %s（%s）：%s", action, error_code, reason)
            failed = self.app.command_receipt(command, ReceiptState.FAILED, error_code=error_code, reason=reason)
            self.storage.save_receipt(command_id, failed)
            self._send_receipt(command, failed)
            self._emit("command.failed", failed)
            return failed

        handler = getattr(self, f"_cmd_{action}", None)
        if handler is None:
            failed = self.app.command_receipt(
                command, ReceiptState.FAILED, error_code=ErrorCode.UNSUPPORTED, reason=f"本机没有实现 {action}"
            )
            self.storage.save_receipt(command_id, failed)
            self._send_receipt(command, failed)
            return failed

        try:
            executed_ok, detail, result = handler(command)
        except Exception as exc:  # noqa: BLE001 - 命令处理异常必须回 failed，不能静默
            log.exception("命令 %s 执行异常", action)
            failed = self.app.command_receipt(
                command, ReceiptState.FAILED, error_code=ErrorCode.IO_ERROR, reason=f"执行异常：{exc}"
            )
            self.storage.save_receipt(command_id, failed)
            self._send_receipt(command, failed)
            return failed

        if executed_ok:
            receipt = self.app.command_receipt(command, ReceiptState.EXECUTED, reason=detail, result=result)
        else:
            receipt = self.app.command_receipt(command, ReceiptState.FAILED, error_code=detail, reason=result.get("reason", ""))
        self.storage.save_receipt(command_id, receipt)
        self._send_receipt(command, receipt)
        self._emit("command.executed" if executed_ok else "command.failed", receipt)
        return receipt

    def _cmd_assign_task(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        payload = command.get("payload") or {}
        ok, message = self.apply_assign_task(payload)
        return ok, message if ok else ErrorCode.UNSUPPORTED, {"message": message}

    def _cmd_apply_config(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        payload = command.get("payload") or {}
        self._on_platform_config(payload)
        snapshot = self.app.config
        if snapshot is None or not snapshot.has_data:
            return False, ErrorCode.UNSUPPORTED, {"reason": "配置载荷缺少 configVersion"}
        # 收到即确认：现场由操作者在界面上点"确认差异"，命令路径只完成接收与持久化
        return True, f"已接收配置 {snapshot.config_version} 并保存快照，等待操作者确认差异", {
            "configVersion": snapshot.config_version,
            "needsOperatorConfirm": True,
            "scope": self.app.command_scope(Command.APPLY_CONFIG),
        }

    def _cmd_pause_capture(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """平台请求暂停：**先停采集推进，再回 executed**（PRD §5.3）。"""
        if self.capture is None or self.app.batch is None:
            return False, ErrorCode.NOT_READY, {"reason": "本机当前没有活动批次，无需暂停"}
        if self.app.task.state == TaskState.PAUSED:
            return True, f"本地采集已处于暂停状态（{self.app.task.reason}），本次命令无额外动作", {
                "scope": self.app.command_scope(Command.PAUSE_CAPTURE),
                "alreadyPaused": True,
            }
        if self.app.task.state != TaskState.RUNNING:
            return False, ErrorCode.TERMINAL_STATE, {"reason": f"任务当前为 {self.app.task.label}，不接受暂停"}
        if self.cfg.obey_pause:
            ok, _ = self.pause_capture(f"平台请求暂停：{command.get('payload', {}).get('reason', '未注明原因')}")
            if not ok:
                return False, ErrorCode.NOT_READY, {"reason": "本地暂停未成功，未回 executed"}
        self.capture.mark_platform_pause()
        self.app.mark_diagnosis_frozen(
            str((command.get("payload") or {}).get("reason") or "平台请求暂停，该批诊断输出冻结")
        )
        return True, "本地采集任务与检测样例回放已停止推进；健康遥测与心跳继续运行", {
            "scope": self.app.command_scope(Command.PAUSE_CAPTURE),
            "frameIndex": self.capture.session.frame_index if self.capture.session else None,
            "savedFrames": self.app.batch.frames_returned,
            "pauseScopeNote": PAUSE_SCOPE_NOTE,
        }

    def _cmd_request_upload(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        payload = command.get("payload") or {}
        batch_id = str(payload.get("batchId") or command.get("targetBatchId") or (self.app.batch.batch_id if self.app.batch else ""))
        if not batch_id:
            return False, ErrorCode.NOT_READY, {"reason": "本机没有可上传的批次"}
        record = self.storage.get_batch(batch_id)
        if not record:
            return False, ErrorCode.BATCH_MISMATCH, {"reason": f"本地没有批次 {batch_id}"}
        if not record["manifest_committed"]:
            return False, ErrorCode.NOT_READY, {"reason": "批次还没封存（manifest 未提交），按规则不允许上传"}
        jobs = self.upload_files(batch_id)
        return True, f"已把 {len(jobs)} 个文件加入上传队列", {"batchId": batch_id, "fileCount": len(jobs)}

    def _cmd_prepare_update(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        if self.update_service is None:
            return False, ErrorCode.UNSUPPORTED, {"reason": "本机未启用更新服务"}
        payload = command.get("payload") or {}
        self.update_service.receive(payload)
        allowed, reason = self.update_service.can_apply_now()
        return True, "已接收更新通知并记录；下载与校验由操作者在更新页执行", {
            "artifact": (self.update_service.notification.raw if self.update_service.notification else {}),
            "canApplyNow": allowed,
            "applyBlockedReason": "" if allowed else reason,
        }

    def _cmd_query_status(self, command: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        return True, "本机状态快照", self.status_snapshot()

    def _send_receipt(self, command: Dict[str, Any], receipt: Dict[str, Any]) -> None:
        if self.platform is None:
            return
        self.platform.enqueue_event(
            EventType.COMMAND_EXECUTED if receipt.get("state") == ReceiptState.EXECUTED else EventType.COMMAND_ACCEPTED
            if receipt.get("state") == ReceiptState.ACCEPTED
            else EventType.COMMAND_FAILED,
            receipt,
            critical=True,
        )

    # ------------------------------------------------------------------ #
    # 事件与日志
    # ------------------------------------------------------------------ #

    def _send_event(self, type_: str, payload: Dict[str, Any], *, critical: bool = True) -> str:
        if self.platform is None:
            return ""
        message_id = self.platform.enqueue_event(type_, payload, critical=critical)
        self.app.pending_events = self.storage.pending_event_count()
        return message_id

    def _dataset_id(self) -> str:
        """本次采集使用的样例包标识。

        样例包自带的 datasetId/datasetHash 只与"用的是哪套样例"有关，
        重复排练两轮必须一致（PRD §13 H06）；批次产物自己的摘要放在
        manifest.datasetHash 里，两者不要混用。
        """
        if self.capture is not None and self.capture.package is not None:
            return str(self.capture.package.manifest.get("datasetId") or "")
        return ""

    def _dataset_hash(self) -> str:
        if self.capture is not None and self.capture.package is not None:
            return self.capture.package.dataset_hash
        return ""

    def _emit(self, topic: str, payload: Any) -> None:
        if self.on_ui_event:
            try:
                self.on_ui_event(topic, payload)
            except Exception:  # noqa: BLE001
                pass

    def _emit_log(self, level: str, source: str, text: str) -> None:
        self._emit("log", {"level": level, "source": source, "text": text})

    def recent_logs(self, limit: int = 200) -> List[LogLine]:
        return self.log_handler.lines(limit)

    # ------------------------------------------------------------------ #
    # 自检
    # ------------------------------------------------------------------ #

    def run_self_check(self) -> SelfCheckReport:
        if self.selfcheck_service is None:
            raise RuntimeError("自检服务未初始化")
        self.refresh_capabilities()
        report = self.selfcheck_service.run()
        self.last_self_check = report
        self._send_event("device.selfcheck", report.to_dict(), critical=True)
        self._emit("selfcheck.done", report.to_dict())
        return report

    # ------------------------------------------------------------------ #
    # 状态快照
    # ------------------------------------------------------------------ #

    def status_snapshot(self) -> Dict[str, Any]:
        batch = self.app.batch
        return {
            "schemaVersion": "1.0",
            "deviceId": self.app.device_id,
            "bootId": self.app.boot_id,
            "operatorId": self.app.operator_id,
            "demoSessionId": self.app.demo_session_id,
            "appVersion": APP_VERSION,
            "adapterVersion": ADAPTER_VERSION,
            "modelVersion": self.app.model_version,
            "controllerVersion": self.app.controller_version,
            "capabilities": self.app.capability.to_dict(),
            "capabilityReasons": dict(self.app.capability.reasons),
            "task": {
                "state": self.app.task.state,
                "stateLabel": self.app.task.label,
                "reason": self.app.task.reason,
                "assignment": self.app.assignment.__dict__,
            },
            "batch": batch.to_manifest() if batch else None,
            "config": self.app.config.to_dict() if self.app.config else None,
            "connection": {
                "state": self.app.connection.state,
                "label": ConnectionState.LABEL.get(self.app.connection.state, self.app.connection.state),
                "detail": self.app.connection.state_detail if hasattr(self.app.connection, "state_detail") else "",
                "platformDetail": self.platform.state_detail if self.platform else "",
                "lastSeenAt": self.app.connection_last_seen,
                "latencyMs": self.app.platform_latency_ms,
            },
            "upload": self.app.upload_summary(),
            "pendingEvents": self.storage.pending_event_count(),
            "update": self.update_service.summary() if self.update_service else None,
            "selfCheck": self.last_self_check.to_dict() if self.last_self_check else None,
            "recoveredBatches": [item.to_dict() for item in self.recovered_batches],
            "fingerprint": self.fingerprint,
            "uptimeSeconds": round(time.monotonic() - self._started_at, 1),
            "generatedAt": utc_now_iso(),
        }
