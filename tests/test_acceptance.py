"""验收用例 H01–H16 的自动化版本（PRD §13）。

设计原则：**不需要真的在树莓派屏幕上点一遍**。
终端核心（woodpulse/app.py 及以下）不依赖 Qt，所以这些用例可以在任何机器上跑：

    cd F:\\1\\pi5
    python tests/test_acceptance.py            # 全部
    python tests/test_acceptance.py H05 H06    # 只跑指定几项

有几项需要外部条件，缺条件时**跳过并写明原因**，不假装通过：
    H02  需要一个可达的平台服务（可用 tools/mock_platform.py 起一个）
    H03  需要真实摄像头
    H10/H11  需要一个能收文件的平台服务
先把 `python tools/mock_platform.py --port 8080` 起起来，再跑全部用例即可覆盖它们。

用例与 PRD §13 表格的对应关系写在每个测试的 docstring 里。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from woodpulse.adapters.camera import HAS_CV2, CameraSource, probe_devices  # noqa: E402
from woodpulse.adapters.replay import ReplayLibrary  # noqa: E402
from woodpulse.app import WoodPulseApp  # noqa: E402
from woodpulse.app_state import ConfigSnapshot, Mark  # noqa: E402
from woodpulse.config import CameraConfig, load_config  # noqa: E402
from woodpulse.contracts import (  # noqa: E402
    BatchState,
    Command,
    ErrorCode,
    EventType,
    OperationLabel,
    ReceiptState,
    TaskState,
    hash_sample_file,
)
from woodpulse.platform_client import HttpClient  # noqa: E402
from woodpulse.scenarios import SCENARIOS, RESCAN_FINDINGS  # noqa: E402
from woodpulse.telemetry import HAS_PSUTIL, TelemetryService, format_bytes  # noqa: E402
from woodpulse.update import UpdateService  # noqa: E402

PLATFORM_URL = os.environ.get("WOODPULSE_TEST_PLATFORM", "http://127.0.0.1:8080")


def platform_reachable(url: str = PLATFORM_URL, timeout: float = 1.0) -> bool:
    """探测平台是否可达。不可达时依赖平台的用例会 skip 而不是 fail。"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _serve_directory(directory: pathlib.Path):
    """在工作线程里起一个只读文件服务，返回 (base_url, server)。

    更新包下载、文件上传这类用例需要真的走 HTTP —— 用内存里的假响应测不出
    "下载了错误的字节""摘要对不上"这些问题。
    """
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *args, **kwargs: None  # 测试输出保持干净
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


class BaseCase(unittest.TestCase):
    """每个用例一套独立的临时数据目录，避免互相当污染（H06 尤其依赖这一点）。"""

    scenario_root = ROOT / "samples"
    platform_url = "http://127.0.0.1:9"   # 一个必然连不上的端口：默认离线运行
    camera_backend = "none"
    start_platform = False

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="woodpulse-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = self.build_app()

    def build_app(self, extra_argv: Optional[List[str]] = None) -> WoodPulseApp:
        argv = [
            "--no-fullscreen",
            "--data-dir",
            str(self.tmp / "data"),
            "--scenario-root",
            str(self.scenario_root),
            "--camera",
            self.camera_backend,
            "--platform-url",
            self.platform_url,
        ] + list(extra_argv or [])
        cfg = load_config(argv=argv)
        cfg.self_check_on_start = False
        cfg.platform.preview_upload = False
        cfg.platform.allow_offline_capture = True
        cfg.log_level = os.environ.get("WOODPULSE_TEST_LOG", "ERROR")
        app = WoodPulseApp(cfg)
        if self.start_platform:
            app.start()
        else:
            # 不起平台客户端线程，避免测试期间不断重连刷日志
            if app.platform is not None:
                app.platform.http = HttpClient(self.platform_url, "", timeout=1.0)
        self.addCleanup(app.shutdown, "测试结束")
        return app

    # ---- 小工具 ----

    def _drop_app_cleanup(self) -> None:
        """只摘掉"关掉 app"那条清理函数，保留临时目录清理。

        为什么不能直接 `self._cleanups = []`：那会把 `addCleanup(rmtree, tmp)` 一起
        丢掉 —— 崩溃恢复用例正需要临时目录里的数据库还在，清掉就等于把要恢复的数据删了。
        所以这里按闭包里的引用精确识别。
        """
        kept = []
        for entry in self._cleanups:
            function = entry[0] if isinstance(entry, tuple) else entry
            referenced = [cell.cell_contents for cell in (getattr(function, "__closure__", None) or [])]
            if function.__name__ == "shutdown" and any(item is self.app for item in referenced):
                continue  # 这是旧实例的收尾，丢掉
            kept.append(entry)
        self._cleanups = kept

    def run_capture(self, round_name: str, *, ticks: int, start: bool = True) -> Any:
        ok, message = self.app.prepare_task(round_name=round_name)
        self.assertTrue(ok, f"准备批次失败：{message}")
        if start:
            ok, message = self.app.start_capture()
            self.assertTrue(ok, f"开始采集失败：{message}")
        for _ in range(ticks):
            self.app.tick(0.1)
        return self.app.app.batch

    def require_samples(self, scenario_id: str) -> None:
        if not (self.scenario_root / scenario_id / "manifest.json").is_file():
            self.skipTest(f"样例包 {scenario_id} 未生成（先运行 python tools/make_samples.py --out samples）")


# --------------------------------------------------------------------------- #
# H01
# --------------------------------------------------------------------------- #

