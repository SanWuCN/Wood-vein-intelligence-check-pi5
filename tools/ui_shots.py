"""界面渲染快照工具：把每个页面渲染成 PNG，用于在开发机上核对 800×480 布局。

用法：
    python tools/ui_shots.py --out .cache/shots            # 全部页面
    python tools/ui_shots.py --page scan --out .cache/shots

为什么要这个工具：
    PRD §13 H16 要求"实际屏幕持续操作并完整排练：大按钮可触达、相机不拉伸、
    日志不挤主画面"。在拿到实机之前，先把每一页按 800×480 渲染出来逐页核对，
    比等到了现场才发现按钮点不到要省事得多。

注意：无头环境（offscreen / minimal 平台）经常没有中文字体，
渲染出来的中文会变成方块。这时用 `--font` 指定一个字体文件即可：

    python tools/ui_shots.py --font C:/Windows/Fonts/msyh.ttc
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("WOODPULSE_CONSOLE_LOG", "0")

PAGES = ["workbench", "scan", "environment", "delivery", "update", "status"]


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 800×480 界面快照")
    parser.add_argument("--out", default=".cache/shots", help="输出目录")
    parser.add_argument("--page", action="append", help="只渲染指定页面，可重复")
    parser.add_argument("--font", default="", help="中文字体文件路径（无头环境常需要）")
    parser.add_argument("--scenario-root", default="", help="样例包根目录")
    parser.add_argument("--data-dir", default="", help="数据目录（默认用临时目录，避免污染真实数据）")
    args = parser.parse_args()

    from PyQt5 import QtGui, QtWidgets  # noqa: F401 - 确认绑定存在

    from woodpulse.app import WoodPulseApp
    from woodpulse.config import load_config

    argv = ["--no-fullscreen"]
    if args.scenario_root:
        argv += ["--scenario-root", args.scenario_root]
    if args.data_dir:
        argv += ["--data-dir", args.data_dir]
    else:
        argv += ["--data-dir", str(ROOT / ".cache" / "ui-shots-data")]

    cfg = load_config(argv=argv)
    cfg.self_check_on_start = False
    cfg.platform.platform_url = "http://127.0.0.1:9"  # 故意不可达：顺便核对离线态界面
    app = WoodPulseApp(cfg)

    qt_app = QtWidgets.QApplication(sys.argv[:1])

    if args.font:
        font_path = pathlib.Path(args.font)
        if font_path.is_file():
            font_id = QtGui.QFontDatabase.addApplicationFont(str(font_path))
            families = QtGui.QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            print(f"已加载字体：{font_path.name} → {families}")
        else:
            print(f"字体文件不存在：{font_path}", file=sys.stderr)

    from woodpulse.ui.shell import Shell
    from woodpulse.ui.theme import apply_theme

    apply_theme(qt_app)
    shell = Shell(app)
    shell.resize(cfg.ui.width, cfg.ui.height)

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = args.page or PAGES
    from woodpulse.ui.state_view import build_snapshot, workbench_cards

    # 让界面有内容可看：跑一轮自检、准备一个批次并推进若干帧
    report = app.run_self_check()
    print(f"自检：{report.summary}")
    round_name = os.environ.get("WOODPULSE_SHOT_ROUND", "rescan")
    ok, message = app.prepare_task(round_name=round_name)
    print(f"准备批次（{round_name}）：{ok} {message}")
    app.start_capture()
    # 走 Shell 的 tick（与现场完全同一条路径），这样序列图、相机预览与
    # 按钮状态都会被真正刷新，快照才是"实机会看到的样子"
    for _ in range(60):
        shell._on_tick()
        qt_app.processEvents()
    for label in ("正面", "", "右侧"):
        app.add_mark(label)
        shell._refresh_page("scan")
    app.pause_capture("快照工具主动暂停")
    shell._on_tick()
    snapshot = build_snapshot(app)
    batch = snapshot.get("batch") or {}
    print(f"推进后：{batch.get('batchId')} 已保存 {batch.get('returnedFrames')} 帧，"
          f"{len(app.app.batch.marks) if app.app.batch else 0} 个标记，"
          f"序列图 {shell.pages['scan'].sequence.column_count} 列")

    snapshot = build_snapshot(app)
    shell._refresh_everything()
    for key in ["workbench", "scan", "environment", "delivery", "update", "status"]:
        shell.show_page(key)

    written = []
    for key in targets:
        shell.show_page(key)
        qt_app.processEvents()
        image = shell.grab()
        path = out_dir / f"{key}.png"
        image.save(str(path))
        written.append(path)
        print(f"已渲染 {key}: {path} ({image.width()}×{image.height()})")

    # 顺带把状态视图打出来，便于比对"界面显示"与"实际状态"是否一致
    print("-" * 60)
    print(f"任务状态：{snapshot['task']['stateLabel']}；批次：{(snapshot.get('batch') or {}).get('batchId')}")
    print(f"连接：{snapshot['connection']['label']}；上传：{snapshot['upload']}")
    print(f"工作台卡片：{workbench_cards(snapshot)['nextTask']}")
    app.shutdown("快照工具结束")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
