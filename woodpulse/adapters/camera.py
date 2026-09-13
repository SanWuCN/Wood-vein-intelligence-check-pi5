"""相机适配器（PRD §2、§10）。

旧版问题：`CameraThread` 用无限循环读 0 号摄像头，没有正常停止与 release；
跨线程传 QImage 时图像内存所有权不清；预缩放后又 `setScaledContents` 拉伸改变比例。
这一版对应处理：

  · **单一采集源**：整个程序只有一个采集线程打开设备，显示、落盘、预览都从它取帧，
    不会出现多个页面反复打开同一个 /dev/video*（PRD §10）。
  · **明确的停止流程**：`stop()` 置退出标志 → 唤醒 → 等线程结束 → release，
    退出时不再有悬挂的捕获句柄。
  · **帧对象带生命周期**：帧是一块 `bytes`（BGR 原始缓冲）+ 宽高 + 单调时间，
    发出前已经定型，谁持有都不会被下一次采集改写。
  · **失败显式化**：打开失败/读帧失败按退避重试，状态与原因上报遥测，
    界面显示真实故障，不返回一张假画面。
  · **无相机可用也不妨碍其它功能**：能力声明为 unavailable，
    响应序列与样例回放照常工作（PRD §5.2 自检逐项报告）。

本模块**不导入 Qt**：帧以 `CameraFrame` 传给上层，由 UI 层决定怎么显示。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("camera")

#: 相机内部状态
STATE_CLOSED = "closed"
STATE_OPENING = "opening"
STATE_STREAMING = "streaming"
STATE_RETRYING = "retrying"
STATE_FAILED = "failed"
STATE_UNAVAILABLE = "unavailable"

STATE_LABEL = {
    STATE_CLOSED: "未启动",
    STATE_OPENING: "打开中",
    STATE_STREAMING: "采集中",
    STATE_RETRYING: "重连中",
    STATE_FAILED: "故障",
    STATE_UNAVAILABLE: "未接入",
}

try:
    import cv2  # type: ignore

    HAS_CV2 = True
except Exception:  # noqa: BLE001 - 没装 OpenCV 也要能跑（相机标记为未接入）
    cv2 = None  # type: ignore
    HAS_CV2 = False

try:
    from picamera2 import Picamera2  # type: ignore

    HAS_PICAMERA2 = True
except Exception:  # noqa: BLE001
    Picamera2 = None  # type: ignore
    HAS_PICAMERA2 = False


@dataclass
class CameraFrame:
    """一帧实拍画面。

    `bgr` 是原始 BGR 字节（宽 × 高 × 3），发出后不再被采集线程改写，
    因此跨线程传递是安全的（旧版 QImage 共享内存的问题在这里消失）。
    """

    index: int
    width: int
    height: int
    bgr: bytes
    monotonic: float
    captured_at: str = ""

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.monotonic) * 1000.0

    def stats(self) -> Dict[str, Any]:
        return {"index": self.index, "width": self.width, "height": self.height, "ageMs": round(self.age_ms, 1)}


@dataclass
class CameraStats:
    """相机运行统计（PRD §6：采集帧率与显示帧率分开）。"""

    state: str = STATE_CLOSED
    backend: str = ""
    device: str = ""
    width: int = 0
    height: int = 0
    capture_fps: float = 0.0
    display_fps: float = 0.0
    delivered_frames: int = 0
    dropped_frames: int = 0
    read_errors: int = 0
    reconnect_count: int = 0
    last_frame_age_ms: Optional[float] = None
    reason: str = ""
    started_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend or None,
            "device": self.device or None,
            "width": self.width or None,
            "height": self.height or None,
            "captureFps": round(self.capture_fps, 1) if self.capture_fps else None,
            "displayFps": round(self.display_fps, 1) if self.display_fps else None,
            "droppedFrames": self.dropped_frames,
            "readErrors": self.read_errors,
            "reconnects": self.reconnect_count,
            "lastFrameAgeMs": self.last_frame_age_ms,
            "state": self.state,
            "stateLabel": STATE_LABEL.get(self.state, self.state),
            "reason": self.reason,
            "sourceMode": "live" if self.state == STATE_STREAMING else "unavailable",
        }


class CameraSource:
    """单一相机采集源。

    用法：
        cam = CameraSource(cfg.camera, on_state=...)   # on_state 是普通回调（UI 再桥成信号）
        cam.start()
        frame = cam.latest()          # 非阻塞取最新帧
        cam.note_display()            # 告诉它"这一帧真的显示出去了"，用于算显示帧率
        cam.stop()
    """

    def __init__(
        self,
        config,
        *,
        on_state: Optional[Callable[[CameraStats], None]] = None,
        buffer_size: int = 4,
    ) -> None:
        self.config = config
        self._on_state = on_state
        self._buffer: Deque[CameraFrame] = deque(maxlen=max(1, buffer_size))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._capture = None
        self._picam = None
        self._stats = CameraStats(backend=config.backend, device=config.device)
        self._frame_counter = 0
        self._read_times: Deque[float] = deque(maxlen=60)
        self._display_times: Deque[float] = deque(maxlen=60)
        self._backoff = max(0.5, float(config.retry_interval_s))
        self._latest: Optional[CameraFrame] = None
        self._capacity_warned = False

    # ---- 生命周期 ----

    def start(self) -> bool:
        """启动采集线程。返回是否成功起了线程（不代表相机已经出图）。"""
        if self._thread and self._thread.is_alive():
            return True
        if self.config.backend == "none":
            self._set_state(STATE_UNAVAILABLE, "配置指定 camera.backend=none，本机不采集相机画面")
            return False
        if self.config.backend == "v4l2" and not HAS_CV2:
            self._set_state(STATE_UNAVAILABLE, "未安装 OpenCV（python3-opencv），无法通过 V4L2 采集")
            return False
        if self.config.backend == "picamera2" and not HAS_PICAMERA2:
            self._set_state(STATE_UNAVAILABLE, "未安装 picamera2，无法使用 Pi 官方相机栈")
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="camera-capture", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """正常停止：请求退出 → 等线程结束 → release（旧版缺的就是这一步）。"""
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning("相机线程在 %.1fs 内没有退出，仍会 release 设备但可能丢弃最后一帧", timeout)
        self._release()
        self._thread = None
        self._set_state(STATE_CLOSED, "已停止并释放相机")

    def _release(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            try:
                capture.release()
            except Exception:  # noqa: BLE001
                pass
        picam, self._picam = self._picam, None
        if picam is not None:
            try:
                picam.stop()
                picam.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 采集循环 ----

    def _run(self) -> None:
        while not self._stop_event.is_set():
            opened = self._open()
            if not opened:
                if self._stop_event.wait(self._backoff):
                    break
                self._backoff = min(self.config.retry_max_interval_s, max(self.config.retry_interval_s, self._backoff * 2))
                continue

            self._backoff = max(0.5, float(self.config.retry_interval_s))
            self._set_state(STATE_STREAMING, "")
            errors = 0
            while not self._stop_event.is_set():
                frame = self._read_one()
                if frame is None:
                    errors += 1
                    self._stats.read_errors += 1
                    if errors == 1:
                        self._set_state(STATE_RETRYING, "读帧失败，正在重试；界面保留最后一帧并显示帧龄")
                    if errors >= 10:
                        log.warning("连续 %d 次读帧失败，重新打开相机设备", errors)
                        self._stats.reconnect_count += 1
                        break
                    if self._stop_event.wait(0.15):
                        break
                    continue

                errors = 0
                self._push(frame)
                # 不额外 sleep：V4L2 的 read() 自身按相机帧率阻塞，
                # 再加 sleep 会把实际帧率压到目标值以下，帧率统计就失真了。
            self._release()
            if self._stop_event.is_set():
                break
            self._set_state(STATE_RETRYING, "相机连接中断，正在重新打开")
            if self._stop_event.wait(self._backoff):
                break
            self._backoff = min(self.config.retry_max_interval_s, self._backoff * 1.5)

        self._release()
        self._set_state(STATE_CLOSED, "采集线程已退出")

    def _open(self) -> bool:
        self._set_state(STATE_OPENING, f"正在打开 {self.config.device}")
        if self.config.backend == "picamera2":
            return self._open_picamera2()
        return self._open_v4l2()

    def _open_v4l2(self) -> bool:
        if not HAS_CV2:
            self._set_state(STATE_UNAVAILABLE, "未安装 OpenCV，无法通过 V4L2 采集")
            return False
        device = self.config.device
        # 设备号写法也支持：纯数字时补成 /dev/videoN
        if str(device).isdigit():
            device = f"/dev/video{device}"
        try:
            import os

            if not os.path.exists(device):
                self._set_state(STATE_RETRYING, f"设备节点不存在：{device}（拔出或未上电）")
                return False
        except OSError:
            pass
        try:
            capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
            if not capture.isOpened():
                # 退回默认后端再试一次（有的发行版 CAP_V4L2 需要不同的打开方式）
                capture.release()
                capture = cv2.VideoCapture(device)
            if not capture.isOpened():
                capture.release()
                self._set_state(STATE_RETRYING, f"无法打开 {device}（可能被其它进程占用或权限不足）")
                return False
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            capture.set(cv2.CAP_PROP_FPS, self.config.fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 只要最新帧，减少延迟
        except Exception as exc:  # noqa: BLE001
            self._set_state(STATE_RETRYING, f"打开相机异常：{exc}")
            return False

        self._capture = capture
        self._stats.width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or self.config.width)
        self._stats.height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or self.config.height)
        log.info(
            "相机已打开：%s %dx%d（请求 %dx%d@%dfps）",
            device,
            self._stats.width,
            self._stats.height,
            self.config.width,
            self.config.height,
            self.config.fps,
        )
        return True

    def _open_picamera2(self) -> bool:
        if not HAS_PICAMERA2:
            self._set_state(STATE_UNAVAILABLE, "未安装 picamera2")
            return False
        try:
            picam = Picamera2()
            config = picam.create_preview_configuration(
                main={"size": (self.config.width, self.config.height), "format": "BGR888"},
                buffer_count=4,
            )
            picam.configure(config)
            picam.start()
        except Exception as exc:  # noqa: BLE001
            self._set_state(STATE_RETRYING, f"picamera2 启动失败：{exc}")
            return False
        self._picam = picam
        self._stats.width = self.config.width
        self._stats.height = self.config.height
        log.info("picamera2 已启动：%dx%d", self.config.width, self.config.height)
        return True

    def _read_one(self) -> Optional[CameraFrame]:
        now = time.monotonic()
        try:
            if self._picam is not None:
                import numpy as np  # type: ignore

                array = self._picam.capture_array()
                height, width = array.shape[0], array.shape[1]
                payload = np.ascontiguousarray(array).tobytes()
            elif self._capture is not None:
                ok, image = self._capture.read()
                if not ok or image is None:
                    return None
                height, width = image.shape[0], image.shape[1]
                payload = image.tobytes()
            else:
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("读帧异常：%s", exc)
            return None

        self._frame_counter += 1
        self._read_times.append(now)
        if len(self._read_times) >= 2:
            span = self._read_times[-1] - self._read_times[0]
            if span > 0:
                self._stats.capture_fps = (len(self._read_times) - 1) / span
        self._stats.width, self._stats.height = width, height
        return CameraFrame(
            index=self._frame_counter,
            width=width,
            height=height,
            bgr=payload,
            monotonic=now,
            captured_at=_iso(now),
        )

    def _push(self, frame: CameraFrame) -> None:
        with self._lock:
            if len(self._buffer) == self._buffer.maxlen:
                # 队列满了说明消费者跟不上：记一次丢帧，不假装数据完整（PRD §10）
                self._stats.dropped_frames += 1
                if not self._capacity_warned:
                    log.warning("原始采集队列已满，开始丢弃旧帧（队列长度 %s）", self._buffer.maxlen)
                    self._capacity_warned = True
            self._buffer.append(frame)
            self._latest = frame
        self._emit_state_if_changed()

    # ---- 取帧 ----

    def latest(self) -> Optional[CameraFrame]:
        """取最新一帧（不弹出队列）。UI 每帧只画最新的一张，宁可丢旧帧也不要积压。"""
        with self._lock:
            return self._latest

    def take(self) -> Optional[CameraFrame]:
        """从队列头取一帧（按顺序消费，用于落盘）。队列空时返回 None。"""
        with self._lock:
            if self._buffer:
                return self._buffer.popleft()
            return None

    def snapshot(self, wait_s: float = 0.6) -> Optional[CameraFrame]:
        """为人工标记取一张**当前**截图。

        相机没出图时最多等 wait_s；仍没有就返回 None，调用方要把标记标成"缺图"，
        不伪装成完整记录（PRD §9）。
        """
        frame = self.latest()
        if frame is not None and frame.age_ms < 1500:
            return frame
        deadline = time.monotonic() + max(0.0, wait_s)
        while time.monotonic() < deadline:
            time.sleep(0.05)
            frame = self.latest()
            if frame is not None and frame.age_ms < 500:
                return frame
        return frame

    def note_display(self) -> None:
        """显示线程每真正画出一帧就调一次，用于算显示帧率。"""
        now = time.monotonic()
        self._display_times.append(now)
        if len(self._display_times) >= 2:
            span = self._display_times[-1] - self._display_times[0]
            if span > 0:
                self._stats.display_fps = (len(self._display_times) - 1) / span

    # ---- 状态 ----

    @property
    def stats(self) -> CameraStats:
        frame = self.latest()
        if frame is not None:
            self._stats.last_frame_age_ms = round(frame.age_ms, 1)
        return self._stats

    @property
    def available(self) -> bool:
        return self._stats.state in (STATE_STREAMING, STATE_RETRYING, STATE_OPENING)

    def capability(self) -> Tuple[str, str]:
        """返回 (capability 取值, 原因)，供 AppState 声明能力（PRD §7.2）。"""
        if self._stats.state == STATE_STREAMING:
            return "live", ""
        if self._stats.state == STATE_UNAVAILABLE:
            return "unavailable", self._stats.reason or "相机不可用"
        return "unavailable", self._stats.reason or f"相机当前状态：{STATE_LABEL.get(self._stats.state, self._stats.state)}"

    def _set_state(self, state: str, reason: str = "") -> None:
        changed = state != self._stats.state or reason != self._stats.reason
        self._stats.state = state
        self._stats.reason = reason
        if state == STATE_STREAMING and self._stats.started_at is None:
            self._stats.started_at = time.monotonic()
        if changed:
            level = log.info if state in (STATE_STREAMING, STATE_CLOSED) else log.warning
            level("相机状态：%s %s", STATE_LABEL.get(state, state), f"— {reason}" if reason else "")
            self._emit_state_if_changed(force=True)

    def _emit_state_if_changed(self, force: bool = False) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(self._stats)
        except Exception:  # noqa: BLE001
            pass


def _iso(monotonic_value: float) -> str:
    from datetime import datetime, timedelta, timezone

    # 把单调时间换算成"墙钟近似值"只用于显示；真正的排序一律用单调时间
    delta = time.monotonic() - monotonic_value
    moment = datetime.now(timezone.utc) - timedelta(seconds=delta)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# 帧缩放与预览图（保持 16:9 比例，绝不拉伸）
# --------------------------------------------------------------------------- #

def scale_bgr(frame: CameraFrame, target_width: int) -> Tuple[int, int, bytes]:
    """按比例缩放到目标宽度，返回 (宽, 高, BGR 字节)。

    旧版把预缩放画面再用 setScaledContents 拉伸，比例被改掉了（PRD §2）。
    这里只做"等比缩放"，显示端只允许整数倍或等比适配。
    """
    if target_width <= 0 or frame.width <= 0:
        return frame.width, frame.height, frame.bgr
    if target_width >= frame.width:
        return frame.width, frame.height, frame.bgr
    ratio = target_width / frame.width
    target_height = max(1, int(round(frame.height * ratio)))
    if HAS_CV2:
        import numpy as np  # type: ignore

        array = np.frombuffer(frame.bgr, dtype=np.uint8).reshape((frame.height, frame.width, 3))
        resized = cv2.resize(array, (target_width, target_height), interpolation=cv2.INTER_AREA)
        return target_width, target_height, resized.tobytes()
    # 没有 OpenCV 时退化为"最近邻抽样"，只保证比例正确
    row_step = frame.height / target_height
    col_step = frame.width / target_width
    out = bytearray(target_width * target_height * 3)
    cursor = 0
    for row in range(target_height):
        source_row = min(frame.height - 1, int(row * row_step))
        base = source_row * frame.width * 3
        for col in range(target_width):
            source_col = min(frame.width - 1, int(col * col_step))
            offset = base + source_col * 3
            out[cursor : cursor + 3] = frame.bgr[offset : offset + 3]
            cursor += 3
    return target_width, target_height, bytes(out)


def encode_jpeg(frame: CameraFrame, target_width: int = 640, quality: int = 70) -> Optional[bytes]:
    """编码低帧率预览图（PRD §4.3：上传预览可另选 640×360 及 1—2FPS，不改变原始照片尺寸）。"""
    if not HAS_CV2:
        return None
    import numpy as np  # type: ignore

    width, height, payload = scale_bgr(frame, target_width)
    array = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3))
    ok, buffer = cv2.imencode(".jpg", array, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return None
    return buffer.tobytes()


def encode_png(frame: CameraFrame, target_width: int = 640) -> Optional[bytes]:
    """编码原始截图（落盘用）。没有 OpenCV 时返回 None，由调用方记录"缺图"。"""
    if not HAS_CV2:
        return None
    import numpy as np  # type: ignore

    width, height, payload = scale_bgr(frame, target_width)
    array = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3))
    ok, buffer = cv2.imencode(".png", array)
    if not ok:
        return None
    return buffer.tobytes()


def probe_devices(max_index: int = 6) -> list:
    """列出可打开的 V4L2 设备，供部署时确认实际路径（PRD §14 待确认项）。"""
    found = []
    if not HAS_CV2:
        return found
    import os

    for index in range(max_index):
        path = f"/dev/video{index}"
        if not os.path.exists(path):
            continue
        entry = {"device": path, "index": index, "openable": False, "detail": ""}
        try:
            capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
            entry["openable"] = bool(capture.isOpened())
            if entry["openable"]:
                entry["width"] = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                entry["height"] = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                entry["fps"] = float(capture.get(cv2.CAP_PROP_FPS))
            capture.release()
        except Exception as exc:  # noqa: BLE001
            entry["detail"] = str(exc)
        found.append(entry)
    return found