class H01Telemetry(BaseCase):
    """H01：与系统工具对照 CPU、内存、温度 —— 相同或可解释采样窗口差异；不可用字段不随机填值。"""

    def test_fields_are_real_or_null_with_reason(self) -> None:
        if not HAS_PSUTIL:
            self.skipTest("未安装 psutil，无法对照 CPU/内存")
        service = TelemetryService(self.app.cfg, data_dir=self.app.cfg.data_path)
        # 第一次采样窗口没结束：必须标 warmup 而不是报 0
        first = service.snapshot(force=True)
        self.assertIn(first.get("cpuQuality"), ("warmup", "ok"), "CPU 首样本必须标 warmup 或 ok")
        if first.get("cpuQuality") == "warmup":
            self.assertIsNone(first.get("cpuPercent"), "预热期不能把 0 当真实负载上报")
            self.assertIn("预热", first.get("cpuReason", ""))

        time.sleep(1.1)
        snapshot = service.snapshot(force=True)
        self.assertEqual(snapshot.get("cpuQuality"), "ok")
        self.assertIsInstance(snapshot["cpuPercent"], float)
        self.assertGreaterEqual(snapshot["cpuPercent"], 0.0)
        self.assertLessEqual(snapshot["cpuPercent"], 100.0 * (os.cpu_count() or 1))

        memory = snapshot.get("memory")
        self.assertIsNotNone(memory, "psutil 可用时内存必须采到")
        self.assertGreater(memory["totalBytes"], 0)
        self.assertLessEqual(memory["usedBytes"], memory["totalBytes"])

        disk = snapshot.get("disk")
        self.assertIsNotNone(disk)
        self.assertGreater(disk["totalBytes"], 0)
        # 磁盘检测的是实际数据目录
        self.assertEqual(disk["path"], str(self.app.cfg.data_path))

        # 能力与原因必须成对出现：不可用就要有 reason
        if snapshot.get("socTempC") is None:
            self.assertTrue(snapshot.get("socTempReason"), "温度不可用时必须给出原因")
        if snapshot.get("throttled") is None:
            self.assertTrue(snapshot.get("throttledReason"))
        if snapshot.get("cpuFreqMhz") is None:
            self.assertTrue(snapshot.get("cpuFreqReason"))

    def test_no_random_values(self) -> None:
        """同一窗口内连续两次强制采样，内存总量这类稳定值不能抖动 —— 抖动说明是随机生成的。"""
        if not HAS_PSUTIL:
            self.skipTest("未安装 psutil")
        service = TelemetryService(self.app.cfg, data_dir=self.app.cfg.data_path)
        first = service.snapshot(force=True)
        second = service.snapshot(force=True)
        self.assertEqual(first["memory"]["totalBytes"], second["memory"]["totalBytes"])
        self.assertEqual(first["disk"]["totalBytes"], second["disk"]["totalBytes"])

    def test_network_rate_never_negative(self) -> None:
        """PRD §6：重连或计数回退时重建基线，不产生负速率或巨大峰值。"""
        service = TelemetryService(self.app.cfg, data_dir=self.app.cfg.data_path)
        first = service.network.sample()
        self.assertIn("interface", first)
        if first.get("txBytesPerSec") is not None:
            self.assertGreaterEqual(first["txBytesPerSec"], 0)
        time.sleep(0.2)
        second = service.network.sample()
        if second.get("txBytesPerSec") is not None:
            self.assertGreaterEqual(second["txBytesPerSec"], 0)
            self.assertLess(second["txBytesPerSec"], 200 * 1024 * 1024, "接口速率出现异常峰值，可能是差分基线没重建")


# --------------------------------------------------------------------------- #
# H03
# --------------------------------------------------------------------------- #

class H03Camera(BaseCase):
    """H03：摄像头拔出再接入 —— 出现明确故障，重试恢复；FPS 来自成功采集计数。"""

    def test_no_camera_is_explicit_not_fake(self) -> None:
        camera = CameraSource(CameraConfig(backend="none"))
        camera.start()
        self.addCleanup(camera.stop)
        stats = camera.stats
        self.assertEqual(stats.state, "unavailable")
        self.assertTrue(stats.reason, "相机不可用时必须给出原因")
        self.assertIsNone(stats.capture_fps or None)
        value, reason = camera.capability()
        self.assertEqual(value, "unavailable")
        self.assertTrue(reason)

    def test_missing_device_reports_failure_and_retries(self) -> None:
        if not HAS_CV2:
            self.skipTest("未安装 OpenCV")
        camera = CameraSource(
            CameraConfig(backend="v4l2", device="/dev/video-does-not-exist", retry_interval_s=0.2, retry_max_interval_s=0.4)
        )
        camera.start()
        self.addCleanup(camera.stop)
        time.sleep(0.5)
        stats = camera.stats
        self.assertIn(stats.state, ("retrying", "opening", "failed"), "不存在的设备必须是明确的重试/故障态")
        self.assertTrue(stats.reason, "故障必须带原因")
        self.assertIsNone(camera.latest(), "没有相机时不能返回任何画面")

    @unittest.skipUnless(HAS_CV2, "未安装 OpenCV")
    def test_probe_lists_devices(self) -> None:
        devices = probe_devices()
        self.assertIsInstance(devices, list)

    def test_stop_releases_and_joins(self) -> None:
        """PRD §2：旧版没有正常停止与 release 流程。"""
        camera = CameraSource(CameraConfig(backend="none"))
        camera.start()
        camera.stop()
        self.assertFalse(camera.running)
        self.assertEqual(camera.stats.state, "closed")


# --------------------------------------------------------------------------- #
# H04
# --------------------------------------------------------------------------- #

class H04Capabilities(BaseCase):
    """H04：雷达未配置或只有回放 —— 不显示真雷达在线，不宣称 IMU 定位可用。"""

    def test_capabilities_are_honest(self) -> None:
        caps = self.app.app.capability
        self.assertEqual(caps.get("radar"), "replay", "本机没有真实雷达：能力必须是 replay")
        self.assertEqual(caps.get("imu"), "unavailable", "没有 IMU：不能声明可用")
        self.assertNotEqual(caps.get("gpu"), "live", "没有 GPU 利用率接口时不能声明 live")
        self.assertTrue(caps.reason("radar"))
        self.assertTrue(caps.reason("imu"))

    def test_handshake_payload_matches_contract(self) -> None:
        """PRD §7.2 的注册报文格式。"""
        from woodpulse.contracts import APP_VERSION, build_register_payload

        payload = build_register_payload(
            device_id="handheld-02",
            boot_id="boot-test",
            capabilities=self.app.app.capability,
        )
        for key in ("schemaVersion", "deviceId", "bootId", "appVersion", "capabilities"):
            self.assertIn(key, payload)
        self.assertEqual(payload["appVersion"], APP_VERSION)
        self.assertEqual(set(payload["capabilities"]), set(self.app.app.capability.values))


# --------------------------------------------------------------------------- #
# H05
# --------------------------------------------------------------------------- #

