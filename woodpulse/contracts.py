"""消息与数据契约（对齐手持端 PRD v0.2 §7、§9）。

这个模块是整个终端的“词汇表”：平台信封、能力声明、能力状态、来源标识、
样例包格式与批次目录清单都在这里定义，其它模块只引用这里，不自己造字段。

设计原则（PRD §7.3、§9）：
  · 字段名一律 lowerCamelCase，与平台 JSON 保持一致，不在端上另起名字；
  · 传输统一用 百分比 0-100 / 字节 / 秒或毫秒 / 摄氏度，UI 再做显示换算；
  · 无法采集的字段必须是 None + reason，绝不用 0 或随机数顶替（PRD §6）；
  · 来源标识 sourceMode 只有 live / replay / unavailable / preset 四种，
    任何画面与曲线都必须能回答“这段数据从哪来”。
"""

from __future__ import annotations

import platform
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #

#: 信封主版本。平台看到不认识的主版本要拒收（PRD §7.3）。
SCHEMA_VERSION = "1.0"

#: 终端应用版本。握手与设备状态页都会报这个值。
APP_VERSION = "2.0.0-demo"

#: 采集数据包格式。没有真实雷达输入，固定用 response-sequence-v1，
#: 不使用原生回波 / I-Q 流 / 雷达实采的命名（PRD §9）。
SAMPLE_FORMAT = "response-sequence-v1"

#: 端侧适配器版本（SCANNER_ADAPTER_VERSION）。上报给平台做通道核对。
ADAPTER_VERSION = "2.0.0-demo"

#: 演示模型版本（可被更新流程切换，本文件只给初始值）。
DEFAULT_MODEL_VERSION = "DEMO-M02"


# --------------------------------------------------------------------------- #
# 能力与来源标识
# --------------------------------------------------------------------------- #

class Capability:
    """capabilities 字段的取值（PRD §7.2）。"""

    LIVE = "live"
    REPLAY = "replay"
    UNAVAILABLE = "unavailable"
    PRESET = "preset"

    ALL = (LIVE, REPLAY, UNAVAILABLE, PRESET)

    #: 给界面用的中文说明。设备状态页与握手回执都直接读这里。
    LABEL = {
        LIVE: "实采",
        REPLAY: "样例回放",
        UNAVAILABLE: "未接入",
        PRESET: "预制结果",
    }


class SourceMode:
    """单条数据的来源标识（PRD §3.2、§7.3）。"""

    LIVE = "live"
    REPLAY = "replay"
    UNAVAILABLE = "unavailable"
    PRESET = "preset"

    LABEL = {
        LIVE: "相机实拍",
        REPLAY: "检测样例",
        UNAVAILABLE: "无数据",
        PRESET: "预制结果",
    }


class PositionSource:
    """positionSource（PRD §9）。只有真正标定过的定位才允许用 CALIBRATED。"""

    OPERATOR_TAG = "operator_tag"
    CALIBRATED_POSE = "calibrated_pose"
    NONE = "none"


class Pairing:
    """实拍与样例的时间配对状态（PRD §3.3、§9）。"""

    UNVERIFIED = "unverified"
    TIME_MATCHED = "time_matched"
    NONE = "none"


class OperationLabel:
    """人工标记的方向值（PRD §3.3）。

    方向由操作者选定，界面标"人工标记"；没有选择时只记录标记 01/02，
    **不猜测所在方位**。空字符串是合法值，表示未选方向。
    """

    FRONT = "正面"
    RIGHT = "右侧"
    BACK = "背面"
    LEFT = "左侧"
    CUSTOM = "自定义"
    NONE = ""

    #: 按钮顺序：绕柱连续移动时最常点的在前
    CHOICES = (NONE, FRONT, RIGHT, BACK, LEFT, CUSTOM)

    LABEL = {
        NONE: "未选方向",
        FRONT: "正面",
        RIGHT: "右侧",
        BACK: "背面",
        LEFT: "左侧",
        CUSTOM: "自定义",
    }


#: 兼容别名（早期脚本里用过这个名字）
OPERATOR_LABELS = OperationLabel.CHOICES


# --------------------------------------------------------------------------- #
# 事件类型（PRD §7.4 的 type 字段）
# --------------------------------------------------------------------------- #

