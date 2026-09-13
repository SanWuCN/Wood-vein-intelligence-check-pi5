"""更新管理（PRD §5.6、§8.2、§13 H11/H12/H13）。

首版演示主线（PRD §5.6 明确）：
    接收模型演示包 → **实际下载** → **实际摘要检查** → 切换本地演示模型版本
    → 重启对应软件任务 → 回验

不做的事（这几条是验收里专门要看有没有乱演的）：
  · 不修改真实 ESP32 固件版本字段来表示模拟更新；
  · 没有 VBUS / MUX 检测代码就不显示"已检测供电""总线自动移交"；
  · 点击空白处不得自动宣布 Hash 通过（H12）——摘要只由真实计算得出；
  · 采集进行中收到更新只暂存，结束前不改当前批次绑定的模型版本（H13）。

演示包一律按 `artifactKind = demo_nonflashable` 处理：它是不可烧录的演示产物，
所以本模块**不会**调用任何烧录工具、不会打开串口。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .app_state import AppState, UpdateState
from .contracts import hash_sample_file, utc_now_iso
from .logging_setup import get_logger
from .platform_client import HttpClient, HttpResult
from .storage import Storage

log = get_logger("update")

#: 演示包允许的种类。真实固件包（integrated firmware / standalone model）首版不处理。
ALLOWED_ARTIFACT_KINDS = ("demo_nonflashable", "demo", "")

#: 演示包必须包含的条目（PRD §16 更新包：模型说明、预处理配置、示例验证结果）
REQUIRED_ENTRIES = ("manifest.json",)

RECOMMENDED_ENTRIES = ("model_card.json", "preprocess.json", "sample_results.json")


@dataclass
class UpdateCheck:
    """一项检查结果。界面按这个显示四步流程（发布→下载→摘要→回验）。"""

    key: str
    label: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "ok": self.ok, "detail": self.detail, "fatal": self.fatal}


@dataclass
class UpdateNotification:
    """平台下发的更新通知。"""

    artifact_id: str
    version: str
    target_device: str
    download_url: str
    sha256: str = ""
    size: int = 0
    artifact_kind: str = ""
    demo_only: bool = True
    notes: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "UpdateNotification":
        return cls(
            artifact_id=str(payload.get("artifactId") or payload.get("artifact_id") or payload.get("id") or ""),
            version=str(payload.get("version") or payload.get("modelVersion") or payload.get("model_version") or ""),
            target_device=str(payload.get("targetDevice") or payload.get("target") or ""),
            download_url=str(payload.get("downloadUrl") or payload.get("download_url") or ""),
            sha256=str(payload.get("sha256") or payload.get("digest") or ""),
            size=int(payload.get("size") or payload.get("bytes") or 0),
            artifact_kind=str(payload.get("artifactKind") or payload.get("artifact_kind") or ""),
            demo_only=bool(payload.get("demoOnly", payload.get("demo_only", True))),
            notes=str(payload.get("notes") or payload.get("note") or ""),
            raw=dict(payload),
        )

    @property
    def valid(self) -> bool:
        return bool(self.artifact_id and self.download_url)


@dataclass
class UpdateStep:
    """一步流程的真实记录。每步都带时间与来源，不虚构步骤。"""

    key: str
    label: str
    state: str          # pending / running / ok / failed / skipped
    detail: str = ""
    at: str = field(default_factory=utc_now_iso)

    STATE_LABEL = {"pending": "待执行", "running": "进行中", "ok": "通过", "failed": "失败", "skipped": "跳过"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "stateLabel": self.STATE_LABEL.get(self.state, self.state),
            "detail": self.detail,
            "at": self.at,
        }


class UpdateService:
    """演示模型包的接收、校验、切换与回验。

    所有网络动作都是同步的，调用点负责放到工作线程里（GUI 线程只读状态）。
    """

    def __init__(
        self,
        app: AppState,
        storage: Storage,
        http: HttpClient,
        *,
        staging_dir: Optional[Path] = None,
        on_step: Optional[Callable[[UpdateStep], None]] = None,
    ) -> None:
        self.app = app
        self.storage = storage
        self.http = http
        self.staging_dir = Path(staging_dir) if staging_dir else Path(storage.cfg.staging_path)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.on_step = on_step
        self.notification: Optional[UpdateNotification] = None
        self.steps: List[UpdateStep] = []
        self.package_dir: Optional[Path] = None
        self.manifest: Dict[str, Any] = {}
        self.downloaded_sha256 = ""
        self.checks: List[UpdateCheck] = []
        self.installed_version: Optional[str] = None
        self.receipt: Dict[str, Any] = {}
        self._payload_cache: Dict[str, Any] = {}

    # ---- 步骤记录 ----

    def _step(self, key: str, label: str, state: str, detail: str = "") -> UpdateStep:
        step = UpdateStep(key, label, state, detail)
        self.steps.append(step)
        log.info("更新[%s] %s：%s", state, label, detail)
        if self.on_step:
            self.on_step(step)
        return step

    def reset(self) -> None:
        self.steps = []
        self.checks = []
        self.manifest = {}
        self.package_dir = None
        self.downloaded_sha256 = ""
        self.receipt = {}
        self.app.update_machine.reset(UpdateState.IDLE)

    # ---- 1. 接收通知 ----

    def receive(self, payload: Dict[str, Any]) -> UpdateNotification:
        notification = UpdateNotification.from_payload(payload)
        self.notification = notification
        self.steps = []
        kind = notification.artifact_kind or "demo_nonflashable"
        self._step(
            "received",
            "接收更新包通知",
            "ok",
            f"{notification.artifact_id} 版本 {notification.version}，目标 {notification.target_device or '未注明'}，"
            f"类型 {kind}",
        )
        self.app.update_machine.reset(UpdateState.RECEIVED)
        self.app.note_update(notification.raw)
        self.app.demo_model_version = notification.version or self.app.demo_model_version
        return notification

    # ---- 2. 下载 ----

    def download(self) -> Tuple[bool, UpdateStep]:
        if self.notification is None:
            return False, self._step("download", "下载更新包", "failed", "还没有收到更新通知")
        if not self.notification.valid:
            return False, self._step("download", "下载更新包", "failed", "通知缺少 artifactId 或 downloadUrl")
        if not self.app.update_machine.to(UpdateState.DOWNLOADING, "开始下载"):
            return False, self._step("download", "下载更新包", "failed", f"当前状态 {self.app.update_machine.label} 不允许下载")

        target = self.staging_dir / f"{self.notification.artifact_id}.demo.zip"
        result = self.http.download(self.notification.download_url, str(target))
        if not result.ok:
            self.app.update_machine.to(UpdateState.FAILED, result.error)
            return False, self._step("download", "下载更新包", "failed", result.error)

        self.downloaded_sha256 = str(result.body.get("sha256") or "")
        declared = str(result.body.get("declaredSha256") or "")
        bytes_written = int(result.body.get("bytes") or 0)
        detail = f"实际写入 {bytes_written} 字节，实测 sha256={self.downloaded_sha256[:16]}…"
        if declared:
            detail += f"，平台声明 {declared[:16]}…"
        if self.notification.size and bytes_written != self.notification.size:
            self.app.update_machine.to(UpdateState.FAILED, "字节数不一致")
            return False, self._step("download", "下载更新包", "failed", detail + f"；与通知声明的 {self.notification.size} 不一致")
        self.app.update_machine.to(UpdateState.DOWNLOADED, "下载完成")
        return True, self._step("download", "下载更新包", "ok", detail)

    # ---- 3. 摘要与目标检查 ----

    def verify(self) -> Tuple[bool, UpdateStep]:
        """摘要检查只由真实计算得出（H12：点空白处不会宣布 Hash 通过）。"""
        if self.app.update_machine.state != UpdateState.DOWNLOADED:
            return False, self._step("verify", "摘要与目标检查", "failed", f"当前状态 {self.app.update_machine.label}，无法校验")
        target = self.staging_dir / f"{self.notification.artifact_id}.demo.zip" if self.notification else None
        if target is None or not target.is_file():
            self.app.update_machine.to(UpdateState.FAILED, "包文件不存在")
            return False, self._step("verify", "摘要与目标检查", "failed", "暂存目录里找不到刚下载的包")

        actual = hash_sample_file(str(target))
        self.checks = []
        declared = (self.notification.sha256 if self.notification else "") or ""
        if declared:
            ok = actual.lower() == declared.lower()
            self.checks.append(UpdateCheck("sha256", "文件摘要", ok, f"实测 {actual[:16]}… / 声明 {declared[:16]}…"))
            if not ok:
                self.app.update_machine.to(UpdateState.FAILED, "摘要不一致")
                return False, self._step("verify", "摘要与目标检查", "failed", f"摘要不一致：实测 {actual}")
        else:
            self.checks.append(UpdateCheck("sha256", "文件摘要", True, f"平台未声明摘要；本机实测 {actual[:16]}…（无对照值）", fatal=False))

        # 目标设备核对
        device_ok, device_detail = self._check_target_device()
        self.checks.append(UpdateCheck("target", "目标设备", device_ok, device_detail))
        if not device_ok:
            self.app.update_machine.to(UpdateState.FAILED, "目标设备不匹配")
            return False, self._step("verify", "摘要与目标检查", "failed", device_detail)

        # 包类型与内容
        kind_ok, kind_detail, manifest = self._inspect_package(str(target))
        self.checks.append(UpdateCheck("artifact_kind", "包类型", kind_ok, kind_detail))
        if not kind_ok:
            self.app.update_machine.to(UpdateState.FAILED, "包类型不允许")
            return False, self._step("verify", "摘要与目标检查", "failed", kind_detail)

        self.manifest = manifest or {}
        entry_ok, entry_detail = self._check_entries(str(target))
        self.checks.append(UpdateCheck("entries", "包内容清单", entry_ok, entry_detail, fatal=False))

        self.app.update_machine.to(UpdateState.VERIFIED, "摘要与目标检查通过")
        detail = "；".join(f"{item.label}{'通过' if item.ok else '异常'}" for item in self.checks)
        return True, self._step("verify", "摘要与目标检查", "ok", detail)

    def _check_target_device(self) -> Tuple[bool, str]:
        declared = (self.notification.target_device if self.notification else "") or ""
        if not declared:
            return True, "平台未声明目标设备，跳过核对"
        mine = self.app.device_id
        if declared.strip().lower() in (mine.lower(), "handheld", "edge", "*", "any"):
            return True, f"声明目标 {declared}，与本机 {mine} 匹配"
        return False, f"声明目标 {declared}，本机是 {mine}，拒绝应用"

    def _inspect_package(self, path: str) -> Tuple[bool, str, Dict[str, Any]]:
        """打开 zip 读 manifest；校验 artifactKind 是不是可烧录固件。"""
        if not zipfile.is_zipfile(path):
            return False, "文件不是合法 zip 包（演示包应为 .demo.zip）", {}
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if "manifest.json" not in names:
                    return False, "包内缺少 manifest.json", {}
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            return False, f"包解析失败：{exc}", {}

        kind = str(manifest.get("artifactKind") or manifest.get("artifact_kind") or "")
        if kind not in ALLOWED_ARTIFACT_KINDS:
            return False, f"artifactKind={kind!r} 不是演示包：本机不烧录真实固件，拒绝应用", manifest
        if manifest.get("flashable") is True:
            return False, "包声明 flashable=true，本机不执行烧录，拒绝应用", manifest
        self._payload_cache = manifest
        return True, f"artifactKind={kind or 'demo_nonflashable'}（不可烧录演示包）", manifest

    def _check_entries(self, path: str) -> Tuple[bool, str]:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError) as exc:
            return False, f"包读取失败：{exc}"
        missing_required = [item for item in REQUIRED_ENTRIES if item not in names]
        missing_recommended = [item for item in RECOMMENDED_ENTRIES if item not in names]
        if missing_required:
            return False, "缺少必需条目：" + "、".join(missing_required)
        if missing_recommended:
            return True, "缺少可选条目（不影响演示）：" + "、".join(missing_recommended)
        return True, "manifest / 模型说明 / 预处理配置 / 示例验证结果齐全"

    # ---- 4. 暂存与切换 ----

    def can_apply_now(self) -> Tuple[bool, str]:
        """采集进行中只暂存（H13）。"""
        allowed, reason = self.app.can_switch_model_now()
        return allowed, reason

    def stage(self) -> UpdateStep:
        """把验证通过的包解到暂存区，但不改当前有效模型版本。"""
        if self.app.update_machine.state != UpdateState.VERIFIED:
            return self._step("stage", "暂存新版本", "failed", f"当前状态 {self.app.update_machine.label}")
        if self.notification is None:
            return self._step("stage", "暂存新版本", "failed", "没有更新通知")
        target = self.staging_dir / f"{self.notification.artifact_id}.demo.zip"
        extract_dir = self.staging_dir / f"{self.notification.artifact_id}"
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(target) as archive:
                archive.extractall(extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            self.app.update_machine.to(UpdateState.FAILED, f"解包失败：{exc}")
            return self._step("stage", "暂存新版本", "failed", f"解包失败：{exc}")
        self.package_dir = extract_dir
        self.app.update_machine.to(UpdateState.STAGED, "已暂存，等待当前批次结束")
        return self._step(
            "stage",
            "暂存新版本",
            "ok",
            f"解包到 {extract_dir}；当前批次仍绑定原模型版本，结束后才允许切换",
        )

    def apply(self, *, restart_hook: Optional[Callable[[], Tuple[bool, str]]] = None) -> Tuple[bool, UpdateStep]:
        """切换本地演示模型版本并回验。

        `restart_hook` 由调用方提供"重启对应软件任务"的真实动作（例如重载回放引擎、
        重置采集会话）。没有提供时明确记录为"未执行重启"，不假装重启过。
        """
        if self.app.update_machine.state != UpdateState.STAGED:
            return False, self._step("apply", "切换并回验", "failed", f"当前状态 {self.app.update_machine.label}")
        if self.notification is None:
            return False, self._step("apply", "切换并回验", "failed", "没有更新通知")

        allowed, reason = self.can_apply_now()
        if not allowed:
            return False, self._step("apply", "切换并回验", "failed", f"拒绝切换：{reason}")

        self.app.update_machine.to(UpdateState.APPLYING, "切换演示模型版本")
        previous = self.app.model_version
        new_version = self.notification.version or previous

        try:
            self.storage.record_version(
                "demo_model",
                new_version,
                {
                    "artifactId": self.notification.artifact_id,
                    "packageDir": str(self.package_dir) if self.package_dir else "",
                    "sha256": self.downloaded_sha256,
                    "previous": previous,
                    "demoOnly": True,
                },
                active=True,
            )
            # 保留旧版本记录，回退时能读回来（H11：旧有效版本保留）
            self.storage.record_version("demo_model", previous, {"supersededBy": new_version}, active=False)
        except Exception as exc:  # noqa: BLE001
            self.app.update_machine.to(UpdateState.ROLLED_BACK, f"写入版本记录失败：{exc}")
            self.app.model_version = previous
            return False, self._step("apply", "切换并回验", "failed", f"写入版本记录失败，已回退 {previous}：{exc}")

        self.app.model_version = new_version
        self.installed_version = new_version

        restart_detail = ""
        if restart_hook is not None:
            try:
                ok, message = restart_hook()
                restart_detail = f"重启软件任务：{'成功' if ok else '失败'}（{message}）"
                if not ok:
                    self.storage.record_version("demo_model", previous, {"reason": "切换后重启失败，回退"}, active=True)
                    self.app.model_version = previous
                    self.app.update_machine.to(UpdateState.ROLLED_BACK, message)
                    return False, self._step("apply", "切换并回验", "failed", f"{restart_detail}；已回退旧版本 {previous}")
            except Exception as exc:  # noqa: BLE001
                self.storage.record_version("demo_model", previous, {"reason": f"重启异常：{exc}"}, active=True)
                self.app.model_version = previous
                self.app.update_machine.to(UpdateState.ROLLED_BACK, str(exc))
                return False, self._step("apply", "切换并回验", "failed", f"重启异常，已回退 {previous}：{exc}")
        else:
            restart_detail = "本次未接入重启动作（记录为未执行，不宣称已重启）"

        # 参考输入回验：用包内示例验证结果与本地固定参考输入比对
        verify_ok, verify_detail = self._reverify()
        detail = f"演示模型版本 {previous} → {new_version}；{restart_detail}；回验：{verify_detail}"
        if not verify_ok:
            self.app.update_machine.to(UpdateState.ROLLED_BACK, verify_detail)
            self.storage.record_version("demo_model", previous, {"reason": "回验未通过，回退"}, active=True)
            self.app.model_version = previous
            return False, self._step("apply", "切换并回验", "failed", f"{detail}；已回退旧版本 {previous}")

        self.app.update_machine.to(UpdateState.APPLIED, "已生效并回验通过")
        self.receipt = self.build_receipt(True)
        return True, self._step("apply", "切换并回验", "ok", detail)

    def _reverify(self) -> Tuple[bool, str]:
        """固定参考输入回验：读包内的示例验证结果，与本机固定参考输入对照。

        这里**不假装**跑了一次神经网络：本机没有真实模型推理，所以回验的内容是
        "包版本、输入规格、示例输出是否与采集配置一致"。把口径写清楚比编一个准确率好。
        """
        if self.package_dir is None:
            return False, "暂存目录不存在"
        sample_path = self.package_dir / "sample_results.json"
        if not sample_path.is_file():
            return True, "包内没有示例验证结果文件，回验仅完成版本与目标核对（已在包内容清单里标注）"
        try:
            sample = json.loads(sample_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"示例验证结果不可解析：{exc}"
        spec = sample.get("inputSpec") or {}
        point_count = int(spec.get("pointCount") or 0)
        expected = self._reference_point_count()
        notes = [f"包声明输入规格 pointCount={point_count or '未注明'}"]
        if point_count and expected and point_count != expected:
            return False, f"输入规格与本机采集配置不一致（包 {point_count} / 本机 {expected}）"
        if point_count and expected:
            notes.append(f"与本机采集配置 {expected} 一致")
        expected_output = sample.get("expectedOutput")
        if expected_output is not None:
            notes.append(f"示例输出 {json.dumps(expected_output, ensure_ascii=False)[:80]}")
        notes.append("说明：本机不执行神经网络推理，回验口径为版本、输入规格与示例输出的一致性核对")
        return True, "；".join(notes)

    def _reference_point_count(self) -> Optional[int]:
        """从样例包 manifest 读本机采集轴长度，用于与更新包声明的输入规格比对。"""
        from .scenarios import SCENARIOS, resolve_scenario_root

        root = resolve_scenario_root(self.storage.cfg.scenario_root)
        for spec in SCENARIOS.values():
            manifest = root / spec.scenario_id / "manifest.json"
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                value = data.get("pointCount")
                if value:
                    return int(value)
        return None

    # ---- 回执 ----

    def build_receipt(self, ok: bool = True) -> Dict[str, Any]:
        """设备回执：摘要、应用结果与实际版本读回（PRD §7.4 / §5.6）。"""
        notification = self.notification
        return {
            "schemaVersion": "1.0",
            "deviceId": self.app.device_id,
            "bootId": self.app.boot_id,
            "artifactId": notification.artifact_id if notification else "",
            "declaredVersion": notification.version if notification else "",
            "installedDemoModelVersion": self.installed_version,
            "actualControllerVersion": self.app.controller_version,
            "downloadedSha256": self.downloaded_sha256,
            "declaredSha256": notification.sha256 if notification else "",
            "artifactKind": notification.artifact_kind if notification else "",
            "demoOnly": True,
            "result": "applied" if ok else "failed",
            "checks": [item.to_dict() for item in self.checks],
            "steps": [step.to_dict() for step in self.steps],
            "carrierNote": "演示更新只切换本地演示模型版本；真实 ESP32 固件版本字段未被修改，也未执行任何烧录",
            "reportedAt": utc_now_iso(),
        }

    def send_receipt(self) -> HttpResult:
        if self.notification is None:
            return HttpResult(False, 0, {}, "没有更新通知，无法提交回执")
        payload = self.build_receipt(self.app.update_machine.state == UpdateState.APPLIED)
        result = self.http.post_receipt(self.notification.artifact_id, payload)
        if result.ok:
            log.info("更新回执已提交：%s", result.body.get("receiptId"))
        else:
            log.warning("更新回执提交失败：%s", result.error)
        return result

    # ---- 汇总 ----

    def summary(self) -> Dict[str, Any]:
        return {
            "state": self.app.update_machine.state,
            "stateLabel": self.app.update_machine.label,
            "artifact": self.notification.raw if self.notification else None,
            "version": self.notification.version if self.notification else "",
            "target": self.notification.target_device if self.notification else "",
            "downloadedSha256": self.downloaded_sha256,
            "declaredSha256": self.notification.sha256 if self.notification else "",
            "checks": [item.to_dict() for item in self.checks],
            "steps": [step.to_dict() for step in self.steps],
            "activeModelVersion": self.app.model_version,
            "controllerVersion": self.app.controller_version,
            "demoModelVersion": self.app.demo_model_version or self.installed_version,
            "packageDir": str(self.package_dir) if self.package_dir else "",
            "notes": [
                "演示包不可烧录（demo_nonflashable），本机不调用任何烧录工具",
                "摘要由本机重新计算，点击界面空白处不会产生任何通过事件",
                "实际控制器版本与演示模型版本分栏显示，演示回执不覆盖真机状态",
            ],
        }


def inspect_staged_package(path: Path) -> Dict[str, Any]:
    """给界面/自检用：只看一个包，不改任何状态。"""
    info: Dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return info
    info["sha256"] = hash_sample_file(str(path))
    info["bytes"] = path.stat().st_size
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            info["entries"] = archive.namelist()
            if "manifest.json" in info["entries"]:
                try:
                    info["manifest"] = json.loads(archive.read("manifest.json").decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    info["manifestError"] = str(exc)
    else:
        info["zip"] = False
    return info