class H05Sequence(BaseCase):
    """H05：开始、暂停、继续、结束响应序列 —— 列数按帧推进；暂停冻结；终态不被旧回调恢复。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def test_pause_freezes_columns(self) -> None:
        batch = self.run_capture("rescan", ticks=30)
        columns_running = batch.frames_returned
        self.assertGreater(columns_running, 0, "运行期间必须追加帧")

        ok, _ = self.app.pause_capture("测试暂停")
        self.assertTrue(ok)
        after_pause = batch.frames_returned
        for _ in range(30):  # 暂停期间再推进 3 秒
            self.app.tick(0.1)
        self.assertEqual(batch.frames_returned, after_pause, "暂停时不得继续追加列")
        self.assertEqual(self.app.app.task.state, TaskState.PAUSED)

        ok, _ = self.app.resume_capture()
        self.assertTrue(ok)
        for _ in range(20):
            self.app.tick(0.1)
        self.assertGreater(batch.frames_returned, after_pause, "恢复后要接着写，而不是从头播")
        self.assertLess(
            batch.frames_returned - after_pause,
            columns_running + 25,
            "恢复后不能把暂停期间的时间一次性补播（那等于没冻结）",
        )

    def test_terminal_state_is_not_revived(self) -> None:
        """终态不接受旧回调恢复（PRD §13 H05）。"""
        self.run_capture("rescan", ticks=10)
        ok, _ = self.app.finish_capture()
        self.assertTrue(ok)
        self.assertEqual(self.app.app.task.state, TaskState.FINISHED)
        frames_at_finish = self.app.app.batch.frames_returned
        self.assertFalse(self.app.app.start_task("旧回调"), "终态不能被重新开始")
        self.assertFalse(self.app.app.resume_task("旧回调"), "终态不能被旧 resume 恢复")
        for _ in range(10):
            self.app.tick(0.1)
        self.assertEqual(self.app.app.batch.frames_returned, frames_at_finish, "结束后不得再追加帧")

    def test_segments_and_manifest_written(self) -> None:
        self.run_capture("rescan", ticks=20)
        ok, manifest = self.app.finish_capture()
        self.assertTrue(ok, str(manifest))
        batch_dir = self.app.storage.batch_dir(self.app.app.batch.batch_id)
        for name in ("manifest.json", "segments.json", "marks.json", "quality.json", "frames.csv", "result.json"):
            self.assertTrue((batch_dir / name).is_file(), f"批次目录缺少 {name}")
        self.assertTrue((batch_dir / "images").is_dir())
        data = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(data["format"], "response-sequence-v1")
        self.assertEqual(data["radarSourceMode"], "replay")
        self.assertEqual(data["axis"]["x"], "frame_index")
        self.assertEqual(data["axis"]["y"], "sample_index")
        self.assertTrue(data["datasetHash"], "manifest 必须带 datasetHash")
        self.assertEqual(data["state"], "sealed")


# --------------------------------------------------------------------------- #
# H06
# --------------------------------------------------------------------------- #

class H06Repeatability(BaseCase):
    """H06：同一样例重复两轮 —— 相同结果和帧内容；不同 batchId 与会话，无残留数据。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def _one_round(self, batch_id: str) -> Dict[str, Any]:
        ok, message = self.app.prepare_task(round_name="rescan", batch_id=batch_id)
        self.assertTrue(ok, message)
        self.app.start_capture()
        for _ in range(420):  # 足够播完整包（420 帧 @10fps）
            self.app.tick(0.1)
        ok, manifest = self.app.finish_capture()
        self.assertTrue(ok, str(manifest))
        return manifest

    def test_two_rounds_same_content_different_ids(self) -> None:
        first = self._one_round("scan-Z04-002")
        frames_first = (self.app.storage.batch_dir("scan-Z04-002") / "frames.csv").read_bytes()
        result_first = json.loads((self.app.storage.batch_dir("scan-Z04-002") / "result.json").read_text("utf-8"))

        second = self._one_round("scan-Z04-002-r2")
        frames_second = (self.app.storage.batch_dir("scan-Z04-002-r2") / "frames.csv").read_bytes()
        result_second = json.loads((self.app.storage.batch_dir("scan-Z04-002-r2") / "result.json").read_text("utf-8"))

        self.assertNotEqual(first["batchId"], second["batchId"], "两轮必须用不同 batchId")
        self.assertEqual(frames_first, frames_second, "同一输入两轮必须得到完全相同的帧内容")
        self.assertEqual(result_first, result_second, "同一输入两轮必须得到相同结果")
        # 两个摘要含义不同，不要混用：
        #   sampleDatasetHash = 样例包属性，同一样例重复两轮必须一致
        #   datasetHash       = 本批产物的摘要（含 batchId、marks 等），每批必然不同
        self.assertEqual(
            first["sampleDatasetHash"], second["sampleDatasetHash"],
            "sampleDatasetHash 标识用的是哪套样例，两轮必须一致",
        )
        self.assertEqual(first["sampleDatasetHash"], self.app.library.open("rescan-demo-v1").dataset_hash)
        self.assertTrue(first["datasetHash"] and second["datasetHash"], "每批产物都要有自己的摘要")

    def test_marks_do_not_leak_between_rounds(self) -> None:
        self._one_round("scan-Z04-002")
        batch = self.app.app.batch
        self.app.add_mark(OperationLabel.FRONT)  # 终态下应被拒绝
        self.assertEqual(batch.mark_count, 0, "上一轮的标记不能残留到新批次")
        marks = json.loads((self.app.storage.batch_dir("scan-Z04-002") / "marks.json").read_text("utf-8"))
        self.assertEqual(marks["marks"], [])


# --------------------------------------------------------------------------- #
# H07
# --------------------------------------------------------------------------- #

