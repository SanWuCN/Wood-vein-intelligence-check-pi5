"""平台客户端：HTTP + WebSocket + 握手/心跳/重连/回执（PRD §7、§8）。

只依赖标准库：
  · HTTP 用 `urllib.request`（流式分片上传，不把整文件读内存）
  · WebSocket 用 `socket` 手写 RFC6455 客户端（掩码、帧解析、ping/pong、close）
这样树莓派上 `apt install python3-pyqt5 python3-psutil` 之后不需要再 pip 装东西，
现场少一个装不上的依赖就少一类故障。

分工（PRD §7.1）：
  · 小消息（遥测、状态事件、命令回执）走 WebSocket
  · 样本 / 报告 / 更新包走 HTTP，分片 + 断点续传
  · 预览图走低帧率 HTTP，不作为原始图像归档

三条不能省的规则：
  1. `accepted` 只表示收到，`executed` 由真正完成动作的地方回；
  2. 关键业务事件至少一次投递（outbox + messageId 去重），
     遥测允许覆盖旧样本，掉线期间不无限堆积每秒 CPU 数据；
  3. 心跳 5 秒，超过 15 秒没收到平台任何消息标为延迟/离线，
     断线退避 1、2、4…加抖动。
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import (
    Command,
    ConnectionState,
    Envelope,
    EventType,
    SCHEMA_VERSION,
    build_capabilities_payload,
    build_register_payload,
)
from .logging_setup import get_logger

log = get_logger("platform")

#: WebSocket 要求的固定 GUID（RFC6455）
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: 重连退避上限内的抖动比例，避免多台设备同时重连打爆平台（PRD §8.2）
JITTER_RATIO = 0.25


# --------------------------------------------------------------------------- #
# 手写 WebSocket（RFC6455 客户端侧）
# --------------------------------------------------------------------------- #

class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """最小可用的 RFC6455 客户端。

    只实现终端需要的部分：文本帧收发、ping/pong 保活、close。
    没有扩展、没有分片写（大消息一律走 HTTP）。
    """

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 5.0) -> None:
        self.url = url
        self.extra_headers = dict(headers or {})
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._closed = False

    # ---- 连接 ----

    def connect(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的 WebSocket 协议：{parsed.scheme}")
        secure = parsed.scheme == "wss"
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if secure else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self._sock = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in self.extra_headers.items():
            request_lines.append(f"{name}: {value}")
        raw.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("ascii"))

        response = self._read_http_response(raw)
        status_line = response.split("\r\n", 1)[0]
        if "101" not in status_line:
            self.close()
            raise WebSocketError(f"WebSocket 升级被拒绝：{status_line}")

        accept_expected = base64.b64encode(
            __import__("hashlib").sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept_expected.lower() not in response.lower():
            self.close()
            raise WebSocketError("Sec-WebSocket-Accept 校验失败，对端可能不是 WebSocket 服务")
        log.info("WebSocket 已连接：%s", self.url)

    def _read_http_response(self, sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手过程中连接被关闭")
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise WebSocketError("握手响应异常地长")
        head, _, rest = bytes(data).partition(b"\r\n\r\n")
        self._buffer.extend(rest)
        return head.decode("utf-8", errors="replace")

    # ---- 帧 ----

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(0x9, payload)

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(0xA, payload)

    def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            payload = code.to_bytes(2, "big")
            self._send_frame(0x8, payload, force=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass

    def _send_frame(self, opcode: int, payload: bytes, *, force: bool = False) -> None:
        sock = self._sock
        if sock is None:
            if force:
                return
            raise WebSocketError("连接已关闭")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        mask_bit = 0x80  # 客户端发出的帧必须掩码
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(mask_bit | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            sock.sendall(bytes(header) + masked)
        except OSError as exc:
            self._closed = True
            raise WebSocketError(f"发送失败：{exc}") from exc

    def recv_text(self, timeout: float = 0.05) -> Optional[str]:
        """非阻塞式收一条文本消息。超时返回 None；连接关闭抛 WebSocketError。"""
        sock = self._sock
        if sock is None:
            raise WebSocketError("连接已关闭")
        sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            frame = self._try_read_frame()
            if frame is not None:
                opcode, payload = frame
                if opcode == 0x1:
                    return payload.decode("utf-8", errors="replace")
                if opcode == 0x8:
                    self._closed = True
                    raise WebSocketError("对端关闭了连接")
                if opcode == 0x9:
                    try:
                        self.send_pong(payload)
                    except WebSocketError:
                        raise
                    continue
                if opcode == 0xA:
                    continue
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                sock.settimeout(min(0.2, max(0.01, remaining)))
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._closed = True
                raise WebSocketError(f"接收失败：{exc}") from exc
            if not chunk:
                self._closed = True
                raise WebSocketError("连接被对端关闭")
            self._buffer.extend(chunk)

    def _try_read_frame(self) -> Optional[Tuple[int, bytes]]:
        data = self._buffer
        if len(data) < 2:
            return None
        first, second = data[0], data[1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(data) < offset + 2:
                return None
            length = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
        elif length == 127:
            if len(data) < offset + 8:
                return None
            length = int.from_bytes(data[offset : offset + 8], "big")
            offset += 8
        mask = b""
        if masked:
            if len(data) < offset + 4:
                return None
            mask = bytes(data[offset : offset + 4])
            offset += 4
        if len(data) < offset + length:
            return None
        payload = bytes(data[offset : offset + length])
        del data[: offset + length]
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    @property
    def closed(self) -> bool:
        return self._closed or self._sock is None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

@dataclass
class HttpResult:
    ok: bool
    status: int = 0
    body: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_ms: float = 0.0


class HttpClient:
    """`urllib` 薄封装。统一错误体解析与超时。"""

    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "woodpulse-terminal/2.0.0"}
        if self.token:
            headers["X-Device-Token"] = self.token
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(extra or {})
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        raw: Optional[bytes] = None,
        content_type: str = "application/json",
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        expect_json: bool = True,
    ) -> HttpResult:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data: Optional[bytes] = None
        headers = self._headers(extra_headers)
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type

        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
                elapsed = (time.monotonic() - started) * 1000.0
                parsed: Dict[str, Any] = {}
                if expect_json and payload:
                    try:
                        parsed = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        parsed = {"raw": payload.decode("utf-8", errors="replace")[:2000]}
                if not isinstance(parsed, dict):
                    parsed = {"data": parsed}
                parsed.setdefault("_status", response.status)
                return HttpResult(True, response.status, parsed, "", elapsed)
        except urllib.error.HTTPError as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            text = ""
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            parsed_body: Dict[str, Any] = {}
            try:
                parsed_body = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed_body = {"raw": text[:500]}
            reason = parsed_body.get("message") or parsed_body.get("code") or f"HTTP {exc.code}"
            return HttpResult(False, exc.code, parsed_body, str(reason), elapsed)
        except (urllib.error.URLError, socket.timeout, OSError, ssl.SSLError) as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            return HttpResult(False, 0, {}, f"网络错误：{exc}", elapsed)

    # ---- 具体接口（PRD §7.4）----

    def health(self) -> HttpResult:
        return self.request("GET", "/api/health", timeout=3.0)

    def register(self, payload: Dict[str, Any]) -> HttpResult:
        return self.request("POST", "/api/devices/register", body=payload)

    def post_events_batch(self, events: List[Dict[str, Any]]) -> HttpResult:
        return self.request("POST", "/api/device-events/batch", body={"events": events})

    def create_upload(self, payload: Dict[str, Any]) -> HttpResult:
        return self.request("POST", "/api/files/uploads", body=payload)

    def put_chunk(self, upload_id: str, offset: int, chunk: bytes, chunk_sha: str) -> HttpResult:
        return self.request(
            "PUT",
            f"/api/files/uploads/{urllib.parse.quote(upload_id)}/chunks",
            raw=chunk,
            content_type="application/octet-stream",
            extra_headers={"X-Chunk-Offset": str(offset), "X-Chunk-Sha256": chunk_sha},
            timeout=max(self.timeout, 60.0),
        )

    def complete_upload(self, upload_id: str, payload: Dict[str, Any]) -> HttpResult:
        return self.request("POST", f"/api/files/uploads/{urllib.parse.quote(upload_id)}/complete", body=payload)

    def submit_batch(self, manifest: Dict[str, Any]) -> HttpResult:
        return self.request("POST", "/api/batches", body=manifest)

    def artifact_manifest(self, artifact_id: str) -> HttpResult:
        return self.request("GET", f"/api/artifacts/{urllib.parse.quote(artifact_id)}/manifest")

    def post_receipt(self, artifact_id: str, payload: Dict[str, Any]) -> HttpResult:
        return self.request("POST", f"/api/artifacts/{urllib.parse.quote(artifact_id)}/receipts", body=payload)

    def post_preview(self, device_id: str, jpeg: bytes, frame_index: int) -> HttpResult:
        return self.request(
            "POST",
            f"/api/devices/{urllib.parse.quote(device_id)}/preview",
            raw=jpeg,
            content_type="image/jpeg",
            extra_headers={"X-Frame-Index": str(frame_index)},
            timeout=5.0,
        )

    def download(self, url: str, dest_path: str, *, max_bytes: int = 512 * 1024 * 1024) -> HttpResult:
        """流式下载到文件，边下边算 sha256（更新包用，避免整包进内存）。"""
        import hashlib

        target = url if url.startswith("http") else f"{self.base_url}{url}"
        request = urllib.request.Request(target, headers=self._headers(), method="GET")
        started = time.monotonic()
        digest = hashlib.sha256()
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 30.0)) as response:
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(dest_path, "wb") as handle:
                    while True:
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise WebSocketError(f"下载超过上限 {max_bytes} 字节")
                        digest.update(block)
                        handle.write(block)
                header_sha = response.headers.get("X-File-Sha256") or ""
                elapsed = (time.monotonic() - started) * 1000.0
                return HttpResult(
                    True,
                    response.status,
                    {"path": dest_path, "bytes": total, "sha256": digest.hexdigest(), "declaredSha256": header_sha},
                    "",
                    elapsed,
                )
        except urllib.error.HTTPError as exc:
            return HttpResult(False, exc.code, {}, f"下载失败：HTTP {exc.code}")
        except (urllib.error.URLError, OSError, WebSocketError) as exc:
            return HttpResult(False, 0, {}, f"下载失败：{exc}")


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #

@dataclass
class UploadOutcome:
    ok: bool
    file_id: str = ""
    bytes_confirmed: int = 0
    sha256: str = ""
    error: str = ""
    needs_recreate: bool = False


def upload_file(
    client: HttpClient,
    *,
    path: str,
    name: str,
    batch_id: str,
    role: str,
    declared_sha256: str = "",
    chunk_size: int = 512 * 1024,
    resume_offset: int = 0,
    upload_id: str = "",
    progress: Optional[Callable[[int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> UploadOutcome:
    """分片上传一个文件，支持断点续传与幂等重发（PRD §7.4、§13 H10）。

    为什么不一次性 POST：批次目录里 frames.csv 有几 MB，全网断一次就全废；
    分片后平台按 offset 记录进度，重连从 `receivedOffset` 继续。

    `declared_sha256` 为空时**一定**在本机算一遍：创建上传时就报摘要，平台才能在
    收完整文件后重算比对（PRD §7.4 `/complete` 要服务端重算摘要）。空摘要直接建单
    会被平台按 422 拒掉，联调时表现为"最后一个文件永远传不上去"。
    """
    import hashlib

    if not os.path.isfile(path):
        return UploadOutcome(False, error=f"文件不存在：{path}")
    size = os.path.getsize(path)
    if not declared_sha256:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        declared_sha256 = digest.hexdigest()

    if not upload_id:
        created = client.create_upload(
            {
                "name": name,
                "size": size,
                "sha256": declared_sha256,
                "batchId": batch_id,
                "role": role,
                "schemaVersion": SCHEMA_VERSION,
            }
        )
        if not created.ok:
            return UploadOutcome(False, error=created.error or "创建上传失败")
        upload_id = str(created.body.get("uploadId") or "")
        resume_offset = int(created.body.get("receivedOffset") or 0)
        if created.body.get("completed") and created.body.get("fileId"):
            return UploadOutcome(
                True,
                file_id=str(created.body["fileId"]),
                bytes_confirmed=int(created.body.get("bytesConfirmed") or size),
                sha256=declared_sha256,
            )
        if not upload_id:
            return UploadOutcome(False, error="平台没有返回 uploadId")

    offset = max(0, resume_offset)
    with open(path, "rb") as handle:
        handle.seek(offset)
        while offset < size:
            if stop_check and stop_check():
                return UploadOutcome(False, error="已取消", needs_recreate=False)
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            chunk_sha = hashlib.sha256(chunk).hexdigest()
            result = client.put_chunk(upload_id, offset, chunk, chunk_sha)
            if not result.ok:
                # 409 通常表示平台侧偏移对不上（进程重启过），需要重新建上传
                needs_recreate = result.status == 409
                return UploadOutcome(False, error=result.error or f"分片失败 HTTP {result.status}", needs_recreate=needs_recreate)
            offset += len(chunk)
            if progress:
                progress(offset, size)

    completed = client.complete_upload(upload_id, {"sha256": declared_sha256, "size": size})
    if not completed.ok:
        return UploadOutcome(False, error=completed.error or "完成上传失败", needs_recreate=completed.status in (404, 409, 410))
    return UploadOutcome(
        True,
        file_id=str(completed.body.get("fileId") or ""),
        bytes_confirmed=int(completed.body.get("bytesConfirmed") or size),
        sha256=declared_sha256,
    )


# --------------------------------------------------------------------------- #
# 客户端主类
# --------------------------------------------------------------------------- #

@dataclass
class ClientCallbacks:
    """客户端向 UI 抛事件用的回调集合。全部可选，方便测试时只挂需要的。"""

    on_state: Optional[Callable[[str, str], None]] = None            # (state, detail)
    on_telemetry_ack: Optional[Callable[[Dict[str, Any]], None]] = None
    on_command: Optional[Callable[[Dict[str, Any]], None]] = None     # 平台下发命令
    on_config: Optional[Callable[[Dict[str, Any]], None]] = None      # 平台下发环境配置
    on_artifact: Optional[Callable[[Dict[str, Any]], None]] = None    # 更新包通知
    on_task: Optional[Callable[[Dict[str, Any]], None]] = None        # 派发任务
    on_events_acked: Optional[Callable[[List[str]], None]] = None
    on_upload_progress: Optional[Callable[[Dict[str, Any]], None]] = None
    on_upload_done: Optional[Callable[[Dict[str, Any]], None]] = None
    on_latency: Optional[Callable[[Optional[float]], None]] = None
    on_log: Optional[Callable[[str, str], None]] = None               # (level, text)


class PlatformClient:
    """终端侧的平台客户端。

    线程模型：`start()` 起一个后台线程跑事件循环（连接、心跳、收消息、刷 outbox），
    GUI 线程只调用 `enqueue_event()` / `request_upload()` 这类入队方法，
    不直接碰 socket。这样"界面线程不跑阻塞网络"这条（PRD §10）就成立了。
    """

    def __init__(self, cfg, storage, callbacks: Optional[ClientCallbacks] = None) -> None:
        self.cfg = cfg
        self.storage = storage
        self.callbacks = callbacks or ClientCallbacks()
        self.http = HttpClient(cfg.platform.platform_url, cfg.platform.device_token, cfg.platform.request_timeout_s)
        self.device_id = cfg.effective_device_id
        self.boot_id = ""
        self._ws: Optional[WebSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._outbox_lock = threading.Lock()
        self._outbox: List[Dict[str, Any]] = []
        self._telemetry_out: Optional[Dict[str, Any]] = None
        self._telemetry_lock = threading.Lock()
        self._upload_queue: List[Dict[str, Any]] = []
        self._upload_lock = threading.Lock()
        self._seq = 0
        self._state = ConnectionState.OFFLINE
        self._state_detail = "尚未连接"
        self._state_lock = threading.Lock()
        self._last_rx = 0.0
        self._last_tx = 0.0
        self._last_heartbeat = 0.0
        #: 上一次把 outbox 推给平台的时间。正常连接期间必须周期性推，
        #: 否则命令回执这类关键事件只会在重连那一刻才发出去。
        self._last_flush = 0.0
        #: 连续多少轮"发出去的事件平台既没确认也没拒收"（静默丢弃检测）
        self._unaccounted_attempts = 0
        self._registered = False
        self._platform_config: Dict[str, Any] = {}
        self._artifact_queue: List[Dict[str, Any]] = []
        self._preview_lock = threading.Lock()
        self._preview_pending: Optional[Tuple[bytes, int]] = None
        self._upload_stop = threading.Event()
        self._session_id = cfg.platform.demo_session_id

    # ---- 状态 ----

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    @property
    def state_detail(self) -> str:
        with self._state_lock:
            return self._state_detail

    @property
    def platform_config(self) -> Dict[str, Any]:
        return dict(self._platform_config)

    def _set_state(self, state: str, detail: str = "") -> None:
        with self._state_lock:
            changed = state != self._state or detail != self._state_detail
            self._state = state
            self._state_detail = detail
        if changed:
            if state == ConnectionState.ONLINE:
                log.info("平台状态：在线 %s", f"— {detail}" if detail else "")
            elif state == ConnectionState.OFFLINE:
                log.warning("平台状态：离线 %s", f"— {detail}" if detail else "")
            else:
                log.info("平台状态：%s %s", ConnectionState.LABEL.get(state, state), f"— {detail}" if detail else "")
            if self.callbacks.on_state:
                self.callbacks.on_state(state, detail)

    # ---- 生命周期 ----

    def start(self, boot_id: str) -> None:
        self.boot_id = boot_id
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="platform-client", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 4.0) -> None:
        self._stop.set()
        self._upload_stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._close_ws()
        self._thread = None
        self._set_state(ConnectionState.OFFLINE, "客户端已停止")

    def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()

    # ---- 出站 ----

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def make_envelope(self, type_: str, payload: Dict[str, Any]) -> Envelope:
        return Envelope(
            type=type_,
            payload=payload,
            device_id=self.device_id,
            boot_id=self.boot_id,
            seq=self.next_seq(),
            demo_session_id=self._session_id,
            schema_version=SCHEMA_VERSION,
        )

    def enqueue_event(self, type_: str, payload: Dict[str, Any], *, critical: bool = True) -> str:
        """关键业务事件进 outbox（至少一次投递），遥测走 `queue_telemetry`。"""
        envelope = self.make_envelope(type_, payload)
        message = envelope.to_dict()
        if critical:
            with self._outbox_lock:
                self._outbox.append(message)
            if self.storage is not None:
                self.storage.enqueue_event(envelope.message_id, type_, message, envelope.seq)
        return envelope.message_id

    def queue_telemetry(self, payload: Dict[str, Any]) -> None:
        """遥测只保留最新一份：掉线期间不堆每秒 CPU 数据（PRD §8.2）。"""
        with self._telemetry_lock:
            self._telemetry_out = self.make_envelope(EventType.DEVICE_TELEMETRY, payload).to_dict()

    def queue_preview(self, jpeg: bytes, frame_index: int) -> None:
        with self._preview_lock:
            self._preview_pending = (jpeg, frame_index)

    def request_upload(self, batch_id: str, files: List[Dict[str, Any]]) -> None:
        with self._upload_lock:
            for item in files:
                self._upload_queue.append({"batchId": batch_id, **item})

    @property
    def pending_uploads(self) -> int:
        with self._upload_lock:
            return len(self._upload_queue)

    @property
    def pending_events(self) -> int:
        with self._outbox_lock:
            return len(self._outbox)

    def flush_outbox(self) -> int:
        """把内存 outbox 一次发出去（断线补传用）。返回发送条数。"""
        with self._outbox_lock:
            pending = list(self._outbox)
        if not pending:
            return 0
        return self._post_events_batch(pending)

    def _post_events_batch(self, events: List[Dict[str, Any]]) -> int:
        """补传关键事件。

        契约要点（PRD §7.4 未写明，见 `contracts.EVENT_BATCH_CONTRACT_NOTE`）：

        · `accepted` / `duplicated` 是 **messageId 数组**，不是计数 ——
          终端要靠这些 ID 才能在 outbox 里精确标记已确认、避免重复投递。
          平台若只回计数，这里退化为按顺序 ack 并在日志里提出建议。
        · `rejected` 每条带 `retryable`：
            `retryable=false` → **永久拒收**，直接出队，不再重试；
            `retryable=true` 或**缺省** → 临时失败，保留在 outbox 里退避重试。
          （缺省按"临时失败"处理是刻意的保守选择：宁可多试几次，也不因为平台
          漏写一个字段就把业务数据丢掉。）
        · 平台若静默丢弃某个 messageId（既不在 accepted/duplicated，也不在
          rejected），终端会保留它并重试；连续多次仍无着落时打一条明确告警，
          指向契约问题而不是设备问题。
        """
        if not events:
            return 0
        result = self.http.post_events_batch(events)
        if not result.ok:
            log.warning("补传关键事件失败（%d 条）：%s", len(events), result.error)
            if self.storage is not None:
                for event in events:
                    self.storage.note_event_failure(str(event.get("messageId")), result.error)
            return 0

        body = result.body or {}
        accepted = body.get("accepted")
        duplicated = body.get("duplicated")
        ids: List[str] = []
        if isinstance(accepted, list) or isinstance(duplicated, list):
            ids = [str(item) for item in (accepted or [])] + [str(item) for item in (duplicated or [])]
        else:
            count = int(accepted or 0) + int(duplicated or 0)
            log.warning(
                "平台 /api/device-events/batch 返回的是计数而不是 messageId 数组，"
                "已按发送顺序确认前 %d 条；建议平台改为返回 ID 数组以避免重复投递",
                count,
            )
            ids = [str(item.get("messageId")) for item in events[:count]]

        # ---- 拒收处理：按 retryable 分流 ----
        rejected = body.get("rejected") or []
        permanent_ids: List[str] = []
        transient_ids: List[str] = []
        for item in rejected:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("messageId") or "")
            if not message_id:
                # 平台回了 rejected 却没给 messageId：终端无从对应，只能整体告警
                log.error("平台 rejected 条目缺少 messageId，无法对应到具体事件：%s", item)
                continue
            retryable = item.get("retryable")
            if retryable is False:
                permanent_ids.append(message_id)
                log.error(
                    "平台永久拒收关键事件 %s：%s（已出队，不再重推）",
                    message_id,
                    item.get("reason") or "未说明原因",
                )
            else:
                transient_ids.append(message_id)
                log.warning(
                    "平台临时拒收关键事件 %s：%s（保留在 outbox，稍后重试）",
                    message_id,
                    item.get("reason") or "未说明原因",
                )
        ids.extend(permanent_ids)

        with self._outbox_lock:
            self._outbox = [item for item in self._outbox if item.get("messageId") not in ids]
        if self.storage is not None and ids:
            self.storage.ack_events(ids)
        if self.callbacks.on_events_acked and ids:
            self.callbacks.on_events_acked(ids)

        # 静默丢弃检测：发出去但既没被确认也没被拒收的 messageId
        accounted = set(ids) | set(transient_ids)
        missing = [str(item.get("messageId")) for item in events if str(item.get("messageId")) not in accounted]
        if missing:
            self._unaccounted_attempts += 1
            if self._unaccounted_attempts in (2, 10, 50):
                log.warning(
                    "平台没有对 %d 条关键事件给出 accepted/duplicated/rejected 中的任何一项"
                    "（第 %d 次）：%s。终端会继续重试；若平台确实不支持这些类型，"
                    "请在 rejected 里带上 messageId 且 retryable=false。",
                    len(missing),
                    self._unaccounted_attempts,
                    "、".join(missing[:3]),
                )
        else:
            self._unaccounted_attempts = 0

        log.debug(
            "关键事件补传完成：接受 %s，重复 %s，永久拒收 %d，临时拒收 %d",
            body.get("acceptedCount", len(accepted) if isinstance(accepted, list) else accepted),
            body.get("duplicatedCount", len(duplicated) if isinstance(duplicated, list) else duplicated),
            len(permanent_ids),
            len(transient_ids),
        )
        return len(ids)

    # ---- 主循环 ----

    def _loop(self) -> None:
        backoff = self.cfg.platform.reconnect_base_s
        while not self._stop.is_set():
            if self._ws is None or self._ws.closed:
                self._set_state(ConnectionState.CONNECTING if self._registered else ConnectionState.OFFLINE, "正在连接平台")
                if not self._connect():
                    delay = self._jitter(backoff)
                    log.info("将在 %.1fs 后重连平台", delay)
                    if self._stop.wait(delay):
                        break
                    backoff = min(self.cfg.platform.reconnect_max_s, backoff * 2)
                    continue
                backoff = self.cfg.platform.reconnect_base_s

            try:
                self._pump()
            except WebSocketError as exc:
                log.warning("平台连接中断：%s", exc)
                self._close_ws()
                self._set_state(ConnectionState.OFFLINE, f"连接中断：{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 后台线程不能因为单次异常退出
                log.exception("平台循环异常：%s", exc)
                time.sleep(0.5)

        self._close_ws()
        self._set_state(ConnectionState.OFFLINE, "客户端已停止")

    def _jitter(self, base: float) -> float:
        return base * (1.0 + random.uniform(-JITTER_RATIO, JITTER_RATIO))

    def _connect(self) -> bool:
        ws_url = self._ws_url()
        try:
            ws = WebSocket(ws_url, headers=self._auth_headers(), timeout=5.0)
            ws.connect()
        except (WebSocketError, OSError) as exc:
            self._set_state(ConnectionState.OFFLINE, f"连接失败：{exc}")
            return False
        self._ws = ws
        self._last_rx = time.monotonic()
        self._set_state(ConnectionState.ONLINE, "WebSocket 已建立")
        # 连上就把身份与能力报上去（PRD §7.2：首次握手上报版本、启动 ID 和 capabilities）
        self._send(self.make_envelope(EventType.DEVICE_HELLO, {
            "schemaVersion": SCHEMA_VERSION,
            "deviceId": self.device_id,
            "bootId": self.boot_id,
            "resumed": self._registered,
        }).to_dict())
        self._registered = True
        self._flush_after_reconnect()
        return True

    def _auth_headers(self) -> Dict[str, str]:
        headers = {}
        if self.cfg.platform.device_token:
            headers["X-Device-Token"] = self.cfg.platform.device_token
            headers["Authorization"] = f"Bearer {self.cfg.platform.device_token}"
        return headers

    def _ws_url(self) -> str:
        base = self.cfg.platform.platform_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        query = urllib.parse.urlencode(
            {
                "deviceToken": self.cfg.platform.device_token,
                "demoSessionId": self._session_id,
                "bootId": self.boot_id,
                "appVersion": "2.0.0-demo",
            }
        )
        return f"{base}/ws/devices/{urllib.parse.quote(self.device_id)}?{query}"

    def _flush_after_reconnect(self) -> None:
        """重新上线：先发最新遥测快照，再补关键业务事件（PRD §8.2）。"""
        with self._telemetry_lock:
            telemetry = self._telemetry_out
        if telemetry:
            try:
                self._send(telemetry)
            except WebSocketError:
                return
        # 内存 outbox 与磁盘 outbox 合并补传
        self._replay_stored_outbox(limit=200)
        self.flush_outbox()

    def _replay_stored_outbox(self, limit: int = 100) -> int:
        """把磁盘 outbox 里未确认的事件补传给平台（平台按 messageId 去重）。"""
        if self.storage is None:
            return 0
        disk_pending = self.storage.pending_events(limit=limit)
        if not disk_pending:
            return 0
        payloads = []
        for row in disk_pending:
            try:
                payloads.append(json.loads(row["payload"]))
            except (json.JSONDecodeError, TypeError):
                continue
        if not payloads:
            return 0
        return self._post_events_batch(payloads)

    def _pump(self) -> None:
        now = time.monotonic()
        ws = self._ws
        if ws is None:
            return

        # 收消息
        message = ws.recv_text(timeout=0.05)
        while message is not None:
            self._last_rx = time.monotonic()
            self._handle_inbound(message)
            message = ws.recv_text(timeout=0.01)

        # 心跳（PRD §8.2 建议 5 秒）
        if now - self._last_heartbeat >= self.cfg.platform.heartbeat_interval_s:
            self._last_heartbeat = now
            latency = self._measure_latency()
            if self.callbacks.on_latency:
                self.callbacks.on_latency(latency)
            try:
                self._send(
                    self.make_envelope(
                        EventType.DEVICE_HEALTH,
                        {
                            "schemaVersion": SCHEMA_VERSION,
                            "deviceId": self.device_id,
                            "bootId": self.boot_id,
                            "heartbeat": True,
                            "platformLatencyMs": latency,
                        },
                    ).to_dict()
                )
            except WebSocketError:
                raise

        # 遥测
        with self._telemetry_lock:
            telemetry = self._telemetry_out
            self._telemetry_out = None
        if telemetry is not None:
            self._send(telemetry)

        # 关键业务事件（命令回执、capture.*、batch.*）在**正常连接期间**也要发出去。
        # 只靠重连时补传是不够的：那样平台会一直看到"命令已下发但没有回执"，
        # 而 PRD §8.1 的三态回执正是靠这些事件送达的。
        if now - self._last_flush >= 0.5:
            self._last_flush = now
            if self.pending_events:
                self.flush_outbox()
            elif self.storage is not None and self.storage.pending_event_count():
                # 上一进程遗留的未确认事件（例如上次退出时还没确认）
                self._replay_stored_outbox(limit=50)

        # 预览图（低帧率 HTTP，不塞进事件通道）
        if self.cfg.platform.preview_upload:
            with self._preview_lock:
                preview = self._preview_pending
                self._preview_pending = None
            if preview is not None:
                jpeg, frame_index = preview
                self.http.post_preview(self.device_id, jpeg, frame_index)

        # 上传队列
        self._drain_uploads()

        # 离线判定：WebSocket 保活成功但长时间没有业务消息也算延迟（PRD §8.2）
        idle = now - self._last_rx
        if idle > self.cfg.platform.offline_after_s:
            self._set_state(ConnectionState.DEGRADED, f"已 {int(idle)} 秒未收到平台消息")
        elif idle > self.cfg.platform.heartbeat_interval_s * 2:
            self._set_state(ConnectionState.DEGRADED, f"平台响应延迟（{int(idle)} 秒）")
        else:
            self._set_state(ConnectionState.ONLINE, "心跳正常")

    def _measure_latency(self) -> Optional[float]:
        result = self.http.health()
        if not result.ok:
            return None
        return round(result.elapsed_ms, 1)

    def _send(self, message: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise WebSocketError("连接已关闭")
        ws.send_text(json.dumps(message, ensure_ascii=False))
        self._last_tx = time.monotonic()

    # ---- 入站 ----

    def _handle_inbound(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("收到无法解析的平台消息：%s", raw[:200])
            return
        if not isinstance(message, dict):
            return

        kind = str(message.get("kind") or message.get("type") or "")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else message

        if kind in ("hello", "welcome"):
            self._platform_config = dict(payload.get("platformConfig") or payload.get("config") or {})
            log.info("平台握手完成：%s", json.dumps(payload, ensure_ascii=False)[:300])
            return
        if kind in ("pong",):
            return
        if kind in ("command", "device.command"):
            if self.callbacks.on_command:
                self.callbacks.on_command(dict(payload))
            return
        if kind in ("device.config", "config.publish", "config"):
            if self.callbacks.on_config:
                self.callbacks.on_config(dict(payload))
            return
        if kind in ("artifact.publish", "artifact", "update.prepare"):
            self._artifact_queue.append(dict(payload))
            if self.callbacks.on_artifact:
                self.callbacks.on_artifact(dict(payload))
            return
        if kind in ("task.assign", "task.assign_task"):
            if self.callbacks.on_task:
                self.callbacks.on_task(dict(payload))
            return
        if kind in ("error",):
            log.warning("平台返回错误：%s", json.dumps(payload, ensure_ascii=False)[:300])
            return
        # 平台也可能直接推业务事件，按 type 分发一次
        self._handle_business_event(kind, dict(payload))

    def _handle_business_event(self, type_: str, payload: Dict[str, Any]) -> None:
        if type_ == EventType.DEVICE_TELEMETRY and self.callbacks.on_telemetry_ack:
            self.callbacks.on_telemetry_ack(payload)
        elif type_ in ("device.events.ack", "events.ack"):
            ids = [str(item) for item in payload.get("messageIds") or []]
            if ids and self.storage is not None:
                self.storage.ack_events(ids)
            if ids and self.callbacks.on_events_acked:
                self.callbacks.on_events_acked(ids)
        elif type_.startswith("command."):
            if self.callbacks.on_command:
                self.callbacks.on_command(payload)
        elif "config" in type_:
            if self.callbacks.on_config:
                self.callbacks.on_config(payload)
        elif type_.startswith("artifact") or "update" in type_:
            self._artifact_queue.append(payload)
            if self.callbacks.on_artifact:
                self.callbacks.on_artifact(payload)

    def take_artifact(self) -> Optional[Dict[str, Any]]:
        if not self._artifact_queue:
            return None
        return self._artifact_queue.pop(0)

    # ---- 上传 ----

    def _drain_uploads(self, max_files: int = 2) -> None:
        """处理上传队列。

        两条容易写错、但在现场一定会遇到的规则：

        1. **失败必须退避**。平台不可达或拒绝时如果立刻重试，一秒钟能打几百次，
           把日志刷爆、把电池和网络都耗掉；离线时更不该空转。这里给每个作业
           记 `attempts` 并按 1、2、4…秒指数退避，退避期间让后面的作业先走。
        2. **失败不能卡住队首**。旧实现遇到失败就 `return`，后面的文件永远排不上，
           表现为"有一部分文件怎么都传不上去"。现在把失败的作业暂时移到队尾。
        """
        if self._upload_stop.is_set():
            return
        now = time.monotonic()
        attempts_this_round = 0
        examined = 0
        queue_len = len(self._upload_queue)
        while examined < queue_len and attempts_this_round < max_files:
            examined += 1
            with self._upload_lock:
                if not self._upload_queue:
                    return
                job = self._upload_queue[0]
                if float(job.get("nextAttemptAt") or 0.0) > now:
                    # 还在退避窗口里：把它挪到队尾，先处理别的作业
                    self._upload_queue.append(self._upload_queue.pop(0))
                    continue
                self._upload_queue.pop(0)

            attempts_this_round += 1
            outcome = upload_file(
                self.http,
                path=str(job.get("path") or ""),
                name=str(job.get("name") or os.path.basename(str(job.get("path") or ""))),
                batch_id=str(job.get("batchId") or ""),
                role=str(job.get("role") or "other"),
                declared_sha256=str(job.get("sha256") or ""),
                # 重试时带上平台已经收到的偏移，直接从断点续传（PRD §13 H10）
                resume_offset=int(job.get("receivedOffset") or 0),
                upload_id=str(job.get("uploadId") or ""),
                progress=lambda done, total, j=job: self._note_upload_progress(j, done, total),
                stop_check=self._upload_stop.is_set,
            )

            if not outcome.ok:
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["lastError"] = outcome.error
                if outcome.needs_recreate:
                    job["uploadId"] = ""
                    job["receivedOffset"] = 0
                delay = self._upload_backoff(int(job["attempts"]))
                job["nextAttemptAt"] = time.monotonic() + delay
                log.warning(
                    "上传 %s 失败（第 %d 次）：%s；%.1fs 后重试",
                    job.get("name"),
                    job["attempts"],
                    outcome.error,
                    delay,
                )
                with self._upload_lock:
                    self._upload_queue.append(job)
                if self.callbacks.on_upload_done:
                    self.callbacks.on_upload_done(
                        {"job": job, "ok": False, "error": outcome.error, "attempts": job["attempts"]}
                    )
                continue

            if self.storage is not None:
                self.storage.set_file_upload_state(
                    str(job.get("batchId") or ""),
                    str(job.get("relPath") or ""),
                    "done",
                    remote_id=outcome.file_id,
                    received_offset=outcome.bytes_confirmed,
                )
            log.info("上传完成：%s（%d 字节，fileId=%s）", job.get("name"), outcome.bytes_confirmed, outcome.file_id)
            if self.callbacks.on_upload_done:
                self.callbacks.on_upload_done({"job": job, "ok": True, "fileId": outcome.file_id})

    @staticmethod
    def _upload_backoff(attempts: int, base: float = 1.0, cap: float = 30.0) -> float:
        """指数退避 + 抖动：1、2、4、8…最多 30 秒。"""
        raw = min(cap, base * (2 ** max(0, attempts - 1)))
        return raw * (1.0 + random.uniform(-JITTER_RATIO, JITTER_RATIO))

    def _note_upload_progress(self, job: Dict[str, Any], done: int, total: int) -> None:
        job["receivedOffset"] = done
        if self.callbacks.on_upload_progress:
            self.callbacks.on_upload_progress(
                {
                    "name": job.get("name"),
                    "batchId": job.get("batchId"),
                    "done": done,
                    "total": total,
                    "percent": round(done * 100.0 / total, 1) if total else 0.0,
                }
            )
        if self.storage is not None:
            self.storage.set_file_upload_state(
                str(job.get("batchId") or ""), str(job.get("relPath") or ""), "active", received_offset=done
            )

    # ---- 便捷方法（在工作线程外调用也可以，都是网络请求）----

    def register_device(self, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        result = self.http.register(payload)
        if result.ok:
            self._platform_config = dict(result.body)
            log.info("设备注册成功：%s", json.dumps(result.body, ensure_ascii=False)[:300])
        else:
            log.warning("设备注册失败：%s", result.error)
        return result.ok, result.body

    def submit_batch(self, manifest: Dict[str, Any]) -> HttpResult:
        return self.http.submit_batch(manifest)

    def fetch_artifact(self, artifact_id: str) -> HttpResult:
        return self.http.artifact_manifest(artifact_id)

    def send_receipt(self, artifact_id: str, payload: Dict[str, Any]) -> HttpResult:
        return self.http.post_receipt(artifact_id, payload)


def capability_payload(capabilities, changed: Optional[List[str]] = None) -> Dict[str, Any]:
    """能力变化时的载荷（薄封装，方便调用点不用记 build_ 前缀）。"""
    return build_capabilities_payload(capabilities, changed)


def register_payload(**kwargs) -> Dict[str, Any]:
    return build_register_payload(**kwargs)
