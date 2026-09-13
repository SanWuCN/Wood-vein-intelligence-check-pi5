#!/usr/bin/env python3
"""木脉智检 · 本地模拟平台服务器（只用 Python 3.11 标准库）。

为什么要有这个文件
------------------
手持端 PRD §7.4 里的接口（设备注册、设备 WebSocket、分片上传、批次提交、产物下发）
在现有平台（`server/`）里**还不存在**。真平台没就绪时，终端侧没法联调：
既没法验证握手、心跳、重连退避，也没法验证分片续传、摘要比对、部分接收这些
"失败分支"——而那些分支恰恰是最容易出问题的地方。

所以这里做一个**能独立跑起来、行为与 PRD 一致**的模拟平台：终端只要改
`platform.platform_url` 就能连它，接口字段与 `woodpulse/contracts.py` 完全同名。

实现取舍
--------
1. 只用标准库：`http.server` + 手写 RFC6455。原因有二：树莓派现场不一定能装
   `flask`/`websockets`；而且终端侧也是手写 WS 客户端，双方独立实现才能互相验证。
2. 元数据全部在内存，字节落 `--data-dir`。模拟平台不是业务系统，重启即清空，
   比维护一个半吊子 SQLite 更能避免"以为是持久化"的误会（启动横幅会明说）。
3. 故障注入是一等公民（`--fail-register` / `--reject-checksum` / `--drop-after-bytes`
   / `--latency-ms`）。终端的重试与部分接收分支，靠正常路径测不出来。
4. 日志逐行真打：时间、方向、方法、路径、状态、耗时、设备号。不"假装"。

启动
----
    python tools/mock_platform.py --port 8080 --data-dir .mock-platform-data \
        --device-token demo-token --verbose

浏览器用的 `/ws`（平台侧 hub.mjs）**不在这里**：模拟平台只占用 `/ws/devices/{deviceId}`，
避免和真平台的浏览器通道抢路径。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 契约：字段名一律照抄 woodpulse/contracts.py，绝不在这里另起名字
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = "1.0"

#: 命令白名单（PRD §8.1，contracts.Command.WHITELIST）
COMMAND_WHITELIST = (
    "assign_task",
    "apply_config",
    "pause_capture",
    "request_upload",
    "prepare_update",
    "query_status",
)

#: 回执三态（PRD §8.1，contracts.ReceiptState）
RECEIPT_ACCEPTED = "accepted"
RECEIPT_EXECUTED = "executed"
RECEIPT_FAILED = "failed"
RECEIPT_STATES = (RECEIPT_ACCEPTED, RECEIPT_EXECUTED, RECEIPT_FAILED)

#: 命令失败错误码（PRD §8.1，contracts.ErrorCode）
ERROR_CODES = (
    "batch_mismatch",
    "revision_stale",
    "command_expired",
    "unsupported",
    "not_ready",
    "terminal_state",
    "scope_limited",
    "checksum_failed",
    "io_error",
)

#: 终端会上行的信封 type（contracts.EventType 里我们认识的子集）
KNOWN_EVENT_TYPES = (
    "device.register",
    "device.hello",
    "device.telemetry",
    "device.health",
    "device.capabilities",
    "capture.started",
    "capture.paused",
    "capture.resumed",
    "capture.finished",
    "capture.mark_created",
    "capture.progress",
    "capture.anomaly",
    "batch.finalized",
    "batch.upload_started",
    "batch.upload_completed",
    "config.received",
    "config.applied",
    "command.accepted",
    "command.executed",
    "command.failed",
    "update.downloaded",
    "update.verified",
    "update.applied",
    "update.failed",
    "system.recovered",
    "system.error",
)

#: contracts.EventType 里没有、但终端实际会发的扩展类型。
#:
#: 默认**收下**是有意的：模拟平台的角色是"照契约演一遍"，而平台侧正确的做法就是
#: 要么收下、要么**显式拒收**；静默丢弃会让终端无法 ack 而无限重推。
#: 想验证"平台暂不支持某类型"的情况，不要删这里，用 `--reject-extra-types`：
#: 那时平台必须回 rejected + messageId + retryable=false，终端据此出队、不再重试。
#: 两条路径都要能演示，所以做成开关而不是写死。
EXTRA_ACCEPTED_TYPES = ("device.selfcheck",)

#: 事件被拒的原因 → 是否可重试（PRD 未规定，这里定清楚，与
#: `contracts.EVENT_BATCH_CONTRACT_NOTE` 一致）。
#:
#:   retryable=False → 永久拒收：重试多少次结果都一样，终端应直接出队，避免无限重推
#:   retryable=True  → 临时失败：过一会儿可能就好了，终端应保留并退避重试
REJECT_RETRYABLE = {
    "missing_type": False,                # 信封不合法，重推也不会变
    "missing_message_id": False,          # 没有 messageId 无从去重，属永久性问题
    "unsupported_schema_version": False,  # 主版本不认识（PRD §7.3：未知主版本拒收）
    "device_id_mismatch": False,          # 信封里的设备号与连接身份不符
    "unknown_type": False,                # 平台没实现这个事件类型 —— 本文件要覆盖的场景
    "envelope_not_object": False,
    "not_an_object": False,
    "queue_full": True,                   # 平台侧队列满：临时状态
    "server_busy": True,                  # 平台过载：临时状态
}


def reject_reason_code(reason: str) -> str:
    """把 ingest 返回的原因串归一到 REJECT_RETRYABLE 的键。

    ingest 返回的是 `unknown_type(capture.xxx)` 这种带参数的串，
    归类时只取括号前的部分，原文仍保留在响应里给人看。
    """
    head = str(reason).split("(", 1)[0].strip()
    return head if head in REJECT_RETRYABLE else "unknown_type"


def reject_is_retryable(reason: str) -> bool:
    """判断一条拒收是否可重试。

    缺省返回 True：宁可让终端多试几次，也不要因为平台漏写一个字段就丢掉业务数据。
    这与终端侧的默认口径一致（`platform_client._post_events_batch`）。
    """
    head = str(reason).split("(", 1)[0].strip()
    return REJECT_RETRYABLE.get(head, True)

#: 批次目录必须存在的文件（PRD §9，contracts.BATCH_REQUIRED_FILES）
BATCH_REQUIRED_FILES = (
    "manifest.json",
    "config.json",
    "marks.json",
    "segments.json",
    "frames.csv",
    "quality.json",
)

#: 文件角色（PRD §9.2 files[].role，并兼容终端 storage._guess_role 的词表）。
#: 不在表里的角色按 "other" 记，不拒收——模拟平台不该因为角色名没见过就把整批打回。
FILE_ROLES = ("radar", "image", "image_index", "frames", "segments", "marks", "marks_csv",
              "manifest", "config", "quality", "result", "dataset", "plan", "events",
              "batch", "report", "artifact", "preview", "other")

#: 演示包类型（平台 PRD §11.3：独立模型包 / 集成固件包 / 演示包）
ARTIFACT_KIND = "demo_nonflashable"
PACKAGE_TYPE = "demo_package"

#: 模拟平台对未知能力名不做校验，只把终端报上来的原样回显给平台侧。
CAPABILITY_VALUES = ("live", "replay", "unavailable", "preset")


def contracts_selftest() -> str:
    """启动时拿真正的 contracts.py 对一遍常量。

    为什么要做：模拟平台和终端各自写着同一批字符串，一旦有人改了契约而另一边没跟上，
    现象会是"命令发出去了但设备不理"，非常难查。这里在启动横幅里直接说清楚。
    导入失败不算错误（模拟平台要能单独跑），只说一声用了内置常量。
    """
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from woodpulse import contracts  # type: ignore
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        return f"内置常量（未能导入 woodpulse.contracts：{exc}）"

    problems: List[str] = []
    if tuple(contracts.Command.WHITELIST) != COMMAND_WHITELIST:
        problems.append(f"命令白名单 contract={contracts.Command.WHITELIST} mock={COMMAND_WHITELIST}")
    for name in ("accepted", "executed", "failed"):
        if getattr(contracts.ReceiptState, name.upper()) != name:
            problems.append(f"回执态 {name} 不一致")
    missing_events = [t for t in KNOWN_EVENT_TYPES if not hasattr(contracts.EventType, t.upper().replace(".", "_"))]
    if missing_events:
        problems.append(f"contracts.EventType 里找不到：{missing_events}")
    missing_codes = [c for c in ERROR_CODES if not hasattr(contracts.ErrorCode, c.upper())]
    if missing_codes:
        problems.append(f"contracts.ErrorCode 里找不到：{missing_codes}")
    if tuple(contracts.BATCH_REQUIRED_FILES) != BATCH_REQUIRED_FILES:
        problems.append("批次必需文件清单不一致")
    if str(contracts.SCHEMA_VERSION) != SCHEMA_VERSION:
        problems.append(f"schemaVersion 不一致 contract={contracts.SCHEMA_VERSION} mock={SCHEMA_VERSION}")
    if problems:
        return "与 woodpulse.contracts 不一致（以下问题不改平台代码，只在终端侧对齐）：\n    - " + "\n    - ".join(problems)
    return "与 woodpulse.contracts 一致（命令白名单、回执态、事件类型、错误码、批次清单）"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_FRAME = 8 * 1024 * 1024          # 单帧上限，超了按 1009 关闭
HTTP_MAX_BODY = 8 * 1024 * 1024         # JSON 请求体上限（分片走流式，不受这个限制）


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    """平台侧时间统一 UTC ISO-8601 带 Z（PRD §7.3），毫秒精度便于看耗时。"""
    dt = dt or utc_now()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_plus_ms(ms: int) -> str:
    return iso(utc_now() + timedelta(milliseconds=ms))


def parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def new_id(prefix: str) -> str:
    """与 contracts.new_id 同款短 ID：cmd-1a2b3c4d5e6f。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def mask_token(token: Optional[str]) -> str:
    """日志里不打印完整设备令牌，只留前 6 位便于比对。"""
    if not token:
        return "(无)"
    return token[:6] + "…" if len(token) > 6 else token


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式摘要：PRD §11 归档校验同款口径，不把大文件读进内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def guess_media_type(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return {
        ".json": "application/json",
        ".csv": "text/csv; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".zip": "application/zip",
    }.get(suffix, "application/octet-stream")


def lan_ip() -> str:
    """给一条能打印到横幅里的局域网地址（UDP connect 不发包，只是问内核选哪张网卡）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class HttpError(Exception):
    """带状态码与错误码的异常。

    错误体沿用平台 `server/api/http.mjs` 的统一形状：
    `{code, message, fieldErrors, retryable, ...extra}`，这样终端只写一套错误解析。
    """

    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.extra = extra

    def body(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "fieldErrors": [],
            "retryable": self.retryable,
        }
        payload.update(self.extra)
        return payload


# --------------------------------------------------------------------------- #
# 内存记录
# --------------------------------------------------------------------------- #

class FileRecord:
    def __init__(self, *, file_id: str, name: str, size: int, sha256: str,
                 media_type: str, path: Path, device_id: str, batch_id: Optional[str],
                 role: str, session_id: str, reused_from: Optional[str] = None) -> None:
        self.file_id = file_id
        self.name = name
        self.size = size
        self.sha256 = sha256
        self.media_type = media_type
        self.path = path
        self.device_id = device_id
        self.batch_id = batch_id
        self.role = role
        self.session_id = session_id
        self.reused_from = reused_from
        self.uploaded_at = iso()
        self.downloads = 0

    def brief(self) -> Dict[str, Any]:
        return {
            "fileId": self.file_id,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "mediaType": self.media_type,
            "role": self.role,
            "batchId": self.batch_id,
        }


class UploadRecord:
    def __init__(self, *, upload_id: str, device_id: str, boot_id: str, name: str,
                 size: int, sha256: str, batch_id: Optional[str], role: str,
                 media_type: str, session_id: str, part_path: Path) -> None:
        self.upload_id = upload_id
        self.device_id = device_id
        self.boot_id = boot_id
        self.name = name
        self.size = size
        self.sha256 = sha256
        self.batch_id = batch_id
        self.role = role
        self.media_type = media_type
        self.session_id = session_id
        self.part_path = part_path
        self.received_offset = 0
        self.chunk_count = 0
        self.duplicate_chunks = 0
        self.completed = False
        self.completed_at: Optional[str] = None
        self.file_id: Optional[str] = None
        self.reused = False
        self.created_at = iso()
        self.expires_at = iso_plus_ms(30 * 60 * 1000)


class CommandRecord:
    def __init__(self, *, command_id: str, device_id: str, type_: str,
                 expected_task_revision: Optional[int], target_batch_id: Optional[str],
                 expires_at: str, payload: Dict[str, Any], actor_id: str,
                 seq: int, boot_id: str, session_id: str) -> None:
        self.command_id = command_id
        self.device_id = device_id
        self.type = type_
        self.expected_task_revision = expected_task_revision
        self.target_batch_id = target_batch_id
        self.expires_at = expires_at
        self.payload = payload
        self.actor_id = actor_id
        self.seq = seq
        self.boot_id = boot_id
        self.session_id = session_id
        self.issued_at = iso()
        self.delivery_attempts = 0
        self.delivered = False
        self.delivered_at: Optional[str] = None
        self.receipt: Optional[Dict[str, Any]] = None
        self.expired = False
        self.expired_notified = False

    def frame(self) -> Dict[str, Any]:
        """下行命令帧。PRD §8.1 要求命令体含 commandId / expectedTaskRevision /
        targetBatchId / expiresAt / payload，所以这几个字段放顶层；同时带上信封公共字段，
        终端可以复用同一套信封解析与 bootId 过期判断。

        为什么 payload 里又镜像一份：终端现有 `platform_client._handle_inbound()`
        对 `kind=command` 的处理是 `on_command(dict(message["payload"]))`——只把 payload
        交给命令处理器。镜像一份，只读 payload 的解析器也能拿到 commandId/expiresAt；
        顶层字段是权威值，两边不一致时以顶层为准。
        """
        payload = dict(self.payload)
        payload.setdefault("commandId", self.command_id)
        payload.setdefault("type", self.type)
        payload.setdefault("expectedTaskRevision", self.expected_task_revision)
        payload.setdefault("targetBatchId", self.target_batch_id)
        payload.setdefault("expiresAt", self.expires_at)
        return {
            "kind": "command",
            "schemaVersion": SCHEMA_VERSION,
            "messageId": new_id("msg"),
            "deviceId": self.device_id,
            "bootId": self.boot_id,
            "seq": self.seq,
            "sentAt": iso(),
            "demoSessionId": self.session_id,
            "type": self.type,
            "commandId": self.command_id,
            "expectedTaskRevision": self.expected_task_revision,
            "targetBatchId": self.target_batch_id,
            "expiresAt": self.expires_at,
            "issuedAt": self.issued_at,
            "actorId": self.actor_id,
            "payload": payload,
        }

    def brief(self) -> Dict[str, Any]:
        return {
            "commandId": self.command_id,
            "type": self.type,
            "expectedTaskRevision": self.expected_task_revision,
            "targetBatchId": self.target_batch_id,
            "expiresAt": self.expires_at,
            "issuedAt": self.issued_at,
            "actorId": self.actor_id,
            "delivered": self.delivered,
            "deliveredAt": self.delivered_at,
            "deliveryAttempts": self.delivery_attempts,
            "expired": self.expired,
            "receipt": self.receipt,
            "payload": self.payload,
        }


class ArtifactRecord:
    def __init__(self, *, artifact_id: str, file: FileRecord, version: str,
                 previous_version: str, target: Dict[str, Any], contents: List[Dict[str, Any]],
                 notes: List[str]) -> None:
        self.artifact_id = artifact_id
        self.file = file
        self.version = version
        self.previous_version = previous_version
        self.target = target
        self.contents = contents
        self.notes = notes
        self.released_at = iso()

    def manifest(self) -> Dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "artifactKind": ARTIFACT_KIND,          # 演示包不可烧录（平台 PRD §11.3）
            "packageType": PACKAGE_TYPE,
            "demoOnly": True,
            "target": self.target,
            "version": self.version,
            "previousVersion": self.previous_version,
            "sha256": self.file.sha256,
            "size": self.file.size,
            "fileId": self.file.file_id,
            "mediaType": self.file.media_type,
            "downloadUrl": f"/api/files/{self.file.file_id}/download",
            "releasedAt": self.released_at,
            "contents": self.contents,
            "notes": self.notes,
        }


class DeviceRecord:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.boot_id: Optional[str] = None
        self.registered_at: Optional[str] = None
        self.register_count = 0
        self.last_seen_at: Optional[str] = None
        self.last_seen_monotonic = 0.0
        self.app_version: Optional[str] = None
        self.adapter_version: Optional[str] = None
        self.model_version: Optional[str] = None
        self.operator_id: Optional[str] = None
        self.host: Dict[str, Any] = {}
        self.capabilities: Dict[str, str] = {}
        self.capability_reasons: Dict[str, str] = {}
        self.last_telemetry: Optional[Dict[str, Any]] = None
        self.last_event: Optional[Dict[str, Any]] = None
        self.last_receipt: Optional[Dict[str, Any]] = None
        self.last_demo_session_id: Optional[str] = None
        self.config_version: Optional[str] = None
        self.task_revision = 0
        self.current_batch_id: Optional[str] = None
        self.commands: Dict[str, CommandRecord] = {}
        self.command_order: List[str] = []
        self.counters = {"telemetry": 0, "events": 0, "eventsDuplicated": 0, "commands": 0, "receipts": 0}
        self.tokens: Dict[str, Optional[float]] = {}      # token → 过期时刻（None=长期有效）
        self.sessions: List["WsSession"] = []
        self.previews: Deque[Dict[str, Any]] = deque()
        self.auth_failures = 0
        self.last_auth_failure: Optional[str] = None

    # ---- 令牌 ----

    def accept_token(self, token: str) -> bool:
        expiry = self.tokens.get(token, "missing")
        if expiry == "missing":
            return False
        if expiry is not None and expiry < time.monotonic():
            return False
        return True

    def rotate_token(self, new_token: str, grace_ms: int) -> None:
        now = time.monotonic()
        for key in list(self.tokens):
            self.tokens[key] = now + grace_ms / 1000.0     # 旧令牌宽限期后失效
        self.tokens[new_token] = None

    # ---- 在线状态 ----

    def connection_state(self, offline_after_ms: int) -> str:
        if self.sessions:
            return "online"
        if not self.last_seen_monotonic:
            return "offline"
        age_ms = (time.monotonic() - self.last_seen_monotonic) * 1000.0
        return "degraded" if age_ms <= offline_after_ms else "offline"

    def state(self, offline_after_ms: int) -> Dict[str, Any]:
        pending = [c.brief() for c in self._commands() if c.receipt is None]
        return {
            "deviceId": self.device_id,
            "registered": self.boot_id is not None,
            "bootId": self.boot_id,
            "registeredAt": self.registered_at,
            "registerCount": self.register_count,
            "lastSeenAt": self.last_seen_at,
            "connectionState": self.connection_state(offline_after_ms),
            "websocketClients": len(self.sessions),
            "appVersion": self.app_version,
            "adapterVersion": self.adapter_version,
            "modelVersion": self.model_version,
            "operatorId": self.operator_id,
            "host": self.host,
            "configVersion": self.config_version,
            "taskRevision": self.task_revision,
            "currentBatchId": self.current_batch_id,
            "capabilities": self.capabilities,
            "capabilityReasons": self.capability_reasons,
            "lastDemoSessionId": self.last_demo_session_id,
            "lastTelemetry": self.last_telemetry,
            "lastEvent": self.last_event,
            "lastReceipt": self.last_receipt,
            "commands": [c.brief() for c in self._commands()],
            "pendingCommands": pending,
            "previews": list(self.previews),
            "counters": dict(self.counters),
            "authFailures": self.auth_failures,
            "lastAuthFailure": self.last_auth_failure,
        }

    def _commands(self) -> List[CommandRecord]:
        return [self.commands[cid] for cid in self.command_order]


# --------------------------------------------------------------------------- #
# WebSocket（RFC6455 手写实现）
# --------------------------------------------------------------------------- #

class IdleTimeout(Exception):
    """空闲：一个帧都没等到。用于发 ping / 判死连接，不代表连接坏了。"""


class WsClosed(Exception):
    """对端关闭或协议错误。"""


class WsSession:
    """一条设备 WebSocket 连接。

    读写都在本连接的线程里（ThreadingHTTPServer 一个连接一个线程），
    但下行命令可能从别的线程（HTTP 请求 / 控制台 / 巡检线程）推过来，
    所以写操作统一走 `send_lock`。
    """

    def __init__(self, platform: "PlatformCore", sock: socket.socket,
                 device_id: str, path: str) -> None:
        self.platform = platform
        self.sock = sock
        self.device_id = device_id
        self.path = path
        self.send_lock = threading.Lock()
        self.closed = False
        self.buffer = bytearray()
        self.frag_opcode: Optional[int] = None
        self.frag_buffer = bytearray()
        self.last_rx = time.monotonic()
        self.opened_at = iso()
        self.frames_in = 0
        self.frames_out = 0
        self.last_ping_at = 0.0

    # ---- 底层收发 ----

    def _recv_exact(self, need: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self.buffer) < need:
            remaining = deadline - time.monotonic()
            started_empty = not self.buffer
            if remaining <= 0:
                # 帧头都没收到才算空闲；帧收到一半超时就是连接坏了
                raise IdleTimeout() if started_empty else WsClosed("帧内读取超时")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                raise IdleTimeout() if started_empty else WsClosed("帧内读取超时")
            except OSError as exc:
                raise WsClosed(f"读取失败：{exc}") from exc
            if not chunk:
                raise WsClosed("对端已关闭")
            self.buffer += chunk
        data = bytes(self.buffer[:need])
        del self.buffer[:need]
        return data

    def read_frame(self, timeout: float) -> Tuple[int, bytes]:
        """返回 (opcode, payload)。客户端发来的帧必须带掩码（RFC6455 §5.3）。"""
        b0, b1 = self._recv_exact(2, timeout)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2, timeout))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8, timeout))[0]
        if length > WS_MAX_FRAME:
            self.send_close(1009, "帧过大")
            raise WsClosed(f"帧过大 {length} 字节")
        mask = self._recv_exact(4, timeout) if masked else None
        payload = self._recv_exact(length, timeout) if length else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        if not fin:
            # 分片：把后续 continuation 拼起来再交给上层
            if opcode != 0x0:
                self.frag_opcode = opcode
                self.frag_buffer = bytearray(payload)
            else:
                self.frag_buffer += payload
            return self.read_frame(timeout)
        if opcode == 0x0 and self.frag_opcode is not None:
            self.frag_buffer += payload
            payload = bytes(self.frag_buffer)
            opcode = self.frag_opcode
            self.frag_buffer = bytearray()
            self.frag_opcode = None
        return opcode, payload

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        header = bytearray()
        header.append(0x80 | opcode)              # FIN=1
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < (1 << 16):
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        with self.send_lock:
            if self.closed:
                return
            try:
                self.sock.sendall(bytes(header) + payload)
            except OSError as exc:
                self.closed = True
                raise WsClosed(f"发送失败：{exc}") from exc
        self.frames_out += 1

    def send_json(self, obj: Dict[str, Any], kind: str = "json") -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_frame(0x1, raw)
        self.platform.log_ws(">>", self.device_id, self.path, kind,
                             len(raw), summary=self.platform.summarize_out(obj))

    def send_ping(self) -> None:
        self.send_frame(0x9, b"woodpulse")
        self.last_ping_at = time.monotonic()

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:100]
        try:
            self.send_frame(0x8, payload)
        except WsClosed:
            pass
        self.closed = True

    # ---- 生命周期 ----

    def run(self) -> None:
        plat = self.platform
        ping_interval = plat.args.ws_ping_interval
        dead_after = max(ping_interval * 2.0, plat.args.offline_after_ms / 1000.0 * 2)
        while not self.closed:
            try:
                opcode, payload = self.read_frame(ping_interval)
            except IdleTimeout:
                # 空闲不是错误：设备遥测停发时用它探活
                if time.monotonic() - self.last_rx > dead_after:
                    plat.log(f"!! 设备 {self.device_id} 的 WS 连接 {dead_after:.0f} 秒无任何帧，判定假活并关闭")
                    self.send_close(1001, "心跳超时")
                    break
                try:
                    self.send_ping()
                except WsClosed:
                    break
                continue
            except WsClosed as exc:
                plat.log(f"-- WS 设备 {self.device_id} 结束：{exc}", verbose_only=False)
                break

            self.last_rx = time.monotonic()
            self.frames_in += 1
            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                plat.log_ws("<<", self.device_id, self.path, f"CLOSE({code})", len(payload))
                self.send_close(1000, "bye")
                break
            if opcode == 0x9:
                plat.log_ws("<<", self.device_id, self.path, "PING", len(payload))
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                plat.log_ws("<<", self.device_id, self.path, "PONG", len(payload))
                continue
            if opcode in (0x1, 0x2):
                kind = "TEXT" if opcode == 0x1 else "BINARY"
                plat.log_ws("<<", self.device_id, self.path, kind, len(payload),
                            summary=plat.summarize_in(payload, opcode))
                plat.touch_device(self.device_id, source="ws")
                if opcode == 0x1:
                    try:
                        plat.on_ws_text(self, payload.decode("utf-8"))
                    except UnicodeDecodeError:
                        plat.log(f"!! 设备 {self.device_id} 发来非 UTF-8 文本帧，已忽略")
                else:
                    plat.log(f"!! 设备 {self.device_id} 发来二进制帧（本通道只收 JSON 文本），已忽略")
                continue
            plat.log(f"!! 未知 WS opcode 0x{opcode:x}（设备 {self.device_id}），忽略")


# --------------------------------------------------------------------------- #
# 平台核心
# --------------------------------------------------------------------------- #

class PlatformCore:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.RLock()
        self.log_lock = threading.Lock()
        self.started = time.monotonic()
        self.devices: Dict[str, DeviceRecord] = {}
        self.uploads: Dict[str, UploadRecord] = {}
        self.files: Dict[str, FileRecord] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.artifacts: Dict[str, ArtifactRecord] = {}
        self.receipts: Dict[str, Dict[str, Any]] = {}          # receiptId → body
        self.receipt_by_command: Dict[str, str] = {}           # commandId → receiptId
        self.seen_messages: Dict[str, str] = {}                # messageId → deviceId（去重）
        self.global_tokens = set(t for t in args.device_token.split(",") if t)
        self.shutdown_event = threading.Event()
        self.data_dir = Path(args.data_dir).expanduser().resolve()
        self.uploads_dir = self.data_dir / "uploads"
        self.files_dir = self.data_dir / "files"
        self.artifacts_dir = self.data_dir / "artifacts"
        self.previews_dir = self.data_dir / "previews"
        for path in (self.uploads_dir, self.files_dir, self.artifacts_dir, self.previews_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.reject_checksum = bool(args.reject_checksum)   # --reject-checksum：每次 complete 都注入损坏

    # ---- 日志 ----

    def log(self, message: str, verbose_only: bool = False) -> None:
        if verbose_only and not self.args.verbose:
            return
        with self.log_lock:
            print(f"{iso()} {message}", flush=True)

    def log_http(self, direction: str, method: str, path: str,
                 status: Optional[int], duration_ms: Optional[float],
                 device_id: Optional[str], extra: str = "") -> None:
        """一行一条：时间、方向、方法、路径、状态、耗时、设备号。"""
        status_text = "-" if status is None else str(status)
        dur_text = "-" if duration_ms is None else f"{duration_ms:.0f}ms"
        dev = device_id or "-"
        suffix = f" {extra}" if extra else ""
        with self.log_lock:
            print(f"{iso()} {direction} {method:<6} {path:<44} status={status_text:<4} "
                  f"dur={dur_text:<7} dev={dev}{suffix}", flush=True)

    def log_ws(self, direction: str, device_id: str, path: str, frame: str,
               size: int, summary: str = "") -> None:
        suffix = f" {summary}" if summary else ""
        with self.log_lock:
            print(f"{iso()} {direction} {'WS':<6} {path:<44} status={'-':<4} dur={'-':<7} "
                  f"dev={device_id} frame={frame} {size}B{suffix}", flush=True)

    def summarize_in(self, payload: bytes, opcode: int) -> str:
        if opcode != 0x1 or not self.args.verbose:
            return ""
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "（非 JSON）"
        if isinstance(obj, dict):
            t = obj.get("type") or obj.get("kind") or "?"
            return f"type={t}"
        return ""

    def summarize_out(self, obj: Dict[str, Any]) -> str:
        if not self.args.verbose:
            return ""
        kind = obj.get("kind", "?")
        if kind == "command":
            return f"kind=command type={obj.get('type')} commandId={obj.get('commandId')}"
        return f"kind={kind}"

    # ---- 设备与令牌 ----

    def device(self, device_id: str, create: bool = True) -> Optional[DeviceRecord]:
        with self.lock:
            rec = self.devices.get(device_id)
            if rec is None and create:
                rec = DeviceRecord(device_id)
                for token in self.global_tokens:
                    rec.tokens[token] = None
                self.devices[device_id] = rec
            return rec

    def token_ok(self, device_id: str, token: Optional[str]) -> bool:
        if not token:
            return False
        with self.lock:
            if token in self.global_tokens:
                return True
            rec = self.devices.get(device_id)
            return bool(rec and rec.accept_token(token))

    def note_auth_failure(self, device_id: str, token: Optional[str], path: str) -> None:
        with self.lock:
            rec = self.device(device_id)
            if rec:
                rec.auth_failures += 1
                rec.last_auth_failure = f"{iso()} {path} token={mask_token(token)}"
        self.log(f"!! 鉴权失败 {path} dev={device_id} token={mask_token(token)} → 401")

    def touch_device(self, device_id: str, *, source: str = "http") -> None:
        with self.lock:
            rec = self.device(device_id)
            if rec:
                rec.last_seen_at = iso()
                rec.last_seen_monotonic = time.monotonic()

    # ---- WebSocket 会话管理 ----

    def ws_attach(self, session: WsSession) -> None:
        rec = self.device(session.device_id)
        assert rec is not None
        with self.lock:
            rec.sessions.append(session)
            rec.last_seen_at = iso()
            rec.last_seen_monotonic = time.monotonic()
            pending = [c.frame() for c in rec._commands() if c.receipt is None and not c.expired]
        self.log(f"++ 设备 {session.device_id} 设备通道已连接（{len(rec.sessions)} 条）")
        # 握手回执：终端据此确认平台认了它这一次 bootId。
        # `payload.platformConfig` 是给终端 platform_client 用的（它读 payload.platformConfig
        # 填自己的 _platform_config），否则终端会以为平台没下发通道参数。
        session.send_json({
            "kind": "hello",
            "schemaVersion": SCHEMA_VERSION,
            "deviceId": session.device_id,
            "bootId": rec.boot_id,
            "demoSessionId": self.args.session_id,
            "heartbeatIntervalMs": self.args.heartbeat_interval_ms,
            "offlineAfterMs": self.args.offline_after_ms,
            "configVersion": self.args.config_version,
            "platformTime": iso(),
            "pendingCommandCount": len(pending),
            "note": "设备通道已就绪；遥测/事件/回执走此通道，样本与更新包走 HTTP",
            "payload": {
                "platformConfig": {
                    "heartbeatIntervalMs": self.args.heartbeat_interval_ms,
                    "offlineAfterMs": self.args.offline_after_ms,
                    "configVersion": self.args.config_version,
                    "demoSessionId": self.args.session_id,
                    "commandTtlMs": self.args.command_ttl_ms,
                    "chunkSizeHint": self.args.chunk_hint,
                    "previewKeep": self.args.preview_keep,
                }
            },
        })
        # 至少一次投递：重连后把还没回执、没过的命令重推一遍（终端按 commandId 幂等）
        with self.lock:
            records = [c for c in rec._commands() if c.receipt is None and not c.expired]
        for record in records:
            if self.push_command(record, reason="reconnect"):
                self.log(f".. 重推未回执命令 {record.command_id}（{record.type}）给 {session.device_id}",
                         verbose_only=True)

    def ws_detach(self, session: WsSession) -> None:
        with self.lock:
            rec = self.devices.get(session.device_id)
            if rec and session in rec.sessions:
                rec.sessions.remove(session)
            left = len(rec.sessions) if rec else 0
        self.log(f"-- 设备 {session.device_id} 设备通道断开（剩余 {left} 条）："
                 f"收 {session.frames_in} 帧 / 发 {session.frames_out} 帧，"
                 f"存活 {time.monotonic() - session.last_rx:.1f}s 内有活动")

    def push_command(self, record: CommandRecord, reason: str = "issue") -> bool:
        """把命令推给该设备所有在线连接。返回是否至少送到一条连接。"""
        with self.lock:
            rec = self.devices.get(record.device_id)
            sessions = list(rec.sessions) if rec else []
            record.delivery_attempts += 1
        if not sessions:
            self.log(f".. 命令 {record.command_id}（{record.type}）目标 {record.device_id} 不在线，"
                     f"已入队等重连（第 {record.delivery_attempts} 次投递尝试）", verbose_only=True)
            return False
        frame = record.frame()
        sent = 0
        for session in sessions:
            try:
                session.send_json(frame, kind="command")
                sent += 1
            except WsClosed:
                continue
        with self.lock:
            if sent:
                record.delivered = True
                record.delivered_at = iso()
        self.log(f">> 下发命令 {record.command_id} type={record.type} "
                 f"dev={record.device_id} 送达={sent} 原因={reason}")
        return sent > 0

    # ---- 命令 ----

    def issue_command(self, device_id: str, type_: str, *,
                      payload: Optional[Dict[str, Any]] = None,
                      target_batch_id: Optional[str] = None,
                      expected_task_revision: Optional[int] = None,
                      command_id: Optional[str] = None,
                      ttl_ms: Optional[int] = None,
                      actor_id: str = "operator",
                      source: str = "http",
                      expires_at: Optional[str] = None) -> CommandRecord:
        if type_ not in COMMAND_WHITELIST:
            raise HttpError(422, "UNSUPPORTED_COMMAND",
                            f"命令 {type_} 不在白名单 {list(COMMAND_WHITELIST)} 内",
                            retryable=False, allowed=list(COMMAND_WHITELIST))
        rec = self.device(device_id)
        assert rec is not None
        if type_ == "prepare_update" and not (payload or {}).get("artifactId"):
            # 只给 prepare_update：payload 里没有产物就现场生成一个演示包。
            # 为什么放在这里而不是要求调用方先发布：模拟平台没有真平台的"发布流程"，
            # 联调时一条命令就该能走通"下载→校验→切换→回执"，少一步人工准备。
            artifact = self.latest_artifact(device_id) or self.make_demo_artifact(device_id)
            merged = self.prepare_update_payload(artifact)
            merged.update(payload or {})
            payload = merged
        with self.lock:
            # 幂等：同一个 commandId 重复提交返回已保存的那一条，不重复下发（PRD §8.1）
            if command_id and command_id in rec.commands:
                existing = rec.commands[command_id]
                existing.payload.setdefault("_replayed", True)
                return existing
            if type_ == "assign_task":
                # 派发任务时由平台推进任务版本，并把新版本放进 payload ——
                # 终端 _cmd_assign_task 会 adopt 这个 taskRevision（app.py 读 payload.taskRevision），
                # 两边因此对同一个 revision 达成一致；否则平台以为版本是 0、终端本地是 1，
                # 后续每条命令都会被终端以 revision_stale 拒掉（实测踩过）。
                new_revision = rec.task_revision + 1
                payload = {**(payload or {}), "taskRevision": new_revision}
                revision: Optional[int] = new_revision
                if target_batch_id is None:
                    target_batch_id = payload.get("batchId") or rec.current_batch_id
            elif expected_task_revision is not None:
                revision = int(expected_task_revision)
            elif rec.task_revision:
                # 平台派发过任务：默认按平台记录的版本校验，旧命令会被设备拒绝（H08）
                revision = rec.task_revision
            else:
                # 从没派发过任务：平台没有版本信息可比，就不带版本校验（null = 不校验）。
                # 终端 app_state.validate_command() 对 None 不做 revision 判定，两边语义一致。
                revision = None
            if target_batch_id is None:
                target_batch_id = rec.current_batch_id
            record = CommandRecord(
                command_id=command_id or new_id("cmd"),
                device_id=device_id,
                type_=type_,
                expected_task_revision=revision,
                target_batch_id=target_batch_id,
                # expiresAt 优先：联调"过期命令必须被设备拒绝"（PRD §13 H08）时，
                # 需要能直接下发一个已经过期的命令，而 ttlMs 只能表达"从现在起多久"。
                expires_at=expires_at or iso_plus_ms(ttl_ms if ttl_ms is not None else self.args.command_ttl_ms),
                payload=dict(payload or {}),
                actor_id=actor_id,
                seq=self.next_seq(rec),
                boot_id=rec.boot_id or "",
                session_id=self.args.session_id,
            )
            rec.commands[record.command_id] = record
            rec.command_order.append(record.command_id)
            rec.counters["commands"] += 1
        if type_ == "assign_task":
            with self.lock:
                rec.task_revision = revision or rec.task_revision
                rec.current_batch_id = record.payload.get("batchId") or rec.current_batch_id
            self.log(f"++ 已派发任务并推进任务版本：dev={device_id} taskRevision={rec.task_revision} "
                     f"batch={rec.current_batch_id}（后续命令默认按这个版本校验）")
        self.push_command(record, reason=source)
        return record

    def next_seq(self, rec: DeviceRecord) -> int:
        """给下行帧用的服务端侧序号（终端只用它丢旧帧）。"""
        rec.counters["serverSeq"] = rec.counters.get("serverSeq", 0) + 1
        return rec.counters["serverSeq"]

    def record_receipt(self, device_id: str, envelope: Dict[str, Any], state: str) -> None:
        payload = envelope.get("payload") or {}
        command_id = payload.get("commandId") or envelope.get("commandId")
        rec = self.device(device_id)
        assert rec is not None
        with self.lock:
            rec.last_receipt = {
                "state": state,
                "commandId": command_id,
                "receivedAt": iso(),
                "sentAt": envelope.get("sentAt"),
                "errorCode": payload.get("errorCode"),
                "reason": payload.get("reason"),
                "scope": payload.get("scope"),
                "payload": payload,
            }
            rec.counters["receipts"] += 1
            record = rec.commands.get(command_id) if command_id else None
            if record:
                record.receipt = rec.last_receipt
        if command_id and record is None:
            self.log(f"!! 收到回执 {state} 但 commandId={command_id} 不在已下发列表里（设备可能记错了）")
        elif command_id:
            self.log(f"<< 回执 {state} commandId={command_id} dev={device_id} "
                     f"errorCode={payload.get('errorCode') or '-'} reason={payload.get('reason') or '-'}")

    # ---- 上行事件 ----

    def on_ws_text(self, session: WsSession, text: str) -> None:
        text = text.strip()
        if text == "ping" or text == '{"kind":"ping"}':
            session.send_json({"kind": "pong", "platformTime": iso()})
            return
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            self.log(f"!! 设备 {session.device_id} 发来的文本帧不是 JSON：{exc}")
            return
        if not isinstance(envelope, dict):
            self.log(f"!! 设备 {session.device_id} 发来的 JSON 不是对象，已忽略")
            return
        kind = envelope.get("kind")
        if kind == "ping":
            session.send_json({"kind": "pong", "platformTime": iso()})
            return
        if kind == "hello":
            session.send_json({"kind": "hello.ack", "deviceId": session.device_id, "platformTime": iso()})
            return
        self.ingest_envelope(session.device_id, envelope, source="ws", session=session)

    def ingest_envelope(self, device_id: str, envelope: Dict[str, Any],
                        source: str = "ws", session: Optional[WsSession] = None) -> str:
        """处理一条上行信封。返回 "accepted" / "duplicated" / 具体拒收原因。"""
        etype = str(envelope.get("type") or "")
        if not etype:
            return "missing_type"
        message_id = envelope.get("messageId")
        version = str(envelope.get("schemaVersion") or SCHEMA_VERSION)
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            return f"unsupported_schema_version({version})"
        env_device = str(envelope.get("deviceId") or device_id)
        if env_device != device_id:
            return f"device_id_mismatch({env_device})"
        # 扩展类型的两种处理路径，由 --reject-extra-types 选择：
        #   默认收下 —— 正确的平台行为之一，也避免终端因为拿不到 ack 而无限重推；
        #   开关打开 —— 演"平台暂不支持这个类型"，此时必须**显式拒收**并标 retryable=false，
        #               终端收到后直接出队、不再重试。两条路径都要能演示。
        reject_extras = bool(getattr(self.args, "reject_extra_types", False))
        if etype not in KNOWN_EVENT_TYPES and (etype not in EXTRA_ACCEPTED_TYPES or reject_extras):
            if session:
                session.send_json({
                    "kind": "warning",
                    "code": "UNKNOWN_TYPE",
                    "message": f"平台不认识事件类型 {etype}，已拒收"
                               f"（retryable=false，终端不必重推；请核对 schemaVersion 与契约版本）",
                    "messageId": message_id,
                    "retryable": False,
                    "platformTime": iso(),
                })
            return f"unknown_type({etype})"
        if etype in EXTRA_ACCEPTED_TYPES and not reject_extras:
            # 不拒收，但要说出来：否则两端都以为对方知道这个类型
            self.log(f".. 收到契约未定义的事件类型 {etype}（已收下，避免终端无限重推）；"
                     f"建议把它补进 contracts.EventType 与平台白名单")
        with self.lock:
            if message_id and message_id in self.seen_messages:
                rec = self.device(device_id)
                if rec:
                    rec.counters["eventsDuplicated"] += 1
                return "duplicated"
            if message_id:
                self.seen_messages[message_id] = device_id
            rec = self.device(device_id)
            assert rec is not None
            rec.counters["events"] += 1
            rec.last_seen_at = iso()
            rec.last_seen_monotonic = time.monotonic()
            demo_session = envelope.get("demoSessionId")
            if demo_session and demo_session != self.args.session_id:
                if rec.last_demo_session_id != demo_session:
                    self.log(f".. 设备 {device_id} 的 demoSessionId={demo_session} 与平台会话 "
                             f"{self.args.session_id} 不同（排练轮次隔离，不覆盖平台记录）", verbose_only=True)
                rec.last_demo_session_id = demo_session

            if etype == "device.telemetry":
                rec.last_telemetry = envelope
                rec.counters["telemetry"] += 1
            elif etype == "device.capabilities":
                payload = envelope.get("payload") or {}
                rec.capabilities = dict(payload.get("capabilities") or rec.capabilities)
                rec.capability_reasons = dict(payload.get("capabilityReasons") or rec.capability_reasons)
                rec.last_event = envelope
            elif etype == "device.register" or etype == "device.hello":
                payload = envelope.get("payload") or {}
                rec.boot_id = str(envelope.get("bootId") or rec.boot_id or "")
                rec.app_version = payload.get("appVersion") or rec.app_version
                rec.last_event = envelope
            elif etype == "config.applied":
                payload = envelope.get("payload") or {}
                if payload.get("configVersion"):
                    rec.config_version = str(payload["configVersion"])
                rec.last_event = envelope
            elif etype.startswith("command."):
                self.record_receipt(device_id, envelope, etype.split(".", 1)[1])
            else:
                rec.last_event = envelope
        if self.args.verbose:
            self.log(f".. 事件 {etype} messageId={message_id} dev={device_id} 来自 {source}", verbose_only=True)
        return "accepted"

    # ---- 文件与上传 ----

    def find_completed_by_sha(self, sha256: str) -> Optional[FileRecord]:
        with self.lock:
            for record in self.files.values():
                if record.sha256 == sha256:
                    return record
        return None

    def create_upload(self, *, device_id: str, boot_id: str, name: str, size: int,
                      sha256: str, batch_id: Optional[str], role: str,
                      media_type: str, session_id: str) -> UploadRecord:
        with self.lock:
            # 续传：同一设备 + 同一业务键（名称/批次/角色/摘要）的未完成上传直接复用
            for record in self.uploads.values():
                if (record.device_id == device_id and record.name == name
                        and record.batch_id == batch_id and record.role == role
                        and record.sha256 == sha256 and not record.completed):
                    self.log(f".. 复用未完成上传 {record.upload_id}，"
                             f"receivedOffset={record.received_offset}/{record.size}", verbose_only=True)
                    return record
                if (record.device_id == device_id and record.name == name
                        and record.batch_id == batch_id and record.role == role
                        and record.sha256 != sha256 and not record.completed):
                    # 同名文件、同批次、摘要不同：这是"文件被重新生成后重传"，
                    # 不是错误。真实场景里终端重跑一次就会让 manifest/quality 的摘要变化，
                    # 若在这里回 409，终端会卡在最后几个文件上永远传不完（实测过）。
                    # 处理办法：作废旧的未完成上传，让新的接管。
                    self.log(f".. 同名文件 {name} 摘要变化，作废未完成的旧上传 {record.upload_id}"
                             f"（{record.sha256[:12]}… → {sha256[:12]}…）")
                    record.completed = True
                    record.file_id = None
                    record.received_offset = record.size
            existing_file = self.find_completed_by_sha(sha256)
            upload_id = new_id("up")
            record = UploadRecord(
                upload_id=upload_id, device_id=device_id, boot_id=boot_id, name=name,
                size=size, sha256=sha256, batch_id=batch_id, role=role,
                media_type=media_type or guess_media_type(name), session_id=session_id,
                part_path=self.uploads_dir / f"{upload_id}.part",
            )
            if existing_file is not None:
                # 平台 PRD §9.2：重复摘要可复用文件，但仍创建本次业务关联
                record.completed = True
                record.reused = True
                record.received_offset = size
                record.file_id = existing_file.file_id
                record.completed_at = iso()
                self.log(f".. 摘要已存在，复用文件 {existing_file.file_id}（{name} 不重复落盘）",
                         verbose_only=True)
            self.uploads[upload_id] = record
            return record

    def complete_upload(self, upload: UploadRecord) -> FileRecord:
        with self.lock:
            if upload.completed and upload.file_id:
                existing = self.files.get(upload.file_id)
                if existing:
                    return existing
            if upload.received_offset < upload.size:
                raise HttpError(409, "UPLOAD_INCOMPLETE",
                                f"只收到 {upload.received_offset}/{upload.size} 字节，不能 complete",
                                retryable=True, uploadId=upload.upload_id,
                                receivedOffset=upload.received_offset, size=upload.size)
            # --reject-checksum：真的把落盘字节改坏 1 个字节，再重算摘要。
            # 不做"假报 422"——注入的故障也必须是真故障，否则测不出终端的处理分支。
            # 每次 complete 都注入（不是一次性的）：要验证的是终端的失败与重传分支，不是运气。
            if self.reject_checksum and upload.part_path.exists() and upload.size > 0:
                with open(upload.part_path, "r+b") as handle:
                    handle.seek(max(0, upload.size // 2))
                    byte = handle.read(1)
                    handle.seek(max(0, upload.size // 2))
                    handle.write(bytes([byte[0] ^ 0xFF]))
                self.log("!! 注入故障 --reject-checksum：已在服务端翻转 1 字节，"
                         "接下来 complete 必然摘要不一致（想成功就重启模拟平台去掉这个开关）")
            actual = sha256_of_file(upload.part_path)
            expected = upload.sha256
            if actual != expected:
                raise HttpError(422, "CHECKSUM_MISMATCH",
                                "服务端重算摘要与声明不一致，整文件不可信，请重传",
                                retryable=True, uploadId=upload.upload_id,
                                expected=expected, actual=actual,
                                size=upload.size, receivedOffset=upload.received_offset)
            file_id = f"file-{uuid.uuid4()}"
            suffix = Path(upload.name).suffix
            target = self.files_dir / f"{file_id}{suffix}"
            os.replace(upload.part_path, target)
            record = FileRecord(
                file_id=file_id, name=upload.name, size=upload.size, sha256=actual,
                media_type=upload.media_type, path=target, device_id=upload.device_id,
                batch_id=upload.batch_id, role=upload.role, session_id=upload.session_id,
            )
            self.files[file_id] = record
            upload.file_id = file_id
            upload.completed = True
            upload.completed_at = iso()
            return record

    # ---- 批次 ----

    def submit_batch(self, body: Dict[str, Any], device_id: str) -> Dict[str, Any]:
        """提交采集 manifest 与 fileIds，回答"整批收到没有"。

        三件事，缺一件这个接口就没用：
          1. `fileIds` 两种写法都认：字符串 fileId，或终端实际发的对象
             `{fileId, role, path, sha256, bytes}`（见 woodpulse/app.py submit_batch）；
          2. "应当交付的清单"取三处的并集：显式 `expectedFiles`、manifest 自带的 `files[].path`、
             fileIds 里的 `path`；三者都没有才退回 PRD §9 的必需文件清单。
             比对时同时按"文件名"和"相对路径"匹配——终端上传时只传 basename，
             而清单里写的是 `images/frame_00000.png` 这种相对路径；
          3. 重算 `datasetHash`（与终端 storage.commit_manifest 同一算法：
             按路径字典序拼接 `path\\0sha256\\n`，不含 manifest.json），不一致就报出来。
             模拟平台只报告不拦截，方便联调；真平台应把它当作整批不可信。
        """
        batch_id = str(body.get("batchId") or "").strip()
        if not batch_id:
            raise HttpError(422, "NO_BATCH_ID", "缺少 batchId", field_errors=[{"field": "batchId"}])
        raw_file_ids = body.get("fileIds") or []
        if not isinstance(raw_file_ids, list):
            raise HttpError(422, "BAD_FILE_IDS", "fileIds 必须是数组")
        expected_entries = body.get("expectedFiles")
        if expected_entries is not None and not isinstance(expected_entries, list):
            raise HttpError(422, "BAD_EXPECTED_FILES", "expectedFiles 必须是数组")
        manifest_files = body.get("files")
        if manifest_files is not None and not isinstance(manifest_files, list):
            manifest_files = []

        # ---- 归一化"应当交付"的清单 ----
        declared: Dict[str, Dict[str, str]] = {}

        def declare(name: Any, role: Any, source: str) -> None:
            if not name:
                return
            text = str(name)
            if text not in declared:
                declared[text] = {"name": text, "role": str(role or "other"), "source": source}

        for entry in expected_entries or []:
            if isinstance(entry, dict):
                declare(entry.get("name"), entry.get("role"), "expectedFiles")
            elif entry:
                declare(entry, "other", "expectedFiles")
        for item in manifest_files or []:
            if isinstance(item, dict):
                declare(item.get("path"), item.get("role"), "manifest.files")
        for entry in raw_file_ids:
            if isinstance(entry, dict):
                declare(entry.get("path"), entry.get("role"), "fileIds")
        if not declared:
            for name in BATCH_REQUIRED_FILES:
                declare(name, "other", "prd-required(§9)")

        with self.lock:
            received: List[Dict[str, Any]] = []
            unknown: List[str] = []
            for entry in raw_file_ids:
                if isinstance(entry, dict):
                    file_id = entry.get("fileId") or entry.get("file_id") or entry.get("id")
                    declared_path = entry.get("path")
                else:
                    file_id, declared_path = entry, None
                record = self.files.get(str(file_id)) if file_id else None
                if record is None:
                    unknown.append(str(file_id) if file_id
                                   else f"(缺少 fileId：{declared_path or '未命名'}）")
                    continue
                brief = record.brief()
                brief["path"] = str(declared_path) if declared_path else record.name
                received.append(brief)

            # 匹配用集合：文件名 + 终端声明的相对路径，两边都放进去
            name_pool = set()
            roles = set()
            for item in received:
                name_pool.add(str(item["name"]))
                name_pool.add(os.path.basename(str(item["name"])))
                if item.get("path"):
                    name_pool.add(str(item["path"]))
                    name_pool.add(os.path.basename(str(item["path"])))
                roles.add(str(item["role"]))

            def satisfied(name: str) -> bool:
                if name.endswith("/"):          # 目录型条目：有对应角色的文件就算满足
                    return "image" in roles
                return name in name_pool or os.path.basename(name) in name_pool

            missing: List[Dict[str, Any]] = []
            for name, entry in declared.items():
                if not satisfied(name):
                    missing.append({"name": name, "role": entry["role"],
                                    "reason": "not_uploaded", "source": entry["source"]})

            # PRD §9 的归档必需清单只做提示：终端自己声明的清单才是"该交什么"的依据
            archive_missing = [name for name in BATCH_REQUIRED_FILES if not satisfied(name)]

            # datasetHash 重算（与终端 storage.commit_manifest 同算法）
            declared_hash = str(body.get("datasetHash") or "").strip()
            digest_entries = sorted(
                ((str(item.get("path") or item["name"]), str(item["sha256"])) for item in received
                 if str(item.get("path") or item["name"]) != "manifest.json"),
                key=lambda pair: pair[0],
            )
            actual_dataset_hash: Optional[str] = None
            if digest_entries:
                hasher = hashlib.sha256()
                for path, sha in digest_entries:
                    hasher.update(path.encode("utf-8"))
                    hasher.update(b"\0")
                    hasher.update(sha.encode("utf-8"))
                    hasher.update(b"\n")
                actual_dataset_hash = hasher.hexdigest()
            dataset_match: Optional[bool] = None
            if declared_hash:
                dataset_match = actual_dataset_hash == declared_hash
                if not dataset_match:
                    self.log(f"!! 批次 {batch_id} 的 datasetHash 不一致：声明 {declared_hash[:16]}… "
                             f"实算 {str(actual_dataset_hash)[:16]}…（缺件或内容被改过）")

            digest = sha256_hex(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            previous = self.batches.get(batch_id)
            replayed = False
            if previous:
                if previous["digest"] != digest:
                    raise HttpError(409, "BATCH_CONFLICT",
                                    f"批次 {batch_id} 已提交且内容不同，不允许原地改写",
                                    retryable=False, batchId=batch_id,
                                    submittedAt=previous["submittedAt"])
                replayed = True
                missing = previous["missing"]
                received = previous["receivedFiles"]
                unknown = previous["unknownFileIds"]
                archive_missing = previous["archiveMissing"]
                dataset_match = previous["datasetHashMatch"]
                actual_dataset_hash = previous["actualDatasetHash"]
            complete = not missing and not unknown
            result = {
                "batchId": batch_id,
                "deviceId": device_id,
                "accepted": True,
                "complete": complete,
                "missing": missing,
                "receivedFiles": received,
                "unknownFileIds": unknown,
                "receivedBytes": sum(item["size"] for item in received),
                "fileCount": len(received),
                "batchState": "uploaded" if complete else "partial",
                "datasetHashMatch": dataset_match,
                "datasetHash": declared_hash or None,
                "actualDatasetHash": actual_dataset_hash,
                "archiveCheck": {
                    "requiredFiles": list(BATCH_REQUIRED_FILES),
                    "missingRequired": archive_missing,
                    "note": "PRD §9 归档必需清单只作提示；complete 以设备自己声明的清单为准",
                },
                "submittedAt": previous["submittedAt"] if previous else iso(),
                "replayed": replayed,
                "note": ("整批接收完成，可进入平台侧分析" if complete else
                         "部分接收：未完成不得进入整批分析（PRD §9.2）"),
            }
            if not previous:
                self.batches[batch_id] = {
                    "digest": digest, "missing": missing, "receivedFiles": received,
                    "unknownFileIds": unknown, "submittedAt": result["submittedAt"],
                    "deviceId": device_id, "result": result,
                    "archiveMissing": archive_missing, "datasetHashMatch": dataset_match,
                    "actualDatasetHash": actual_dataset_hash,
                }
            return result

    # ---- 产物 ----

    def make_demo_artifact(self, device_id: str, version: Optional[str] = None) -> ArtifactRecord:
        """生成一个**真的**演示更新包（.demo.zip）。

        内容确定性生成：同一版本号每次生成内容一致，便于反复验证摘要。
        artifactKind=demo_nonflashable：明确它不是可烧录固件（平台 PRD §11.3）。
        """
        with self.lock:
            version = version or self.args.artifact_version
            previous = self.args.artifact_previous_version
            blob = b"".join(hashlib.sha256(f"woodpulse-demo-model-{version}-{i}".encode()).digest()
                            for i in range(512))          # 16KB 确定性权重占位
            contents = [
                {"name": "manifest.json", "role": "manifest"},
                {"name": "model_card.json", "role": "report"},
                {"name": "preprocess.json", "role": "config"},
                {"name": "receipt_schema.json", "role": "config"},
                {"name": "README.txt", "role": "report"},
                {"name": "weights/demo-model.bin", "role": "artifact"},
            ]
            manifest_inner = {
                "artifactId": None,          # 落盘后回填
                "artifactKind": ARTIFACT_KIND,
                "packageType": PACKAGE_TYPE,
                "demoOnly": True,
                "version": version,
                "previousVersion": previous,
                "target": {"deviceId": device_id, "deviceModel": "handheld-pi5",
                           "controller": "esp32-s3-demo", "flashable": False},
                "format": "demo-package-v1",
                "createdAt": iso(),
                "contents": contents,
                "notes": [
                    "这是演示包，不写入 ESP32 固件版本字段，不调用任何烧录工具",
                    "SHA-256 只证明完整性，不证明发布者身份",
                ],
            }
            artifact_id = new_id("art")
            manifest_inner["artifactId"] = artifact_id
            buffer_path = self.artifacts_dir / f"{artifact_id}.demo.zip"
            import io
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest_inner, ensure_ascii=False, indent=2))
                zf.writestr("model_card.json", json.dumps({
                    "version": version, "format": "response-sequence-v1",
                    "inputShape": [32, 64], "notes": "演示模型卡，指标来自样例回放，不是实机训练结果",
                }, ensure_ascii=False, indent=2))
                zf.writestr("preprocess.json", json.dumps({
                    "normalize": "per-frame-max", "windowFrames": 32, "configVersion": self.args.config_version,
                }, ensure_ascii=False, indent=2))
                zf.writestr("receipt_schema.json", json.dumps({
                    "required": ["commandId", "artifactId", "sha256", "applyResult", "versionReadBack"],
                    "applyResult": ["applied", "failed", "skipped"],
                }, ensure_ascii=False, indent=2))
                zf.writestr("README.txt",
                            "木脉智检演示更新包\n"
                            f"版本 {version}（上一版本 {previous}）\n"
                            "用途：验证终端下载、摘要校验、版本切换与回执上报。\n"
                            "不可烧录，不修改控制器固件版本。\n")
                zf.writestr("weights/demo-model.bin", blob)
            payload = stream.getvalue()
            buffer_path.write_bytes(payload)
            digest = sha256_hex(payload)
            file_id = f"file-{uuid.uuid4()}"
            file_record = FileRecord(
                file_id=file_id, name=f"{artifact_id}.demo.zip", size=len(payload),
                sha256=digest, media_type="application/zip", path=buffer_path,
                device_id=device_id, batch_id=None, role="artifact", session_id=self.args.session_id,
            )
            self.files[file_id] = file_record
            artifact = ArtifactRecord(
                artifact_id=artifact_id, file=file_record, version=version, previous_version=previous,
                target=manifest_inner["target"], contents=contents, notes=list(manifest_inner["notes"]),
            )
            self.artifacts[artifact_id] = artifact
        self.log(f"++ 生成演示更新包 {artifact_id} version={version} "
                 f"{len(payload)}B sha256={digest[:16]}… → files/{artifact_id}.demo.zip")
        return artifact

    def latest_artifact(self, device_id: str) -> Optional[ArtifactRecord]:
        with self.lock:
            candidates = [a for a in self.artifacts.values() if a.target.get("deviceId") in (device_id, None)]
            if not candidates:
                return None
            return sorted(candidates, key=lambda a: a.released_at)[-1]

    @staticmethod
    def prepare_update_payload(artifact: ArtifactRecord) -> Dict[str, Any]:
        """prepare_update 的 payload：终端拿它去下载、校验、切换演示版本、写回执。"""
        manifest = artifact.manifest()
        return {
            "artifactId": artifact.artifact_id,
            "artifactKind": ARTIFACT_KIND,
            "packageType": PACKAGE_TYPE,
            "demoOnly": True,
            "version": artifact.version,
            "previousVersion": artifact.previous_version,
            "sha256": artifact.file.sha256,
            "size": artifact.file.size,
            "downloadUrl": manifest["downloadUrl"],
            "fileId": artifact.file.file_id,
            "target": artifact.target,
            "note": "演示包：只下载、校验摘要、切换演示模型版本；不烧录固件（平台 PRD §11.3）",
        }

    # ---- 巡检线程 ----

    def housekeeping(self) -> None:
        """离线判定与过期命令：平台侧必须自己算，不能等设备说话。"""
        offline_after_ms = self.args.offline_after_ms
        while not self.shutdown_event.wait(2.0):
            now = utc_now()
            with self.lock:
                records = list(self.devices.values())
            for rec in records:
                if rec.last_seen_monotonic and not rec.sessions:
                    age_ms = (time.monotonic() - rec.last_seen_monotonic) * 1000.0
                    if age_ms > offline_after_ms and not rec.counters.get("_offline_logged"):
                        rec.counters["_offline_logged"] = 1
                        self.log(f"!! 设备 {rec.device_id} 已 {age_ms / 1000:.0f} 秒无任何上行，"
                                 f"超过 offlineAfterMs={offline_after_ms}，判为离线")
                elif rec.sessions and rec.counters.get("_offline_logged"):
                    rec.counters["_offline_logged"] = 0
                with self.lock:
                    pending = [c for c in rec._commands() if c.receipt is None and not c.expired]
                for command in pending:
                    expires = parse_iso(command.expires_at)
                    if expires and expires < now:
                        command.expired = True
                        self.log(f"!! 命令 {command.command_id}（{command.type}，设备 {command.device_id}）"
                                 f"已过期：expiresAt={command.expires_at}；设备此刻才拿到应回 "
                                 f"failed/command_expired")

    # ---- 停机 ----

    def attach_server(self, httpd: ThreadingHTTPServer) -> None:
        """控制台在主线程之外跑，quit 时只能靠 httpd.shutdown() 让 serve_forever 返回。"""
        self.httpd = httpd

    def request_stop(self) -> None:
        self.shutdown_event.set()
        httpd = getattr(self, "httpd", None)
        if httpd is not None:
            # shutdown() 必须从"非 serve_forever 线程"调用，控制台线程正好合适
            httpd.shutdown()

    def shutdown(self) -> None:
        self.shutdown_event.set()


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #

ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("POST", re.compile(r"^/api/devices/register$"), "h_register"),
    ("GET", re.compile(r"^/api/devices$"), "h_device_list"),
    ("POST", re.compile(r"^/api/devices/(?P<deviceId>[^/]+)/commands$"), "h_device_commands"),
    ("GET", re.compile(r"^/api/devices/(?P<deviceId>[^/]+)/state$"), "h_device_state"),
    ("POST", re.compile(r"^/api/devices/(?P<deviceId>[^/]+)/preview$"), "h_device_preview"),
    ("POST", re.compile(r"^/api/device-events/batch$"), "h_device_events_batch"),
    ("POST", re.compile(r"^/api/files/uploads$"), "h_upload_create"),
    ("PUT", re.compile(r"^/api/files/uploads/(?P<uploadId>[^/]+)/chunks$"), "h_upload_chunk"),
    ("POST", re.compile(r"^/api/files/uploads/(?P<uploadId>[^/]+)/complete$"), "h_upload_complete"),
    ("POST", re.compile(r"^/api/batches$"), "h_batch_submit"),
    ("GET", re.compile(r"^/api/artifacts/(?P<artifactId>[^/]+)/manifest$"), "h_artifact_manifest"),
    ("POST", re.compile(r"^/api/artifacts/(?P<artifactId>[^/]+)/receipts$"), "h_artifact_receipts"),
    ("GET", re.compile(r"^/api/files/(?P<fileId>[^/]+)/download$"), "h_file_download"),
    ("GET", re.compile(r"^/api/health$"), "h_health"),
]

WS_DEVICE_RE = re.compile(r"^/ws/devices/(?P<deviceId>[^/]+)$")

CORS_HEADERS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": ("authorization, content-type, x-device-token, x-device-id, "
                                     "x-chunk-sha256, x-frame-index, x-captured-at, x-frame-sha256"),
    "access-control-allow-methods": "GET, POST, PUT, OPTIONS",
    "access-control-expose-headers": "x-file-sha256, accept-ranges, content-range, x-upload-id",
}


class MockPlatformHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WoodPulse-MockPlatform/1.0"
    platform: PlatformCore = None  # type: ignore[assignment]  # 由 main() 注入

    # ---- 日志：BaseHTTPRequestHandler 自带的 stderr 输出走这里，统一格式 ----

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        self.platform.log(f".. http.server: {fmt % args}", verbose_only=True)

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003
        self.platform.log(f"!! http.server: {fmt % args}")

    # ---- 入口 ----

    def do_GET(self) -> None:      # noqa: N802
        self._handle()

    def do_POST(self) -> None:     # noqa: N802
        self._handle()

    def do_PUT(self) -> None:      # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:     # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        plat = self.platform
        parsed = urlparse(self.path)
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        started = time.perf_counter()
        device_hint = self.headers.get("X-Device-Id")
        length = int(self.headers.get("Content-Length") or 0)
        plat.log_http("<<", self.command, path, None, None, device_hint,
                      extra=f"len={length}")

        # OPTIONS 预检：浏览器/curl 调试用，模拟平台不拦
        if self.command == "OPTIONS":
            self.send_response(204)
            for key, value in CORS_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            plat.log_http(">>", self.command, path, 204, 0.0, device_hint)
            return

        # 设备 WebSocket 通道：握手成功后这条连接就交给 WsSession 了
        match = WS_DEVICE_RE.match(path)
        if match:
            try:
                self._serve_ws(match.group("deviceId"), query)
            except Exception:  # pragma: no cover - 兜底，不能让线程静默死掉
                plat.log("!! WS 处理异常：\n" + traceback.format_exc())
            return

        status: Optional[int] = None
        try:
            handler = self._resolve(self.command, path)
            if handler is None:
                if path == "/ws":
                    raise HttpError(404, "NO_ROUTE",
                                    "浏览器通道 /ws 由真平台服务提供；模拟平台只开 /ws/devices/{deviceId}",
                                    retryable=False)
                raise HttpError(404, "NO_ROUTE", f"没有这个接口：{self.command} {path}", retryable=False)
            if self.platform.args.latency_ms:
                time.sleep(self.platform.args.latency_ms / 1000.0)
            status = handler(match=match, query=query) if False else self._dispatch(handler, query)
        except HttpError as exc:
            status = exc.status
            self._send_json(exc.status, exc.body())
        except (BrokenPipeError, ConnectionResetError):
            status = 499  # 客户端先走了：注入断连时就是这条路径
        except Exception:  # pragma: no cover
            status = 500
            plat.log("!! 未处理异常：\n" + traceback.format_exc())
            self._send_json(500, {"code": "INTERNAL", "message": "模拟平台内部错误",
                                  "fieldErrors": [], "retryable": True})
        finally:
            target = getattr(self, "_log_device", None) or device_hint
            duration = (time.perf_counter() - started) * 1000.0
            if status is not None:
                plat.log_http(">>", self.command, path, status, duration, target)

    def _resolve(self, method: str, path: str) -> Optional[str]:
        for route_method, regex, handler_name in ROUTES:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                self._route_match = match
                return handler_name
        return None

    def _dispatch(self, handler_name: str, query: Dict[str, str]) -> int:
        match = self._route_match
        params = {key: match.group(key) for key in match.groupdict()}
        self._log_device = params.get("deviceId")
        return getattr(self, handler_name)(params, query)

    # ---- 响应工具 ----

    def _send_json(self, status: int, payload: Dict[str, Any],
                   extra_headers: Optional[Dict[str, str]] = None) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def _read_body_bytes(self, limit: int = HTTP_MAX_BODY) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > limit:
            raise HttpError(413, "BODY_TOO_LARGE", f"请求体 {length} 字节超过上限 {limit}",
                            retryable=False)
        data = self.rfile.read(length)
        if len(data) != length:
            raise HttpError(400, "SHORT_BODY", "请求体不完整（连接被提前关闭）", retryable=True)
        return data

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body_bytes()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(422, "BAD_JSON", f"请求体不是合法 JSON：{exc}", retryable=False)
        if not isinstance(parsed, dict):
            raise HttpError(422, "BAD_JSON", "请求体必须是 JSON 对象", retryable=False)
        return parsed

    def _device_token(self, body: Optional[Dict[str, Any]] = None,
                      query: Optional[Dict[str, str]] = None) -> Optional[str]:
        """设备令牌来源，按优先级：X-Device-Token 头 → ?deviceToken= → ?token= → body.deviceToken。

        为什么认这么多种写法：终端 `platform_client.HttpClient` 用头发令牌，
        `_ws_url()` 只能用查询串（WebSocket 没有自定义头），而 PRD §7.4 只说"使用设备token认证"，
        没规定放哪里。模拟平台全认，联调时不用猜；真平台应当**只认一种**并写进文档。
        """
        token = self.headers.get("X-Device-Token")
        if not token and query:
            token = query.get("deviceToken") or query.get("token")
        if not token and body:
            token = body.get("deviceToken")
        return token

    def _resolve_device_id(self, path_device_id: Optional[str] = None,
                           body: Optional[Dict[str, Any]] = None,
                           query: Optional[Dict[str, str]] = None,
                           events: Optional[List[Any]] = None) -> str:
        """确定"这条请求说的是哪台设备"。

        为什么需要推断：PRD §7.4 只在注册和 WS 路径里规定了 deviceId 的位置，
        而终端现有的 platform_client 在创建上传 / 提交批次 / 补传事件时请求体里**不带**
        deviceId（deviceId 在事件信封里，或只在 URL 上）。模拟平台因此按下面的顺序推断，
        而不是直接回 422 让联调卡住：
            1. 路径参数 / X-Device-Id 头 / 请求体 deviceId；
            2. 事件信封里的 deviceId；
            3. 设备专属令牌（rotatetoken 之后）反查；
            4. 平台当前只注册了一台设备时，就用它。
        多台设备同时注册又都不显式给 deviceId，才报 422（这时确实无法判定，不能猜）。
        """
        explicit = path_device_id or self.headers.get("X-Device-Id")
        if not explicit and body:
            explicit = body.get("deviceId")
        if explicit:
            return str(explicit)
        for item in events or []:
            if isinstance(item, dict) and item.get("deviceId"):
                return str(item["deviceId"])
        token = self._device_token(body, query)
        with self.platform.lock:
            owners = [rec.device_id for rec in self.platform.devices.values()
                      if token and token in rec.tokens]
            if len(owners) == 1:
                return owners[0]
            known = list(self.platform.devices)
        if len(known) == 1:
            return known[0]
        raise HttpError(422, "NO_DEVICE_ID",
                        "无法确定 deviceId：请在请求体带 deviceId（或使用 X-Device-Id 头）。"
                        f"当前已注册 {len(known)} 台设备，令牌不足以唯一识别",
                        field_errors=[{"field": "deviceId", "message": "必填"}])

    def _require_device(self, device_id: str, body: Optional[Dict[str, Any]] = None,
                        query: Optional[Dict[str, str]] = None) -> DeviceRecord:
        token = self._device_token(body, query)
        if not self.platform.token_ok(device_id, token):
            self.platform.note_auth_failure(device_id, token, self.path)
            raise HttpError(401, "UNAUTHORIZED",
                            "设备令牌无效：请检查 X-Device-Token 与平台登记的设备令牌是否一致",
                            retryable=False)
        self.platform.touch_device(device_id)
        return self.platform.device(device_id)  # type: ignore[return-value]

    # ---- WebSocket 握手 ----

    def _serve_ws(self, device_id: str, query: Dict[str, str]) -> None:
        plat = self.platform
        started = time.perf_counter()
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if "websocket" not in upgrade:
            raise HttpError(426, "UPGRADE_REQUIRED",
                            "该路径只接受 WebSocket 升级请求（Upgrade: websocket）", retryable=False)
        if self.headers.get("Sec-WebSocket-Version") != "13":
            raise HttpError(426, "BAD_WS_VERSION", "只支持 Sec-WebSocket-Version: 13", retryable=False)
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            raise HttpError(400, "NO_WS_KEY", "缺少 Sec-WebSocket-Key", retryable=False)

        # 认证：?deviceToken=（终端 platform_client 的写法）/ ?token= /
        #       X-Device-Token 头 / Sec-WebSocket-Protocol: woodpulse.v1, auth.<token>
        offered = [p.strip() for p in (self.headers.get("Sec-WebSocket-Protocol") or "").split(",") if p.strip()]
        token = query.get("deviceToken") or query.get("token") or self.headers.get("X-Device-Token")
        if not token:
            for proto in offered:
                if proto.startswith("auth."):
                    token = proto[5:]
                    break
        if not plat.token_ok(device_id, token):
            # 401 必须在 101 之前回，终端才能把"握手被拒"和"连上后被踢"分开处理
            plat.note_auth_failure(device_id, token, self.path)
            self._send_json(401, {"code": "UNAUTHORIZED", "message": "设备令牌无效",
                                  "fieldErrors": [], "retryable": False})
            return
        chosen = next((p for p in offered if p in ("woodpulse.v1", "woodpulse")), None)
        plat.log(f"++ WS 升级参数 dev={device_id} bootId={query.get('bootId', '-')} "
                 f"demoSessionId={query.get('demoSessionId', '-')} "
                 f"appVersion={query.get('appVersion', '-')} token={mask_token(token)}",
                 verbose_only=True)
        if query.get("demoSessionId") and query["demoSessionId"] != plat.args.session_id:
            plat.log(f".. 设备 {device_id} 连的是平台会话 {plat.args.session_id}，"
                     f"但查询串里写的是 {query['demoSessionId']}（排练轮次不一致，注意别串场）")

        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept}",
        ]
        if chosen:
            lines.append(f"Sec-WebSocket-Protocol: {chosen}")
        self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        self.wfile.flush()
        plat.log_http(">>", "GET", self.path, 101, (time.perf_counter() - started) * 1000.0,
                      device_id, extra=f"subprotocol={chosen or '-'}")

        # 握手完成后这条连接归 WS 会话管，别再让 http.server 去读下一个请求
        self.close_connection = True
        session = WsSession(plat, self.connection, device_id, self.path)
        plat.ws_attach(session)
        try:
            session.run()
        finally:
            plat.ws_detach(session)
            try:
                self.connection.close()
            except OSError:
                pass

    # ===================================================================== #
    # 接口实现
    # ===================================================================== #

    # ---- 设备注册（PRD §7.4） ----

    def h_register(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        body = self._read_json()
        device_id = str(body.get("deviceId") or self.headers.get("X-Device-Id") or "").strip()
        if not device_id:
            raise HttpError(422, "NO_DEVICE_ID", "缺少 deviceId",
                            field_errors=[{"field": "deviceId", "message": "必填"}])
        self._log_device = device_id
        boot_id = str(body.get("bootId") or "").strip()
        if not boot_id:
            raise HttpError(422, "NO_BOOT_ID", "缺少 bootId（一次进程启动一个，用于区分重启）",
                            field_errors=[{"field": "bootId", "message": "必填"}])
        version = str(body.get("schemaVersion") or SCHEMA_VERSION)
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise HttpError(422, "UNSUPPORTED_SCHEMA",
                            f"不支持的信封主版本 {version}，平台只认 {SCHEMA_VERSION}.x",
                            retryable=False, supported=SCHEMA_VERSION)

        token = self._device_token(body, query)
        if not self.platform.token_ok(device_id, token):
            self.platform.note_auth_failure(device_id, token, self.path)
            raise HttpError(401, "UNAUTHORIZED",
                            "设备令牌无效：请检查 X-Device-Token（平台登记值与终端配置必须一致）",
                            retryable=False)

        # 故障注入：让注册必须重试，用来验证终端的退避重连
        if self.platform.args.fail_register:
            self.platform.log("!! 注入故障 --fail-register：本次注册被拒（503，可重试）")
            raise HttpError(503, "REGISTER_REJECTED",
                            "平台正在维护，请退避后重试（--fail-register 注入）", retryable=True)

        rec = self.platform.device(device_id)
        assert rec is not None
        capabilities = body.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise HttpError(422, "BAD_CAPABILITIES", "capabilities 必须是对象")
        unknown_caps = {k: v for k, v in capabilities.items() if v not in CAPABILITY_VALUES}
        with self.platform.lock:
            new_boot = rec.boot_id != boot_id
            if new_boot:
                rec.boot_id = boot_id
                rec.registered_at = iso()
                rec.task_revision = 0
            rec.register_count += 1
            rec.app_version = body.get("appVersion") or rec.app_version
            rec.adapter_version = body.get("adapterVersion") or rec.adapter_version
            rec.model_version = body.get("modelVersion") or rec.model_version
            rec.operator_id = body.get("operatorId") or rec.operator_id
            rec.host = body.get("host") or rec.host
            rec.capabilities = dict(capabilities) or rec.capabilities
            rec.capability_reasons = dict(body.get("capabilityReasons") or rec.capability_reasons)
            rec.last_seen_at = iso()
            rec.last_seen_monotonic = time.monotonic()
        self.platform.log(f"++ 注册 {'新 bootId' if new_boot else '同一 bootId 重复注册'} "
                          f"dev={device_id} bootId={boot_id} app={rec.app_version} "
                          f"caps={json.dumps(rec.capabilities, ensure_ascii=False)}",
                          verbose_only=False)
        if unknown_caps:
            self.platform.log(f".. 能力取值不在契约枚举里：{unknown_caps}（原样保存，不猜）",
                              verbose_only=True)
        # 响应字段严格按 PRD §7.4 的清单，不多不少
        return self._send_json(200, {
            "deviceId": device_id,
            "registeredAt": rec.registered_at,
            "heartbeatIntervalMs": self.platform.args.heartbeat_interval_ms,
            "offlineAfterMs": self.platform.args.offline_after_ms,
            "configVersion": self.platform.args.config_version,
            "demoSessionId": self.platform.args.session_id,
            "platformTime": iso(),
        })

    # ---- 调试用设备列表（模拟平台扩展，真平台不要求） ----

    def h_device_list(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        with self.platform.lock:
            records = list(self.platform.devices.values())
        return self._send_json(200, {
            "devices": [
                {
                    "deviceId": rec.device_id,
                    "bootId": rec.boot_id,
                    "connectionState": rec.connection_state(self.platform.args.offline_after_ms),
                    "lastSeenAt": rec.last_seen_at,
                    "capabilities": rec.capabilities,
                    "pendingCommands": sum(1 for c in rec._commands() if c.receipt is None),
                }
                for rec in records
            ],
            "note": "模拟平台调试接口（真平台不要求实现）",
            "time": iso(),
        })

    # ---- 设备状态（调试用） ----

    def h_device_state(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        rec = self.platform.device(params["deviceId"], create=False)
        if rec is None:
            raise HttpError(404, "NO_DEVICE", f"设备 {params['deviceId']} 未注册", retryable=False)
        return self._send_json(200, rec.state(self.platform.args.offline_after_ms))

    # ---- 平台下发命令（PRD §7.4） ----

    def h_device_commands(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        device_id = params["deviceId"]
        body = self._read_json()
        # 操作员身份：真平台必须走角色鉴权（auth.mjs 的 Bearer 令牌）。
        # 模拟平台两种都收：设备令牌（方便终端/冒烟脚本）或任意 Bearer（方便人工联调）。
        bearer = self.headers.get("Authorization") or ""
        actor_id = "operator"
        if bearer.lower().startswith("bearer "):
            actor_id = f"bearer:{bearer[7:][:8]}"
        else:
            token = self._device_token(body, query)
            if not self.platform.token_ok(device_id, token):
                self.platform.note_auth_failure(device_id, token, self.path)
                raise HttpError(401, "UNAUTHORIZED",
                                "缺少操作员令牌或设备令牌无效", retryable=False)
            actor_id = "device-token"
        type_ = str(body.get("type") or body.get("action") or "").strip()
        if not type_:
            raise HttpError(422, "NO_COMMAND_TYPE",
                            f"缺少 type；白名单：{list(COMMAND_WHITELIST)}",
                            field_errors=[{"field": "type", "message": "必填"}])
        expected_revision = body.get("expectedTaskRevision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError):
                raise HttpError(422, "BAD_REVISION", "expectedTaskRevision 必须是整数")
        ttl_ms = body.get("ttlMs")
        record = self.platform.issue_command(
            device_id, type_,
            payload=body.get("payload") or {},
            target_batch_id=body.get("targetBatchId"),
            expected_task_revision=expected_revision,
            command_id=body.get("commandId"),
            ttl_ms=int(ttl_ms) if ttl_ms is not None else None,
            actor_id=actor_id,
            source="http",
            expires_at=body.get("expiresAt"),
        )
        # 注意：返回 202 而不是 200——命令只是"已受理"，真正执行要看回执（PRD §8.1）
        return self._send_json(202, {
            "commandId": record.command_id,
            "type": record.type,
            "deviceId": record.device_id,
            "expectedTaskRevision": record.expected_task_revision,
            "targetBatchId": record.target_batch_id,
            "expiresAt": record.expires_at,
            "issuedAt": record.issued_at,
            "delivered": record.delivered,
            "connectionState": self.platform.device(device_id).connection_state(  # type: ignore[union-attr]
                self.platform.args.offline_after_ms),
            "replayed": bool(record.payload.get("_replayed")),
            "note": "命令已入队；平台不因“发出”就标成功，请等 accepted / executed / failed 回执",
        })

    # ---- 断线补传（PRD §7.4） ----

    def h_device_events_batch(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        body = self._read_json()
        events = body.get("events")
        if not isinstance(events, list):
            raise HttpError(422, "NO_EVENTS", "events 必须是数组")
        if len(events) > 500:
            raise HttpError(413, "TOO_MANY_EVENTS", "单次最多补传 500 条事件", retryable=True)
        device_id = self._resolve_device_id(None, body, query, events=events)
        self._log_device = device_id
        self._require_device(device_id, body, query)
        accepted: List[str] = []
        duplicated: List[str] = []
        rejected: List[Dict[str, Any]] = []
        for item in events:
            if not isinstance(item, dict):
                # 连对象都不是：没有 messageId 可回，只能给个明确的永久拒收说明
                rejected.append({"messageId": None, "reason": "not_an_object", "code": "not_an_object", "retryable": False})
                continue
            message_id = item.get("messageId")
            if not message_id:
                rejected.append({"messageId": None, "reason": "missing_messageId", "code": "missing_message_id", "retryable": False})
                continue
            outcome = self.platform.ingest_envelope(device_id, item, source="batch")
            if outcome == "accepted":
                accepted.append(str(message_id))
            elif outcome == "duplicated":
                duplicated.append(str(message_id))
            else:
                # 拒收必须带 messageId 与 retryable：终端据此决定"出队"还是"退避重试"。
                # 只给 reason 的话终端只能猜，猜错的两头都难受（丢数据 或 无限重推）。
                rejected.append({
                    "messageId": str(message_id),
                    "reason": outcome,
                    "code": reject_reason_code(outcome),
                    "retryable": reject_is_retryable(outcome),
                })
        # 契约自检：每个收到的 messageId 必须恰好落进 accepted / duplicated / rejected 之一。
        # 这就是"平台不得静默丢弃"的可执行版本 —— 少一个就意味着终端会无限重推。
        accounted = set(accepted) | set(duplicated) | {
            str(entry["messageId"]) for entry in rejected if entry.get("messageId")
        }
        submitted = {
            str(item.get("messageId")) for item in events if isinstance(item, dict) and item.get("messageId")
        }
        unaccounted = submitted - accounted
        if unaccounted:
            self.platform.log(
                f"!! 契约违规：{len(unaccounted)} 个 messageId 既未接受也未拒收："
                f"{'、'.join(sorted(unaccounted)[:3])}（终端会无限重推）"
            )
        permanent = sum(1 for entry in rejected if entry.get("retryable") is False)
        self.platform.log(
            f"<< 补传事件 dev={device_id} 共 {len(events)} 条："
            f"入库 {len(accepted)} / 去重 {len(duplicated)} / 拒收 {len(rejected)}"
            + (f"（其中永久拒收 {permanent}）" if rejected else "")
        )
        # accepted / duplicated 用 messageId 数组：终端 platform_client._post_events_batch()
        # 直接把它们当成"可以 ack 掉的事件 ID 列表"用。同时给计数，便于人读与断言。
        # PRD §7.4 没写这两个字段是计数还是清单，这里按终端实现取清单，另附 *Count。
        return self._send_json(200, {
            "deviceId": device_id,
            "accepted": accepted,
            "duplicated": duplicated,
            "acceptedCount": len(accepted),
            "duplicatedCount": len(duplicated),
            "rejected": rejected,
            "rejectedCount": len(rejected),
            "unaccounted": sorted(unaccounted),
            "receivedAt": iso(),
            "note": "按 messageId 去重；重复提交同一批只会命中 duplicated，不重复入库。"
                    "rejected 每条带 retryable：false=永久拒收（终端出队不再重试），"
                    "true=临时失败（终端保留并退避重试）。",
        })

    # ---- 分片上传：创建（PRD §7.4） ----

    def h_upload_create(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        body = self._read_json()
        device_id = self._resolve_device_id(None, body, query)
        self._log_device = device_id
        self._require_device(device_id, body, query)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HttpError(422, "NO_NAME", "缺少 name（文件名，用于角色与媒体类型推断）")
        try:
            size = int(body.get("size"))
        except (TypeError, ValueError):
            raise HttpError(422, "NO_SIZE", "缺少 size（整文件字节数）")
        if size < 0:
            raise HttpError(422, "BAD_SIZE", "size 不能为负")
        sha256 = str(body.get("sha256") or "").strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise HttpError(422, "BAD_SHA256", "sha256 必须是 64 位十六进制小写字符串")
        role = str(body.get("role") or "other")
        if role not in FILE_ROLES:
            role = "other"
        record = self.platform.create_upload(
            device_id=device_id,
            boot_id=str(body.get("bootId") or ""),
            name=name, size=size, sha256=sha256,
            batch_id=body.get("batchId"), role=role,
            media_type=str(body.get("mediaType") or ""),
            session_id=str(body.get("demoSessionId") or self.platform.args.session_id),
        )
        status = 200 if (record.received_offset > 0 or record.completed) else 201
        self.platform.log(f"<< 创建上传 {record.upload_id} name={name} size={size} role={role} "
                          f"batch={record.batch_id} 已完成={record.completed}", verbose_only=True)
        return self._send_json(status, {
            "uploadId": record.upload_id,
            "deviceId": record.device_id,
            "name": record.name,
            "size": record.size,
            "sha256": record.sha256,
            "role": record.role,
            "batchId": record.batch_id,
            "mediaType": record.media_type,
            "receivedOffset": record.received_offset,
            "completed": record.completed,
            "reused": record.reused,
            "fileId": record.file_id,
            "chunkSizeHint": self.platform.args.chunk_hint,
            "expiresAt": record.expires_at,
            "platformTime": iso(),
        }, extra_headers={"X-Upload-Id": record.upload_id})

    # ---- 分片上传：写分片（PRD §7.4） ----

    def h_upload_chunk(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        upload_id = params["uploadId"]
        upload = self.platform.uploads.get(upload_id)
        if upload is None:
            raise HttpError(404, "NO_UPLOAD", f"上传 {upload_id} 不存在或已过期")
        self._log_device = upload.device_id
        self._require_device(upload.device_id, None, query)
        # 偏移量两种写法都认：?offset= 或 X-Chunk-Offset 头。
        # 终端现有 platform_client.put_chunk() 用头发；PRD §7.4 只写了"校验偏移"没说放哪，
        # 真平台应当二选一并写进接口文档（见 docs/平台接口说明.md 的契约歧义一节）。
        raw_offset = query.get("offset")
        if raw_offset is None:
            raw_offset = self.headers.get("X-Chunk-Offset")
        if raw_offset is None:
            raise HttpError(422, "NO_OFFSET",
                            "缺少偏移量：请用 ?offset= 查询参数或 X-Chunk-Offset 头")
        try:
            offset = int(raw_offset)
        except ValueError:
            raise HttpError(422, "BAD_OFFSET", "offset 必须是整数")
        total = int(self.headers.get("Content-Length") or 0)
        if total <= 0:
            raise HttpError(422, "EMPTY_CHUNK", "分片长度为 0；空分片不推进 offset")
        declared_chunk_sha = (self.headers.get("X-Chunk-Sha256") or "").strip().lower()

        with self.platform.lock:
            if upload.completed:
                return self._send_json(200, {
                    "uploadId": upload_id, "receivedOffset": upload.received_offset,
                    "size": upload.size, "duplicated": True, "completed": True,
                    "fileId": upload.file_id,
                    "note": "该上传已完成，重复分片按幂等处理，不再落盘",
                })
            expected_offset = upload.received_offset
        if offset != expected_offset:
            # 重复片（offset 比已接收位置靠前）：比对磁盘上的字节，一致就幂等返回
            if offset < expected_offset:
                if self._chunk_matches_disk(upload, offset, total):
                    with self.platform.lock:
                        upload.duplicate_chunks += 1
                    self.platform.log(f".. 重复分片 offset={offset} len={total} 内容一致 → 幂等返回",
                                      verbose_only=True)
                    return self._send_json(200, {
                        "uploadId": upload_id, "receivedOffset": upload.received_offset,
                        "size": upload.size, "duplicated": True, "completed": False,
                        "note": "同一分片重复提交，字节一致，不重复写入",
                    })
                raise HttpError(409, "CHUNK_CONFLICT",
                                f"offset={offset} 处已有不同内容，拒绝覆盖", retryable=False,
                                expected=upload.received_offset, actual=offset)
            raise HttpError(409, "OFFSET_MISMATCH",
                            f"偏移不连续：期望 offset={expected_offset}，收到 {offset}",
                            retryable=True, expected=expected_offset, actual=offset,
                            receivedOffset=expected_offset, size=upload.size)
        if offset + total > upload.size:
            raise HttpError(422, "CHUNK_OVERRUN",
                            f"分片越界：offset {offset} + {total} > size {upload.size}",
                            retryable=False, size=upload.size, receivedOffset=offset)

        # 流式收：边收边写盘边算摘要，不把分片整块读进内存
        digest = hashlib.sha256()
        written = 0
        drop_at = self.platform.args.drop_after_bytes
        mode = "r+b" if upload.part_path.exists() else "wb"
        with open(upload.part_path, mode) as handle:
            handle.seek(offset)
            remaining = total
            while remaining > 0:
                block = self.rfile.read(min(65536, remaining))
                if not block:
                    raise HttpError(400, "SHORT_BODY", "分片未收完连接就断了", retryable=True)
                remaining -= len(block)
                # --drop-after-bytes：第 N 字节之后直接掐连接，制造"传一半断网"
                if drop_at is not None and offset + written + len(block) > drop_at:
                    allowed = max(0, drop_at - (offset + written))
                    if allowed:
                        handle.write(block[:allowed])
                        digest.update(block[:allowed])
                        written += allowed
                    handle.flush()
                    os.fsync(handle.fileno())
                    with self.platform.lock:
                        upload.received_offset = offset + written
                    self.platform.log(
                        f"!! 注入故障 --drop-after-bytes={drop_at}：已接收 offset="
                        f"{upload.received_offset}/{upload.size} 后强行断开连接（不给响应）")
                    self._abort_connection()
                    return 499
                handle.write(block)
                digest.update(block)
                written += len(block)
            handle.flush()
            os.fsync(handle.fileno())

        actual_chunk_sha = digest.hexdigest()
        if declared_chunk_sha and declared_chunk_sha != actual_chunk_sha:
            raise HttpError(422, "CHUNK_CHECKSUM_MISMATCH",
                            "分片摘要与 X-Chunk-Sha256 不一致，请重发该分片",
                            retryable=True, expected=declared_chunk_sha, actual=actual_chunk_sha,
                            offset=offset, length=total)
        with self.platform.lock:
            upload.received_offset = offset + written
            upload.chunk_count += 1
            received_offset = upload.received_offset
        self.platform.log(f"<< 分片 {upload_id} offset={offset} len={written} "
                          f"→ receivedOffset={received_offset}/{upload.size}", verbose_only=True)
        return self._send_json(200, {
            "uploadId": upload_id,
            "receivedOffset": received_offset,
            "size": upload.size,
            "duplicated": False,
            "completed": False,
            "chunkSha256": actual_chunk_sha,
            "platformTime": iso(),
        })

    def _chunk_matches_disk(self, upload: UploadRecord, offset: int, length: int) -> bool:
        """重复分片幂等的前提：磁盘上那一段字节要和这次发来的完全一致。"""
        if offset + length > upload.size:
            return False
        try:
            with open(upload.part_path, "rb") as handle:
                handle.seek(offset)
                on_disk = handle.read(length)
        except OSError:
            return False
        if len(on_disk) != length:
            return False
        incoming = self.rfile.read(length)
        if len(incoming) != length:
            return False
        return on_disk == incoming

    def _abort_connection(self) -> None:
        """不发任何响应，直接把 TCP 掐掉：模拟真实网络里"传一半断了"。"""
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    # ---- 分片上传：完成（PRD §7.4） ----

    def h_upload_complete(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        upload_id = params["uploadId"]
        upload = self.platform.uploads.get(upload_id)
        if upload is None:
            raise HttpError(404, "NO_UPLOAD", f"上传 {upload_id} 不存在或已过期")
        self._log_device = upload.device_id
        body = self._read_json()
        self._require_device(upload.device_id, body, query)
        declared = str(body.get("sha256") or upload.sha256).strip().lower()
        replayed = upload.completed
        file_record = self.platform.complete_upload(upload)
        self.platform.log(f"++ 上传完成 {upload_id} → {file_record.file_id} "
                          f"{file_record.size}B sha256={file_record.sha256[:16]}… "
                          f"（重算一致，{'幂等回放' if replayed else '首次确认'}）")
        return self._send_json(200, {
            "uploadId": upload_id,
            "fileId": file_record.file_id,
            "name": file_record.name,
            "bytesConfirmed": file_record.size,
            "sha256": file_record.sha256,
            "declaredSha256": declared,
            "mediaType": file_record.media_type,
            "batchId": file_record.batch_id,
            "role": file_record.role,
            "completedAt": upload.completed_at,
            "replayed": replayed,
            "platformTime": iso(),
        })

    # ---- 批次提交（PRD §7.4） ----

    def h_batch_submit(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        body = self._read_json()
        device_id = self._resolve_device_id(None, body, query)
        self._log_device = device_id
        self._require_device(device_id, body, query)
        result = self.platform.submit_batch(body, device_id)
        self.platform.log(f"<< 提交批次 {result['batchId']} 文件 {result['fileCount']} 个 "
                          f"{result['receivedBytes']}B → complete={result['complete']} "
                          f"缺 {[m['name'] for m in result['missing']]}")
        return self._send_json(200, result)

    # ---- 产物清单（PRD §7.4） ----

    def h_artifact_manifest(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        artifact = self.platform.artifacts.get(params["artifactId"])
        if artifact is None:
            raise HttpError(404, "NO_ARTIFACT", f"产物 {params['artifactId']} 不存在")
        self._require_device(artifact.target.get("deviceId") or "-", None, query)
        return self._send_json(200, artifact.manifest())

    # ---- 产物回执（PRD §7.4） ----

    def h_artifact_receipts(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        artifact = self.platform.artifacts.get(params["artifactId"])
        if artifact is None:
            raise HttpError(404, "NO_ARTIFACT", f"产物 {params['artifactId']} 不存在")
        body = self._read_json()
        device_id = self._resolve_device_id(None, body, query)
        self._log_device = device_id
        self._require_device(device_id, body, query)
        command_id = str(body.get("commandId") or "").strip()
        if not command_id:
            raise HttpError(422, "NO_COMMAND_ID",
                            "缺少 commandId：回执必须对应一条具体命令，否则无法幂等")
        with self.platform.lock:
            existing_id = self.platform.receipt_by_command.get(command_id)
            if existing_id:
                # 同 commandId 幂等：返回已保存结果，不重复入库（PRD §8.1）
                stored = dict(self.platform.receipts[existing_id])
                stored["replayed"] = True
                return self._send_json(200, stored)
        declared = str(body.get("sha256") or "").strip().lower()
        if declared and declared != artifact.file.sha256:
            raise HttpError(409, "ARTIFACT_CHECKSUM_MISMATCH",
                            "回执里的摘要与平台登记的产物摘要不一致，请重新下载",
                            retryable=True, expected=artifact.file.sha256, actual=declared)
        apply_result = str(body.get("applyResult") or "").strip()
        if apply_result not in ("applied", "failed", "skipped"):
            raise HttpError(422, "BAD_APPLY_RESULT",
                            "applyResult 必须是 applied / failed / skipped")
        receipt_id = new_id("rcpt")
        record = {
            "receiptId": receipt_id,
            "deviceId": device_id,
            "artifactId": artifact.artifact_id,
            "commandId": command_id,
            "sha256": declared or None,
            "downloadVerified": bool(body.get("downloadVerified", bool(declared))),
            "applyResult": apply_result,
            "versionReadBack": body.get("versionReadBack"),
            "previousVersion": body.get("previousVersion") or artifact.previous_version,
            "errorCode": body.get("errorCode"),
            "reason": body.get("reason"),
            "steps": body.get("steps") or [],
            "startedAt": body.get("startedAt"),
            "finishedAt": body.get("finishedAt"),
            "acceptedAt": iso(),
            "replayed": False,
        }
        with self.platform.lock:
            self.platform.receipts[receipt_id] = record
            self.platform.receipt_by_command[command_id] = receipt_id
            rec = self.platform.device(device_id)
            if rec and command_id in rec.commands:
                command = rec.commands[command_id]
                command.receipt = {
                    "state": "executed" if apply_result == "applied" else "failed",
                    "commandId": command_id,
                    "receivedAt": iso(),
                    "errorCode": record["errorCode"],
                    "reason": record["reason"],
                    "scope": "artifact_apply",
                    "payload": record,
                }
        self.platform.log(f"++ 收到产物回执 {receipt_id} artifact={artifact.artifact_id} "
                          f"applyResult={apply_result} versionReadBack={record['versionReadBack']} "
                          f"dev={device_id}")
        return self._send_json(201, record)

    # ---- 下载真实字节（PRD §7.4 / 平台 PRD §14.2） ----

    def h_file_download(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        file_id = params["fileId"]
        record = self.platform.files.get(file_id)
        if record is None:
            raise HttpError(404, "NOT_FOUND", "文件不存在")
        self._require_device(record.device_id, None, query)
        if not record.path.exists():
            raise HttpError(410, "GONE", "文件已不在磁盘上")
        size = record.path.stat().st_size
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
            if not match:
                raise HttpError(416, "BAD_RANGE", "只支持单段 bytes=start-end", retryable=False,
                                **{"Content-Range": f"bytes */{size}"})
            raw_start, raw_end = match.group(1), match.group(2)
            if raw_start == "" and raw_end == "":
                raise HttpError(416, "BAD_RANGE", "Range 两端不能都为空", retryable=False)
            if raw_start == "":
                start = max(0, size - int(raw_end))
                end = size - 1
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end else size - 1
            if start >= size or start > end:
                raise HttpError(416, "RANGE_NOT_SATISFIABLE",
                                f"请求范围 {range_header} 超出文件大小 {size}",
                                retryable=False, size=size)
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", record.media_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-File-Sha256", record.sha256)
        self.send_header("X-File-Id", record.file_id)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{record.file_id}{record.path.suffix}"')
        for key, value in CORS_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        with open(record.path, "rb") as handle:
            handle.seek(start)
            left = length
            while left > 0:
                block = handle.read(min(65536, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)
        with self.platform.lock:
            record.downloads += 1
        self.platform.log(f"<< 下载 {record.file_id} {record.name} 范围 {start}-{end}/{size} "
                          f"sha256={record.sha256[:16]}…")
        self._log_device = record.device_id
        return status

    # ---- 低帧率预览图（PRD §7.4） ----

    def h_device_preview(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        device_id = params["deviceId"]
        self._require_device(device_id, None, query)
        frame_index_raw = self.headers.get("X-Frame-Index")
        if frame_index_raw is None:
            raise HttpError(422, "NO_FRAME_INDEX",
                            "缺少 X-Frame-Index 头（预览图必须能对上是第几帧）")
        try:
            frame_index = int(frame_index_raw)
        except ValueError:
            raise HttpError(422, "BAD_FRAME_INDEX", "X-Frame-Index 必须是整数")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise HttpError(422, "EMPTY_FRAME", "预览图长度为 0")
        if length > 2 * 1024 * 1024:
            raise HttpError(413, "FRAME_TOO_LARGE", "预览图超过 2MiB；预览应当下采样（PRD §4.3）")
        data = self._read_body_bytes(limit=2 * 1024 * 1024 + 1)
        if not data.startswith(b"\xff\xd8"):
            raise HttpError(422, "NOT_JPEG", "预览图必须是 JPEG（缺少 SOI 标记 FF D8）")
        digest = sha256_hex(data)
        declared = (self.headers.get("X-Frame-Sha256") or "").strip().lower()
        if declared and declared != digest:
            raise HttpError(422, "FRAME_CHECKSUM_MISMATCH", "预览图摘要不一致",
                            retryable=True, expected=declared, actual=digest)
        batch_id = query.get("batchId")
        captured_at = self.headers.get("X-Captured-At") or iso()
        with self.platform.lock:
            rec = self.platform.device(device_id)
            assert rec is not None
            target_dir = self.platform.previews_dir / device_id
            target_dir.mkdir(parents=True, exist_ok=True)
            frame_path = target_dir / f"{frame_index:08d}-{int(time.time() * 1000)}.jpg"
            frame_path.write_bytes(data)
            entry = {
                "frameIndex": frame_index,
                "batchId": batch_id,
                "bytes": len(data),
                "sha256": digest,
                "capturedAt": captured_at,
                "receivedAt": iso(),
                "path": str(frame_path.relative_to(self.platform.data_dir)),
            }
            rec.previews.append(entry)
            dropped = []
            while len(rec.previews) > self.platform.args.preview_keep:
                old = rec.previews.popleft()
                dropped.append(old["frameIndex"])
                try:
                    (self.platform.data_dir / old["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
        if dropped:
            self.platform.log(f".. 预览图只保留最近 {self.platform.args.preview_keep} 张，"
                              f"已丢弃帧 {dropped}（不作为原始图像归档，PRD §7.4）")
        self.platform.log(f"<< 预览图 dev={device_id} frameIndex={frame_index} "
                          f"{len(data)}B batch={batch_id or '-'}")
        return self._send_json(202, {
            "deviceId": device_id,
            "frameIndex": frame_index,
            "bytes": len(data),
            "sha256": digest,
            "batchId": batch_id,
            "capturedAt": captured_at,
            "receivedAt": entry["receivedAt"],
            "keptFrames": len(rec.previews),
            "retentionLimit": self.platform.args.preview_keep,
            "note": "预览图只用于现场画面查看，不作为完整原始图像归档",
        })

    # ---- 健康检查（与平台 /api/health 同路径） ----

    def h_health(self, params: Dict[str, str], query: Dict[str, str]) -> int:
        with self.platform.lock:
            records = list(self.platform.devices.values())
            states = [rec.connection_state(self.platform.args.offline_after_ms) for rec in records]
            detail = [
                {
                    "deviceId": rec.device_id,
                    "connectionState": state,
                    "bootId": rec.boot_id,
                    "lastSeenAt": rec.last_seen_at,
                    "wsClients": len(rec.sessions),
                    "capabilities": rec.capabilities,
                    "pendingCommands": sum(1 for c in rec._commands() if c.receipt is None),
                    "previews": len(rec.previews),
                }
                for rec, state in zip(records, states)
            ]
            return self._send_json(200, {
                "ok": True,
                "service": "mock-platform",
                "devices": {
                    "total": len(records),
                    "online": states.count("online"),
                    "degraded": states.count("degraded"),
                    "offline": states.count("offline"),
                    "ids": [rec.device_id for rec in records],
                    "detail": detail,
                },
                "time": iso(),
                "uptimeSeconds": round(time.monotonic() - self.platform.started, 1),
                "dataDir": str(self.platform.data_dir),
                "faults": self.platform.fault_summary(),
                "note": "元数据在内存，进程重启即清空；--data-dir 只保留已接收的字节与生成的更新包",
            })


# --------------------------------------------------------------------------- #
# 故障注入摘要（写进横幅，避免"忘了自己开了注入"）
# --------------------------------------------------------------------------- #

def _fault_summary(self: PlatformCore) -> List[str]:
    faults: List[str] = []
    if self.args.fail_register:
        faults.append("fail-register：注册一律 503（验证终端退避重试）")
    if self.args.reject_checksum:
        faults.append("reject-checksum：complete 前翻转 1 字节（验证摘要不一致分支）")
    if self.args.drop_after_bytes is not None:
        faults.append(f"drop-after-bytes={self.args.drop_after_bytes}：达到该字节数后掐断连接")
    if self.args.latency_ms:
        faults.append(f"latency-ms={self.args.latency_ms}：每个请求前多睡这么久")
    return faults


PlatformCore.fault_summary = _fault_summary  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 调试控制台（从 stdin 读一行手动下发命令）
# --------------------------------------------------------------------------- #

HELP_TEXT = """可用命令（人工联调用，不影响正常接口）：
  help                                        显示本帮助
  devices                                     列出已注册设备与在线状态
  telemetry <deviceId>                        打印该设备最后一条遥测
  state <deviceId>                            打印该设备状态摘要（命令与回执）
  uploads <deviceId>                          列出该设备的上传与落盘情况
  batches                                     列出已提交批次
  cmd <deviceId> pause_capture [batchId]       下发暂停采集
  cmd <deviceId> apply_config CFG-02           下发环境配置
  cmd <deviceId> prepare_update                下发准备更新（无产物时自动生成一个）
  cmd <deviceId> assign_task SH-2026-0901 Z04 Z04-lower   下发派发任务
  cmd <deviceId> request_upload [batchId]      下发请求上传
  cmd <deviceId> query_status                  下发查询状态
  expire <deviceId>                           把已下发未回执命令的 expiresAt 改成过去
  artifact <deviceId> [version]               生成演示更新包并下发 prepare_update
  rotatetoken <deviceId>                      轮换设备令牌（旧令牌留宽限期）
  faults                                      打印当前故障注入开关
  quit                                        停止模拟平台

非交互环境（stdin 已关闭）时控制台会静默退出，服务器继续跑。
"""


def _stdin_state() -> Tuple[bool, bool]:
    """返回 (是真终端, 是空设备)。

    Windows 上 NUL 是字符设备，`isatty()` 会返回 True——所以 DEVNULL 启动时不能只看 isatty，
    否则会在日志里打印一堆没人看的 `mock>` 提示符。这里再用 stat 比对一次 NUL。
    """
    stream = sys.stdin
    if stream is None:
        return False, True
    try:
        is_tty = bool(stream.isatty())
    except (ValueError, AttributeError, OSError):
        return False, True
    devnull = False
    if os.name == "nt" and is_tty:
        try:
            here = os.fstat(stream.fileno())
            null_stat = os.stat(os.devnull)
            devnull = (here.st_dev, here.st_ino) == (null_stat.st_dev, null_stat.st_ino)
        except (OSError, ValueError, AttributeError):
            devnull = False
    return (is_tty and not devnull), devnull


class Console(threading.Thread):
    def __init__(self, platform: PlatformCore) -> None:
        super().__init__(name="mock-console", daemon=True)
        self.platform = platform
        is_tty, is_devnull = _stdin_state()
        self.prompt = is_tty          # 只有真终端才打提示符，重定向/管道时不污染日志
        self.usable = not is_devnull  # stdin 是空设备就干脆不启动控制台

    def run(self) -> None:
        plat = self.platform
        if not self.usable:
            # 非交互环境（DEVNULL）：静默退出，服务器继续跑
            plat.log(".. stdin 是空设备（DEVNULL），调试控制台未启动，服务器正常运行")
            return
        while not plat.shutdown_event.is_set():
            if self.prompt:
                print("mock> ", end="", flush=True)
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                plat.log(".. 调试控制台：stdin 不可读，控制台退出（服务器继续运行）")
                return
            if not line:      # EOF：非交互环境（管道）下静默退出，不崩
                if self.prompt:
                    plat.log(".. 调试控制台：stdin 关闭，控制台退出（服务器继续运行）")
                return
            line = line.strip()
            if not line:
                continue
            try:
                if self.dispatch(line):
                    return
            except Exception as exc:   # 控制台出错不能带崩服务器
                plat.log(f"!! 控制台命令执行失败：{exc}")
                if plat.args.verbose:
                    plat.log(traceback.format_exc())

    # ---- 分发 ----

    def dispatch(self, line: str) -> bool:
        plat = self.platform
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return False
        head, rest = parts[0].lower(), parts[1:]
        if head in ("help", "?", "h"):
            print(HELP_TEXT, end="", flush=True)
            return False
        if head in ("quit", "exit", "q"):
            plat.log("-- 收到 quit，模拟平台正在停止")
            plat.request_stop()
            return True
        if head == "devices":
            with plat.lock:
                records = list(plat.devices.values())
            if not records:
                print("（还没有设备注册）", flush=True)
            for rec in records:
                pending = sum(1 for c in rec._commands() if c.receipt is None)
                print(f"  {rec.device_id:<16} {rec.connection_state(plat.args.offline_after_ms):<9} "
                      f"bootId={rec.boot_id} 最后上行={rec.last_seen_at} "
                      f"命令={len(rec.command_order)}(未回执 {pending}) "
                      f"遥测={rec.counters['telemetry']} 事件={rec.counters['events']} "
                      f"caps={json.dumps(rec.capabilities, ensure_ascii=False)}", flush=True)
            return False
        if head == "faults":
            faults = plat.fault_summary()
            print("  故障注入：" + ("；".join(faults) if faults else "无"), flush=True)
            return False
        if head in ("telemetry", "state", "uploads"):
            if not rest:
                print(f"用法：{head} <deviceId>", flush=True)
                return False
            rec = plat.device(rest[0], create=False)
            if rec is None:
                print(f"  设备 {rest[0]} 未注册", flush=True)
                return False
            if head == "telemetry":
                if rec.last_telemetry is None:
                    print("  （还没有遥测）", flush=True)
                else:
                    print(json.dumps(rec.last_telemetry, ensure_ascii=False, indent=2), flush=True)
            elif head == "state":
                print(json.dumps(rec.state(plat.args.offline_after_ms), ensure_ascii=False, indent=2),
                      flush=True)
            else:
                with plat.lock:
                    rows = [u for u in plat.uploads.values() if u.device_id == rec.device_id]
                for row in rows:
                    print(f"  {row.upload_id} {row.name:<24} {row.received_offset}/{row.size} "
                          f"完成={row.completed} fileId={row.file_id} 重复片={row.duplicate_chunks}",
                          flush=True)
                if not rows:
                    print("  （没有上传记录）", flush=True)
            return False
        if head == "batches":
            with plat.lock:
                items = list(plat.batches.values())
            for item in items:
                result = item["result"]
                print(f"  {result['batchId']:<20} complete={result['complete']} "
                      f"files={result['fileCount']} bytes={result['receivedBytes']} "
                      f"missing={[m['name'] for m in result['missing']]}", flush=True)
            if not items:
                print("  （没有批次）", flush=True)
            return False
        if head == "artifact":
            if not rest:
                print("用法：artifact <deviceId> [version]", flush=True)
                return False
            device_id = rest[0]
            artifact = plat.make_demo_artifact(device_id, rest[1] if len(rest) > 1 else None)
            self._issue_prepare_update(device_id, artifact)
            return False
        if head == "expire":
            if not rest:
                print("用法：expire <deviceId>", flush=True)
                return False
            device_id = rest[0]
            rec = plat.device(device_id, create=False)
            if rec is None:
                print(f"  设备 {device_id} 未注册", flush=True)
                return False
            past = iso(utc_now() - timedelta(seconds=1))
            touched = []
            with plat.lock:
                targets = [c for c in rec._commands() if c.receipt is None]
                for command in targets:
                    command.expires_at = past
                    command.expired = True
                    touched.append(command)
            for command in touched:
                plat.log(f"!! 已把命令 {command.command_id}（{command.type}）的 expiresAt 改成 {past}，"
                         f"重推一次供终端验证“拒执过期命令”")
                plat.push_command(command, reason="expire-debug")
            if not touched:
                print("  （没有未回执的命令可过期）", flush=True)
            return False
        if head == "rotatetoken":
            if not rest:
                print("用法：rotatetoken <deviceId>", flush=True)
                return False
            device_id = rest[0]
            rec = plat.device(device_id)
            assert rec is not None
            new_token = f"tok-{uuid.uuid4().hex[:12]}"
            with plat.lock:
                rec.rotate_token(new_token, plat.args.token_grace_ms)
            print(f"  新令牌：{new_token}", flush=True)
            print(f"  旧令牌在 {plat.args.token_grace_ms}ms 内仍可用；之后请让终端只带新令牌。",
                  flush=True)
            plat.log(f"++ 设备 {device_id} 令牌已轮换（新令牌 {mask_token(new_token)}，"
                     f"旧令牌宽限 {plat.args.token_grace_ms}ms）")
            return False
        if head == "cmd":
            return self._console_cmd(rest)
        print(f"  不认识命令：{head}（输入 help 看清单）", flush=True)
        return False

    def _console_cmd(self, rest: List[str]) -> bool:
        plat = self.platform
        if len(rest) < 2:
            print("用法：cmd <deviceId> <type> [参数...]，白名单：" + ", ".join(COMMAND_WHITELIST),
                  flush=True)
            return False
        device_id, type_, args = rest[0], rest[1], rest[2:]
        if type_ not in COMMAND_WHITELIST:
            print(f"  {type_} 不在白名单：{', '.join(COMMAND_WHITELIST)}", flush=True)
            return False
        batch_id = None
        payload: Dict[str, Any] = {}
        if type_ == "pause_capture":
            batch_id = args[0] if args else None
            payload = {
                "reason": "初扫出现疑似异常，平台请求暂停复核",
                "scope": "local_capture",
                "requestedBy": "operator",
                "note": "只暂停本地采集/回放推进；是否停雷达发射由专用驱动确认（PRD §5.3）",
            }
        elif type_ == "apply_config":
            version = args[0] if args else plat.args.config_version
            payload = {
                "configVersion": version,
                "airTempC": 24.5,
                "relativeHumidityPct": 58.0,
                "windSpeedMs": 0.4,
                "source": "manual-instrument",
                "publishedAt": iso(),
                "publishedBy": "manager",
                "note": "EMC 是环境先验，不是木柱内部实测含水率",
            }
        elif type_ == "assign_task":
            order_id = args[0] if args else "SH-2026-0901"
            component_id = args[1] if len(args) > 1 else "Z04"
            zone_id = args[2] if len(args) > 2 else f"{component_id}-lower"
            batch_id = args[3] if len(args) > 3 else f"scan-{component_id.lower()}-002"
            payload = {
                "orderId": order_id,
                "projectId": "site-demo",
                "componentId": component_id,
                "zoneId": zone_id,
                "zoneIds": [zone_id],
                "batchId": batch_id,
                "scenarioId": "rescan-demo-v1",
                "configVersion": plat.args.config_version,
                "modelVersion": plat.args.artifact_previous_version,
                "note": "连续绕柱；方向由操作者标记，端上不猜（PRD §3.3）",
            }
        elif type_ == "request_upload":
            batch_id = args[0] if args else None
            payload = {
                "batchId": batch_id,
                "reason": "平台请求交付本批次数据",
                "priority": "normal",
                "includePreview": False,
            }
        elif type_ == "prepare_update":
            artifact = plat.latest_artifact(device_id) or plat.make_demo_artifact(device_id)
            self._issue_prepare_update(device_id, artifact)
            return False
        elif type_ == "query_status":
            payload = {"includeHealth": True, "includeUpload": True, "reason": "平台例行查询"}
        try:
            record = plat.issue_command(device_id, type_, payload=payload,
                                        target_batch_id=batch_id, actor_id="console",
                                        source="console")
        except HttpError as exc:
            print(f"  下发失败：{exc.code} {exc.message}", flush=True)
            return False
        print(f"  已下发 {record.command_id} type={record.type} "
              f"targetBatchId={record.target_batch_id} expiresAt={record.expires_at} "
              f"delivered={record.delivered}", flush=True)
        if type_ == "assign_task":
            print(f"  提示：assign_task 已把设备任务版本推进到 "
                  f"{plat.device(device_id).task_revision}（下一次命令用这个 expectedTaskRevision）",
                  flush=True)
        return False

    def _issue_prepare_update(self, device_id: str, artifact: ArtifactRecord) -> None:
        plat = self.platform
        manifest = artifact.manifest()
        record = plat.issue_command(device_id, "prepare_update",
                                    payload=plat.prepare_update_payload(artifact),
                                    actor_id="console", source="console")
        print(f"  已下发 {record.command_id} type=prepare_update "
              f"artifactId={artifact.artifact_id} version={artifact.version} "
              f"downloadUrl={manifest['downloadUrl']} delivered={record.delivered}", flush=True)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="木脉智检 · 本地模拟平台服务器（标准库实现，用于终端联调）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址；树莓派要连就保持 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--data-dir", default=".mock-platform-data",
                        help="字节落盘目录（上传分片、完成的文件、演示更新包、预览图）")
    parser.add_argument("--device-token", default="demo-token",
                        help="设备令牌；多个用逗号分隔（用于令牌轮换验证）")
    parser.add_argument("--session-id", default="demo-01", help="demoSessionId（排练轮次）")
    parser.add_argument("--config-version", default="CFG-02", help="register 返回的 configVersion")
    parser.add_argument("--device-id", default="handheld-02", help="仅用于横幅提示，不限制注册")
    parser.add_argument("--heartbeat-interval-ms", type=int, default=5000,
                        help="下发给设备的心跳间隔（PRD §8.2 建议 5 秒）")
    parser.add_argument("--offline-after-ms", type=int, default=15000,
                        help="多久没上行判离线（PRD §8.2 约 15 秒）")
    parser.add_argument("--command-ttl-ms", type=int, default=300000,
                        help="命令默认有效期，超过则设备应回 failed/command_expired")
    parser.add_argument("--token-grace-ms", type=int, default=60000,
                        help="令牌轮换时旧令牌的宽限期")
    parser.add_argument("--ws-ping-interval", type=float, default=20.0,
                        help="设备通道空闲多久发一次 WS ping")
    parser.add_argument("--chunk-hint", type=int, default=256 * 1024,
                        help="建议分片大小（仅作为响应里的 chunkSizeHint 可参考）")
    parser.add_argument("--preview-keep", type=int, default=10,
                        help="每台设备只保留最近 N 张预览图（PRD §7.4：不作为原始图像归档）")
    parser.add_argument("--artifact-version", default="DEMO-M03", help="生成的演示包版本号")
    parser.add_argument("--artifact-previous-version", default="DEMO-M02",
                        help="演示包声明的上一版本（终端切换失败时要退回它）")
    parser.add_argument("--fail-register", action="store_true", help="故障注入：注册一律 503")
    parser.add_argument("--reject-checksum", action="store_true",
                        help="故障注入：complete 前把落盘文件翻转 1 字节，强制摘要不一致")
    parser.add_argument("--drop-after-bytes", type=int, default=None,
                        help="故障注入：单次上传累计收到 N 字节后直接掐断连接")
    parser.add_argument("--latency-ms", type=int, default=0, help="故障注入：每个请求额外延迟")
    parser.add_argument("--no-console", action="store_true", help="不启动 stdin 调试控制台")
    parser.add_argument("--reject-extra-types", action="store_true",
                        help="把契约未定义的事件类型（如 device.selfcheck）显式拒收并标 retryable=false，"
                             "用于验证终端「出队不再重试」的分支；默认是收下")
    parser.add_argument("--verbose", action="store_true",
                        help="打印更细的日志（事件去重、分片偏移、投递尝试等）")
    return parser


def banner(plat: PlatformCore, args: argparse.Namespace, host: str, port: int) -> None:
    display = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    base = f"http://{display}:{port}"
    faults = plat.fault_summary()
    lines = [
        "",
        "木脉智检 · 本地模拟平台（mock-platform）已启动",
        f"  监听        {args.host}:{port}",
        f"  终端配置    platformUrl={base}  deviceId={args.device_id}  deviceToken={mask_token(args.device_token.split(',')[0])}",
        f"  局域网地址  http://{lan_ip()}:{port}   （树莓派上用这个地址）",
        f"  设备通道    ws://{display}:{port}/ws/devices/{{deviceId}}?token=<deviceToken>",
        f"  数据目录    {plat.data_dir}",
        f"  演示会话    demoSessionId={args.session_id}  configVersion={args.config_version}",
        f"  心跳参数    heartbeatIntervalMs={args.heartbeat_interval_ms}  offlineAfterMs={args.offline_after_ms}",
        f"  契约自检    {contracts_selftest()}",
        "  存储说明    元数据（设备/上传/批次/命令/回执）只在内存里，进程重启即清空；",
        "              --data-dir 只保留已接收的字节、完成的文件、演示更新包与预览图。",
        "  路径说明    本服务只占用 /ws/devices/{deviceId}；浏览器用的 /ws 由真平台提供。",
    ]
    if faults:
        lines.append("  故障注入    开：" + "；".join(faults))
    else:
        lines.append("  故障注入    无（正常行为）")
    if not args.no_console:
        lines.append("  调试控制台  从 stdin 读命令，输入 help 看清单（非交互环境自动静默退出）")
    lines.append("")
    with plat.log_lock:
        print("\n".join(lines), flush=True)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    plat = PlatformCore(args)
    MockPlatformHandler.platform = plat

    server = ThreadingHTTPServer((args.host, args.port), MockPlatformHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    plat.attach_server(server)

    watchdog = threading.Thread(target=plat.housekeeping, name="mock-housekeeping", daemon=True)
    watchdog.start()

    # 先打横幅再起控制台：否则管道喂命令时，命令输出会跑在横幅前面（看起来像没启动）
    banner(plat, args, args.host, port)

    if not args.no_console:
        Console(plat).start()

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        plat.log("-- 收到 Ctrl+C，正在停止")
    finally:
        plat.shutdown()
        server.shutdown()
        server.server_close()
        plat.log("-- 模拟平台已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