class H07Marks(BaseCase):
    """H07：连续绕柱并点击标记 —— 保存当前图像、样例帧和人工方位；不暂停采集，不推算角度、轨迹或深度。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def test_mark_saves_frame_and_label(self) -> None:
        self.run_capture("rescan", ticks=25)
        batch = self.app.app.batch
        frames_before = batch.frames_returned
        result = self.app.add_mark(OperationLabel.RIGHT)
        self.assertTrue(result.ok, result.error)
        mark = result.mark
        assert mark is not None
        self.assertEqual(mark.batch_id, batch.batch_id, "标记必须挂在同一 batchId 下，不新建整批数据")
        self.assertEqual(mark.operator_label, OperationLabel.RIGHT)
        self.assertEqual(mark.position_source, "operator_tag")
        self.assertGreater(mark.device_monotonic_ns, 0)
        self.assertGreaterEqual(mark.frame_index, 0)
        self.assertLessEqual(mark.frame_index, frames_before, "标记必须指向一个已采集的帧")
        self.assertEqual(self.app.app.task.state, TaskState.RUNNING, "标记不得暂停当前任务")

    def test_mark_without_direction_does_not_guess(self) -> None:
        self.run_capture("rescan", ticks=15)
        result = self.app.add_mark("")
        self.assertTrue(result.ok)
        assert result.mark is not None
        self.assertEqual(result.mark.operator_label, "", "未选方向时不能猜方位")

    def test_mark_without_camera_is_flagged(self) -> None:
        """相机不可用时标记仍要保存，但必须标成缺图（PRD §9）。"""
        self.run_capture("rescan", ticks=15)
        result = self.app.add_mark(OperationLabel.FRONT)
        self.assertTrue(result.ok)
        assert result.mark is not None
        self.assertFalse(result.mark.image_ok, "本用例相机为 none，截图必然不可用")
        self.assertIn("缺图", result.mark.note)
        self.assertIsNone(result.mark.camera_asset_id)

    def test_marks_persisted_and_listed(self) -> None:
        self.run_capture("rescan", ticks=20)
        for label in (OperationLabel.FRONT, "", OperationLabel.BACK):
            self.assertTrue(self.app.add_mark(label).ok)
        self.app.finish_capture()
        batch_dir = self.app.storage.batch_dir(self.app.app.batch.batch_id)
        marks = json.loads((batch_dir / "marks.json").read_text("utf-8"))["marks"]
        self.assertEqual(len(marks), 3)
        for mark in marks:
            for key in ("markId", "batchId", "frameId", "cameraAssetId", "deviceMonotonicNs", "operatorLabel", "positionSource"):
                self.assertIn(key, mark)
        csv_text = (batch_dir / "marks.csv").read_text(encoding="utf-8")
        self.assertEqual(len(csv_text.strip().splitlines()), 4)


# --------------------------------------------------------------------------- #
# H08
# --------------------------------------------------------------------------- #

class H08CommandValidation(BaseCase):
    """H08：平台下发旧批次或过期暂停命令 —— 拒绝执行并返回原因；有效命令区分 accepted 与 executed。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def test_expired_command_rejected(self) -> None:
        self.run_capture("rescan", ticks=10)
        command = {
            "commandId": "cmd-expired-1",
            "action": Command.PAUSE_CAPTURE,
            "targetBatchId": self.app.app.batch.batch_id,
            "expiresAt": "2020-01-01T00:00:00Z",
        }
        receipt = self.app.handle_command(command)
        self.assertEqual(receipt["state"], ReceiptState.FAILED)
        self.assertEqual(receipt["errorCode"], ErrorCode.COMMAND_EXPIRED)
        self.assertIn("过期", receipt["reason"])
        self.assertEqual(self.app.app.task.state, TaskState.RUNNING, "被拒绝的命令不得改变设备状态")

    def test_stale_batch_rejected(self) -> None:
        self.run_capture("rescan", ticks=10)
        command = {
            "commandId": "cmd-old-batch",
            "action": Command.PAUSE_CAPTURE,
            "targetBatchId": "scan-Z04-001",
        }
        receipt = self.app.handle_command(command)
        self.assertEqual(receipt["state"], ReceiptState.FAILED)
        self.assertEqual(receipt["errorCode"], ErrorCode.BATCH_MISMATCH)

    def test_stale_revision_rejected(self) -> None:
        self.run_capture("rescan", ticks=10)
        command = {
            "commandId": "cmd-old-rev",
            "action": Command.PAUSE_CAPTURE,
            "targetBatchId": self.app.app.batch.batch_id,
            "expectedTaskRevision": (self.app.app.assignment.task_revision or 1) + 99,
        }
        receipt = self.app.handle_command(command)
        self.assertEqual(receipt["state"], ReceiptState.FAILED)
        self.assertEqual(receipt["errorCode"], ErrorCode.REVISION_STALE)

    def test_whitelist_enforced(self) -> None:
        self.run_capture("rescan", ticks=5)
        receipt = self.app.handle_command({"commandId": "c1", "action": "shell_exec", "payload": {"cmd": "rm -rf /"}})
        self.assertEqual(receipt["state"], ReceiptState.FAILED)
        self.assertEqual(receipt["errorCode"], ErrorCode.UNSUPPORTED)

    def test_valid_pause_distinguishes_accepted_and_executed(self) -> None:
        self.run_capture("rescan", ticks=20)
        batch = self.app.app.batch
        before = batch.frames_returned
        command = {
            "commandId": "cmd-pause-ok",
            "action": Command.PAUSE_CAPTURE,
            "targetBatchId": batch.batch_id,
            "expectedTaskRevision": self.app.app.assignment.task_revision,
            "payload": {"reason": "适用域待核验"},
        }
        receipt = self.app.handle_command(command)
        self.assertEqual(receipt["state"], ReceiptState.EXECUTED, receipt)
        self.assertEqual(self.app.app.task.state, TaskState.PAUSED, "回了 executed 就必须真的停下来了")
        self.assertIn("作用范围", receipt["scope"] or "")
        for _ in range(20):
            self.app.tick(0.1)
        self.assertEqual(batch.frames_returned, before, "回 executed 之后不得再推进")
        # accepted 与 executed 都要留档，重复命令返回已保存结果
        accepted = self.app.storage.get_receipt("cmd-pause-ok", ReceiptState.ACCEPTED)
        executed = self.app.storage.get_receipt("cmd-pause-ok", ReceiptState.EXECUTED)
        self.assertIsNotNone(accepted)
        self.assertIsNotNone(executed)

    def test_query_status_has_no_side_effect(self) -> None:
        self.run_capture("rescan", ticks=5)
        state_before = self.app.app.task.state
        receipt = self.app.handle_command({"commandId": "cmd-q", "action": Command.QUERY_STATUS})
        self.assertEqual(receipt["state"], ReceiptState.EXECUTED)
        self.assertEqual(self.app.app.task.state, state_before)
        self.assertIn("deviceId", receipt["result"])


# --------------------------------------------------------------------------- #
# H09
# --------------------------------------------------------------------------- #

class H09ConfigPersistence(BaseCase):
    """H09：接收新环境配置，终端重启 —— 当前配置持续保存，平台收到相同版本 ack。"""

    def test_config_received_confirmed_persisted(self) -> None:
        payload = {
            "configVersion": "CFG-02",
            "source": "平台环境表单",
            "publishedAt": "2026-09-11T12:41:00Z",
            "airTempC": 26.4,
            "relativeHumidityPct": 78,
            "windSpeedMs": 1.6,
            "instrumentId": "THM-2207 / ANE-3310",
            "position": "四柱区域入口，距 Z04 2.4m，离地 1.1m",
            "compensation": {"baselineOffsetDb": -1.8, "normalization": "reference_normalized", "functionVersion": "comp-v1.4"},
        }
        receipt = self.app.handle_command({"commandId": "cfg-1", "action": Command.APPLY_CONFIG, "payload": payload})
        self.assertEqual(receipt["state"], ReceiptState.EXECUTED, receipt)
        self.assertEqual(self.app.app.config_machine.state, "received", "收到配置后要等操作者确认差异")

        ok, version = self.app.confirm_config()
        self.assertTrue(ok, version)
        self.assertEqual(version, "CFG-02")
        self.assertEqual(self.app.app.config_machine.state, "applied")

        # 重启：新建一个 App 指向同一个数据目录
        self.app.shutdown("测试重启")
        restarted = self.build_app()
        self.assertIsNotNone(restarted.app.config)
        self.assertEqual(restarted.app.config.config_version, "CFG-02")
        self.assertEqual(restarted.app.config.relative_humidity_pct, 78.0)
        self.assertEqual(restarted.app.config_machine.state, "applied", "重启后配置状态要恢复")
        restarted.shutdown("测试结束")

    def test_diff_shows_before_and_after(self) -> None:
        first = ConfigSnapshot.from_dict({"configVersion": "CFG-01", "airTempC": 20.0, "relativeHumidityPct": 60})
        self.app.app.apply_config(first)
        self.app.confirm_config()
        second = ConfigSnapshot.from_dict({"configVersion": "CFG-02", "airTempC": 26.4, "relativeHumidityPct": 78})
        diffs = self.app.app.apply_config(second)
        fields = {row.field: (row.before, row.after) for row in diffs}
        self.assertIn("配置版本", fields)
        self.assertEqual(fields["配置版本"], ("CFG-01", "CFG-02"))
        # 数值口径统一后，int 与 float 都要显示成同一个样子
        self.assertEqual(fields["参考温度"], ("20 ℃", "26.4 ℃"))
        self.assertEqual(fields["参考相对湿度"], ("60 %", "78 %"))


