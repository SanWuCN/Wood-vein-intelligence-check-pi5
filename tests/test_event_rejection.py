"""关键事件拒收契约的回归测试（平台回执里的 retryable）。

这一组用例守的是三条**只有真跑起来才会暴露**的规则：

  1. `accepted` / `duplicated` 是 messageId 数组。平台若只回计数，终端的 ack 会失效。
  2. `rejected` 每条带 `retryable`：
        false → 永久拒收，终端**直接出队**，不再重试
        true 或缺省 → 临时失败，终端**保留并退避重试**
     缺省按"临时失败"处理是刻意的保守选择：宁可多试几次，也不因为平台漏写字段丢数据。
  3. 平台**不得静默丢弃**：每个收到的 messageId 最终要进 accepted 或 rejected 之一。
     静默丢弃时终端拿不到确认，会按重试节奏无限重推（实测 `device.selfcheck` 每 0.5s 一次）。

这里用一个**可控的假平台**（只回我们要测的那一种响应），
所以这组用例不需要任何外部服务，离线也能跑。
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_acceptance import BaseCase  # noqa: E402

from woodpulse.contracts import (  # noqa: E402
    EVENT_BATCH_CONTRACT_NOTE,
    EventType,
    REJECTED_ENTRY_FIELDS,
)
from woodpulse.platform_client import ClientCallbacks, PlatformClient  # noqa: E402


class _ScriptedPlatform(BaseHTTPRequestHandler):
    """只回固定响应的假平台。

    `/api/device-events/batch` 的响应由类属性 `batch_response` 决定；
    `/api/health` 与 `/api/devices/register` 给最小可用响应，
    这样 PlatformClient 能正常起来（但不参与本组用例的断言）。
    """

    batch_response: dict = {}
    received: list = []

    def log_message(self, *args, **kwargs) -> None:  # noqa: D102 - 保持测试输出干净
        return

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/health"):
            self._send(200, {"ok": True, "service": "scripted"})
            return
        self._send(404, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path.startswith("/api/device-events/batch"):
            try:
                payload = json.loads(raw.decode("utf-8"))
                type(self).received.append(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            self._send(200, dict(type(self).batch_response))
            return
        if self.path.startswith("/api/devices/register"):
            self._send(200, {"deviceId": "handheld-02", "heartbeatIntervalMs": 5000})
            return
        self._send(200, {})


def start_scripted_platform(response: dict):
    """起一个假平台，返回 (base_url, server, received 列表)。"""
    _ScriptedPlatform.batch_response = response
    _ScriptedPlatform.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedPlatform)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server, _ScriptedPlatform.received


def flush_once(client: PlatformClient) -> int:
    """直接调一次批量补传（不依赖后台循环的节奏），返回被 ack 掉的条数。"""
    with client._outbox_lock:
        pending = list(client._outbox)
    return client._post_events_batch(pending)


class RejectionContract(BaseCase):
    """拒收契约：retryable 决定"出队"还是"重试"。"""

    def _client(self, response: dict):
        base, server, received = start_scripted_platform(response)
        self.addCleanup(server.shutdown)
        self.app.cfg.platform.platform_url = base
        client = PlatformClient(self.app.cfg, self.app.storage, ClientCallbacks())
        client.boot_id = self.app.app.boot_id
        self.addCleanup(client.stop)
        return client, received

    def _enqueue(self, client: PlatformClient, count: int = 2):
        envelope_ids = []
        for index in range(count):
            envelope_ids.append(
                client.enqueue_event(
                    EventType.DEVICE_SELFCHECK,
                    {"schemaVersion": "1.0", "summary": {"ok": index}, "source": "test"},
                    critical=True,
                )
            )
        self.assertEqual(client.pending_events, count)
        return envelope_ids

    # ---- retryable=false ----

    def test_permanent_rejection_drops_event_and_stops_retrying(self) -> None:
        """永久拒收：终端必须出队，且**再次 flush 时不再发送**。"""
        client, received = self._client({"accepted": [], "duplicated": [], "rejected": []})
        # messageId 由终端生成，平台只能按收到的值回执 —— 所以先收一次再决定怎么拒
        message_ids = self._enqueue(client)
        _ScriptedPlatform.batch_response = {
            "accepted": [],
            "duplicated": [],
            "rejected": [
                {
                    "messageId": mid,
                    "reason": "unknown_type(device.selfcheck)",
                    "code": "unknown_type",
                    "retryable": False,
                }
                for mid in message_ids
            ],
            "rejectedCount": len(message_ids),
        }

        flush_once(client)
        self.assertEqual(client.pending_events, 0, "永久拒收的事件必须出队")
        self.assertEqual(self.app.storage.pending_event_count(), 0, "本地 outbox 也要清掉，否则重启后会重推")

        # 再 flush 两次：不应该再发出任何东西
        flush_once(client)
        flush_once(client)
        self.assertEqual(len(received), 1, "永久拒收之后不允许再重推同一批事件")
        self.assertEqual(client.pending_events, 0)

    # ---- retryable=true ----

    def test_transient_rejection_keeps_event_for_retry(self) -> None:
        """临时失败：终端必须保留事件，下次 flush 还要再发。"""
        client, received = self._client({"accepted": [], "duplicated": [], "rejected": []})
        message_ids = self._enqueue(client, count=1)
        _ScriptedPlatform.batch_response = {
            "accepted": [],
            "duplicated": [],
            "rejected": [
                {"messageId": message_ids[0], "reason": "queue_full", "code": "queue_full", "retryable": True}
            ],
            "rejectedCount": 1,
        }

        flush_once(client)
        self.assertEqual(client.pending_events, 1, "临时失败必须保留在 outbox")
        self.assertEqual(self.app.storage.pending_event_count(), 1, "持久化 outbox 同样要保留")

        # 平台恢复后重试成功 → 出队
        _ScriptedPlatform.batch_response = {
            "accepted": message_ids,
            "duplicated": [],
            "rejected": [],
            "acceptedCount": 1,
        }
        flush_once(client)
        self.assertEqual(client.pending_events, 0, "重试成功后才出队")
        self.assertEqual(len(received), 2, "临时失败要真的重试过一次")

    # ---- 缺省 retryable ----

    def test_missing_retryable_defaults_to_retry(self) -> None:
        """平台漏写 retryable 时按「临时失败」处理 —— 宁可多试也不丢业务数据。"""
        client, received = self._client({"accepted": [], "duplicated": [], "rejected": []})
        message_ids = self._enqueue(client, count=1)
        # 故意不带 retryable 字段
        _ScriptedPlatform.batch_response = {
            "accepted": [],
            "duplicated": [],
            "rejected": [{"messageId": message_ids[0], "reason": "unknown_type(device.selfcheck)"}],
        }
        flush_once(client)
        self.assertEqual(client.pending_events, 1, "缺 retryable 不能当成永久拒收，否则会静默丢数据")
        self.assertEqual(len(received), 1)

    # ---- 计数而不是数组 ----

    def test_count_only_response_still_acks_in_order(self) -> None:
        """平台只回计数（不合契约）时，终端按发送顺序 ack 并继续工作，不崩也不死循环。"""
        client, _received = self._client({"accepted": 2, "duplicated": 0, "rejected": []})
        self._enqueue(client, count=2)
        flush_once(client)
        self.assertEqual(client.pending_events, 0, "只回计数时也要能推进，不能卡死在 outbox")

    # ---- 静默丢弃 ----

    def test_silently_dropped_event_is_retried_and_warned(self) -> None:
        """平台既不确认也不拒收：终端保留并重试，且要给出指向契约的告警。"""
        client, received = self._client({"accepted": [], "duplicated": [], "rejected": []})
        client.enqueue_event(EventType.DEVICE_SELFCHECK, {"schemaVersion": "1.0"}, critical=True)
        flush_once(client)
        self.assertEqual(client.pending_events, 1, "静默丢弃时终端不能当作已送达")
        self.assertEqual(client._unaccounted_attempts, 1, "应记录一次未被确认的尝试")
        flush_once(client)
        self.assertEqual(client._unaccounted_attempts, 2, "连续未被确认要能累计，用于给出告警")

    # ---- 契约自身的完整性 ----

    def test_contract_note_covers_all_three_rules(self) -> None:
        """契约说明必须同时写明三件事，避免后来人只看模板漏掉语义。"""
        for keyword in ("messageId 数组", "retryable", "不得静默丢弃"):
            self.assertIn(keyword, EVENT_BATCH_CONTRACT_NOTE, f"契约说明缺少「{keyword}」")
        self.assertIn("retryable", REJECTED_ENTRY_FIELDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
