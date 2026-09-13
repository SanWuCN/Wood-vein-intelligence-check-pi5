#!/usr/bin/env python3
"""木脉智检 · 模拟平台端到端冒烟测试（Python 3.11 标准库，只用 urllib.request）。

为什么要有这个文件
------------------
模拟平台的"能起来"和"能用"是两件事。真正常出问题的是：分片幂等、摘要不一致、
部分接收、事件去重、命令回执这三态——这些都必须真的跑一遍才算验证过。

这个脚本做两轮：
  第一轮（正常行为）：注册 → 设备 WebSocket 握手 → 上行遥测 → 分片上传（含重复片）
                     → 摘要不一致 → 批次部分接收 → 事件补传去重 → 下发命令并回执
                     → 下载真实字节（Range）→ 产物清单与回执幂等 → 鉴权与白名单拒绝
  第二轮（故障注入）：--fail-register / --latency-ms / --drop-after-bytes / --reject-checksum
                     四个开关各自真的生效（终端的重试与部分接收分支靠这一轮覆盖）

模拟平台由本脚本自动起停（随机空闲端口，stdin=DEVNULL 顺带验证"非交互环境不崩"）。

用法
----
    python tools/smoke_mock_platform.py                     # 自动起停，两轮都跑
    python tools/smoke_mock_platform.py --show-server-log    # 同时打印模拟平台的真实日志
    python tools/smoke_mock_platform.py --base-url http://127.0.0.1:8080 --no-faults
                                                            # 只测一个已经起好的实例
    python tools/smoke_mock_platform.py --keep-data          # 保留临时数据目录

退出码：全 PASS → 0；有 FAIL → 1。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client as http_client
import json
import os
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TOKEN = "demo-token"
DEVICE_ID = "handheld-02"
BOOT_ID = "boot-smoke-0001"
SESSION_ID = "demo-01"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

HERE = Path(__file__).resolve().parent
MOCK_PATH = HERE / "mock_platform.py"

RESULTS: List[Tuple[str, bool, str]] = []
BASE_URL = ""


# --------------------------------------------------------------------------- #
# 断言与输出
# --------------------------------------------------------------------------- #

def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def brief(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def note(text: str) -> None:
    print(f"       {text}", flush=True)


# --------------------------------------------------------------------------- #
# HTTP（只用 urllib.request）
# --------------------------------------------------------------------------- #

class HttpOutcome:
    def __init__(self, status: int, body: Any, headers: Dict[str, str], error: str = "") -> None:
        self.status = status
        self.body = body if isinstance(body, dict) else {"data": body}
        self.headers = headers
        self.error = error

    def get(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)

    def __repr__(self) -> str:  # 便于失败时打印
        return f"HTTP {self.status} {brief(self.body, 160)}"


def http(method: str, path: str, *, body: Any = None, raw: Optional[bytes] = None,
         token: Optional[str] = TOKEN, headers: Optional[Dict[str, str]] = None,
         timeout: float = 15.0, content_type: str = "application/json") -> HttpOutcome:
    url = BASE_URL + path
    data = raw if raw is not None else (json.dumps(body, ensure_ascii=False).encode("utf-8")
                                        if body is not None else None)
    head: Dict[str, str] = {}
    if token:
        head["X-Device-Token"] = token
    if data is not None:
        head["Content-Type"] = content_type
    head.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=head, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            parsed: Any = {}
            if payload:
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = {"raw": payload[:200].decode("utf-8", "replace")}
            return HttpOutcome(response.status, parsed, {k.lower(): v for k, v in response.headers.items()})
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = {"raw": text[:400]}
        return HttpOutcome(exc.code, parsed, {k.lower(): v for k, v in (exc.headers or {}).items()})
    except Exception as exc:  # noqa: BLE001 - 断连注入时就是这条路径
        return HttpOutcome(0, {}, {}, f"{type(exc).__name__}: {exc}")


def http_raw(method: str, path: str, *, token: Optional[str] = TOKEN,
             headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> Tuple[int, bytes, Dict[str, str]]:
    """下载专用：返回原始字节（不做 JSON 解析）。"""
    head: Dict[str, str] = {}
    if token:
        head["X-Device-Token"] = token
    head.update(headers or {})
    request = urllib.request.Request(BASE_URL + path, headers=head, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()}


# --------------------------------------------------------------------------- #
# 手写 WebSocket 客户端（服务端也是手写的，两端独立实现才能互相验证）
# --------------------------------------------------------------------------- #

class WsClient:
    def __init__(self, base_url: str, device_id: str, token: str, timeout: float = 8.0) -> None:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = (f"/ws/devices/{urllib.parse.quote(device_id)}"
                f"?deviceToken={urllib.parse.quote(token)}&demoSessionId={SESSION_ID}&bootId={BOOT_ID}")
        self.conn = http_client.HTTPConnection(host, port, timeout=timeout)
        self.conn.request("GET", path, headers={
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
        })
        response = self.conn.getresponse()
        if response.status != 101:
            raise RuntimeError(f"WS 升级失败：HTTP {response.status} {response.read()[:200]!r}")
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        actual = response.getheader("Sec-WebSocket-Accept") or ""
        if actual != expected:
            raise RuntimeError(f"Sec-WebSocket-Accept 不对：expected={expected} actual={actual}")
        self.sock = self.conn.sock
        assert self.sock is not None
        self.sock.settimeout(timeout)
        self.buffer = bytearray()

    # ---- 帧 ----

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])                      # FIN + text
        mask_bit = 0x80                                 # 客户端必须掩码
        if len(payload) < 126:
            header.append(mask_bit | len(payload))
        elif len(payload) < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", len(payload))
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", len(payload))
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj: Dict[str, Any]) -> None:
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def _read_exact(self, need: int, deadline: float) -> bytes:
        while len(self.buffer) < need:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 WS 帧超时")
            ready, _, _ = select.select([self.sock], [], [], remaining)
            if not ready:
                raise TimeoutError("等待 WS 帧超时")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS 对端已关闭")
            self.buffer += chunk
        data = bytes(self.buffer[:need])
        del self.buffer[:need]
        return data

    def recv_frame(self, timeout: float = 5.0) -> Tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        b0, b1 = self._read_exact(2, deadline)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
        mask = self._read_exact(4, deadline) if (b1 & 0x80) else None
        payload = self._read_exact(length, deadline) if length else b""
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload

    def recv_json(self, timeout: float = 5.0) -> Dict[str, Any]:
        """收下一条 JSON 消息；遇到 ping 自动回 pong，遇到 close 抛异常。"""
        deadline = time.monotonic() + timeout
        while True:
            left = max(0.05, deadline - time.monotonic())
            opcode, payload = self.recv_frame(left)
            if opcode == 0x9:                     # ping → pong（带掩码）
                mask = os.urandom(4)
                self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask
                                  + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                continue
            if opcode == 0xA:                     # pong
                continue
            if opcode == 0x8:
                raise ConnectionError("平台发来 close 帧")
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))
            continue

    def close(self) -> None:
        try:
            mask = os.urandom(4)
            body = struct.pack("!H", 1000)
            self.sock.sendall(bytes([0x88, 0x80 | len(body)]) + mask
                              + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 信封与文件工具
# --------------------------------------------------------------------------- #

_seq = 0


def envelope(type_: str, payload: Dict[str, Any], *, device_id: str = DEVICE_ID,
             boot_id: str = BOOT_ID) -> Dict[str, Any]:
    global _seq
    _seq += 1
    return {
        "schemaVersion": "1.0",
        "messageId": f"msg-{uuid.uuid4().hex[:12]}",
        "deviceId": device_id,
        "bootId": boot_id,
        "seq": _seq,
        "sentAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        "demoSessionId": SESSION_ID,
        "type": type_,
        "payload": payload,
    }


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class MockServer:
    """把模拟平台作为子进程起停。

    stdin 故意用 DEVNULL：顺带验证"非交互环境 stdin 关闭时控制台静默退出而不是崩溃"。
    """

    def __init__(self, args: List[str], data_dir: Path, verbose: bool) -> None:
        self.port = free_port()
        self.log_path = data_dir / f"mock-{self.port}.log"
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"        # 子进程日志里有中文，别让编码把日志搞崩
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_PATH), "--port", str(self.port),
             "--host", "127.0.0.1", "--data-dir", str(data_dir / f"data-{self.port}"),
             "--device-token", TOKEN, "--verbose", *args],
            stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT, env=env,
        )
        self.base_url = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.base_url + "/api/health", timeout=2.0) as response:
                    if response.status == 200:
                        return True
            except Exception:  # noqa: BLE001 - 还没起来
                time.sleep(0.2)
        return False

    def log_tail(self, lines: int = 30) -> str:
        try:
            if not self.log_file.closed:
                self.log_file.flush()
            content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return ""
        return "\n".join(content[-lines:])

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_file.close()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


# --------------------------------------------------------------------------- #
# 第一轮：正常行为
# --------------------------------------------------------------------------- #

def phase_normal() -> None:
    print("\n=== 第一轮：正常行为 ===", flush=True)

    # ---- 1. 注册 ----
    register_payload = {
        "schemaVersion": "1.0",
        "deviceId": DEVICE_ID,
        "bootId": BOOT_ID,
        "appVersion": "2.0.0-demo",
        "adapterVersion": "2.0.0-demo",
        "modelVersion": "DEMO-M02",
        "operatorId": "rao",
        "host": {"hostname": "raspberrypi", "machine": "aarch64", "platform": "Linux-6.6", "python": "3.11.0"},
        "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "capabilities": {"camera": "live", "radar": "replay", "imu": "unavailable",
                         "battery": "unavailable", "telemetry": "live"},
        "capabilityReasons": {"imu": "没有 IMU 硬件", "battery": "没有电量计接口"},
    }
    registered = http("POST", "/api/devices/register", body=register_payload)
    note(f"→ {brief(registered.body)}")
    check("1a 设备注册成功（200，返回心跳参数与配置版本）",
          registered.status == 200
          and registered.get("deviceId") == DEVICE_ID
          and registered.get("heartbeatIntervalMs") == 5000
          and registered.get("offlineAfterMs") == 15000
          and registered.get("configVersion") == "CFG-02"
          and registered.get("demoSessionId") == SESSION_ID
          and bool(registered.get("registeredAt")) and bool(registered.get("platformTime")),
          f"status={registered.status}")
    wrong = http("POST", "/api/devices/register", body=register_payload, token="wrong-token")
    note(f"→ 错误令牌 {brief(wrong.body)}")
    check("1b 令牌错误必须 401（不能因为设备号对就放行）",
          wrong.status == 401 and wrong.get("code") == "UNAUTHORIZED",
          f"status={wrong.status} code={wrong.get('code')}")

    # ---- 2. 设备 WebSocket：hello → 上行遥测 → 平台能看到 ----
    ws = WsClient(BASE_URL, DEVICE_ID, TOKEN)
    hello = ws.recv_json(timeout=6.0)
    note(f"→ hello {brief(hello)}")
    check("2a WS 握手成功并收到 hello（含心跳参数）",
          hello.get("kind") == "hello" and hello.get("deviceId") == DEVICE_ID
          and hello.get("heartbeatIntervalMs") == 5000,
          f"kind={hello.get('kind')}")

    telemetry = envelope("device.telemetry", {
        "sourceMode": "live", "sampleWindowMs": 1000, "cpuPercent": 37.5,
        "processCpuPercent": 18.2, "processRssBytes": 96_000_000,
        "socTempC": 48.6, "socTempSource": "vcgencmd",
        "network": {"interface": "wlan0", "address": "192.168.1.42", "linkUp": True,
                    "txBytesPerSec": 42000, "rxBytesPerSec": 18000, "isWireless": True},
        "camera": {"backend": "v4l2", "device": "/dev/video0", "captureFps": 24.8,
                   "displayFps": 15.0, "droppedFrames": 0, "lastFrameAgeMs": 80, "state": "ok"},
        "replay": {"scenarioId": "rescan-demo-v1", "batchId": "scan-z04-002", "frameIndex": 12,
                   "frameCount": 240, "playbackFps": 1.0, "datasetHash": "sha256:demo",
                   "sourceMode": "replay"},
        "upload": {"queued": 0, "pendingBytes": 0, "confirmedBytes": 0, "activeFile": None,
                   "lastError": None},
        "versions": {"appVersion": "2.0.0-demo", "adapterVersion": "2.0.0-demo",
                     "controllerVersion": "esp32-demo-0.9", "demoModelVersion": "DEMO-M02",
                     "configVersion": "CFG-02"},
        "uptimeSeconds": 128.5,
    })
    ws.send_json(telemetry)
    time.sleep(0.6)
    state = http("GET", f"/api/devices/{DEVICE_ID}/state")
    got_cpu = ((state.get("lastTelemetry") or {}).get("payload") or {}).get("cpuPercent")
    note(f"→ state.connectionState={state.get('connectionState')} lastTelemetry.cpuPercent={got_cpu}")
    check("2b WS 上行遥测后，GET /state 能看到这条遥测且设备在线",
          state.status == 200 and got_cpu == 37.5 and state.get("connectionState") == "online"
          and state.get("bootId") == BOOT_ID,
          f"cpuPercent={got_cpu} state={state.get('connectionState')}")

    # ---- 3. 分片上传：3 片 + 重复片幂等 + complete ----
    content = bytes((i * 7 + 13) % 256 for i in range(300))
    digest = sha256_hex(content)
    # 故意不带 deviceId：终端 platform_client 也不带，平台应按令牌/唯一设备推断出来
    created = http("POST", "/api/files/uploads", body={
        "name": "frames.csv", "size": len(content), "sha256": digest,
        "batchId": "scan-z04-002", "role": "radar", "schemaVersion": "1.0",
    })
    note(f"→ 创建上传 {brief(created.body)}")
    upload_id = created.get("uploadId")
    check("3a 创建上传返回 uploadId 与 receivedOffset（无 deviceId 也能推断出设备）",
          created.status in (200, 201) and bool(upload_id) and created.get("receivedOffset") == 0
          and created.get("size") == 300,
          f"status={created.status} uploadId={upload_id}")

    offsets_ok = True
    for index in range(3):
        offset = index * 100
        chunk = content[offset:offset + 100]
        # 用 X-Chunk-Offset 头（终端 platform_client 的写法）
        result = http("PUT", f"/api/files/uploads/{upload_id}/chunks", raw=chunk,
                      headers={"X-Chunk-Offset": str(offset), "X-Chunk-Sha256": sha256_hex(chunk)},
                      content_type="application/octet-stream")
        offsets_ok = offsets_ok and result.status == 200 and result.get("receivedOffset") == offset + 100
    note(f"→ 三片写完 receivedOffset={result.get('receivedOffset')}")
    # 重复发第 2 片：用 ?offset= 查询参数（另一种写法），应幂等返回且不重复写
    duplicate_chunk = content[100:200]
    dup = http("PUT", f"/api/files/uploads/{upload_id}/chunks?offset=100", raw=duplicate_chunk,
               headers={"X-Chunk-Sha256": sha256_hex(duplicate_chunk)},
               content_type="application/octet-stream")
    note(f"→ 重复第 2 片 {brief(dup.body)}")
    completed = http("POST", f"/api/files/uploads/{upload_id}/complete", body={"sha256": digest, "size": 300})
    note(f"→ complete {brief(completed.body)}")
    check("3b 分片上传 3 片、重复片幂等、complete 摘要一致",
          offsets_ok and dup.status == 200 and dup.get("duplicated") is True
          and dup.get("receivedOffset") == 300
          and completed.status == 200 and completed.get("sha256") == digest
          and completed.get("bytesConfirmed") == 300 and bool(completed.get("fileId")),
          f"duplicated={dup.get('duplicated')} bytesConfirmed={completed.get('bytesConfirmed')} "
          f"sha256={str(completed.get('sha256'))[:16]}…")
    good_file_id = completed.get("fileId")

    # ---- 3c. 同摘要复用：平台侧已经有一份同摘要文件时直接给 fileId，不重复落盘 ----
    reused = http("POST", "/api/files/uploads", body={
        "name": "frames.csv", "size": len(content), "sha256": digest,
        "batchId": "scan-z04-002", "role": "radar", "schemaVersion": "1.0",
    })
    note(f"→ 同摘要再建一次 {brief({k: reused.get(k) for k in ('uploadId', 'completed', 'reused', 'fileId', 'receivedOffset')})}")
    check("3c 同摘要已存在时直接返回已完成（不重复落盘，仍建立本次业务关联）",
          reused.get("completed") is True and reused.get("reused") is True
          and reused.get("fileId") == good_file_id and reused.get("receivedOffset") == 300,
          f"completed={reused.get('completed')} fileId={reused.get('fileId')}")

    # ---- 4. 摘要不一致必须 422 ----
    # 注意：这里必须用一份"平台还没见过"的内容——如果声明的摘要在平台已存在，
    # 平台会按 §9.2 的复用规则直接返回已完成，根本不会走摘要比对（见 3c）。
    content2 = bytes((i * 13 + 29) % 256 for i in range(300))
    digest2 = sha256_hex(content2)
    bad_content = bytearray(content2)
    bad_content[123] ^= 0xFF
    bad = http("POST", "/api/files/uploads", body={
        "name": "segments.json", "size": len(content2), "sha256": digest2,   # 声明的是好文件的摘要
        "batchId": "scan-z04-002", "role": "manifest", "schemaVersion": "1.0",
    })
    bad_id = bad.get("uploadId")
    for index in range(3):
        offset = index * 100
        http("PUT", f"/api/files/uploads/{bad_id}/chunks?offset={offset}",
             raw=bytes(bad_content[offset:offset + 100]),
             content_type="application/octet-stream")
    bad_done = http("POST", f"/api/files/uploads/{bad_id}/complete", body={"sha256": digest2, "size": 300})
    note(f"→ complete（第 124 字节被改坏）{brief(bad_done.body)}")
    check("4 故意改坏 1 字节后 complete 必须 422，并返回 expected/actual",
          bad_done.status == 422 and bad_done.get("code") == "CHECKSUM_MISMATCH"
          and bad_done.get("expected") == digest2
          and bad_done.get("actual") not in (None, digest2),
          f"status={bad_done.status} code={bad_done.get('code')} "
          f"expected={str(bad_done.get('expected'))[:12]}… actual={str(bad_done.get('actual'))[:12]}…")

    # ---- 5. 批次部分接收 ----
    marks = json.dumps({"batchId": "scan-z04-002", "marks": []}).encode("utf-8")
    marks_sha = sha256_hex(marks)
    up2 = http("POST", "/api/files/uploads", body={
        "name": "marks.json", "size": len(marks), "sha256": marks_sha,
        "batchId": "scan-z04-002", "role": "marks", "schemaVersion": "1.0"})
    http("PUT", f"/api/files/uploads/{up2.get('uploadId')}/chunks?offset=0", raw=marks,
         content_type="application/octet-stream")
    done2 = http("POST", f"/api/files/uploads/{up2.get('uploadId')}/complete", body={})

    # fileIds 用终端 app.submit_batch 的真实形状：对象数组（带 path/role/sha256/bytes）
    file_entries = [
        {"fileId": good_file_id, "role": "frames", "path": "frames.csv",
         "sha256": digest, "bytes": len(content)},
        {"fileId": done2.get("fileId"), "role": "marks", "path": "marks.json",
         "sha256": marks_sha, "bytes": len(marks)},
    ]
    # datasetHash 与终端 storage.commit_manifest 同算法：按 path 字典序拼 path\0sha256\n
    dataset_hasher = hashlib.sha256()
    for path, sha in sorted((item["path"], item["sha256"]) for item in file_entries):
        dataset_hasher.update(path.encode("utf-8") + b"\0" + sha.encode("utf-8") + b"\n")
    dataset_hash = dataset_hasher.hexdigest()

    batch_body = {
        "batchId": "scan-z04-002",
        "projectId": "site-demo", "orderId": "SH-2026-0901", "componentId": "Z04",
        "zoneId": "Z04-lower", "configVersion": "CFG-02", "modelVersion": "DEMO-M02",
        "scenarioId": "rescan-demo-v1", "format": "response-sequence-v1",
        "radarSourceMode": "replay", "cameraSourceMode": "live", "pairing": "unverified",
        "datasetHash": dataset_hash,
        "files": [
            {"role": "frames", "path": "frames.csv", "bytes": len(content), "sha256": digest},
            {"role": "marks", "path": "marks.json", "bytes": len(marks), "sha256": marks_sha},
            {"role": "quality", "path": "quality.json", "bytes": 128, "sha256": "0" * 64},
        ],
        "fileIds": file_entries,
    }
    batch = http("POST", "/api/batches", body=batch_body)
    missing_names = [item.get("name") for item in (batch.get("missing") or [])]
    note(f"→ {brief({k: batch.get(k) for k in ('batchId', 'accepted', 'complete', 'batchState', 'missing', 'datasetHashMatch')})}")
    check("5 少交一个文件时 complete=false 且 missing 列出缺的文件（fileIds 用对象数组形状）",
          batch.status == 200 and batch.get("accepted") is True and batch.get("complete") is False
          and missing_names == ["quality.json"] and batch.get("batchState") == "partial"
          and batch.get("datasetHashMatch") is True,
          f"complete={batch.get('complete')} missing={missing_names} "
          f"datasetHashMatch={batch.get('datasetHashMatch')}")

    # 5b. datasetHash 改一位 → 平台重算后必须报不一致（不拦截，但要说出来）
    # 换个 batchId：同一个 batchId 重复提交且内容不同会被 409 拦下（那是设计如此）
    tampered = dict(batch_body)
    tampered["batchId"] = "scan-z04-002-hashcheck"
    tampered["datasetHash"] = dataset_hash[:-1] + ("0" if dataset_hash[-1] != "0" else "1")
    tampered_result = http("POST", "/api/batches", body=tampered)
    note(f"→ 改一位 datasetHash {brief({k: tampered_result.get(k) for k in ('datasetHashMatch', 'actualDatasetHash')})}")
    check("5b datasetHash 不一致时平台报出重算结果（缺件/被改过必须看得见）",
          tampered_result.get("datasetHashMatch") is False
          and tampered_result.get("actualDatasetHash") == dataset_hash,
          f"match={tampered_result.get('datasetHashMatch')} "
          f"actual={str(tampered_result.get('actualDatasetHash'))[:16]}…")

    # ---- 6. 断线补传去重 ----
    events = [
        envelope("capture.anomaly", {"batchId": "scan-z04-002", "zoneId": "Z04-lower",
                                     "kind": "possible_moisture", "confidence": 0.71}),
        envelope("capture.mark_created", {"batchId": "scan-z04-002", "markId": "mark-0001",
                                          "operatorLabel": "右侧", "positionSource": "operator_tag"}),
    ]
    for index, item in enumerate(events):
        item["seq"] = 900 + index
    first = http("POST", "/api/device-events/batch", body={"events": events})
    note(f"→ 第一次 {brief(first.body)}")
    second = http("POST", "/api/device-events/batch", body={"events": events})
    note(f"→ 第二次 {brief(second.body)}")
    check("6 同一批事件补传两次：第一次全部入库，第二次全部命中 duplicated",
          first.status == 200 and first.get("acceptedCount") == 2 and first.get("duplicatedCount") == 0
          and second.status == 200 and second.get("acceptedCount") == 0
          and second.get("duplicatedCount") == 2
          and sorted(second.get("duplicated") or []) == sorted(e["messageId"] for e in events),
          f"first={first.get('acceptedCount')}/{first.get('duplicatedCount')} "
          f"second={second.get('acceptedCount')}/{second.get('duplicatedCount')}")

    # ---- 7. 下发命令 → WS 收到 → 回执 executed ----
    issued = http("POST", f"/api/devices/{DEVICE_ID}/commands", body={
        "type": "pause_capture", "targetBatchId": "scan-z04-002",
        "expectedTaskRevision": 3, "payload": {"reason": "初扫疑似异常", "scope": "local_capture"},
    })
    note(f"→ 下发 {brief(issued.body)}")
    command_id = issued.get("commandId")
    received = ws.recv_json(timeout=6.0)
    note(f"→ WS 收到 {brief(received)}")
    ok_ws = (received.get("kind") == "command" and received.get("type") == "pause_capture"
             and received.get("commandId") == command_id
             and received.get("targetBatchId") == "scan-z04-002"
             and bool(received.get("expiresAt")))
    # 设备回执：先 accepted 再 executed（PRD §8.1 三态）
    ws.send_json(envelope("command.accepted", {"commandId": command_id, "state": "accepted",
                                               "scope": "local_capture"}))
    time.sleep(0.2)
    ws.send_json(envelope("command.executed", {"commandId": command_id, "state": "executed",
                                               "scope": "local_capture",
                                               "note": "本地采集/回放已停止推进"}))
    time.sleep(0.6)
    state2 = http("GET", f"/api/devices/{DEVICE_ID}/state")
    commands = {item.get("commandId"): item for item in (state2.get("commands") or [])}
    receipt = (commands.get(command_id) or {}).get("receipt") or {}
    note(f"→ state 里该命令的 receipt={brief(receipt)}")
    check("7 平台下发 pause_capture → WS 收到命令 → 回执 executed 被平台记录（平台不自动标成功）",
          issued.status == 202 and bool(command_id) and ok_ws
          and receipt.get("state") == "executed",
          f"status={issued.status} commandId={command_id} receipt.state={receipt.get('state')}")

    # ---- 8（附加）. 下载真实字节 + Range + X-File-Sha256 ----
    dl_status, dl_bytes, dl_headers = http_raw("GET", f"/api/files/{good_file_id}/download")
    rg_status, rg_bytes, rg_headers = http_raw("GET", f"/api/files/{good_file_id}/download",
                                               headers={"Range": "bytes=10-19"})
    note(f"→ 全量 HTTP{dl_status} {len(dl_bytes)}B sha头={str(dl_headers.get('x-file-sha256'))[:16]}… ; "
         f"Range 10-19 HTTP{rg_status} {len(rg_bytes)}B")
    check("8（附加）下载返回真实字节、带 X-File-Sha256 头、支持 Range",
          dl_status == 200 and dl_bytes == content and dl_headers.get("x-file-sha256") == digest
          and rg_status == 206 and rg_headers.get("x-file-sha256") == digest
          and rg_bytes == content[10:20],
          f"status={dl_status}/{rg_status} 全量={len(dl_bytes)}B range={len(rg_bytes)}B")

    # ---- 9（附加）. 产物清单 + 回执幂等 ----
    artifact = request_artifact()
    manifest = http("GET", f"/api/artifacts/{artifact['artifactId']}/manifest")
    note(f"→ manifest {brief({k: manifest.get(k) for k in ('artifactId', 'artifactKind', 'demoOnly', 'version', 'sha256', 'downloadUrl')})}")
    receipt_body = {
        "deviceId": DEVICE_ID, "commandId": artifact["commandId"], "sha256": artifact["sha256"],
        "downloadVerified": True, "applyResult": "applied", "versionReadBack": artifact["version"],
        "previousVersion": "DEMO-M02", "steps": [{"step": "download", "ok": True},
                                                 {"step": "verify", "ok": True},
                                                 {"step": "switch", "ok": True}],
    }
    r1 = http("POST", f"/api/artifacts/{artifact['artifactId']}/receipts", body=receipt_body)
    r2 = http("POST", f"/api/artifacts/{artifact['artifactId']}/receipts", body=receipt_body)
    note(f"→ 回执第一次 receiptId={r1.get('receiptId')}；第二次 receiptId={r2.get('receiptId')} replayed={r2.get('replayed')}")
    check("9（附加）产物清单含 demoOnly/artifactKind/downloadUrl；同 commandId 回执幂等",
          manifest.status == 200 and manifest.get("demoOnly") is True
          and manifest.get("artifactKind") == "demo_nonflashable"
          and str(manifest.get("downloadUrl") or "").startswith("/api/files/")
          and bool(manifest.get("sha256"))
          and r1.status == 201 and bool(r1.get("receiptId"))
          and r2.get("receiptId") == r1.get("receiptId") and r2.get("replayed") is True,
          f"artifactKind={manifest.get('artifactKind')} receiptId={r1.get('receiptId')}")

    # ---- 10（附加）. 预览图与白名单拒绝 ----
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
    preview = http("POST", f"/api/devices/{DEVICE_ID}/preview?batchId=scan-z04-002", raw=jpeg,
                   headers={"X-Frame-Index": "12"}, content_type="application/octet-stream")
    not_jpeg = http("POST", f"/api/devices/{DEVICE_ID}/preview", raw=b"not-a-jpeg",
                    headers={"X-Frame-Index": "13"}, content_type="application/octet-stream")
    bad_cmd = http("POST", f"/api/devices/{DEVICE_ID}/commands", body={"type": "run_shell"})
    note(f"→ 预览 {brief(preview.body)}；非 JPEG {brief(not_jpeg.body)}；非白名单命令 {brief(bad_cmd.body)}")
    check("10（附加）预览图入库但不归档；非 JPEG 422；非白名单命令 422",
          preview.status == 202 and preview.get("frameIndex") == 12 and preview.get("bytes") == len(jpeg)
          and not_jpeg.status == 422 and not_jpeg.get("code") == "NOT_JPEG"
          and bad_cmd.status == 422 and bad_cmd.get("code") == "UNSUPPORTED_COMMAND",
          f"preview={preview.status} notJpeg={not_jpeg.status} badCmd={bad_cmd.status}")

    ws.close()


def request_artifact() -> Dict[str, Any]:
    """让模拟平台生成一个演示更新包并下发 prepare_update，再读回产物信息。

    模拟平台没有真平台的"发布流程"，但 `prepare_update` 的 payload 里没有 artifactId 时
    会自动生成一个演示包（见 mock_platform.PlatformCore.issue_command），
    所以这里一条命令就能把"下载→校验→切换→回执"整条链路跑起来。
    """
    issued = http("POST", f"/api/devices/{DEVICE_ID}/commands", body={
        "type": "prepare_update", "payload": {"note": "冒烟测试触发演示包生成"}})
    state = http("GET", f"/api/devices/{DEVICE_ID}/state")
    commands = {item.get("commandId"): item for item in (state.get("commands") or [])}
    payload = (commands.get(issued.get("commandId")) or {}).get("payload") or {}
    if not payload.get("artifactId"):
        raise RuntimeError(f"平台没有为 prepare_update 生成演示包：{brief(issued.body)}")
    return {"artifactId": payload["artifactId"], "commandId": issued.get("commandId"),
            "sha256": payload.get("sha256"), "version": payload.get("version"),
            "downloadUrl": payload.get("downloadUrl")}


# --------------------------------------------------------------------------- #
# 第二轮：故障注入
# --------------------------------------------------------------------------- #

def phase_faults(server: MockServer) -> None:
    print("\n=== 第二轮：故障注入（--fail-register / --latency-ms / "
          "--drop-after-bytes / --reject-checksum）===", flush=True)
    body = {
        "schemaVersion": "1.0", "deviceId": DEVICE_ID, "bootId": "boot-fault-0001",
        "appVersion": "2.0.0-demo", "capabilities": {"camera": "live", "radar": "replay"},
    }
    rejected = http("POST", "/api/devices/register", body=body)
    note(f"→ register {brief(rejected.body)}")
    check("F1 --fail-register：注册返回 503 且 retryable=true（终端应退避重试）",
          rejected.status == 503 and rejected.get("retryable") is True,
          f"status={rejected.status} code={rejected.get('code')} retryable={rejected.get('retryable')}")

    started = time.monotonic()
    http("GET", "/api/health", body=None)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    check("F2 --latency-ms 120：每个请求真的多花掉这段延迟",
          elapsed_ms >= 110.0, f"实测 {elapsed_ms:.0f}ms（要求 ≥110ms）")

    # drop-after-bytes=150：4 片 × 50 字节，第 4 片时连接被掐断，已接收 150 字节
    payload = bytes((i * 11 + 5) % 256 for i in range(200))
    digest = sha256_hex(payload)
    created = http("POST", "/api/files/uploads", body={
        "deviceId": DEVICE_ID, "name": "partial.bin", "size": 200, "sha256": digest,
        "batchId": "scan-z04-002", "role": "radar"})
    upload_id = created.get("uploadId")
    outcomes: List[str] = []
    for index in range(4):
        offset = index * 50
        result = http("PUT", f"/api/files/uploads/{upload_id}/chunks?offset={offset}",
                      raw=payload[offset:offset + 50], content_type="application/octet-stream")
        outcomes.append(f"offset={offset}:HTTP{result.status}{' ' + result.error if result.error else ''}")
        if result.status != 200:
            break
    note("→ 分片结果 " + " | ".join(outcomes))
    resumed = http("POST", "/api/files/uploads", body={
        "deviceId": DEVICE_ID, "name": "partial.bin", "size": 200, "sha256": digest,
        "batchId": "scan-z04-002", "role": "radar"})
    note(f"→ 重新创建上传 → receivedOffset={resumed.get('receivedOffset')}（断点续传点位）")
    check("F3 --drop-after-bytes 150：第 4 片被掐断，重建上传后 receivedOffset=150",
          uploaded_ok(outcomes) and resumed.get("receivedOffset") == 150,
          f"断在第 {'/'.join(o.split(':')[0] for o in outcomes[-1:])} 片，receivedOffset={resumed.get('receivedOffset')}")

    # reject-checksum：另起一个小文件（100B < 150，不会被掐），complete 必然 422
    small = b"woodpulse-demo-payload-" * 4
    small_sha = sha256_hex(small)
    up = http("POST", "/api/files/uploads", body={
        "deviceId": DEVICE_ID, "name": "integrity.bin", "size": len(small), "sha256": small_sha,
        "batchId": "scan-z04-002", "role": "report"})
    http("PUT", f"/api/files/uploads/{up.get('uploadId')}/chunks?offset=0", raw=small,
         content_type="application/octet-stream")
    corrupt = http("POST", f"/api/files/uploads/{up.get('uploadId')}/complete", body={})
    note(f"→ complete（服务端注入损坏）{brief(corrupt.body)}")
    check("F4 --reject-checksum：服务端翻转 1 字节后 complete 返回 422（expected≠actual）",
          corrupt.status == 422 and corrupt.get("code") == "CHECKSUM_MISMATCH"
          and corrupt.get("expected") == small_sha and corrupt.get("actual") != small_sha,
          f"status={corrupt.status} expected={str(corrupt.get('expected'))[:12]}… "
          f"actual={str(corrupt.get('actual'))[:12]}…")


def uploaded_ok(outcomes: List[str]) -> bool:
    """断言"前三片成功、第四片断掉"这个形状。"""
    if len(outcomes) != 4:
        return False
    return (outcomes[0].endswith("HTTP200") and outcomes[1].endswith("HTTP200")
            and outcomes[2].endswith("HTTP200") and not outcomes[3].endswith("HTTP200"))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    global BASE_URL
    parser = argparse.ArgumentParser(description="模拟平台端到端冒烟测试")
    parser.add_argument("--base-url", default="",
                        help="测一个已经启动的模拟平台；不给就自动起停（推荐）")
    parser.add_argument("--data-dir", default="", help="临时数据目录（默认系统临时目录）")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录便于排查")
    parser.add_argument("--show-server-log", action="store_true", help="打印模拟平台的真实日志")
    parser.add_argument("--no-faults", action="store_true", help="不跑第二轮故障注入")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="wp-mock-smoke-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"模拟平台冒烟测试 · 数据目录 {data_dir}")

    servers: List[MockServer] = []
    try:
        if args.base_url:
            BASE_URL = args.base_url.rstrip("/")
            print(f"使用已启动的实例：{BASE_URL}")
            phase_normal()
        else:
            print("\n--- 启动模拟平台（正常行为）---")
            normal = MockServer([], data_dir, verbose=True)
            servers.append(normal)
            if not normal.wait_ready():
                check("0 模拟平台启动", False, f"起不来，日志尾部：\n{normal.log_tail(20)}")
                return 1
            print(f"模拟平台已就绪：{normal.base_url}")
            BASE_URL = normal.base_url
            phase_normal()
            # 控制台在 stdin=DEVNULL 下应静默退出，进程要还活着
            check("11 非交互环境（stdin=DEVNULL）下调试控制台静默退出，服务器继续运行",
                  normal.alive, f"子进程存活={normal.alive}")
            if args.show_server_log:
                print("\n--- 模拟平台日志（正常行为，尾部 40 行）---")
                print(normal.log_tail(40))
            normal.stop()

        if not args.no_faults:
            print("\n--- 启动模拟平台（故障注入）---")
            faulty = MockServer(["--fail-register", "--reject-checksum",
                                 "--drop-after-bytes", "150", "--latency-ms", "120"], data_dir, verbose=True)
            servers.append(faulty)
            if not faulty.wait_ready():
                check("F0 故障注入实例启动", False, f"起不来，日志尾部：\n{faulty.log_tail(20)}")
            else:
                BASE_URL = faulty.base_url
                phase_faults(faulty)
                if args.show_server_log:
                    print("\n--- 模拟平台日志（故障注入，尾部 40 行）---")
                    print(faulty.log_tail(40))
                faulty.stop()
    finally:
        for server in servers:
            server.stop()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 72)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 72)
    print(f"合计 {len(RESULTS)} 项：PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}")
    if failed:
        print("\n未通过项：")
        for name, _, detail in failed:
            print(f"  · {name}  — {detail}")
        # 失败时把服务端日志尾部带出来，省得再去翻
        for server in servers:
            tail = server.log_tail(25)
            if tail:
                print(f"\n--- 服务端日志尾部（端口 {server.port}）---\n{tail}")
    if not failed and servers and not args.show_server_log:
        print("\n（加 --show-server-log 可以看到模拟平台逐行的真实收发日志）")
    if args.keep_data or failed:
        print(f"数据目录保留在：{data_dir}")
    elif not args.data_dir:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