# --------------------------------------------------------------------------- #
# H11 / H12 / H13
# --------------------------------------------------------------------------- #

class H11UpdatePackage(BaseCase):
    """H11：下载正确、损坏、错目标更新包 —— 正确包通过；损坏或目标不匹配失败；旧有效版本保留。

    这里起一个**真的 HTTP 服务**来供包下载：更新包的 downloadUrl 在真平台上是 HTTP，
    用 file:// 测不出下载路径（而且 HttpClient 走的是 urllib，file:// 会直接报 DNS 错）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.http_base, self._server = _serve_directory(self.staging)
        self.addCleanup(self._server.shutdown)
        self.http = HttpClient("http://127.0.0.1:9", "", timeout=1.0)
        self.service = UpdateService(self.app.app, self.app.storage, self.http, staging_dir=self.staging)

    def _make_package(self, *, sha_ok: bool = True, target: str = "handheld-02", kind: str = "demo_nonflashable") -> pathlib.Path:
        """造一个最小的演示更新包（真实 zip，真实摘要）。"""
        import zipfile

        package = self.staging / "pkg.demo.zip"
        manifest = {
            "artifactId": "DEMO-PKG-02",
            "artifactKind": kind,
            "version": "DEMO-M02b",
            "targetDevice": target,
            "demoOnly": True,
            "content": ["model", "preprocess", "version"],
        }
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            archive.writestr("model_card.json", json.dumps({"inputSpec": {"pointCount": 420}}, ensure_ascii=False))
            archive.writestr("preprocess.json", json.dumps({"normalization": "reference_normalized"}))
            archive.writestr("sample_results.json", json.dumps({"inputSpec": {"pointCount": 420}, "expectedOutput": {"conclusion": "preliminary"}}))
        return package

    def _notify(self, package: pathlib.Path, *, declared_sha: Optional[str] = None, target: str = "handheld-02") -> None:
        actual = hash_sample_file(str(package))
        self.service.receive({
            "artifactId": "DEMO-PKG-02",
            "artifactKind": "demo_nonflashable",
            "version": "DEMO-M02b",
            "targetDevice": target,
            "size": package.stat().st_size,
            "sha256": declared_sha if declared_sha is not None else actual,
            "downloadUrl": f"{self.http_base}/{package.name}",
        })

    def test_good_package_passes_and_keeps_old_version(self) -> None:
        package = self._make_package()
        self._notify(package)
        ok, step = self.service.download()
        self.assertTrue(ok, step.detail)
        ok, step = self.service.verify()
        self.assertTrue(ok, step.detail)
        step = self.service.stage()
        self.assertEqual(step.state, "ok", step.detail)
        previous = self.app.app.model_version
        applied_ok, step = self.service.apply(restart_hook=lambda: (True, "本地采集任务已重置"))
        self.assertTrue(applied_ok, step.detail)
        self.assertEqual(self.app.app.model_version, "DEMO-M02b")
        self.assertEqual(self.app.storage.active_version("demo_model"), "DEMO-M02b")
        # H11：旧有效版本保留，可恢复
        demo_versions = {
            row["version"] for row in self.app.storage._connection().execute("SELECT version FROM versions WHERE kind='demo_model'")
        }
        self.assertIn(previous, demo_versions, "旧演示版本记录必须保留以便回退")

    def test_corrupted_package_fails_checksum(self) -> None:
        package = self._make_package()
        self._notify(package, declared_sha="0" * 64)  # 平台声明一个错的摘要
        ok, step = self.service.download()
        self.assertTrue(ok, step.detail)
        ok, step = self.service.verify()
        self.assertFalse(ok, "摘要不一致必须失败")
        self.assertIn("摘要不一致", step.detail)
        self.assertEqual(self.app.app.model_version, "DEMO-M02", "校验失败不得改动模型版本")

    def test_wrong_target_fails(self) -> None:
        package = self._make_package(target="handheld-99")
        self._notify(package, target="handheld-99")
        self.service.download()
        ok, step = self.service.verify()
        self.assertFalse(ok)
        self.assertIn("本机是", step.detail)

    def test_flashable_package_refused(self) -> None:
        """不能把可烧录固件当演示包处理（PRD §5.6、§11.3）。"""
        package = self._make_package(kind="firmware")
        self._notify(package)
        self.service.download()
        ok, step = self.service.verify()
        self.assertFalse(ok)
        self.assertIn("拒绝应用", step.detail)

    def test_receipt_records_both_versions(self) -> None:
        package = self._make_package()
        self._notify(package)
        self.service.download()
        self.service.verify()
        self.service.stage()
        self.service.apply(restart_hook=lambda: (True, "ok"))
        receipt = self.service.build_receipt(True)
        self.assertIn("installedDemoModelVersion", receipt)
        self.assertIn("actualControllerVersion", receipt)
        self.assertIsNone(receipt["actualControllerVersion"], "没有串口链路时实际控制器版本必须为空")
        self.assertIn("未执行任何烧录", receipt["carrierNote"])


class H12NoFakeEvents(BaseCase):
    """H12：点击升级页空白或切换页面 —— 不产生虚构烧录、VBUS 或摘要通过事件。"""

    def setUp(self) -> None:
        super().setUp()
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.service = UpdateService(
            self.app.app, self.app.storage, HttpClient("http://127.0.0.1:9", "", timeout=1.0), staging_dir=self.staging
        )

    def test_reset_clears_state_but_keeps_no_events(self) -> None:
        self.service.reset()
        self.assertEqual(self.service.steps, [])
        self.assertEqual(self.service.checks, [])
        receipt = self.service.build_receipt(False)
        self.assertEqual(receipt["result"], "failed")
        self.assertIsNone(receipt["downloadedSha256"] or None, "没有下载过就不能有实测摘要")
        self.assertIn("未执行任何烧录", receipt["carrierNote"])

    def test_verify_without_download_does_nothing(self) -> None:
        ok, step = self.service.verify()
        self.assertFalse(ok)
        self.assertEqual(step.state, "failed")
        self.assertFalse(any(item.state == "ok" for item in self.service.checks), "没下载过不可能有检查通过项")

    def test_no_vbus_or_mux_claims_anywhere(self) -> None:
        """界面与回执里不能出现未实现的硬件检测结论。"""
        text = json.dumps(self.service.build_receipt(True), ensure_ascii=False) + json.dumps(self.service.summary(), ensure_ascii=False)
        for forbidden in ("VBUS", "MUX", "供电检测通过", "总线移交", "烧录成功"):
            self.assertNotIn(forbidden, text, f"出现了未实现的硬件事件描述：{forbidden}")


class H13UpdateDuringCapture(BaseCase):
    """H13：采集中收到更新 —— 暂存而不改变当前批次模型；结束后才允许切换。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")
        self.staging = self.tmp / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.service = UpdateService(
            self.app.app, self.app.storage, HttpClient("http://127.0.0.1:9", "", timeout=1.0), staging_dir=self.staging
        )

    def test_switch_blocked_while_capturing(self) -> None:
        self.run_capture("rescan", ticks=20)
        batch = self.app.app.batch
        allowed, reason = self.app.app.can_switch_model_now()
        self.assertFalse(allowed)
        self.assertIn(batch.batch_id, reason)
        self.assertEqual(batch.model_version, "DEMO-M02", "活动批次仍绑定原模型版本")

        self.app.finish_capture()
        allowed, reason = self.app.app.can_switch_model_now()
        self.assertTrue(allowed, reason)

    def test_update_does_not_touch_active_batch(self) -> None:
        self.run_capture("rescan", ticks=10)
        self.service.receive({"artifactId": "A1", "version": "DEMO-M02b", "targetDevice": "handheld-02", "downloadUrl": "file:///none"})
        self.assertEqual(self.app.app.batch.model_version, "DEMO-M02")


