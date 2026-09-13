"""任务、批次、配置与更新状态机（PRD §10：app_state.py 替代零散布尔值）。

为什么要有这个模块：
    旧版把"是否连接""是否在扫描""是否升级完成"散成若干布尔量，界面和线程各改一份，
    暂停后又收到旧回调就复活了（PRD §2 的旧版审查结论）。这里把状态收敛成显式状态
    + 受检查的迁移，任何非法迁移直接拒绝并留下原因，界面只读状态、不猜状态。

三条硬规则（PRD §8.1、§8.2、§13 H05/H08/H13）：
    1. 终态（finished / interrupted）不再接受旧的 resume 或 start；
    2. 批次与模型版本在批次活动期间绑定，更新包只能暂存，结束前不许切换；
    3. 每个批次只属于一个 Task，标记共用 batchId 下的 markId，不新建整批数据。

本模块**不导入 Qt**，因此可以在没有 PyQt 的机器上直接跑单元测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from .contracts import (
    BatchState,
    CapabilityReport,
    Command,
    ConfigState,
    ConnectionState,
    ErrorCode,
    EventType,
    OperationLabel,
    PositionSource,
    TaskState,
    UploadState,
    new_id,
    utc_now_iso,
)

# --------------------------------------------------------------------------- #
# 变更通知
# --------------------------------------------------------------------------- #

Listener = Callable[[str, Any], None]


class Notifier:
    """极简发布订阅：GUI 把它桥接成 Qt 信号，测试里直接收回调。

    `topic` 用点号分层（`task.changed`、`batch.mark_added`、`log.appended`），
    订阅支持前缀匹配，`subscribe("task")` 能收到 `task.changed`。
    """

    def __init__(self) -> None:
        self._listeners: List[tuple] = []

    def subscribe(self, topic: str, callback: Listener) -> Callable[[], None]:
        entry = (topic, callback)
        self._listeners.append(entry)
        return lambda: self._listeners.remove(entry) if entry in self._listeners else None

    def emit(self, topic: str, payload: Any = None) -> None:
        for subscribed, callback in list(self._listeners):
            if topic == subscribed or topic.startswith(subscribed + "."):
                try:
                    callback(topic, payload)
                except Exception:  # noqa: BLE001 - 一个订阅者崩了不能拖垮采集
                    import logging

                    logging.getLogger("woodpulse.state").exception("订阅者 %r 处理 %s 时异常", callback, topic)


# --------------------------------------------------------------------------- #
# 任务 / 批次
# --------------------------------------------------------------------------- #

@dataclass
class Mark:
    """人工标记（PRD §3.3、§9）。

    `operator_label` 为空表示操作者没选方向，界面只显示"标记 01"，
    **绝不猜测方位**；`position_source` 在无 IMU 的情况下恒为 operator_tag。
    """

    mark_id: str
    batch_id: str
    frame_id: str
    frame_index: int
    camera_asset_id: Optional[str]
    device_monotonic_ns: int
    operator_label: str = ""
    position_source: str = PositionSource.OPERATOR_TAG
    note: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    #: 截图失败时置 False，界面显示"缺图"，不伪装成完整记录（PRD §9）
    image_ok: bool = True

    @property
    def display_label(self) -> str:
        if self.operator_label:
            return f"标记{self.mark_id[-2:]}·{self.operator_label}"
        return f"标记{self.mark_id[-2:]}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "markId": self.mark_id,
            "batchId": self.batch_id,
            "frameId": self.frame_id,
            "cameraAssetId": self.camera_asset_id,
            "deviceMonotonicNs": self.device_monotonic_ns,
            "operatorLabel": self.operator_label,
            "positionSource": self.position_source,
            "note": self.note,
            "createdAt": self.created_at,
            "frameIndex": self.frame_index,
            "imageOk": self.image_ok,
        }


@dataclass
class BatchRecord:
    """一个采集批次的运行期状态。落盘权威在 services/storage.py，这里只管内存视图。"""

    batch_id: str
    component_id: str
    zone_id: str
    order_id: str
    round: str  # initial / rescan / reference
    scenario_id: str
    config_version: str
    model_version: str
    frame_count_expected: int = 0
    source_mode: str = "replay"
    camera_source_mode: str = "live"
    position_source: str = PositionSource.OPERATOR_TAG
    pairing: str = "unverified"
    state: str = BatchState.OPEN
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None
    frames_saved: int = 0
    frames_returned: int = 0
    upload_state: str = UploadState.QUEUED
    total_bytes: int = 0
    uploaded_bytes: int = 0
    dataset_hash: str = ""
    #: 所用样例包自带的 datasetHash。它只与"用的是哪套样例"有关，同一样例重复两轮
    #: 必须一致（PRD §13 H06、§7.1），所以对外事件上报的是它；
    #: 而 `dataset_hash` 是按批次目录实际写出的文件算出来的产物摘要（每批不同）。
    sample_dataset_hash: str = ""
    sample_dataset_id: str = ""
    interrupt_reason: str = ""
    #: 适用域检查未通过时冻结诊断输出（PRD §5.3、剧本 S12）
    diagnosis_frozen: bool = False
    freeze_reason: str = ""
    marks: List[Mark] = field(default_factory=list)
    dir_path: str = ""

    @property
    def mark_count(self) -> int:
        return len(self.marks)

    @property
    def progress(self) -> float:
        """回放/采集推进比例。分母为 0 时返回 0，不产生除零或假进度。"""
        if self.frame_count_expected <= 0:
            return 0.0
        return min(1.0, self.frames_returned / self.frame_count_expected)

    def to_manifest(self) -> Dict[str, Any]:
        return {
            "schemaVersion": "1.0",
            "batchId": self.batch_id,
            "projectId": "temple-demo",
            "orderId": self.order_id,
            "componentId": self.component_id,
            "zoneId": self.zone_id,
            "positionSource": self.position_source,
            "radarSourceMode": self.source_mode,
            "cameraSourceMode": self.camera_source_mode,
            "pairing": self.pairing,
            "format": "response-sequence-v1",
            "axis": {"x": "frame_index", "y": "sample_index"},
            "configVersion": self.config_version,
            "modelVersion": self.model_version,
            "scenarioId": self.scenario_id,
            "round": self.round,
            "frameCount": self.frame_count_expected,
            "returnedFrames": self.frames_returned,
            "state": self.state,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "markCount": self.mark_count,
            "datasetHash": self.dataset_hash,
            "sampleDatasetHash": self.sample_dataset_hash,
            "sampleDatasetId": self.sample_dataset_id,
            "interruptReason": self.interrupt_reason,
            "privacyNote": "响应序列为预制检测样例(replay)，不是雷达实采，也不代表木柱内部真实结构",
        }


class TransitionError(RuntimeError):
    """非法状态迁移。带上 from/to 便于测试断言与日志。"""

    def __init__(self, machine: str, source: str, target: str, detail: str = "") -> None:
        self.machine = machine
        self.source = source
        self.target = target
        message = f"{machine}: 不允许 {source} → {target}"
        if detail:
            message += f"（{detail}）"
        super().__init__(message)


class StateMachine:
    """显式迁移表的小状态机。`transitions` 里没写的组合一律拒绝。"""

    name = "state"

    def __init__(self, notifier: Optional[Notifier] = None) -> None:
        self._notifier = notifier
        self._state = ""
        self._reason = ""
        self._history: List[tuple] = []

    # 子类覆盖
    transitions: Dict[str, Iterable[str]] = {}
    initial = ""
    terminal: Iterable[str] = ()
    labels: Dict[str, str] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def reason(self) -> str:
        """最近一次迁移的原因（暂停原因、失败原因），界面直接显示。"""
        return self._reason

    @property
    def history(self) -> List[tuple]:
        return list(self._history)

    def reset(self, state: Optional[str] = None, reason: str = "") -> None:
        self._state = state or self.initial
        self._reason = reason
        self._history = [(self._state, time.monotonic(), reason)]

    def can(self, target: str) -> bool:
        return target in tuple(self.transitions.get(self._state, ()))

    @property
    def is_terminal(self) -> bool:
        return self._state in tuple(self.terminal)

    def to(self, target: str, reason: str = "", force: bool = False) -> bool:
        """迁移到 target。非法迁移返回 False（不抛异常），调用方据此决定回执内容。

        需要在一开始就暴露错误的地方（单测、协议回执）可以传 force=False 后检查返回值；
        真正写错的代码用 `to_or_raise`。
        """
        if target == self._state and not force:
            return True
        if not force and not self.can(target):
            return False
        previous, self._state, self._reason = self._state, target, reason
        self._history.append((target, time.monotonic(), reason))
        if self._notifier:
            self._notifier.emit(
                f"{self.name}.changed",
                {"from": previous, "to": target, "state": target, "reason": reason, "machine": self.name},
            )
        return True

    def to_or_raise(self, target: str, reason: str = "") -> None:
        if not self.to(target, reason):
            raise TransitionError(self.name, self._state, target, reason)

    @property
    def label(self) -> str:
        return self.labels.get(self._state, self._state)


class TaskMachine(StateMachine):
    """采集任务状态机（PRD §5.3：待开始 → 采集中 → 暂停 → 已结束）。"""

    name = "task"
    initial = TaskState.IDLE
    labels = TaskState.LABEL
    terminal = TaskState.TERMINAL
    transitions = {
        TaskState.IDLE: (TaskState.READY,),
        TaskState.READY: (TaskState.RUNNING, TaskState.IDLE),
        TaskState.RUNNING: (TaskState.PAUSED, TaskState.FINISHED, TaskState.INTERRUPTED),
        TaskState.PAUSED: (TaskState.RUNNING, TaskState.FINISHED, TaskState.INTERRUPTED),
        # 终态只能通过 reset() 回到 idle（开始新批次），不接受 resume
        TaskState.FINISHED: (),
        TaskState.INTERRUPTED: (),
    }


class BatchMachine(StateMachine):
    """批次状态机。sealed 之后才允许上传（PRD §8.2：先原子提交 manifest，再允许上传）。"""

    name = "batch"
    initial = BatchState.OPEN
    labels = BatchState.LABEL
    terminal = (BatchState.UPLOADED, BatchState.FAILED)
    transitions = {
        BatchState.OPEN: (BatchState.SEALED, BatchState.FAILED),
        BatchState.SEALED: (BatchState.UPLOADING,),
        BatchState.UPLOADING: (BatchState.UPLOADED, BatchState.PARTIAL, BatchState.FAILED),
        # 部分接收后可以续传（PRD §13 H10）
        BatchState.PARTIAL: (BatchState.UPLOADING, BatchState.UPLOADED, BatchState.FAILED),
        BatchState.UPLOADED: (),
        BatchState.FAILED: (BatchState.UPLOADING,),
    }


class ConfigMachine(StateMachine):
    """环境配置生命周期（PRD §5.2：接收 → 操作者确认 → 生效回 ack）。"""

    name = "config"
    initial = ConfigState.NONE
    labels = ConfigState.LABEL
    transitions = {
        ConfigState.NONE: (ConfigState.RECEIVED,),
        ConfigState.RECEIVED: (ConfigState.CONFIRMED, ConfigState.NONE),
        ConfigState.CONFIRMED: (ConfigState.APPLIED,),
        ConfigState.APPLIED: (ConfigState.RECEIVED,),  # 收到新版本配置
    }


class UpdateState:
    """更新流程状态（PRD §5.6、§8.2）。"""

    IDLE = "idle"
    RECEIVED = "received"          # 平台下发 prepare_update
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VERIFIED = "verified"          # 摘要与目标检查通过
    STAGED = "staged"              # 暂存，等活动批次结束（PRD §13 H13）
    APPLYING = "applying"
    APPLIED = "applied"            # 版本读回一致
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"

    LABEL = {
        IDLE: "无更新",
        RECEIVED: "已接收更新通知",
        DOWNLOADING: "下载中",
        DOWNLOADED: "已下载",
        VERIFIED: "摘要与目标检查通过",
        STAGED: "已暂存（等当前批次结束）",
        APPLYING: "切换中",
        APPLIED: "已生效并回验",
        FAILED: "更新失败",
        ROLLED_BACK: "已回退旧版本",
    }


class UpdateMachine(StateMachine):
    name = "update"
    initial = UpdateState.IDLE
    labels = UpdateState.LABEL
    terminal = (UpdateState.APPLIED,)
    transitions = {
        UpdateState.IDLE: (UpdateState.RECEIVED,),
        UpdateState.RECEIVED: (UpdateState.DOWNLOADING, UpdateState.FAILED),
        UpdateState.DOWNLOADING: (UpdateState.DOWNLOADED, UpdateState.FAILED),
        UpdateState.DOWNLOADED: (UpdateState.VERIFIED, UpdateState.FAILED),
        UpdateState.VERIFIED: (UpdateState.STAGED, UpdateState.FAILED),
        UpdateState.STAGED: (UpdateState.APPLYING, UpdateState.IDLE),
        UpdateState.APPLYING: (UpdateState.APPLIED, UpdateState.ROLLED_BACK, UpdateState.FAILED),
        UpdateState.ROLLED_BACK: (UpdateState.DOWNLOADED, UpdateState.IDLE),
        UpdateState.APPLIED: (UpdateState.IDLE,),
        UpdateState.FAILED: (UpdateState.DOWNLOADING, UpdateState.IDLE),
    }


class ConnectionMachine(StateMachine):
    """平台连接状态（PRD §8.2：5 秒心跳，15 秒未收到标延迟或离线）。"""

    name = "connection"
    initial = ConnectionState.OFFLINE
    labels = ConnectionState.LABEL
    transitions = {
        ConnectionState.OFFLINE: (ConnectionState.CONNECTING,),
        ConnectionState.CONNECTING: (ConnectionState.ONLINE, ConnectionState.OFFLINE),
        ConnectionState.ONLINE: (ConnectionState.DEGRADED, ConnectionState.OFFLINE, ConnectionState.CONNECTING),
        ConnectionState.DEGRADED: (ConnectionState.ONLINE, ConnectionState.OFFLINE),
    }


# --------------------------------------------------------------------------- #
# 系统自检结果
# --------------------------------------------------------------------------- #

@dataclass
class SelfCheckItem:
    """自检的一项（PRD §5.2：逐项报告，未接入项显示未配置/不可用，不全绿）。"""

    key: str
    label: str
    state: str            # ok / warn / fail / unavailable
    detail: str = ""
    source: str = ""      # 这一项是实测还是回放还是未接入
    checked_at: str = field(default_factory=utc_now_iso)

    #: 中文状态词。界面同时用文字与颜色（PRD §15：状态不能只靠红绿）
    STATE_LABEL = {"ok": "正常", "warn": "注意", "fail": "故障", "unavailable": "未接入"}

    @property
    def state_label(self) -> str:
        return self.STATE_LABEL.get(self.state, self.state)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "stateLabel": self.state_label,
            "detail": self.detail,
            "source": self.source,
            "checkedAt": self.checked_at,
        }


# --------------------------------------------------------------------------- #
# 环境配置
# --------------------------------------------------------------------------- #

@dataclass
class ConfigDiffRow:
    field: str
    before: str
    after: str
    note: str = ""


@dataclass
class ConfigSnapshot:
    """终端正在使用的配置快照（PRD §5.2、§13 H09）。"""

    config_version: str = ""
    source: str = ""
    published_at: str = ""
    air_temp_c: Optional[float] = None
    relative_humidity_pct: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    instrument_id: str = ""
    position: str = ""
    compensation: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    received_at: str = ""

    @property
    def has_data(self) -> bool:
        return bool(self.config_version)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configVersion": self.config_version,
            "source": self.source,
            "publishedAt": self.published_at,
            "airTempC": self.air_temp_c,
            "relativeHumidityPct": self.relative_humidity_pct,
            "windSpeedMs": self.wind_speed_ms,
            "instrumentId": self.instrument_id,
            "position": self.position,
            "compensation": dict(self.compensation),
            "receivedAt": self.received_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConfigSnapshot":
        return cls(
            config_version=str(data.get("configVersion") or data.get("config_version") or ""),
            source=str(data.get("source") or ""),
            published_at=str(data.get("publishedAt") or data.get("published_at") or ""),
            air_temp_c=_maybe_float(data.get("airTempC", data.get("air_temp_c"))),
            relative_humidity_pct=_maybe_float(data.get("relativeHumidityPct", data.get("relative_humidity_pct"))),
            wind_speed_ms=_maybe_float(data.get("windSpeedMs", data.get("wind_speed_ms"))),
            instrument_id=str(data.get("instrumentId") or data.get("instrument_id") or ""),
            position=str(data.get("position") or ""),
            compensation=dict(data.get("compensation") or {}),
            raw=dict(data),
            received_at=str(data.get("receivedAt") or utc_now_iso()),
        )

    def diff(self, other: Optional["ConfigSnapshot"]) -> List[ConfigDiffRow]:
        """与上一版做差异，界面展示"新旧差异、来源和发布时间"（PRD §5.2）。"""
        if other is None or not other.has_data:
            return [
                ConfigDiffRow("配置版本", "（无）", self.config_version or "（无）", "首次接收"),
            ]
        rows: List[ConfigDiffRow] = []

        def add(name: str, before: Any, after: Any, note: str = "") -> None:
            before_text = "—" if before in (None, "") else str(before)
            after_text = "—" if after in (None, "") else str(after)
            if before_text != after_text:
                rows.append(ConfigDiffRow(name, before_text, after_text, note))

        add("配置版本", other.config_version, self.config_version, "配置版本不可原地修改")
        add("参考温度", _fmt(other.air_temp_c, " ℃"), _fmt(self.air_temp_c, " ℃"), "取本次实测值")
        add("参考相对湿度", _fmt(other.relative_humidity_pct, " %"), _fmt(self.relative_humidity_pct, " %"), "0 ≤ RH ≤ 100")
        if self.wind_speed_ms is not None or other.wind_speed_ms is not None:
            add("风速", _fmt(other.wind_speed_ms, " m/s"), _fmt(self.wind_speed_ms, " m/s"), "仅作采集稳定性记录，不代入 HH 模型")
        for key, label in (
            ("baselineOffsetDb", "基线偏移量"),
            ("normalization", "归一化方式"),
            ("functionVersion", "处理函数版本"),
        ):
            add(label, other.compensation.get(key), self.compensation.get(key), "由参考件回波基线确定")
        return rows


def _maybe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Optional[float], unit: str) -> str:
    """把数值带单位格式化成一个稳定形式。

    固定一位小数、去掉多余的尾零（`60.0` → `60`，`26.4` → `26.4`）：
    平台发来的值可能是 int 也可能是 float，统一口径后差异表两列才能直接对照。
    """
    if value is None:
        return "—"
    if isinstance(value, bool):
        return f"{value}{unit}"
    if isinstance(value, int):
        return f"{value}{unit}"
    text = f"{float(value):.1f}".rstrip("0").rstrip(".")
    return f"{text or '0'}{unit}"


# --------------------------------------------------------------------------- #
# 汇总状态
# --------------------------------------------------------------------------- #

@dataclass
class TaskAssignment:
    """平台下发的工单绑定（PRD §5.3：开始前绑定工单、柱、测区、配置版本、模型版本）。"""

    order_id: str = "SH-2026-0901"
    component_id: str = "Z04"
    zone_id: str = "Z04-lower"
    round: str = "initial"
    task_revision: int = 1
    scenario_id: str = "initial-anomaly-v1"
    config_version: str = "CFG-02"
    model_version: str = "DEMO-M02"
    source: str = "local-default"
    received_at: str = ""


@dataclass
class UploadJob:
    """上传队列里的一项（PRD §5.5、§6）。"""

    upload_id: str
    batch_id: str
    file_id: str
    name: str
    role: str
    path: str
    size: int
    sha256: str = ""
    state: str = UploadState.QUEUED
    received_offset: int = 0
    attempts: int = 0
    last_error: str = ""
    remote_file_id: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.size - self.received_offset)


class AppState:
    """终端全局状态的唯一持有者（PRD §10）。

    所有读写都在 GUI 线程；工作线程只通过信号请求变更，不直接改这里的字段。
    这样"界面显示的状态"和"设备实际状态"不会各说各话。
    """

    def __init__(
        self,
        capability: Optional[CapabilityReport] = None,
        *,
        device_id: str = "handheld-02",
        boot_id: str = "",
        demo_session_id: str = "demo-01",
        operator_id: str = "rao",
    ) -> None:
        self.notifier = Notifier()
        self.device_id = device_id
        self.boot_id = boot_id or new_id("boot")
        self.demo_session_id = demo_session_id
        self.operator_id = operator_id

        self.capability = capability or CapabilityReport()
        self.task = TaskMachine(self.notifier)
        self.task.reset()
        self.batch_machine = BatchMachine(self.notifier)
        self.batch_machine.reset()
        self.config_machine = ConfigMachine(self.notifier)
        self.config_machine.reset()
        self.update_machine = UpdateMachine(self.notifier)
        self.update_machine.reset()
        self.connection = ConnectionMachine(self.notifier)
        self.connection.reset()
        self.upload_state = UploadState.QUEUED
        self.upload_state_reason = ""

        self.assignment = TaskAssignment()
        self.batch: Optional[BatchRecord] = None
        self.batches: List[BatchRecord] = []
        self.config: Optional[ConfigSnapshot] = None
        self.previous_config: Optional[ConfigSnapshot] = None
        self.self_checks: List[SelfCheckItem] = []
        self.telemetry: Dict[str, Any] = {}
        self.telemetry_updated_at: str = ""
        self.connection_last_seen: str = ""
        self.platform_latency_ms: Optional[float] = None
        self.uploads: List[UploadJob] = []
        self.pending_events: int = 0

        self.seq = 0
        self.model_version = self.assignment.model_version
        self.controller_version: Optional[str] = None      # 未接入时保持 None（PRD §5.6）
        self.demo_model_version: Optional[str] = None
        self.progress_message = ""

    # ---- 事件序号 ----

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    # ---- 能力 ----

    def set_capability(self, name: str, value: str, reason: str = "") -> None:
        self.capability.set(name, value, reason)
        self.notifier.emit("capability.changed", {"name": name, "value": value, "reason": reason})

    def source_label(self, name: str) -> str:
        return self.capability.label(name)

    # ---- 任务 ----

    def bind_assignment(self, assignment: TaskAssignment) -> None:
        self.assignment = assignment
        self.model_version = assignment.model_version
        self.notifier.emit("assignment.changed", assignment)

    def prepare_task(self, batch: BatchRecord) -> None:
        """开始前把批次登记进来。此时还没开始采集。"""
        self.batch = batch
        if batch not in self.batches:
            self.batches.append(batch)
        self.task.reset(TaskState.READY)
        self.batch_machine.reset(BatchState.OPEN)
        self.notifier.emit("task.prepared", batch)

    def start_task(self, reason: str = "本机开始") -> bool:
        if self.task.state in TaskState.TERMINAL:
            # H05：终态不被旧回调恢复
            return False
        if not self.task.to(TaskState.RUNNING, reason):
            return False
        self.notifier.emit("task.started", self.batch)
        return True

    def pause_task(self, reason: str, source: str = "local") -> bool:
        if self.task.state != TaskState.RUNNING:
            return False
        self.task.to(TaskState.PAUSED, reason)
        self.notifier.emit("task.paused", {"reason": reason, "source": source})
        return True

    def resume_task(self, reason: str = "本机继续", source: str = "local") -> bool:
        if self.task.state != TaskState.PAUSED:
            return False
        self.task.to(TaskState.RUNNING, reason)
        self.notifier.emit("task.resumed", {"reason": reason, "source": source})
        return True

    def finish_task(self, reason: str = "操作者确认结束") -> bool:
        if not self.task.to(TaskState.FINISHED, reason):
            return False
        self.batch_machine.to(BatchState.SEALED, reason)
        if self.batch:
            self.batch.state = BatchState.SEALED
            self.batch.finished_at = utc_now_iso()
        self.notifier.emit("task.finished", self.batch)
        return True

    def interrupt_task(self, reason: str) -> bool:
        if not self.task.to(TaskState.INTERRUPTED, reason):
            return False
        self.batch_machine.to(BatchState.SEALED, reason)
        if self.batch:
            self.batch.state = BatchState.SEALED
            self.batch.interrupt_reason = reason
            self.batch.finished_at = utc_now_iso()
        self.notifier.emit("task.interrupted", {"reason": reason, "batch": self.batch})
        return True

    def mark_diagnosis_frozen(self, reason: str) -> None:
        """适用域待核验 → 冻结该批次诊断输出（PRD §5.3、§11 剧本 S12）。"""
        if not self.batch:
            return
        self.batch.diagnosis_frozen = True
        self.batch.freeze_reason = reason
        self.notifier.emit("batch.frozen", {"batchId": self.batch.batch_id, "reason": reason})

    # ---- 标记 ----

    def add_mark(self, mark: Mark) -> None:
        if not self.batch:
            return
        self.batch.marks.append(mark)
        self.notifier.emit("batch.mark_added", mark)

    def latest_marks(self, limit: int = 8) -> List[Mark]:
        if not self.batch:
            return []
        return self.batch.marks[-limit:]

    # ---- 配置 ----

    def apply_config(self, snapshot: ConfigSnapshot) -> List[ConfigDiffRow]:
        """收到平台配置：进入"待确认"，差异算好给界面（PRD §5.2）。"""
        previous = self.config if self.config and self.config.has_data else None
        diffs = snapshot.diff(previous)
        self.previous_config = previous
        self.config = snapshot
        self.config_machine.to(ConfigState.RECEIVED, f"收到 {snapshot.config_version}")
        self.notifier.emit("config.received", {"snapshot": snapshot, "diff": diffs})
        return diffs

    def confirm_config(self) -> bool:
        if self.config_machine.state != ConfigState.RECEIVED:
            return False
        self.config_machine.to(ConfigState.CONFIRMED, "操作者已确认差异")
        self.notifier.emit("config.confirmed", self.config)
        return True

    def mark_config_applied(self) -> bool:
        if self.config_machine.state != ConfigState.CONFIRMED:
            return False
        self.config_machine.to(ConfigState.APPLIED, "已生效并回传 ack")
        self.notifier.emit("config.applied", self.config)
        return True

    def active_config_version(self) -> str:
        if self.config and self.config.has_data:
            return self.config.config_version
        return self.assignment.config_version

    # ---- 更新 ----

    def note_update(self, info: Dict[str, Any]) -> None:
        self.notifier.emit("update.noted", info)

    def can_switch_model_now(self) -> tuple:
        """更新与采集不能同时改有效模型版本（PRD §8.2、§13 H13）。

        返回 (允许?, 原因)。批次活动期间只允许暂存。
        """
        if self.task.state in (TaskState.RUNNING, TaskState.PAUSED) and self.batch:
            return False, f"当前批次 {self.batch.batch_id} 使用 {self.batch.model_version}，结束后才允许切换"
        return True, "无活动批次"

    # ---- 上传队列 ----

    def upsert_upload(self, job: UploadJob) -> None:
        for index, existing in enumerate(self.uploads):
            if existing.file_id == job.file_id and existing.batch_id == job.batch_id:
                self.uploads[index] = job
                self.notifier.emit("upload.changed", job)
                return
        self.uploads.append(job)
        self.notifier.emit("upload.changed", job)

    @property
    def pending_bytes(self) -> int:
        return sum(job.remaining for job in self.uploads if job.state not in (UploadState.DONE,))

    @property
    def confirmed_bytes(self) -> int:
        return sum(job.received_offset for job in self.uploads)

    def upload_summary(self) -> Dict[str, Any]:
        queued = [job for job in self.uploads if job.state not in (UploadState.DONE,)]
        last_error = ""
        for job in self.uploads:
            if job.last_error:
                last_error = job.last_error
        return {
            "queued": len(queued),
            "pendingBytes": self.pending_bytes,
            "confirmedBytes": self.confirmed_bytes,
            "activeFile": queued[0].name if queued else None,
            "lastError": last_error or None,
        }

    # ---- 平台连接 ----

    def set_connection(self, state: str, detail: str = "") -> None:
        if state == self.connection.state:
            if detail:
                self.connection.to(state, detail, force=True)
            return
        self.connection.to(state, detail)

    def note_platform_seen(self) -> None:
        self.connection_last_seen = utc_now_iso()

    # ---- 命令回执 ----

    def command_receipt(
        self,
        command: Dict[str, Any],
        state: str,
        *,
        error_code: str = "",
        reason: str = "",
        result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造回执报文（PRD §8.1）。

        accepted 只表示已接收；executed 必须由真正完成动作的地方设置。
        """
        return {
            "commandId": command.get("commandId") or command.get("command_id") or "",
            "action": command.get("action") or command.get("type") or "",
            "state": state,
            "errorCode": error_code,
            "reason": reason,
            "targetBatchId": command.get("targetBatchId") or command.get("target_batch_id") or "",
            "expectedTaskRevision": command.get("expectedTaskRevision"),
            "executedAt": utc_now_iso(),
            "deviceId": self.device_id,
            "bootId": self.boot_id,
            "scope": self.command_scope(command.get("action") or command.get("type") or ""),
            "result": result or {},
        }

    def command_scope(self, action: str) -> str:
        """说明命令的作用范围（PRD §5.3：设备回执必须说明作用范围）。

        每条回执都显式带上"作用范围："前缀：现场和答辩时要一眼看出
        "这次停的是本地采集，不是硬件发射"。
        """
        if action == Command.PAUSE_CAPTURE:
            parts = ["本地采集任务", "检测样例回放"]
            if self.capability.is_live("camera"):
                parts.append("相机采集")
            return (
                "作用范围："
                + "、".join(parts)
                + "。健康遥测、心跳与关键事件通道继续运行；"
                "真实雷达是否停止发射由专用驱动确认，本机无法代为断言。"
            )
        if action == Command.ASSIGN_TASK:
            return "仅更新终端任务绑定，不改变任何硬件状态。"
        if action == Command.APPLY_CONFIG:
            return "写入终端补偿参数快照并回传 ack，不直接写入采集硬件。"
        if action == Command.PREPARE_UPDATE:
            return "下载并暂存演示模型包，切换前需本机确认。"
        if action == Command.REQUEST_UPLOAD:
            return "触发本机上传队列，不改变采集状态。"
        return "本机状态查询，无副作用。"

    # ---- 自检 ----

    def set_self_checks(self, items: List[SelfCheckItem]) -> None:
        self.self_checks = items
        self.notifier.emit("selfcheck.updated", items)

    def self_check_summary(self) -> Dict[str, int]:
        summary = {"ok": 0, "warn": 0, "fail": 0, "unavailable": 0}
        for item in self.self_checks:
            summary[item.state] = summary.get(item.state, 0) + 1
        return summary

    # ---- 校验命令（PRD §8.1）----

    def validate_command(self, command: Dict[str, Any], now: Optional[float] = None) -> tuple:
        """命令白名单、过期、批次匹配、版本匹配四项检查。

        返回 (ok, error_code, reason)。任何一项不过就拒绝执行并给出原因，
        这是剧本 S12 与验收 H08 的直接依据。
        """
        action = str(command.get("action") or command.get("type") or "")
        if action not in Command.WHITELIST:
            return False, ErrorCode.UNSUPPORTED, f"命令 {action or '(空)'} 不在白名单内"

        expires_at = command.get("expiresAt") or command.get("expires_at")
        if expires_at:
            expired = _is_past(expires_at, now)
            if expired:
                return False, ErrorCode.COMMAND_EXPIRED, f"命令已于 {expires_at} 过期，设备拒绝执行"

        if self.task.is_terminal and action in ("resume_capture",):
            return False, ErrorCode.TERMINAL_STATE, "任务已进入终态，不接受旧的继续命令"

        expected_revision = command.get("expectedTaskRevision")
        if expected_revision is not None and self.assignment.task_revision:
            if int(expected_revision) != int(self.assignment.task_revision):
                return (
                    False,
                    ErrorCode.REVISION_STALE,
                    f"命令针对任务版本 {expected_revision}，当前为 {self.assignment.task_revision}",
                )

        target_batch = command.get("targetBatchId") or command.get("target_batch_id")
        if target_batch and self.batch and target_batch != self.batch.batch_id:
            return (
                False,
                ErrorCode.BATCH_MISMATCH,
                f"命令针对批次 {target_batch}，当前活动批次为 {self.batch.batch_id}，设备只作用于匹配批次",
            )
        if target_batch and not self.batch:
            return False, ErrorCode.BATCH_MISMATCH, f"命令针对批次 {target_batch}，本机当前没有活动批次"

        if action == Command.APPLY_CONFIG:
            payload = command.get("payload") or {}
            version = payload.get("configVersion") or payload.get("config_version")
            if not version:
                return False, ErrorCode.UNSUPPORTED, "配置命令缺少 configVersion"

        return True, "", ""


def _is_past(iso_text: str, now: Optional[float] = None) -> bool:
    from datetime import datetime, timezone

    try:
        text = str(iso_text).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    reference = now if now is not None else time.time()
    return moment.timestamp() < reference