class EventType:
    DEVICE_REGISTER = "device.register"
    DEVICE_HELLO = "device.hello"
    DEVICE_TELEMETRY = "device.telemetry"
    DEVICE_HEALTH = "device.health"
    DEVICE_CAPABILITIES = "device.capabilities"
    #: 自检报告。**平台契约必须收下这个类型**：它走关键事件通道（outbox 至少一次投递），
    #: 平台若按"未知类型"静默丢弃、又不把 messageId 放进 accepted/duplicated，
    #: 终端无法 ack，就会按重试节奏无限重推同一份报告（联调时实测到过）。
    #: 平台要拒收也必须**显式**把它列进 rejected（见下面 REJECTABLE 说明）。
    DEVICE_SELFCHECK = "device.selfcheck"

    CAPTURE_STARTED = "capture.started"
    CAPTURE_PAUSED = "capture.paused"
    CAPTURE_RESUMED = "capture.resumed"
    CAPTURE_FINISHED = "capture.finished"
    CAPTURE_MARK_CREATED = "capture.mark_created"
    CAPTURE_PROGRESS = "capture.progress"
    CAPTURE_ANOMALY = "capture.anomaly"

    BATCH_FINALIZED = "batch.finalized"
    BATCH_UPLOAD_STARTED = "batch.upload_started"
    BATCH_UPLOAD_COMPLETED = "batch.upload_completed"

    CONFIG_RECEIVED = "config.received"
    CONFIG_APPLIED = "config.applied"

    COMMAND_ACCEPTED = "command.accepted"
    COMMAND_EXECUTED = "command.executed"
    COMMAND_FAILED = "command.failed"

    UPDATE_DOWNLOADED = "update.downloaded"
    UPDATE_VERIFIED = "update.verified"
    UPDATE_APPLIED = "update.applied"
    UPDATE_FAILED = "update.failed"

    SYSTEM_RECOVERED = "system.recovered"
    SYSTEM_ERROR = "system.error"


#: 终端可能通过关键事件通道发出的全部类型。平台侧应据此做白名单核对，
#: 至少要保证"能收下"或"显式拒收"二者之一 —— 静默丢弃会导致无限重推。
ALL_EVENT_TYPES = tuple(
    value
    for name, value in vars(EventType).items()
    if not name.startswith("_") and isinstance(value, str)
)

#: 平台回 `/api/device-events/batch` 时的契约（PRD §7.4 未写明，这里定清楚）：
#:
#:     {
#:       "accepted":   ["msg-...", ...],   # **messageId 数组**，不是计数
#:       "duplicated": ["msg-...", ...],   # 同上，平台按 messageId 去重后命中的
#:       "rejected":   [{"messageId": "...", "reason": "...", "retryable": false}],
#:       "acceptedCount": 3, "duplicatedCount": 0, "rejectedCount": 0   # 计数是附带信息，可省
#:     }
#:
#: 三条平台侧必须遵守的规则：
#:
#:   1. **accepted / duplicated 是 messageId 数组，不是计数。**
#:      终端要靠这些 ID 在本地 outbox 里精确标记"已确认"。只回计数的话
#:      终端只能按发送顺序猜，一旦有事件被拒就会错位、重复投递。
#:
#:   2. **每个收到的 messageId 最终必须进入 accepted 或 rejected 之一。**
#:      不支持的类型（例如平台暂时没有实现 `device.selfcheck`）不能静默丢弃 ——
#:      静默丢弃时终端拿不到确认，会按重试节奏无限重推同一份报文（联调时实测到过）。
#:      平台要么收下，要么在 rejected 里带上它的 messageId。
#:
#:   3. **rejected 每条要带 retryable**，明确区分临时失败与永久拒收：
#:        retryable=false → 终端**直接出队**，不再重试（例如"不支持的事件类型"
#:                          "字段校验不通过"这类重试多少次都一样的错误）
#:        retryable=true  → 终端**保留消息并按退避重试**（例如"服务暂时不可用"
#:                          "队列已满"这类过一会儿可能就好的错误）
#:      不写 retryable 时，终端按"**临时失败**"处理（保守做法：宁可重试也不丢数据）。
EVENT_BATCH_CONTRACT_NOTE = (
    "accepted/duplicated 为 messageId 数组（非计数）；rejected 为 "
    "[{messageId, reason, retryable}]，retryable=false 表示永久拒收、终端直接出队，"
    "retryable=true 或缺省表示临时失败、终端保留并退避重试；"
    "平台必须保证每个收到的 messageId 最终进入 accepted 或 rejected 之一，不得静默丢弃。"
)