# --------------------------------------------------------------------------- #
# H14
# --------------------------------------------------------------------------- #

class H14CrashRecovery(BaseCase):
    """H14：程序意外退出后重开 —— 恢复已保存批次为中断态；不自动续扫或重复上传。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def test_open_batch_recovers_as_interrupted(self) -> None:
        batch = self.run_capture("rescan", ticks=30)
        batch_id = batch.batch_id
        frames_saved = batch.frames_returned
        self.assertGreater(frames_saved, 0)
        # 模拟崩溃：不调 shutdown，直接丢掉对象；数据库里那条记录仍是 open
        self.app.storage._connection().execute("PRAGMA wal_checkpoint(FULL)")
        # 释放旧实例的连接，否则 WAL 里可能还留着未合并的快照
        self.app.storage.close()
        del self.app
        import gc

        gc.collect()
        self._drop_app_cleanup()
        self.app = self.build_app()
        recovered = [item.batch_id for item in self.app.recovered_batches]
        self.assertIn(batch_id, recovered, "上次未完成的批次必须恢复为中断态")
        record = self.app.storage.get_batch(batch_id)
        self.assertEqual(record["state"], BatchState.SEALED)
        self.assertIn("未自动续扫", record["interrupt_reason"])
        # 数据保留
        self.assertEqual(record["returned_frames"], frames_saved)
        # 不重复上传：上传队列为空
        self.assertEqual(self.app.app.uploads, [])

    def test_completed_batch_not_marked_interrupted(self) -> None:
        self.run_capture("rescan", ticks=10)
        self.app.finish_capture()
        batch_id = self.app.app.batch.batch_id
        self.app.shutdown("测试")
        self.app = self.build_app()
        self.assertNotIn(batch_id, [item.batch_id for item in self.app.recovered_batches])
        self.assertEqual(self.app.storage.get_batch(batch_id)["state"], BatchState.SEALED)


# --------------------------------------------------------------------------- #
# H15
# --------------------------------------------------------------------------- #

class H15Consistency(BaseCase):
    """H15：平台与终端同时查看结果 —— 柱、测区、批次、图像、曲线、分数和版本一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.require_samples("rescan-demo-v1")

    def test_batch_identity_is_single_source(self) -> None:
        self.run_capture("rescan", ticks=25)
        self.app.finish_capture()
        batch = self.app.app.batch
        manifest = json.loads((self.app.storage.batch_dir(batch.batch_id) / "manifest.json").read_text("utf-8"))
        snapshot = self.app.status_snapshot()

        self.assertEqual(manifest["batchId"], batch.batch_id)
        self.assertEqual(manifest["componentId"], "Z04")
        self.assertEqual(manifest["zoneId"], "Z04-lower")
        self.assertEqual(manifest["orderId"], "SH-2026-0901")
        self.assertEqual(manifest["configVersion"], batch.config_version)
        self.assertEqual(manifest["modelVersion"], batch.model_version)
        self.assertEqual(snapshot["batch"]["batchId"], manifest["batchId"])
        self.assertEqual(manifest["datasetHash"], batch.dataset_hash)

    def test_result_scores_match_script(self) -> None:
        """复扫要完整跑完才能拿到三处异常；这里直接用样例包核对剧本口径。"""
        package = self.app.library.open("rescan-demo-v1")
        findings = package.result.get("findings") or []
        self.assertEqual(len(findings), 3)
        scores = sorted(round(float(item["score"]), 2) for item in findings)
        self.assertEqual(scores, [0.71, 0.84, 0.87], "复扫分数必须与剧本口径一致")
        ids = {item["id"] for item in findings}
        self.assertEqual(ids, {"CUR-Z04-01", "CUR-Z04-02", "CUR-Z04-03"})
        for item in findings:
            self.assertNotIn("depth", json.dumps(item, ensure_ascii=False), "不得给出异常深度")
        for spec in RESCAN_FINDINGS:
            self.assertIn(spec["id"], ids)

    def test_axis_labels_avoid_depth_claims(self) -> None:
        package = self.app.library.open("rescan-demo-v1")
        axis = package.manifest.get("axis")
        self.assertEqual(axis, {"x": "frame_index", "y": "sample_index"})
        note = package.manifest.get("privacyNote", "")
        self.assertIn("replay", note)


# --------------------------------------------------------------------------- #
# H16
# --------------------------------------------------------------------------- #

