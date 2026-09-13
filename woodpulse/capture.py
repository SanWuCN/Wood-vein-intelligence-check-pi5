"""采集编排：把样例回放、相机截图、落盘与批次生命周期串起来（PRD §5.3、§9）。

一次检测作业在这里的完整路径：

    准备批次 → 开始 → （每 tick）推进样例回放 → 追加帧数据 → 落盘
                     ↘ 触屏标记 → 取当前相机截图 + 记录样例帧号 → marks.json
                     ↘ 平台请求暂停 → 停回放（健康遥测继续）→ 回 executed
    结束 → 写 segments/quality/result → **原子提交 manifest** → 允许上传

三条不可动摇的性质：
  · 回放推进只由"运行/暂停/结束"控制，暂停时不再追加任何列（H05）；
  · 同一批次重复排练得到相同帧内容与相同结果（H06）——因为数据来自只读样例包；
  · 终态批次不接受旧回调或旧命令复活（H05/H08）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters.camera import CameraSource, encode_png
from .adapters.replay import ReplayError, ReplayLibrary, ReplaySession, SamplePackage
from .app_state import AppState, BatchRecord, Mark, TaskAssignment
from .contracts import (
    BatchState,
    EventType,
    OperationLabel,
    TaskState,
    monotonic_ns,
    new_id,
    utc_now_iso,
)
from .logging_setup import get_logger
from .scenarios import SCENARIOS, PAUSE_SCOPE_NOTE
from .storage import BatchWriter, Storage

log = get_logger("capture")

#: 每积累这么多帧就往磁盘刷一次。太大丢数据风险高，太小则频繁 fsync 拖慢采集。
FRAME_FLUSH_INTERVAL = 40

#: 响应序列图保留的帧数（PRD §10：二维图采用环形缓冲保存显示窗口）
RING_CAPACITY = 600


@dataclass
class CaptureTick:
    """一次 tick 的结果，交给 UI 决定怎么画。"""

    frames_added: int = 0
    frame_index: int = -1
    events: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    reason: str = ""


@dataclass
class MarkResult:
    ok: bool
    mark: Optional[Mark] = None
    error: str = ""


class BatchManager:
    """批次目录与 manifest 的落盘编排（磁盘侧）。"""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._writers: Dict[str, BatchWriter] = {}
        self._segments: Dict[str, List[Dict[str, Any]]] = {}
        self._quality: Dict[str, Dict[str, Any]] = {}

    def begin(self, batch_id: str) -> BatchWriter:
        writer = BatchWriter(self.storage, batch_id)
        writer.open_frames()
        self._writers[batch_id] = writer
        self._segments[batch_id] = []
        self._quality[batch_id] = {
            "frameCount": 0,
            "saturationPct": 0.0,
            "emptyFrames": 0,
            "nanValues": 0,
            "duplicateFrames": 0,
            "levelSamples": 0,
            "note": "响应序列为预制检测样例(replay)，不是雷达实采",
        }
        return writer

    def writer(self, batch_id: str) -> Optional[BatchWriter]:
        return self._writers.get(batch_id)

    def append_frame(self, batch_id: str, frame) -> None:
        writer = self._writers.get(batch_id)
        if writer is None:
            return
        writer.append_frame(frame.frame_index, frame.amplitudes, frame.t_ms)
        quality = self._quality.setdefault(batch_id, {})
        quality["frameCount"] = int(quality.get("frameCount", 0)) + 1
        quality["levelSamples"] = quality.get("levelSamples", 0) + len(frame.amplitudes)
        if not frame.amplitudes:
            quality["emptyFrames"] = int(quality.get("emptyFrames", 0)) + 1
        peak = max(frame.amplitudes) if frame.amplitudes else 0.0
        if peak >= 0.99:
            quality["saturationPct"] = round(float(quality.get("saturationPct", 0.0)) + 100.0 / max(1, len(frame.amplitudes)), 4)

        segment = frame.segment or {
            "segmentId": f"echo-{batch_id}-seg-{frame.frame_index:03d}",
            "batchId": batch_id,
            "frameId": frame.frame_id,
            "frameIndex": frame.frame_index,
            "tNs": int(frame.t_ms * 1_000_000),
            "deviceMonotonicNs": frame.frame_index * 100_000_000,
            "sampleCount": len(frame.amplitudes),
            "axes": {"x": "frame_index", "y": "sample_index"},
            "sourceMode": "replay",
            "pairedImage": None,
            "quality": {"maxAmplitude": round(peak, 6)},
            "peaks": [],
        }
        self._segments.setdefault(batch_id, []).append(segment)

    def flush(self, batch_id: str) -> None:
        writer = self._writers.get(batch_id)
        if writer is not None:
            writer.close_frames()
            writer.open_frames()

    def save_mark_image(self, batch_id: str, frame_index: int, data: bytes) -> Optional[Dict[str, Any]]:
        writer = self._writers.get(batch_id)
        if writer is None:
            return None
        return writer.save_image(frame_index, data, suffix=".png")

    def finalize(
        self,
        record: BatchRecord,
        *,
        config_snapshot: Optional[Dict[str, Any]] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        result: Optional[Dict[str, Any]] = None,
        quality_extra: Optional[Dict[str, Any]] = None,
        marks: Optional[List[Dict[str, Any]]] = None,
        copy_from: Optional[SamplePackage] = None,
        image_index: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """封存批次：写全部产物 → 原子提交 manifest（PRD §8.2 的上传前置条件）。"""
        batch_id = record.batch_id
        writer = self._writers.get(batch_id)
        if writer is None:
            raise RuntimeError(f"批次 {batch_id} 没有打开的写入器")

        writer.close_frames()

        # 1) 配置快照（批次绑定的是哪一个 CFG）
        if config_snapshot is not None:
            writer.write_json("config.json", config_snapshot)

        # 2) 分段记录
        writer.write_json("segments.json", {"batchId": batch_id, "segments": self._segments.get(batch_id, [])})

        # 3) 质量记录；样例包自带的采数质量（例如初扫缺 34 帧）优先保留其口径
        quality = dict(self._quality.get(batch_id, {}))
        if copy_from is not None and copy_from.quality:
            quality["sampleQuality"] = copy_from.quality
            declared = int(copy_from.declared_frames or 0)
            returned = int(copy_from.returned_frames or 0)
            if declared and declared != returned:
                quality["expectedFrames"] = declared
                quality["missingFrames"] = declared - returned
                quality["note"] = (
                    f"因平台请求暂停 / 适用域待核验，第 {returned} 帧后未继续采集，"
                    f"剩余 {declared - returned} 帧未回传"
                )
        if quality_extra:
            quality.update(quality_extra)
        quality.setdefault("expectedFrames", record.frame_count_expected)
        quality.setdefault("missingFrames", max(0, record.frame_count_expected - int(quality.get("frameCount", 0))))
        writer.write_json("quality.json", quality)

        # 4) 人工标记
        mark_list = marks or []
        writer.write_json("marks.json", {"batchId": batch_id, "marks": mark_list})
        writer.write_marks_csv(mark_list)

        # 5) 结果（有结论才写；没结论就明确写 withheld / none，不留空）
        if result is not None:
            writer.write_json("result.json", result)

        # 6) 图片索引
        if image_index is not None:
            writer.write_json("images/index.json", {"batchId": batch_id, "images": image_index})

        # 7) 阶段事件：直接用样例包的记录，保证与平台剧本一致（不凭空生成）
        if copy_from is not None:
            source_events = copy_from.dir / "events.log"
            if source_events.is_file():
                shutil.copyfile(source_events, writer.dir / "events.log")
        if events:
            writer.write_json("session-events.json", {"batchId": batch_id, "events": events})

        # 8) 数据集信息（参考样本采集才有）
        if copy_from is not None and copy_from.dataset:
            writer.write_json("dataset.json", copy_from.dataset)
        if copy_from is not None and copy_from.plan:
            writer.write_json("plan.json", copy_from.plan)

        # 9) 原子提交 manifest
        manifest = record.to_manifest()
        manifest["samplePackage"] = {
            "scenarioId": record.scenario_id,
            "batchId": copy_from.manifest.get("batchId") if copy_from else None,
            "datasetId": copy_from.manifest.get("datasetId") if copy_from else None,
            "datasetHash": copy_from.dataset_hash if copy_from else "",
            "format": "response-sequence-v1",
        }
        manifest["responseSequenceSource"] = {
            "kind": "replay-sample-package",
            "note": "响应序列来自固定检测样例包；不是雷达实采，也不代表木柱内部真实结构",
            "sourceFile": "frames.csv",
        }
        roles = {item["relPath"]: "image" for item in (image_index or [])}
        roles["frames.csv"] = "frames"
        roles["segments.json"] = "segments"
        roles["marks.json"] = "marks"
        roles["marks.csv"] = "marks_csv"
        roles["quality.json"] = "quality"
        roles["result.json"] = "result"
        roles["config.json"] = "config"
        roles["dataset.json"] = "dataset"
        roles["plan.json"] = "plan"
        roles["events.log"] = "events"
        roles["images/index.json"] = "image_index"
        committed = writer.commit_manifest(manifest, roles)
        self._writers.pop(batch_id, None)
        return committed


class CaptureController:
    """采集主流程。GUI 每 100ms 调一次 `tick()`，其余全是同步方法。"""

    def __init__(
        self,
        app: AppState,
        storage: Storage,
        library: ReplayLibrary,
        camera: Optional[CameraSource] = None,
        *,
        on_event: Optional[Callable[[str, str, str], None]] = None,
    ) -> None:
        self.app = app
        self.storage = storage
        self.library = library
        self.camera = camera
        self.batches = BatchManager(storage)
        self.on_event = on_event
        self.session: Optional[ReplaySession] = None
        self.package: Optional[SamplePackage] = None
        self._ring: List[Any] = []
        self._pending_flush = 0
        self._events: List[Dict[str, Any]] = []
        self._image_index: List[Dict[str, Any]] = []
        self._mark_counter = 0
        self._started_monotonic: Optional[float] = None
        self._paused_by_platform = False

    # ---- 准备 ----

    def prepare(self, assignment: TaskAssignment, *, batch_id: Optional[str] = None) -> Tuple[bool, str]:
        """绑定工单/柱/测区/配置版本/模型版本，并把批次目录准备好（PRD §5.3）。"""
        if self.app.task.state in (TaskState.RUNNING, TaskState.PAUSED):
            return False, "当前已有进行中的采集任务，请先结束"

        try:
            package = self.library.open(assignment.scenario_id)
        except ReplayError as exc:
            return False, f"样例包不可用：{exc}"

        spec = SCENARIOS.get(assignment.scenario_id)
        record = BatchRecord(
            batch_id=batch_id or (spec.batch_id if spec else f"scan-{assignment.component_id}-{new_id('b')[-4:]}"),
            component_id=assignment.component_id,
            zone_id=assignment.zone_id,
            order_id=assignment.order_id,
            round=assignment.round,
            scenario_id=assignment.scenario_id,
            config_version=assignment.config_version,
            model_version=assignment.model_version,
            frame_count_expected=package.returned_frames,
            source_mode="replay",
            camera_source_mode="live" if (self.camera and self.camera.available) else "unavailable",
            # 样例包自带的摘要与标识：它只与"用的是哪套样例"有关，
            # 重复排练两轮必须一致，所以对外事件上报的是它而不是批次产物摘要。
            sample_dataset_hash=package.dataset_hash,
            sample_dataset_id=str(package.manifest.get("datasetId") or ""),
            marks=[],
        )
        record.dir_path = str(self.storage.batch_dir(record.batch_id))
        self.storage.create_batch_dir(record.batch_id)
        self.storage.save_batch(record, boot_id=self.app.boot_id)
        self.storage.log_event("batch.prepared", record.batch_id, record.to_manifest())

        self.batches.begin(record.batch_id)
        self.package = package
        self.session = ReplaySession(package, spec)
        self._ring = []
        self._events = []
        self._image_index = []
        self._mark_counter = 0
        self._pending_flush = 0
        self._started_monotonic = None
        self._paused_by_platform = False
        self.app.prepare_task(record)
        log.info(
            "批次已准备：%s（%s / %s / %s）样例 %s %d 帧",
            record.batch_id,
            record.order_id,
            record.component_id,
            record.zone_id,
            package.scenario_id,
            package.returned_frames,
        )
        return True, ""

    # ---- 控制 ----

    def start(self) -> Tuple[bool, str]:
        if self.session is None or self.app.batch is None:
            return False, "还没有准备批次"
        if self.app.task.state in TaskState.TERMINAL:
            return False, "任务已结束，不能重新开始（请准备新批次）"
        if not self.app.start_task("操作者开始本次扫描"):
            return False, f"当前状态 {self.app.task.label} 不能开始"
        self.session.play()
        self._started_monotonic = self._started_monotonic or __import__("time").monotonic()
        self.storage.save_batch(self.app.batch, boot_id=self.app.boot_id)
        self.storage.log_event("capture.started", self.app.batch.batch_id, {"scenarioId": self.app.batch.scenario_id})
        self._emit("INFO", f"本次扫描开始：{self.app.batch.component_id} {self.app.batch.zone_id}")
        return True, ""

    def pause(self, reason: str, source: str = "local") -> bool:
        if self.app.batch is None:
            return False
        if not self.app.pause_task(reason, source):
            return False
        if self.session is not None:
            self.session.pause(reason)
        self.batches.flush(self.app.batch.batch_id)
        self.storage.log_event("capture.paused", self.app.batch.batch_id, {"reason": reason, "source": source})
        return True

    def resume(self, reason: str = "操作者继续", source: str = "local") -> bool:
        if self.app.batch is None:
            return False
        if not self.app.resume_task(reason, source):
            return False
        if self.session is not None:
            self.session.play()
        self._paused_by_platform = False
        self.storage.log_event("capture.resumed", self.app.batch.batch_id, {"reason": reason, "source": source})
        return True

    def mark_platform_pause(self) -> None:
        self._paused_by_platform = True

    @property
    def paused_by_platform(self) -> bool:
        return self._paused_by_platform

    def finish(self, reason: str = "操作者确认结束") -> Tuple[bool, Dict[str, Any]]:
        """结束本次扫描：封存批次、生成不可变作业记录与标记列表（PRD §5.3）。"""
        if self.app.batch is None:
            return False, {}
        record = self.app.batch
        if self.session is not None:
            self.session.stop()
        if not self.app.finish_task(reason):
            return False, {}

        marks = [mark.to_dict() for mark in record.marks]
        result = self._build_result(record)
        quality_extra = {
            "markCount": len(marks),
            "savedFrames": record.frames_returned,
            "batchState": record.state,
            "playbackFps": self.session.playback_fps if self.session else None,
            "pauseScopeNote": PAUSE_SCOPE_NOTE if self._paused_by_platform else "",
        }
        try:
            manifest = self.batches.finalize(
                record,
                config_snapshot=self.app.config.to_dict() if self.app.config and self.app.config.has_data else None,
                events=self._events,
                result=result,
                quality_extra=quality_extra,
                marks=marks,
                copy_from=self.package,
                image_index=self._image_index,
            )
        except Exception as exc:  # noqa: BLE001 - 落盘失败必须显式暴露
            log.exception("批次封存失败：%s", exc)
            record.state = BatchState.FAILED
            record.interrupt_reason = f"批次封存失败：{exc}"
            self.storage.save_batch(record, boot_id=self.app.boot_id)
            return False, {"error": str(exc)}

        record.frames_saved = int(manifest.get("returnedFrames") or record.frames_returned)
        record.dataset_hash = str(manifest.get("datasetHash") or "")
        record.state = BatchState.SEALED
        integrity = self.storage.batch_integrity(record.batch_id)
        record.total_bytes = int(integrity.get("totalBytes") or 0)
        self.storage.save_batch(record, manifest_committed=True, boot_id=self.app.boot_id)
        self.storage.log_event("batch.finalized", record.batch_id, {"datasetHash": record.dataset_hash})
        log.info(
            "批次封存完成：%s，%d 个文件，%s 字节，datasetHash=%s",
            record.batch_id,
            integrity.get("fileCount"),
            record.total_bytes,
            record.dataset_hash[:12],
        )
        return True, manifest

    def interrupt(self, reason: str) -> Tuple[bool, Dict[str, Any]]:
        """异常中断（例如程序要退出、相机故障）。保留已保存数据，不伪装成正常结束。"""
        if self.app.batch is None:
            return False, {}
        if self.session is not None:
            self.session.stop()
        if not self.app.interrupt_task(reason):
            return False, {}
        record = self.app.batch
        try:
            self.batches.finalize(
                record,
                config_snapshot=self.app.config.to_dict() if self.app.config and self.app.config.has_data else None,
                events=self._events,
                result={
                    "conclusion": "withheld",
                    "reason": reason,
                    "findings": [],
                    "modelVersion": record.model_version,
                    "adequateDomain": False,
                },
                quality_extra={"interrupted": True, "markCount": len(record.marks)},
                marks=[mark.to_dict() for mark in record.marks],
                copy_from=self.package,
                image_index=self._image_index,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("中断批次封存失败：%s", exc)
        return True, {"reason": reason}

    # ---- 推进 ----

    def tick(self, dt: float) -> CaptureTick:
        """推进采集。GUI 定时器每 100ms 调一次。"""
        tick = CaptureTick()
        if self.session is None or self.app.batch is None:
            return tick
        if self.app.task.state != TaskState.RUNNING:
            # 暂停或终态：不追加任何列（H05）
            return tick

        frames = self.session.advance(dt)
        if frames:
            for frame in frames:
                self.batches.append_frame(self.app.batch.batch_id, frame)
                self._ring.append(frame)
                self._pending_flush += 1
            if len(self._ring) > RING_CAPACITY:
                del self._ring[: len(self._ring) - RING_CAPACITY]
            if self._pending_flush >= FRAME_FLUSH_INTERVAL:
                self.batches.flush(self.app.batch.batch_id)
                self._pending_flush = 0
                self.storage.save_batch(self.app.batch, boot_id=self.app.boot_id)
            self.app.batch.frames_returned = self.session.frames_returned
            tick.frames_added = len(frames)
            tick.frame_index = frames[-1].frame_index

            # 阶段事件与结论揭示
            for event in self.session.drain_events(frames[-1].frame_index):
                payload = {"frameIndex": event.frame_index, "level": event.level, "text": event.text, "kind": event.kind}
                self._events.append({**payload, "at": utc_now_iso()})
                tick.events.append(payload)
                self._emit(event.level, event.text)
                if event.kind == "finding":
                    tick.findings.append(payload)
            if self.session.revealed_findings:
                tick.findings = tick.findings or [
                    {"finding": item.finding, "frameIndex": item.frame_index} for item in self.session.revealed_findings
                ]

            # 适用域待核验：冻结该批诊断输出（剧本 S12）
            if self.session.spec and self.session.spec.expected_interrupt and not self.app.batch.diagnosis_frozen:
                threshold = int(self.session.spec.events[-1]["frameIndex"]) if self.session.spec.events else 10**9
                if frames[-1].frame_index >= threshold:
                    reason = self.session.spec.events[-1]["text"]
                    self.app.mark_diagnosis_frozen(reason)
                    self._emit("WARNING", f"{reason}；该批诊断输出已冻结，等待暂停请求与操作确认")

        if self.session.exhausted and not tick.finished:
            tick.finished = True
            tick.reason = "样例段播放完毕"
        return tick

    # ---- 标记 ----

    def add_mark(self, operator_label: str = "") -> MarkResult:
        """一键标记（PRD §3.3）：保存 markId、当前相机截图、样例帧序号与单调时间。

        标记**不暂停**当前任务，也不新建整批数据；方向由操作者选定，没选就不猜。
        """
        if self.app.batch is None:
            return MarkResult(False, error="还没有活动批次")
        if self.app.task.state not in (TaskState.RUNNING, TaskState.PAUSED):
            return MarkResult(False, error="当前不在采集状态，无法标记")

        record = self.app.batch
        self._mark_counter += 1
        frame_index = self.session.frame_index if self.session else -1
        frame_id = f"frame-{frame_index:05d}" if frame_index >= 0 else ""
        mark_id = f"mark-{record.batch_id}-{self._mark_counter:02d}"
        stamp = monotonic_ns()

        # 取当前相机截图；失败也要保留标记，并显示"缺图"（PRD §9）
        camera_asset_id = ""
        image_ok = False
        if self.camera is not None and self.camera.available:
            snapshot = self.camera.snapshot(wait_s=0.6)
            if snapshot is not None:
                data = encode_png(snapshot, target_width=min(720, snapshot.width))
                if data:
                    entry = self.batches.save_mark_image(record.batch_id, frame_index, data)
                    if entry:
                        camera_asset_id = entry["relPath"]
                        image_ok = True
                        self._image_index.append(
                            {
                                "frameId": frame_id,
                                "file": entry["file"],
                                "relPath": entry["relPath"],
                                "kind": "mark_snapshot",
                                "frameIndex": frame_index,
                                "markId": mark_id,
                                "cameraIndex": snapshot.index,
                                "bytes": entry["bytes"],
                            }
                        )

        label = operator_label if operator_label in OperationLabel.CHOICES else ""
        mark = Mark(
            mark_id=mark_id,
            batch_id=record.batch_id,
            frame_id=frame_id,
            frame_index=frame_index,
            camera_asset_id=camera_asset_id or None,
            device_monotonic_ns=stamp,
            operator_label=label,
            note="" if image_ok else "相机截图不可用，本标记缺图",
            image_ok=image_ok,
        )
        self.app.add_mark(mark)
        self.storage.log_event("capture.mark_created", record.batch_id, mark.to_dict())
        self._emit(
            "INFO",
            f"已标记 {mark.display_label}（样例帧 {frame_index}，"
            + ("已保存当前相机截图" if image_ok else "缺图：相机截图不可用")
            + "）",
        )
        return MarkResult(True, mark=mark)

    def set_last_mark_label(self, operator_label: str) -> bool:
        """给最近一个标记补方向（先一键标记、再有空选方向，不打断扫描）。"""
        if self.app.batch is None or not self.app.batch.marks:
            return False
        if operator_label not in OperationLabel.CHOICES:
            return False
        mark = self.app.batch.marks[-1]
        mark.operator_label = operator_label
        self.storage.log_event("capture.mark_labeled", self.app.batch.batch_id, mark.to_dict())
        return True

    # ---- 结果 ----

    def _build_result(self, record: BatchRecord) -> Dict[str, Any]:
        """端侧结果。冻结的批次一律 withheld，不给确定性结论（剧本 S12）。"""
        if record.diagnosis_frozen:
            return {
                "conclusion": "withheld",
                "reason": record.freeze_reason or "适用域待核验，诊断输出冻结",
                "findings": [],
                "modelVersion": record.model_version,
                "adequateDomain": False,
                "note": "端侧不出确定性结论；设备证据与模型证据分别核对后再决定是否继续输出",
            }
        findings = [item.finding for item in (self.session.revealed_findings if self.session else [])]
        if record.round == "reference":
            return {
                "conclusion": "none",
                "reason": "参考样本采集只做采集与交付，不输出缺陷结论；标签依据在平台侧审核",
                "findings": [],
                "modelVersion": record.model_version,
                "adequateDomain": True,
            }
        if not findings:
            return {
                "conclusion": "none",
                "reason": "本次扫描未出现达到阈值的响应段，端侧不出结论",
                "findings": [],
                "modelVersion": record.model_version,
                "adequateDomain": True,
            }
        return {
            "conclusion": "preliminary",
            "reviewer": "platform",
            "note": "端侧初筛，待平台复核；不输出异常深度与形状（当前硬件资料未给出可验证的内部成像分辨率）",
            "findings": findings,
            "modelVersion": record.model_version,
            "adequateDomain": True,
        }

    # ---- 访问器 ----

    @property
    def ring(self) -> List[Any]:
        """响应序列图的环形缓冲（PRD §10）。"""
        return self._ring

    @property
    def latest_frame(self):
        return self._ring[-1] if self._ring else None

    @property
    def playback_fps(self) -> float:
        return self.session.playback_fps if self.session else 0.0

    def telemetry_stats(self) -> Dict[str, Any]:
        if self.session is None:
            return {
                "scenarioId": None,
                "batchId": None,
                "frameIndex": None,
                "frameCount": None,
                "playbackFps": None,
                "datasetHash": "",
                "sourceMode": "replay",
                "reason": "本机当前没有回放任务",
            }
        return self.session.telemetry()

    def _emit(self, level: str, text: str) -> None:
        logger = log.warning if level in ("WARNING", "WARN") else log.info
        logger(text)
        if self.on_event:
            self.on_event(level, "capture", text)