#: 平台回执里 rejected 一条记录的字段（契约测试与平台实现共用）
REJECTED_ENTRY_FIELDS = ("messageId", "reason", "retryable")


# --------------------------------------------------------------------------- #
# 命令白名单（PRD §8.1）
# --------------------------------------------------------------------------- #

class Command:
    ASSIGN_TASK = "assign_task"
    APPLY_CONFIG = "apply_config"
    PAUSE_CAPTURE = "pause_capture"
    REQUEST_UPLOAD = "request_upload"
    PREPARE_UPDATE = "prepare_update"
    QUERY_STATUS = "query_status"

    #: 远端 start / resume 需要本机确认（PRD §8.1），不在自动执行白名单里。
    WHITELIST = (ASSIGN_TASK, APPLY_CONFIG, PAUSE_CAPTURE, REQUEST_UPLOAD, PREPARE_UPDATE, QUERY_STATUS)

    #: 需要操作者在终端上点确认才执行的命令。
    REQUIRES_LOCAL_CONFIRM = ()

    #: 中文名，界面与日志用。
    LABEL = {
        ASSIGN_TASK: "派发任务",
        APPLY_CONFIG: "应用环境配置",
        PAUSE_CAPTURE: "请求暂停采集",
        REQUEST_UPLOAD: "请求上传",
        PREPARE_UPDATE: "准备更新",
        QUERY_STATUS: "查询状态",
    }


class ReceiptState:
    """回执三态（PRD §8.1）。平台不能凭“消息发出”就认为设备已执行。"""

    ACCEPTED = "accepted"
    EXECUTED = "executed"
    FAILED = "failed"

    LABEL = {ACCEPTED: "已接收", EXECUTED: "已执行", FAILED: "失败"}


class ErrorCode:
    """命令失败错误码（PRD §8.1 failed 要带错误码与原因）。"""

    BATCH_MISMATCH = "batch_mismatch"
    REVISION_STALE = "revision_stale"
    COMMAND_EXPIRED = "command_expired"
    UNSUPPORTED = "unsupported"
    NOT_READY = "not_ready"
    TERMINAL_STATE = "terminal_state"
    SCOPE_LIMITED = "scope_limited"
    CHECKSUM_FAILED = "checksum_failed"
    IO_ERROR = "io_error"


# --------------------------------------------------------------------------- #
# 任务 / 批次 / 测区 状态机词表（PRD §5.3、§10 app_state.py）
# --------------------------------------------------------------------------- #

class TaskState:
    IDLE = "idle"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    INTERRUPTED = "interrupted"

    LABEL = {
        IDLE: "待开始",
        READY: "已就绪",
        RUNNING: "采集中",
        PAUSED: "已暂停",
        FINISHED: "已结束",
        INTERRUPTED: "已中断",
    }

    #: 终态不接受旧 resume（PRD §8.1）。
    TERMINAL = (FINISHED, INTERRUPTED)


class BatchState:
    OPEN = "open"
    SEALED = "sealed"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PARTIAL = "partial"
    FAILED = "failed"

    LABEL = {
        OPEN: "记录中",
        SEALED: "已封存",
        UPLOADING: "上传中",
        UPLOADED: "平台已接收",
        PARTIAL: "部分接收",
        FAILED: "上传失败",
    }


class ConfigState:
    NONE = "none"
    RECEIVED = "received"       # 收到但未确认
    CONFIRMED = "confirmed"     # 操作者确认
    APPLIED = "applied"         # 已生效并回传 ack

    LABEL = {NONE: "无配置", RECEIVED: "待确认", CONFIRMED: "已确认", APPLIED: "已生效"}