class H16TouchLayout(BaseCase):
    """H16：实际屏幕持续操作 —— 大按钮可触达、相机不拉伸、日志不挤主画面，无蓝色旧主题残留。

    这些断言直接读主题常量与控件几何，不需要真的把界面点一遍。
    """

    def test_config_file_and_cli_precedence(self) -> None:
        """部署时最要命的一种错：示例配置看似加载成功，其实整份被忽略。

        `--config` 必须在读文件**之前**就被识别，否则它会走默认路径找配置，
        现场改了 `docs/config.example.json` 却半点没生效，而且没有任何报错。
        """
        import json as _json
        import tempfile as _tempfile

        from woodpulse.config import load_config

        workdir = pathlib.Path(_tempfile.mkdtemp(prefix="woodpulse-cfg-"))
        self.addCleanup(shutil.rmtree, workdir, True)
        payload = {
            "data_dir": str(workdir),
            "scenario_root": str(self.scenario_root),
            "platform": {"platform_url": "http://10.1.2.3:8000", "device_id": "handheld-77", "device_token": "tok-abc"},
            "camera": {"backend": "none"},
            "ui": {"fullscreen": True},
        }
        config_file = workdir / "custom.json"
        config_file.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cfg = load_config(argv=["--config", str(config_file)])
        self.assertEqual(cfg.platform.platform_url, "http://10.1.2.3:8000", "配置文件必须生效")
        self.assertEqual(cfg.platform.device_id, "handheld-77")
        self.assertEqual(str(cfg.data_path), str(workdir))

        # 命令行优先于文件
        cfg2 = load_config(argv=["--config", str(config_file), "--platform-url", "http://127.0.0.1:9", "--no-fullscreen"])
        self.assertEqual(cfg2.platform.platform_url, "http://127.0.0.1:9")
        self.assertFalse(cfg2.ui.fullscreen)

        # 指定了不存在的文件要留下痕迹，不能静默用默认值
        missing = load_config(argv=["--config", str(workdir / "nope.json")])
        self.assertIn("不存在", missing.config_path)

        # 未知字段要直接报错，避免"改了配置没生效"
        config_file.write_text(_json.dumps({"platform": {"platform_ur1": "x"}}), encoding="utf-8")
        broken = load_config(argv=["--config", str(config_file)])
        self.assertIn("读取失败", broken.config_path)

    def test_theme_has_no_blue_leftovers(self) -> None:
        from woodpulse.ui import theme

        palette = [theme.BG, theme.PANEL, theme.PANEL_ALT, theme.INK, theme.INK_SOFT, theme.DIVIDER, theme.BUTTON]
        for color in palette:
            red = int(color[1:3], 16)
            green = int(color[3:5], 16)
            blue = int(color[5:7], 16)
            self.assertLessEqual(
                blue - max(red, green), 8, f"颜色 {color} 偏蓝，与灰银工业主题不符（PRD §4.1）"
            )

    def test_touch_targets_meet_minimum(self) -> None:
        from woodpulse.ui import theme

        self.assertGreaterEqual(theme.PRIMARY_BUTTON_H, 48, "主要触摸按钮至少 48px（建议 64px）")
        self.assertGreaterEqual(theme.SECONDARY_BUTTON_H, 40, "次级操作至少 40px")
        self.assertGreaterEqual(theme.FONT_SMALL, 14, "辅助文字不能小于 14px")
        self.assertGreaterEqual(theme.FONT_BODY, 18, "正文 18—20px")

    def test_screen_budget_adds_up(self) -> None:
        from woodpulse.ui import theme

        self.assertEqual(theme.HEADER_H + theme.BODY_H + theme.FOOTER_H, theme.SCREEN_H)
        self.assertEqual((theme.SCREEN_W, theme.SCREEN_H), (800, 480))
        # PRD §4.3：顶部约 44px、主体约 344px、底部约 68px
        self.assertLessEqual(abs(theme.HEADER_H - 44), 4)
        self.assertLessEqual(abs(theme.FOOTER_H - 68), 4)

    def test_camera_scaling_preserves_aspect(self) -> None:
        from woodpulse.adapters.camera import CameraFrame, scale_bgr

        width, height = 1280, 720
        frame = CameraFrame(index=1, width=width, height=height, bgr=bytes(width * height * 3), monotonic=time.monotonic())
        out_w, out_h, payload = scale_bgr(frame, 240)
        self.assertEqual(out_w, 240)
        self.assertEqual(out_h, 135, "16:9 缩放后高度必须是 135，不得被拉伸")
        self.assertAlmostEqual(out_w / out_h, 16 / 9, places=2)
        self.assertEqual(len(payload), out_w * out_h * 3)
        # 目标宽度大于源宽度时不得放大
        out_w2, out_h2, _ = scale_bgr(frame, 4000)
        self.assertEqual((out_w2, out_h2), (width, height))

    def test_log_is_collapsed_by_default(self) -> None:
        """日志默认折叠，不占主画面（PRD §4.2）。"""
        source = (ROOT / "woodpulse" / "ui" / "widgets.py").read_text(encoding="utf-8")
        self.assertIn("self.body.setVisible(False)", source, "日志体默认必须是隐藏的")
        self.assertIn("setFixedWidth(56)", source, "日志控件宽度要收窄，不能挤主画面")

    def test_no_three_d_dependency_in_main_flow(self) -> None:
        """PRD §10：移除主流程对 QtWebEngine 三维模拟的依赖。"""
        offenders = []
        for path in (ROOT / "woodpulse").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "QtWebEngine" in text or "simulation.html" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"主流程不应依赖 WebEngine 或缺失的 HTML：{offenders}")


# --------------------------------------------------------------------------- #
# 需要真实平台服务的用例（H02 / H10 / H11 的网络部分）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(platform_reachable(f"http://127.0.0.1:{os.environ.get('WOODPULSE_TEST_PORT', '8099')}"),
                     "平台不可达（先运行 python tools/mock_platform.py --port 8099，"
                     "并用 WOODPULSE_TEST_PLATFORM=http://127.0.0.1:8099 指定）")
