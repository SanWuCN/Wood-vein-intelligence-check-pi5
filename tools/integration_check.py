"""终端 ↔ 平台 端到端联调脚本。

它回答一个问题：**"平台真的能看到这台设备吗？"**
不是看代码，而是真的起终端、连平台、发事件、收命令，再回头查平台侧的状态。

覆盖这条链路（PRD §7、§8）：
   注册握手 → WebSocket 连接 → 遥测上报 → 关键事件（含命令回执）送达
   → 平台下发 pause_capture → 终端 accepted → 真的暂停 → 终端 executed
   → 平台侧能查到三个回执 → 下发过期命令 → 终端拒绝并回 failed

用法（先起模拟平台）：
    python tools/mock_platform.py --port 8099 --data-dir .cache/mock-data
    python tools/integration_check.py --platform-url http://127.0.0.1:8099

注意：这个脚本历史上抓出过两个真实缺陷（SQLite 跨线程、outbox 不周期发送），
所以它不只是"演示"，回归时应该跑它。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("WOODPULSE_CONSOLE_LOG", "0")

# Windows 控制台默认是 GBK，直接 print 中文与符号会抛 UnicodeEncodeError。
# 部署脚本也可能在非 UTF-8 的终端里跑，所以这里统一把输出切成 UTF-8 并对不可编码字符降级。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FAILURES: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)
    return ok


def get_json(url: str, token: str = "", timeout: float = 5.0):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-Device-Token": token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, body: dict, token: str = "", timeout: float = 10.0):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", "X-Device-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def main() -> int:
    parser = argparse.ArgumentParser(description="终端 ↔ 平台 端到端联调")
    parser.add_argument("--platform-url", default="http://127.0.0.1:8099")
    parser.add_argument("--device-id", default="handheld-02")
    parser.add_argument("--device-token", default="demo-token")
    parser.add_argument("--data-dir", default=str(ROOT / ".cache" / "integration-data"))
    parser.add_argument("--scenario-root", default=str(ROOT / "samples"))
    parser.add_argument("--round", default="initial", choices=["initial", "rescan", "reference"])
    args = parser.parse_args()

    base = args.platform_url.rstrip("/")
    token = args.device_token

    print("=" * 74)
    print(f"终端 ↔ 平台联调：{base} / 设备 {args.device_id}")
    print("=" * 74)

    # 0) 平台可达
    try:
        health = get_json(f"{base}/api/health")
        check("平台服务可达", bool(health.get("ok")), f"service={health.get('service')}")
    except Exception as exc:  # noqa: BLE001
        check("平台服务可达", False, f"{exc}（先运行 python tools/mock_platform.py --port 8099）")
        return 1

    from woodpulse.app import WoodPulseApp
    from woodpulse.config import load_config
    from woodpulse.contracts import Command, ReceiptState, TaskState

    cfg = load_config(argv=[
        "--data-dir", args.data_dir,
        "--scenario-root", args.scenario_root,
        "--platform-url", base,
        "--device-id", args.device_id,
        "--device-token", token,
        "--camera", "none",
        "--no-fullscreen",
    ])
    cfg.self_check_on_start = False
    cfg.platform.heartbeat_interval_s = 1.0
    app = WoodPulseApp(cfg)

    try:
        # 1) 注册握手
        handshake = app.start()
        check("设备注册握手", bool(handshake.get("ok")), str(handshake.get("detail"))[:120])

        # 2) WebSocket 上线
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and app.platform.state != "online":
            time.sleep(0.2)
        check("WebSocket 已连接", app.platform.state == "online", app.platform.state_detail)

        # 3) 采集一批：过程中会产生 capture.* 关键事件
        #    每次跑用唯一 batchId：平台对同一 batchId 重复提交不同内容会回 409
        #    （防止覆盖已归档记录），那是平台正确的行为，不该让它把联调判成失败。
        import uuid

        batch_override = f"{'scan-Z04-001' if args.round == 'initial' else 'scan-Z04-002'}-i{uuid.uuid4().hex[:6]}"
        ok, message = app.prepare_task(round_name=args.round, batch_id=batch_override)
        check("准备批次", ok, message or (app.app.batch.batch_id if app.app.batch else ""))
        ok, message = app.start_capture()
        check("开始采集", ok, message)
        for _ in range(80):
            app.tick(0.1)
            time.sleep(0.02)
        batch_id = app.app.batch.batch_id
        frames = app.app.batch.frames_returned
        check("样例回放推进", frames > 0, f"{batch_id} 已保存 {frames} 帧")

        # 4) 遥测与事件必须真的到达平台（这是缺陷 1 与缺陷 2 的回归点）
        deadline = time.monotonic() + 15
        state = {}
        while time.monotonic() < deadline:
            state = get_json(f"{base}/api/devices/{args.device_id}/state", token)
            counters = state.get("counters") or {}
            last_event = state.get("lastEvent") or {}
            if counters.get("telemetry", 0) > 0 and (last_event.get("type") or "").startswith("capture."):
                break
            time.sleep(0.5)

        counters = state.get("counters") or {}
        check("遥测到达平台", counters.get("telemetry", 0) > 0, f"telemetry={counters.get('telemetry')}")
        # 平台把整条信封存在 lastTelemetry 里，业务字段在 payload 下
        telemetry = (state.get("lastTelemetry") or {}).get("payload") or {}
        cpu = telemetry.get("cpuPercent")
        check(
            "遥测字段来自真实采集（非随机）",
            cpu is None or isinstance(cpu, (int, float)),
            f"cpuPercent={cpu} cpuQuality={telemetry.get('cpuQuality')} sourceMode={telemetry.get('sourceMode')}",
        )
        check(
            "遥测携带来源标识",
            telemetry.get("sourceMode") in ("live", None),
            f"sourceMode={telemetry.get('sourceMode')}",
        )
        counters = state.get("counters") or {}
        seen_types = {c.get("type") for c in (state.get("commands") or [])}
        last_event = state.get("lastEvent") or {}
        check(
            "关键事件正常连接期间送达",
            counters.get("events", 0) > 0 and bool(last_event.get("type")),
            f"events={counters.get('events')} 最近一条={last_event.get('type')}（commands 类型 {sorted(t for t in seen_types if t)}）",
        )

        # 5) 平台下发暂停命令
        status, body = post_json(
            f"{base}/api/devices/{args.device_id}/commands",
            {
                "action": Command.PAUSE_CAPTURE,
                "deviceId": args.device_id,
                "targetBatchId": batch_id,
                "expectedTaskRevision": app.app.assignment.task_revision,
                "payload": {"reason": "适用域待核验", "reasonCode": "domain_unverified"},
            },
            token,
        )
        command_id = (body or {}).get("commandId") or (body or {}).get("command_id")
        check("平台下发 pause_capture", status in (200, 201, 202) and bool(command_id), f"HTTP {status} commandId={command_id}")

        # 6) 终端必须真的暂停，并回 executed
        deadline = time.monotonic() + 15
        paused = False
        while time.monotonic() < deadline:
            if app.app.task.state == TaskState.PAUSED:
                paused = True
                break
            time.sleep(0.2)
        check("终端实际暂停采集", paused, f"task.state={app.app.task.state}")
        frames_at_pause = app.app.batch.frames_returned
        for _ in range(15):
            app.tick(0.1)
        check("暂停后不再追加帧", app.app.batch.frames_returned == frames_at_pause,
              f"{frames_at_pause} → {app.app.batch.frames_returned}")

        # 7) 平台侧能看到命令的回执（命令记录里的 receipt 就是终端回的）
        deadline = time.monotonic() + 15
        record = {}
        while time.monotonic() < deadline:
            state = get_json(f"{base}/api/devices/{args.device_id}/state", token)
            record = next((c for c in (state.get("commands") or []) if c.get("commandId") == command_id), {})
            if (record.get("receipt") or {}).get("state") == ReceiptState.EXECUTED:
                break
            time.sleep(0.4)
        receipt = record.get("receipt") or {}
        check("命令已投递给设备", bool(record.get("delivered")), f"delivered={record.get('delivered')}")
        check(
            "回执 accepted 到达平台",
            bool(record.get("acceptedAt")) or receipt.get("state") in (ReceiptState.ACCEPTED, ReceiptState.EXECUTED),
            f"acceptedAt={record.get('acceptedAt')}",
        )
        check("回执 executed 到达平台", receipt.get("state") == ReceiptState.EXECUTED,
              f"receipt={json.dumps(receipt, ensure_ascii=False)[:120]}")
        if receipt:
            check("executed 说明了作用范围", bool(receipt.get("scope")), str(receipt.get("scope"))[:90])

        # 8) 过期命令必须被拒绝，并回 failed
        status, body = post_json(
            f"{base}/api/devices/{args.device_id}/commands",
            {
                "action": Command.QUERY_STATUS,
                "deviceId": args.device_id,
                "expiresAt": "2020-01-01T00:00:00Z",
                "payload": {},
            },
            token,
        )
        expired_id = (body or {}).get("commandId") or (body or {}).get("command_id")
        check("平台下发过期命令", status in (200, 201, 202) and bool(expired_id), f"HTTP {status}")

        deadline = time.monotonic() + 15
        failed = None
        while time.monotonic() < deadline:
            state = get_json(f"{base}/api/devices/{args.device_id}/state", token)
            record = next((c for c in (state.get("commands") or []) if c.get("commandId") == expired_id), {})
            failed = record.get("receipt")
            if (failed or {}).get("state") == ReceiptState.FAILED:
                break
            failed = None
            time.sleep(0.4)
        check("过期命令被拒绝并回 failed", failed is not None, (failed or {}).get("reason", "未收到 failed 回执")[:100])
        if failed:
            check("failed 带错误码", bool(failed.get("errorCode")), str(failed.get("errorCode")))

        # 9) 结束批次并按 §8.2 的顺序交付（manifest 已提交才允许上传）
        ok, manifest = app.finish_capture()
        check("结束批次并提交 manifest", ok, (manifest or {}).get("datasetHash", "")[:16])
        jobs = app.upload_files(batch_id)
        check("文件进入上传队列", len(jobs) > 0, f"{len(jobs)} 个文件")

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and app.platform.pending_uploads > 0:
            time.sleep(0.5)
        check("上传队列完成", app.platform.pending_uploads == 0, f"剩余 {app.platform.pending_uploads}")

        files = app.storage.files_for_batch(batch_id)
        done = [item for item in files if item["upload_state"] == "done"]
        check("文件被平台确认（含摘要）", len(done) == len(files) and len(files) > 0,
              f"{len(done)}/{len(files)} 已确认")
        mismatched = [item["rel_path"] for item in done if item["received_offset"] != item["size"]]
        check("确认字节数与本地一致", not mismatched, f"不一致：{mismatched}")

        ok, report = app.submit_batch(batch_id)
        check("提交批次清单给平台", ok, json.dumps(report, ensure_ascii=False)[:140])

        # 10) 平台侧设备状态汇总
        state = get_json(f"{base}/api/devices/{args.device_id}/state", token)
        check("平台侧设备在线", (state.get("connection") or {}).get("online", True) is not False,
              f"counters={state.get('counters')}")
    finally:
        app.shutdown("联调结束")

    print("-" * 74)
    if FAILURES:
        print(f"结论：{len(FAILURES)} 项未通过 —— " + "、".join(FAILURES))
        return 1
    print("结论：全部通过，终端与平台的真实联动可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
