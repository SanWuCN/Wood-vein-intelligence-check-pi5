"""主窗口：800×480 触摸屏外壳（PRD §4.3、§5、§10）。

布局固定为三段：
    顶部 44px  StatusStrip   当前柱、测区、平台连接、设备健康摘要
    主体 344px 页面堆栈      检测作业 / 参考样本 / 环境配置 / 数据交付 / 更新管理 / 设备状态
    底部 68px  导航 + 折叠日志

线程模型（PRD §10）：
  · GUI 线程只更新界面，跑 100ms 定时器推进采集与刷新；
  · 平台客户端在自己的后台线程里跑事件循环，通过回调 → Qt 信号回到 GUI；
  · 自检、上传提交、更新下载这类会阻塞的动作交给 `TaskRunner`（QThread）执行。

这样"界面线程不跑阻塞网络、不跑文件摘要"这条就落实到了代码里，
现场按按钮时不会出现整屏卡住。
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from ..app import WoodPulseApp
from ..contracts import Command, ConnectionState, TaskState
from ..logging_setup import LogLine
from ..telemetry import format_bytes
from . import theme
from .qt import HAVE_QT, QtCore, QtGui, QtWidgets, image_from_bgr, require_qt
from .delivery_page import DeliveryPage
from .environment_page import EnvironmentPage
from .scan_page import ScanPage
from .state_view import build_snapshot, workbench_cards
from .status_page import DeviceStatusPage
from .update_page import UpdatePage
from .widgets import LogView, StatusStrip
from .workbench_page import WorkbenchPage

NAV_ITEMS = [
    ("workbench", "任务工作台"),
    ("scan", "检测作业"),
    ("environment", "环境与自检"),
    ("delivery", "数据交付"),
    ("update", "更新管理"),
    ("status", "设备状态"),
]


class TaskRunner(QtCore.QThread if HAVE_QT else object):
    """在后台线程跑一个阻塞函数，结果通过信号回到 GUI。

    自检要读盘与跑子进程、上传要发网络、更新要下载文件 —— 都不该压住界面线程。
    """

    finished_ok = QtCore.pyqtSignal(str, object) if HAVE_QT else None
    failed = QtCore.pyqtSignal(str, str) if HAVE_QT else None

    def __init__(self, label: str, fn: Callable[[], Any], parent=None) -> None:
        super().__init__(parent)
        self.label = label
        self._fn = fn

    def run(self) -> None:  # noqa: D401 - QThread 接口
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - 后台异常必须回到界面显示
            traceback.print_exc()
            if self.failed is not None:
                self.failed.emit(self.label, f"{type(exc).__name__}: {exc}")
            return
        if self.finished_ok is not None:
            self.finished_ok.emit(self.label, result)


class Shell(QtWidgets.QMainWindow if HAVE_QT else object):
    """终端主窗口。"""

    #: 内部信号：把非 GUI 线程的回调搬到 GUI 线程执行
    uiEvent = QtCore.pyqtSignal(str, object) if HAVE_QT else None

    def __init__(self, app: WoodPulseApp, parent=None) -> None:
        require_qt()
        super().__init__(parent)
        self.app = app
        self._runners: List[TaskRunner] = []
        self._last_paint = 0.0
        self._frame_counter = 0
        self._display_fps_window: List[float] = []
        self._pending_marks: List[Dict[str, Any]] = []

        app.on_ui_event = self._forward_ui_event
        if self.uiEvent is not None:
            self.uiEvent.connect(self._on_ui_event)

        self.setWindowTitle("木脉智检 · 手持检测终端（800×480）")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.status_strip = StatusStrip()
        root.addWidget(self.status_strip)

        self.stack = QtWidgets.QStackedWidget()
        self.pages: Dict[str, Any] = {
            "workbench": WorkbenchPage(),
            "scan": ScanPage(),
            "environment": EnvironmentPage(),
            "delivery": DeliveryPage(),
            "update": UpdatePage(),
            "status": DeviceStatusPage(),
        }
        for key, _label in NAV_ITEMS:
            self.stack.addWidget(self.pages[key])
        root.addWidget(self.stack, 1)

        footer = QtWidgets.QFrame()
        footer.setObjectName("Footer")
        footer.setFixedHeight(theme.FOOTER_H)
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(8, 6, 8, 6)
        footer_layout.setSpacing(6)

        self.nav_buttons: Dict[str, Any] = {}
        for key, label in NAV_ITEMS:
            button = theme.make_button(label, kind="tab", checkable=True)
            button.setMinimumHeight(52)
            button.clicked.connect(lambda _=False, k=key: self.show_page(k))
            footer_layout.addWidget(button)
            self.nav_buttons[key] = button

        self.log_view = LogView()
        self.log_view.setFixedWidth(96)
        footer_layout.addWidget(self.log_view)
        root.addWidget(footer)

        self._wire_signals()
        self._apply_geometry()
        self.show_page("workbench")

        # 日志：把已有的历史一次性灌进去，之后逐条追加
        for line in self.app.recent_logs(200):
            self.log_view.append_line(line)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start()

        self.slow_timer = QtCore.QTimer(self)
        self.slow_timer.setInterval(2000)
        self.slow_timer.timeout.connect(self._refresh_everything)
        self.slow_timer.start()

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def _apply_geometry(self) -> None:
        width = self.app.cfg.ui.width or theme.SCREEN_W
        height = self.app.cfg.ui.height or theme.SCREEN_H
        if self.app.cfg.ui.fullscreen:
            self.setFixedSize(width, height)
            self.showFullScreen()
        else:
            self.resize(width, height)
            self.setFixedSize(width, height)

    def _wire_signals(self) -> None:
        workbench: WorkbenchPage = self.pages["workbench"]
        workbench.prepareRequested.connect(self._on_prepare)
        workbench.selfCheckRequested.connect(self._on_self_check)

        scan: ScanPage = self.pages["scan"]
        scan.startRequested.connect(self._on_start)
        scan.pauseRequested.connect(self._on_pause)
        scan.resumeRequested.connect(self._on_resume)
        scan.finishRequested.connect(self._on_finish)
        scan.markRequested.connect(self._on_mark)

        environment: EnvironmentPage = self.pages["environment"]
        environment.confirmRequested.connect(self._on_confirm_config)
        environment.selfCheckRequested.connect(self._on_self_check)

        delivery: DeliveryPage = self.pages["delivery"]
        delivery.uploadRequested.connect(self._on_upload)
        delivery.submitRequested.connect(self._on_submit)
        delivery.verifyRequested.connect(self._on_verify_batch)

        update: UpdatePage = self.pages["update"]
        update.downloadRequested.connect(lambda: self._run_task("下载更新包", self._do_update_download))
        update.verifyRequested.connect(lambda: self._run_task("校验更新包", self._do_update_verify))
        update.applyRequested.connect(lambda: self._run_task("切换并回验", self._do_update_apply))
        update.receiptRequested.connect(lambda: self._run_task("提交更新回执", self._do_update_receipt))

        status: DeviceStatusPage = self.pages["status"]
        status.diagnosticsRequested.connect(self._on_diagnostics)

    # ------------------------------------------------------------------ #
    # 页面切换
    # ------------------------------------------------------------------ #

    def show_page(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == key)
        self._refresh_page(key)
        self.log_view.append_line(
            LogLine(seq=self.app.log_handler.last_seq, at=time.strftime("%H:%M:%S"), level="DEBUG", source="ui", text=f"切换到页面 {key}")
        )

    def _refresh_page(self, key: str) -> None:
        snapshot = build_snapshot(self.app)
        if key == "workbench":
            self.pages["workbench"].update_view(snapshot, workbench_cards(snapshot))
        elif key == "environment":
            active = self.app.app.config
            config_payload = active.to_dict() if active is not None else {}
            state = self.app.app.config_machine.state if active is not None else "none"
            self.pages["environment"].update_config(config_payload, state=state, diffs=snapshot.get("configDiff") or [])
            self.pages["environment"].update_telemetry(snapshot.get("telemetry") or {})
            if snapshot.get("selfCheck"):
                self.pages["environment"].update_self_check(snapshot["selfCheck"])
            self.pages["environment"].set_hh_note(
                "平衡含水率估计（Hailwood-Horrobin）是环境先验，不能当成木柱内部实测含水率；"
                "风速只作采集稳定性记录，不代入 HH 公式。"
            )
        elif key == "delivery":
            batch = snapshot.get("batch")
            batch_id = (batch or {}).get("batchId") or ""
            files = self.app.storage.files_for_batch(batch_id) if batch_id else []
            integrity = self.app.storage.batch_integrity(batch_id) if batch_id else None
            result = None
            if batch_id:
                result = _read_json(self.app.storage.batch_dir(batch_id) / "result.json")
            self.pages["delivery"].show_batch(
                batch,
                files=files,
                integrity=integrity,
                result=result,
                upload_summary=snapshot.get("upload") or {},
                delivery=snapshot.get("delivery"),
            )
            self.pages["delivery"].update_history(self.app.storage.list_batches(20))
        elif key == "update":
            self.pages["update"].update_view(snapshot.get("update"), snapshot)
        elif key == "status":
            self.pages["status"].update_telemetry(snapshot.get("telemetry") or {}, snapshot)
        elif key == "scan":
            self.pages["scan"].update_summary(snapshot)
            self.pages["scan"].update_buttons(
                (snapshot.get("task") or {}).get("state", TaskState.IDLE),
                camera_live=(snapshot.get("capabilityFlags") or {}).get("cameraLive", False),
            )

    def _refresh_everything(self) -> None:
        snapshot = build_snapshot(self.app)
        self.status_strip.update_from(snapshot)
        self.pages["scan"].update_summary(snapshot)
        self.pages["scan"].update_buttons(
            (snapshot.get("task") or {}).get("state", TaskState.IDLE),
            camera_live=(snapshot.get("capabilityFlags") or {}).get("cameraLive", False),
        )
        current = self.stack.currentWidget()
        if current is self.pages["workbench"]:
            self.pages["workbench"].update_view(snapshot, workbench_cards(snapshot))
        elif current is self.pages["status"]:
            self.pages["status"].update_telemetry(snapshot.get("telemetry") or {}, snapshot)
        elif current is self.pages["environment"]:
            self.pages["environment"].update_telemetry(snapshot.get("telemetry") or {})

    # ------------------------------------------------------------------ #
    # 定时推进
    # ------------------------------------------------------------------ #

    def _on_tick(self) -> None:
        started = time.monotonic()
        tick = self.app.tick(0.1)
        self._update_camera_preview()
        if tick.frames_added:
            scan: ScanPage = self.pages["scan"]
            paused = self.app.app.task.state == TaskState.PAUSED
            scan.set_frames(self.app.capture.ring if self.app.capture else [], paused=paused)
            self._frame_counter += 1
        elapsed = time.monotonic() - started
        if elapsed > 0.2:
            # 单次 tick 超过 200ms 说明有东西卡住了，记一笔便于现场定位
            self.log_view.append_line(
                LogLine(
                    seq=self.app.log_handler.last_seq,
                    at=time.strftime("%H:%M:%S"),
                    level="WARNING",
                    source="ui",
                    text=f"刷新耗时 {elapsed * 1000:.0f} ms，界面可能感到卡顿",
                )
            )

    def _update_camera_preview(self) -> None:
        scan: ScanPage = self.pages["scan"]
        camera = self.app.camera
        if camera is None or not camera.available:
            reason = camera.stats.reason if camera else "未启用相机适配器"
            scan.set_camera_unavailable(reason)
            return
        frame = camera.latest()
        if frame is None:
            scan.set_camera_unavailable("等待第一帧画面")
            return
        # 预览按比例缩到 240 宽（16:9），只做等比缩放，不拉伸
        from ..adapters.camera import scale_bgr

        width, height, payload = scale_bgr(frame, theme.CAMERA_W)
        image = image_from_bgr(payload, width, height)
        scan.set_camera_image(
            image,
            paused=self.app.app.task.state == TaskState.PAUSED,
            info={"sourceMode": "live", "frameIndex": frame.index},
        )
        camera.note_display()

    # ------------------------------------------------------------------ #
    # 事件桥
    # ------------------------------------------------------------------ #

    def _forward_ui_event(self, topic: str, payload: Any) -> None:
        """从任意线程调用：转成 Qt 信号，保证在 GUI 线程处理。"""
        if self.uiEvent is not None:
            self.uiEvent.emit(topic, payload)

    def _on_ui_event(self, topic: str, payload: Any) -> None:
        if topic == "log":
            line = payload if isinstance(payload, LogLine) else None
            if line is not None:
                self.log_view.append_line(line)
            return
        if topic == "capture.frames":
            return  # 由 _on_tick 统一刷新，避免每帧重复重绘
        if topic == "mark.added" and isinstance(payload, dict):
            self.pages["scan"].add_mark(payload)
            return
        if topic == "task.prepared" and isinstance(payload, dict):
            self.pages["scan"].begin_batch(payload)
            return
        if topic == "config.received":
            self.show_page("environment")
            return
        if topic == "selfcheck.done":
            self.pages["environment"].update_self_check(payload if isinstance(payload, dict) else {})
            return
        if topic in ("upload.progress", "upload.done"):
            self._refresh_page("delivery")
            return
        if topic == "platform.state":
            self.status_strip.update_from(build_snapshot(self.app))
            return

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #

    def _run_task(self, label: str, fn: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None) -> None:
        runner = TaskRunner(label, fn, self)
        runner.finished_ok.connect(lambda name, result: self._on_task_done(name, result, on_done))
        runner.failed.connect(self._on_task_failed)
        runner.finished.connect(lambda: self._runners.remove(runner) if runner in self._runners else None)
        self._runners.append(runner)
        self.log_view.append_line(
            LogLine(seq=self.app.log_handler.last_seq, at=time.strftime("%H:%M:%S"), level="INFO", source="ui", text=f"开始后台任务：{label}")
        )
        runner.start()

    def _on_task_done(self, label: str, result: Any, on_done: Optional[Callable[[Any], None]]) -> None:
        self.log_view.append_line(
            LogLine(seq=self.app.log_handler.last_seq, at=time.strftime("%H:%M:%S"), level="INFO", source="ui", text=f"后台任务完成：{label}")
        )
        if on_done:
            on_done(result)
        self._refresh_everything()

    def _on_task_failed(self, label: str, message: str) -> None:
        self.log_view.append_line(
            LogLine(seq=self.app.log_handler.last_seq, at=time.strftime("%H:%M:%S"), level="ERROR", source="ui", text=f"{label} 失败：{message}")
        )
        self._toast(f"{label} 失败", message, error=True)

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #

    def _on_prepare(self, round_name: str) -> None:
        ok, message = self.app.prepare_task(round_name=round_name)
        if not ok:
            self._toast("无法准备批次", message, error=True)
            return
        self.show_page("scan")
        self._toast("批次已就绪", f"{self.app.app.batch.batch_id}（{round_name}）")

    def _on_start(self) -> None:
        ok, message = self.app.start_capture()
        if not ok:
            self._toast("无法开始", message, error=True)
        self._refresh_page("scan")

    def _on_pause(self) -> None:
        ok, message = self.app.pause_capture()
        if not ok:
            self._toast("无法暂停", message, error=True)
        self._refresh_page("scan")

    def _on_resume(self) -> None:
        ok, message = self.app.resume_capture()
        if not ok:
            self._toast("无法继续", message, error=True)
        self._refresh_page("scan")

    def _on_finish(self) -> None:
        ok, manifest = self.app.finish_capture()
        if not ok:
            self._toast("结束失败", str(manifest.get("error") or "批次封存失败"), error=True)
            return
        batch = self.app.app.batch
        self._toast(
            "本次扫描已结束",
            f"{batch.batch_id}：{batch.frames_returned} 帧、{batch.mark_count} 个标记；"
            f"manifest 已提交，datasetHash {batch.dataset_hash[:12]}…",
        )
        self.show_page("delivery")

    def _on_mark(self, operator_label: str) -> None:
        result = self.app.add_mark(operator_label)
        if not result.ok:
            self._toast("标记未保存", result.error or "当前状态不允许标记", error=True)
            return
        # 短暂"已标记"反馈，不打断扫描，也不弹需要确认的对话框
        assert result.mark is not None
        suffix = "" if result.mark.image_ok else "（缺图）"
        self._toast("已标记", f"{result.mark.display_label} 帧 {result.mark.frame_index}{suffix}", duration_ms=1200)
        self._refresh_page("scan")

    def _on_confirm_config(self) -> None:
        ok, message = self.app.confirm_config()
        if not ok:
            self._toast("确认失败", message, error=True)
            return
        self._toast("配置已生效", f"{message}，ack 已回传平台")
        self._refresh_page("environment")

    def _on_self_check(self) -> None:
        self._run_task("开机自检", self.app.run_self_check, on_done=lambda report: self._show_self_check_report(report))

    def _show_self_check_report(self, report: Any) -> None:
        summary = report.summary if hasattr(report, "summary") else {}
        blockers = report.blockers if hasattr(report, "blockers") else []
        self._toast(
            "自检完成",
            f"正常 {summary.get('ok', 0)} · 注意 {summary.get('warn', 0)} · 故障 {summary.get('fail', 0)} · "
            f"未接入 {summary.get('unavailable', 0)}"
            + (f"；需关注：{'、'.join(item.label for item in blockers)}" if blockers else ""),
            duration_ms=3500,
            error=any(item.state == "fail" for item in blockers),
        )
        self.show_page("environment")

    def _on_upload(self, batch_id: str) -> None:
        if not batch_id:
            self._toast("没有可上传的批次", "请先完成一次采集", error=True)
            return
        jobs = self.app.upload_files(batch_id)
        self._toast("已加入上传队列", f"{len(jobs)} 个文件；断网会自动续传")
        self._refresh_page("delivery")

    def _on_submit(self, batch_id: str) -> None:
        if not batch_id:
            return
        self._run_task("提交批次清单", lambda: self.app.submit_batch(batch_id), on_done=lambda result: self._show_submit_result(result))

    def _show_submit_result(self, result: Any) -> None:
        ok, payload = result if isinstance(result, tuple) else (False, {"error": "未知返回"})
        if not ok:
            self._toast("提交失败", str(payload.get("error") or "平台未接受"), error=True)
        else:
            self._toast(
                "平台已接收",
                ("完整接收" if payload.get("complete") else "部分接收")
                + (f"；缺少 {'、'.join(payload.get('missing') or [])}" if payload.get("missing") else "")
                + "（平台已接收 ≠ 平台分析完成）",
            )
        self.show_page("delivery")

    def _on_verify_batch(self, batch_id: str) -> None:
        if not batch_id:
            return
        integrity = self.app.storage.batch_integrity(batch_id)
        files = self.app.storage.files_for_batch(batch_id)
        mismatched = []
        for item in files:
            path = self.app.storage.batch_dir(batch_id) / item["rel_path"]
            if not path.is_file():
                mismatched.append(f"{item['rel_path']} 缺失")
                continue
            from ..contracts import hash_sample_file

            actual = hash_sample_file(str(path))
            if item["sha256"] and actual != item["sha256"]:
                mismatched.append(f"{item['rel_path']} 摘要不一致")
        if not mismatched and integrity.get("complete"):
            self._toast("本地校验通过", f"{len(files)} 个文件摘要一致，必需文件齐全")
        else:
            self._toast("本地校验发现问题", "；".join(mismatched or integrity.get("missingRequired") or ["文件不完整"]), error=True)
        self._refresh_page("delivery")

    def _on_diagnostics(self) -> None:
        def collect() -> Dict[str, Any]:
            snapshot = build_snapshot(self.app)
            return {
                "generatedAt": snapshot.get("generatedAt"),
                "status": snapshot,
                "logs": [line.to_dict() for line in self.app.recent_logs(300)],
                "batches": self.app.storage.list_batches(20),
                "receipts": self.app.storage.recent_receipts(20),
                "packageStatus": self.app.library.status(),
            }

        def save(_result: Dict[str, Any]) -> None:
            target = self.app.cfg.logs_path / f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.json"
            import json

            target.write_text(json.dumps(_result, ensure_ascii=False, indent=2), encoding="utf-8")
            self._toast("诊断包已生成", str(target))

        self._run_task("生成诊断包", collect, on_done=save)

    # ---- 更新 ----

    def _do_update_download(self) -> Any:
        service = self.app.update_service
        if service is None:
            raise RuntimeError("本机未启用更新服务")
        return service.download()

    def _do_update_verify(self) -> Any:
        service = self.app.update_service
        if service is None:
            raise RuntimeError("本机未启用更新服务")
        return service.verify()

    def _do_update_apply(self) -> Any:
        service = self.app.update_service
        if service is None:
            raise RuntimeError("本机未启用更新服务")
        return service.apply(restart_hook=self._restart_software_tasks)

    def _do_update_receipt(self) -> Any:
        service = self.app.update_service
        if service is None:
            raise RuntimeError("本机未启用更新服务")
        return service.send_receipt()

    def _restart_software_tasks(self) -> Any:
        """"重启对应软件任务"的真实动作：重建回放会话与采集批次绑定。

        这里不假装重启了硬件：只重启终端里的软件任务（样例回放引擎与批次绑定），
        并在回执里写清楚作用范围。
        """
        if self.app.app.task.state in (TaskState.RUNNING, TaskState.PAUSED):
            return False, "当前仍在采集，按规则不允许切换模型版本"
        before = self.app.app.batch.batch_id if self.app.app.batch else "（无批次）"
        self.app.app.task.reset(TaskState.IDLE)
        return True, f"已重置本地采集任务（原批次 {before}）与样例回放会话"

    # ------------------------------------------------------------------ #
    # 提示
    # ------------------------------------------------------------------ #

    def _toast(self, title: str, detail: str = "", *, error: bool = False, duration_ms: int = 2600) -> None:
        """轻量提示条。不遮住主画面，也不需要点确认（PRD §15：弹窗不遮住现场控件）。"""
        panel = QtWidgets.QFrame(self)
        panel.setObjectName("Panel")
        panel.setStyleSheet(
            f"QFrame#Panel {{ background-color: {theme.PANEL}; border: 1px solid "
            f"{theme.RED if error else theme.DIVIDER}; border-radius: 3px; }}"
        )
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        title_label = QtWidgets.QLabel(title)
        title_label.setStyleSheet(
            f"color: {theme.RED if error else theme.GREEN}; font-size: {theme.FONT_STRONG}px; background: transparent;"
        )
        layout.addWidget(title_label)
        if detail:
            detail_label = QtWidgets.QLabel(detail)
            detail_label.setWordWrap(True)
            detail_label.setStyleSheet(f"color: {theme.INK}; background: transparent;")
            layout.addWidget(detail_label)
        panel.setFixedWidth(min(520, max(320, len(title) * 24 + 40)))
        panel.adjustSize()
        panel.move(max(6, (self.width() - panel.width()) // 2), theme.HEADER_H + 8)
        panel.show()
        panel.raise_()
        QtCore.QTimer.singleShot(duration_ms, panel.deleteLater)


def _read_json(path) -> Optional[Dict[str, Any]]:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