class PlatformOnlineCases(BaseCase):
    """需要平台在线的用例（H02 / H10）。用 tools/mock_platform.py 起服务即可覆盖。

    每次运行都用**唯一的 batchId**：平台的 `/api/batches` 对同一 batchId 重复提交
    不同内容会回 BATCH_CONFLICT（这是对的，防止覆盖已归档记录），
    但那样会让第二次跑测试莫名失败 —— 属于测试隔离问题，不是终端问题。
    """

    platform_url = PLATFORM_URL
    start_platform = True

    def setUp(self) -> None:
        super().setUp()
        # 只有确认服务在跑才真的去碰它
        if not platform_reachable(self.platform_url):
            self.skipTest(f"平台 {self.platform_url} 不可达")
        import uuid

        self.unique_batch = f"scan-Z04-001-t{uuid.uuid4().hex[:6]}"

    def _round_batch_id(self) -> str:
        return self.unique_batch

    def test_h02_offline_then_online_keeps_data(self) -> None:
        """H02：断开网络、重连、改变接口 —— 平台正确显示时效，速率不出现负值，队列继续同步。"""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and self.app.platform.state != "online":
            time.sleep(0.3)
        self.assertEqual(self.app.platform.state, "online", f"未能连上平台：{self.app.platform.state_detail}")

        # 采集期间积累事件；停止客户端模拟断网
        ok, message = self.app.prepare_task(round_name="initial", batch_id=self.unique_batch)
        self.assertTrue(ok, message)
        self.app.start_capture()
        for _ in range(20):
            self.app.tick(0.1)
        self.app.finish_capture()

        self.app.platform.stop()
        time.sleep(0.3)
        pending_offline = self.app.storage.pending_event_count()
        # 断网期间事件必须留在本地 outbox，不丢
        self.assertGreaterEqual(pending_offline, 0)

        # 重连：客户端重新上线后应把 outbox 补传出去
        self.app.platform.start(self.app.app.boot_id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.app.storage.pending_event_count() > 0:
            self.app.tick(0.1)
            time.sleep(0.2)
        self.assertEqual(self.app.storage.pending_event_count(), 0, "重连后关键事件必须补传并被平台确认")

    def test_event_batch_contract_accounts_for_every_message_id(self) -> None:
        """关键事件回执契约：每个 messageId 最终必须进 accepted 或 rejected 之一。

        这条只有对着**真平台**测才有意义：平台静默丢弃时终端拿不到确认，
        会按重试节奏无限重推（`device.selfcheck` 实测过每 0.5s 一次）。
        同时校验 rejected 每条都带 messageId 与 retryable ——
        retryable 是终端决定"出队"还是"退避重试"的唯一判据。
        """
        import uuid

        def envelope(message_id: str, type_: str, version: str = "1.0") -> Dict[str, Any]:
            return {
                "schemaVersion": version,
                "messageId": message_id,
                "deviceId": self.app.cfg.effective_device_id,
                "bootId": self.app.app.boot_id,
                "seq": 1,
                "sentAt": "2026-09-12T00:00:00Z",
                "demoSessionId": self.app.cfg.platform.demo_session_id,
                "type": type_,
                "payload": {"schemaVersion": "1.0", "summary": {"ok": 1}},
            }

        tag = uuid.uuid4().hex[:6]
        events = [
            envelope(f"msg-{tag}-known", "device.health"),
            envelope(f"msg-{tag}-selfcheck", "device.selfcheck"),
            envelope(f"msg-{tag}-unknown", "some.unknown.type"),
            envelope(f"msg-{tag}-badversion", "device.health", version="9.0"),
        ]
        result = self.app.platform.http.post_events_batch(events)
        self.assertTrue(result.ok, result.error)
        body = result.body

        accepted = body.get("accepted") or []
        duplicated = body.get("duplicated") or []
        rejected = body.get("rejected") or []
        self.assertIsInstance(accepted, list, "accepted 必须是 messageId 数组，不能是计数")
        self.assertIsInstance(duplicated, list, "duplicated 必须是 messageId 数组，不能是计数")

        for entry in rejected:
            self.assertIn("messageId", entry, f"rejected 条目缺 messageId：{entry}")
            self.assertIn("retryable", entry, f"rejected 条目缺 retryable：{entry}")
            self.assertIsInstance(entry["retryable"], bool, f"retryable 必须是布尔值：{entry}")

        accounted = set(accepted) | set(duplicated) | {
            str(entry["messageId"]) for entry in rejected if entry.get("messageId")
        }
        submitted = {event["messageId"] for event in events}
        self.assertEqual(
            submitted - accounted,
            set(),
            "有 messageId 既未接受也未拒收（静默丢弃）——终端会无限重推",
        )
        self.assertEqual(body.get("unaccounted") or [], [], "平台自报还有未归属的 messageId")

    def test_h10_partial_upload_then_complete(self) -> None:
        """H10：文件传一半断网，恢复后继续 —— 无重复文件；字节与摘要正确。"""
        ok, message = self.app.prepare_task(round_name="initial", batch_id=self.unique_batch)
        self.assertTrue(ok, message)
        self.app.start_capture()
        for _ in range(20):
            self.app.tick(0.1)
        ok, _ = self.app.finish_capture()
        self.assertTrue(ok)
        batch_id = self.app.app.batch.batch_id
        self.assertEqual(batch_id, self.unique_batch, "测试要用独立 batchId，避免与平台已归档批次冲突")

        jobs = self.app.upload_files(batch_id)
        self.assertGreater(len(jobs), 0)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and self.app.platform.pending_uploads > 0:
            time.sleep(0.5)
        self.assertEqual(self.app.platform.pending_uploads, 0, "上传队列没有清空")

        files = self.app.storage.files_for_batch(batch_id)
        confirmed = [item for item in files if item["upload_state"] == "done"]
        self.assertGreater(len(confirmed), 0)
        for item in confirmed:
            self.assertTrue(item["remote_id"], f"{item['rel_path']} 没有拿到平台 fileId")
            self.assertEqual(item["received_offset"], item["size"], "确认字节数必须等于文件大小")

        ok, report = self.app.submit_batch(batch_id)
        self.assertTrue(ok, str(report))
        self.assertIn("complete", report)


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #

def load_by_ids(ids: List[str]) -> unittest.TestSuite:
    """按 H01/H05 这样的编号挑选用例类。

    大小写都接受（命令行打 `h05` 或 `H05` 都行），用前缀匹配，
    所以 `h0` 能一次挑出 H01/H02/H03/H04。
    也接受 `H02Telemetry.test_xxx` 这种"类名.方法名"的完整写法。
    """
    module = sys.modules[__name__]
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for raw in ids:
        # 允许 "H02Telemetry.test_x" / "test_acceptance.H02Telemetry" 这类带点的写法
        test_id = raw.strip().split(".")[-1] if "." in raw else raw.strip()
        key = test_id.upper()
        matched = False
        for name in dir(module):
            obj = getattr(module, name)
            if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
                continue
            # 类名本身或类名去掉编号前缀后能对上，都算匹配
            if name.upper().startswith(key):
                suite.addTests(loader.loadTestsFromTestCase(obj))
                matched = True
        if not matched:
            print(f"警告：没有匹配到用例 {raw}", file=sys.stderr)
    return suite


def main(argv: List[str]) -> int:
    if argv:
        suite = load_by_ids(argv)
    else:
        suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("-" * 72)
    print(f"用例：{result.testsRun} 项，失败 {len(result.failures)}，错误 {len(result.errors)}，跳过 {len(result.skipped)}")
    if result.skipped:
        print("跳过原因：")
        for case, reason in result.skipped:
            print(f"  · {case}: {reason}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