class ConnectionState:
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ONLINE = "online"
    DEGRADED = "degraded"

    LABEL = {OFFLINE: "离线", CONNECTING: "连接中", ONLINE: "在线", DEGRADED: "延迟"}


class UploadState:
    QUEUED = "queued"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    PAUSED_OFFLINE = "paused_offline"

    LABEL = {
        QUEUED: "排队",
        ACTIVE: "上传中",
        DONE: "已完成",
        FAILED: "失败",
        PAUSED_OFFLINE: "待上传（离线）",
    }


# --------------------------------------------------------------------------- #
# 信封
# --------------------------------------------------------------------------- #

def utc_now_iso() -> str:
    """平台侧时间统一 UTC ISO-8601，带 Z（PRD §7.3）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Envelope:
    """所有上行事件的统一信封（PRD §7.3）。

    `seq` 在一次 bootId 内单调递增；重连补传时按 messageId 去重由平台负责。
    """

    type: str
    payload: Dict[str, Any]
    device_id: str
    boot_id: str
    seq: int
    demo_session_id: str
    schema_version: str = SCHEMA_VERSION
    message_id: str = field(default_factory=lambda: new_id("msg"))
    sent_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "messageId": self.message_id,
            "deviceId": self.device_id,
            "bootId": self.boot_id,
            "seq": self.seq,
            "sentAt": self.sent_at,
            "demoSessionId": self.demo_session_id,
            "type": self.type,
            "payload": self.payload,
        }


def envelope_from_dict(raw: Mapping[str, Any]) -> Envelope:
    """解析平台/别端发来的信封；字段缺失直接抛 ValueError。

    未知主版本在 platform_client 里拒收，这里只做结构校验（PRD §7.3）。
    """
    required = ("type", "deviceId", "bootId", "seq", "sentAt")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"信封缺少字段: {', '.join(missing)}")
    version = str(raw.get("schemaVersion", SCHEMA_VERSION))
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise ValueError(f"不支持的消息主版本 {version}")
    return Envelope(
        type=str(raw["type"]),
        payload=dict(raw.get("payload") or {}),
        device_id=str(raw["deviceId"]),
        boot_id=str(raw["bootId"]),
        seq=int(raw["seq"]),
        demo_session_id=str(raw.get("demoSessionId") or ""),
        schema_version=version,
        message_id=str(raw.get("messageId") or new_id("msg")),
        sent_at=str(raw["sentAt"]),
    )


# --------------------------------------------------------------------------- #
# 能力探测结果的载体
# --------------------------------------------------------------------------- #

@dataclass
class CapabilityReport:
    """启动检查生成的能力表（PRD §7.2：能力字段必须由启动检查生成）。"""

    values: Dict[str, str] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)

    def set(self, name: str, value: str, reason: Optional[str] = None) -> None:
        if value not in Capability.ALL:
            raise ValueError(f"未知能力取值 {value!r}（允许 {Capability.ALL}）")
        self.values[name] = value
        if reason:
            self.reasons[name] = reason

    def get(self, name: str, default: str = Capability.UNAVAILABLE) -> str:
        return self.values.get(name, default)

    def reason(self, name: str) -> str:
        return self.reasons.get(name, "")

    def label(self, name: str) -> str:
        return Capability.LABEL.get(self.get(name), self.get(name))

    def is_live(self, name: str) -> bool:
        return self.get(name) == Capability.LIVE

    def to_dict(self) -> Dict[str, str]:
        return dict(self.values)


# --------------------------------------------------------------------------- #
# 注册报文（PRD §7.2 示例格式）
# --------------------------------------------------------------------------- #

def build_register_payload(
    *,
    device_id: str,
    boot_id: str,
    capabilities: CapabilityReport,
    started_at: Optional[float] = None,
    app_version: str = APP_VERSION,
    adapter_version: str = ADAPTER_VERSION,
    model_version: str = DEFAULT_MODEL_VERSION,
    operator_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "deviceId": device_id,
        "bootId": boot_id,
        "appVersion": app_version,
        "adapterVersion": adapter_version,
        "modelVersion": model_version,
        "operatorId": operator_id,
        "host": {
            "hostname": platform.node(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "startedAt": (
            datetime.fromtimestamp(started_at, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if started_at
            else utc_now_iso()
        ),
        "capabilities": capabilities.to_dict(),
        "capabilityReasons": dict(capabilities.reasons),
    }


def build_capabilities_payload(capabilities: CapabilityReport, changed: Optional[list] = None) -> Dict[str, Any]:
    """能力变化时单独发一条，平台据此更新通道状态（PRD §7.2、§11）。"""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "capabilities": capabilities.to_dict(),
        "capabilityReasons": dict(capabilities.reasons),
        "changed": list(changed or []),
    }


# --------------------------------------------------------------------------- #
# 遥测（PRD §6 表格逐行对应）
# --------------------------------------------------------------------------- #

#: 遥测里允许出现在 payload 顶层的字段，用于自检与契约测试。
TELEMETRY_KEYS = (
    "sourceMode",
    "sampleWindowMs",
    "cpuPercent",
    "cpuQuality",
    "processCpuPercent",
    "processRssBytes",
    "memory",
    "disk",
    "socTempC",
    "socTempSource",
    "throttled",
    "cpuFreqMhz",
    "network",
    "platformLatencyMs",
    "camera",
    "replay",
    "upload",
    "versions",
    "wifi",
    "uptimeSeconds",
)

#: 网络子字段
NETWORK_KEYS = ("interface", "address", "linkUp", "txBytesPerSec", "rxBytesPerSec", "isWireless")

#: 相机子字段
CAMERA_KEYS = ("backend", "device", "captureFps", "displayFps", "droppedFrames", "lastFrameAgeMs", "state", "reason")

#: 样例播放器子字段
REPLAY_KEYS = ("scenarioId", "batchId", "frameIndex", "frameCount", "playbackFps", "datasetHash", "sourceMode")

#: 上传队列子字段
UPLOAD_KEYS = ("queued", "pendingBytes", "confirmedBytes", "activeFile", "lastError")

#: 版本子字段
VERSION_KEYS = ("appVersion", "adapterVersion", "controllerVersion", "demoModelVersion", "configVersion")


def null_field(reason: str) -> Dict[str, Any]:
    """不可用字段的标准写法（PRD §6：返回 null、supported=false 或 quality=unavailable，并带 reason）。"""
    return {"value": None, "supported": False, "quality": "unavailable", "reason": reason}


# --------------------------------------------------------------------------- #
# 批次目录清单（PRD §9）
# --------------------------------------------------------------------------- #

#: 批次目录里必须存在的文件与目录，缺失在自检里报出来。
BATCH_REQUIRED_FILES = (
    "manifest.json",
    "config.json",
    "marks.json",
    "segments.json",
    "frames.csv",
    "quality.json",
)

BATCH_REQUIRED_DIRS = ("images",)

BATCH_OPTIONAL_FILES = ("batch.json", "dataset.json", "result.json", "plan.json", "events.log")

#: segments.json 的一条记录字段
SEGMENT_KEYS = (
    "segmentId",
    "batchId",
    "frameId",
    "frameIndex",
    "zoneId",
    "tNs",
    "deviceMonotonicNs",
    "sampleCount",
    "axes",
    "sourceMode",
    "pairedImage",
    "quality",
    "peaks",
)

#: marks.json 的一条记录字段（PRD §9）
MARK_KEYS = (
    "markId",
    "batchId",
    "frameId",
    "cameraAssetId",
    "deviceMonotonicNs",
    "operatorLabel",
    "positionSource",
    "note",
    "createdAt",
)

#: frames.csv 列顺序（format=response-sequence-v1）
FRAMES_CSV_COLUMNS = ("frame_index", "sample_index", "amplitude", "t_ms")

#: marks.csv 列顺序（人可读副本，权威记录是 marks.json）
MARKS_CSV_COLUMNS = ("mark_id", "frame_index", "zone_id", "operator_label", "position_source", "device_monotonic_ns")


def monotonic_ns() -> int:
    """设备单调时间。标记与样例帧都靠它做同机排序（PRD §9）。"""
    return time.monotonic_ns()


def hash_sample_file(path: str, algo: str = "sha256", chunk: int = 1 << 20) -> str:
    """流式摘要，避免把大文件读进内存（PRD §11 归档校验同款口径）。"""
    import hashlib

    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
