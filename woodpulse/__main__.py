"""命令行入口：`python -m woodpulse [选项]`。

行为：
  · 加载配置 → 建数据目录 → 起终端核心 → 起界面；
  · 没有图形环境（或显式 --headless）时打印状态报告并退出，退出码 0（Qt 可用）或 2（Qt 缺失）；
  · --check 只做预检：样例包、数据目录、平台可达性、依赖，适合部署脚本调用。
"""

from __future__ import annotations

import sys
from typing import List, Optional

from .config import HELP_TEXT, load_config


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cfg = load_config(argv=args)

    if cfg.__dict__.get("_want_help"):
        print(HELP_TEXT)
        return 0

    if "--check" in args:
        return _preflight(cfg)

    from .adapters.camera import HAS_CV2
    from .logging_setup import get_logger
    from .ui.qt import HAVE_QT

    headless = "--headless" in args or not HAVE_QT

    if headless and not HAVE_QT and "--headless" not in args:
        # 没有 Qt 时不静默退出：明确告诉用户缺什么、怎么装
        print("警告：没有找到 PyQt5 / PySide6，将以无图形模式运行。", file=sys.stderr)

    from .app import WoodPulseApp

    app = WoodPulseApp(cfg)
    log = get_logger("main")
    log.info(
        "依赖情况：Qt=%s，OpenCV=%s，psutil=%s",
        HAVE_QT,
        HAS_CV2,
        __import__("woodpulse.telemetry", fromlist=["HAS_PSUTIL"]).HAS_PSUTIL,
    )

    if headless:
        from .ui.main import headless_report

        return headless_report(app)

    from .ui.main import run_gui

    return run_gui(app, [sys.argv[0], *args])


def _preflight(cfg) -> int:
    """部署预检：回答"这台机器现在能不能跑起来"。"""
    from pathlib import Path

    from .adapters.camera import HAS_CV2, probe_devices
    from .adapters.replay import ReplayLibrary
    from .platform_client import HttpClient
    from .storage import Storage
    from .telemetry import HAS_PSUTIL, TemperatureProbe

    checks = []

    def add(key: str, label: str, ok: bool, detail: str, *, fatal: bool = True) -> None:
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail, "fatal": fatal})

    add("python", "Python 版本", sys.version_info >= (3, 9), f"{sys.version.split()[0]}")
    from .ui.qt import binding_info

    info = binding_info()
    add("qt", "Qt 绑定（PyQt5 / PySide6）", info["available"], f"{info['binding'] or '未找到'} {info['qtVersion'] or ''}".strip(), fatal=False)
    add("psutil", "psutil（CPU/内存/网卡计数）", HAS_PSUTIL, "已安装" if HAS_PSUTIL else "未安装：pip install psutil 或 apt install python3-psutil", fatal=False)
    add("opencv", "OpenCV（相机实采）", HAS_CV2, "已安装" if HAS_CV2 else "未安装：apt install python3-opencv", fatal=False)

    try:
        storage = Storage(cfg)
        add("storage", "数据目录可写", True, str(cfg.data_path))
        storage.close()
    except Exception as exc:  # noqa: BLE001
        add("storage", "数据目录可写", False, f"{cfg.data_path}：{exc}")

    library = ReplayLibrary(configured=cfg.scenario_root)
    status = library.status()
    add(
        "samples",
        "检测样例包",
        status["ready"] == status["total"] and status["total"] > 0,
        f"{status['ready']}/{status['total']} 就绪（{status['root']}）"
        + ("" if status["ready"] == status["total"] else "；缺少：" + "、".join(i["scenarioId"] for i in status["items"] if not i["present"])),
    )

    probe = TemperatureProbe()
    add("soc-temp", "SoC 温度接口", bool(probe.source), probe.source or "未探测到 vcgencmd / thermal 传感器", fatal=False)

    devices = probe_devices()
    add(
        "camera",
        "摄像头",
        any(item.get("openable") for item in devices),
        "、".join(item["device"] for item in devices if item.get("openable")) or "未发现可打开的 V4L2 设备（无相机时终端照常运行，能力声明为未接入）",
        fatal=False,
    )

    client = HttpClient(cfg.platform.platform_url, cfg.platform.device_token, timeout=3.0)
    health = client.health()
    add(
        "platform",
        "平台可达性",
        health.ok,
        f"{cfg.platform.platform_url} → {'HTTP ' + str(health.status) if health.ok else health.error}"
        "（不可达时终端仍可离线采集，事件进 outbox）",
        fatal=False,
    )

    print("=" * 72)
    print("木脉智检手持终端 · 部署预检")
    print("=" * 72)
    for item in checks:
        mark = "通过" if item["ok"] else ("失败" if item["fatal"] else "注意")
        print(f"[{mark}] {item['label']}：{item['detail']}")
    blocking = [item for item in checks if not item["ok"] and item["fatal"]]
    print("-" * 72)
    if blocking:
        print(f"结论：有 {len(blocking)} 项阻断问题，无法启动：" + "、".join(item["label"] for item in blocking))
        return 1
    warnings = [item for item in checks if not item["ok"]]
    print("结论：可以启动" + (f"（{len(warnings)} 项非阻断注意项）" if warnings else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
