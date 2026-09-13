"""SQLite 与批次目录（PRD §8.2、§9、§10）。

保存四类东西：
  · 任务与批次记录（谁在扫哪根柱、用的哪个配置和模型版本）
  · 版本快照（配置版本、演示模型版本、实际控制器版本分别记，PRD §5.6）
  · 文件索引（批次目录里的每个文件、大小、摘要、上传状态）
  · 关键事件 outbox（至少一次投递，平台按 messageId 去重，PRD §8.2）

两条不能省的规则：
  1. **先原子提交 manifest，再允许上传**。manifest 落地是"这批数据完整"的唯一依据；
  2. 进程崩溃后重开，未完成的批次恢复为 interrupted，保留已保存数据，
     **不无声续扫、不重复上传**（PRD §13 H14）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .contracts import (
    BATCH_REQUIRED_DIRS,
    BATCH_REQUIRED_FILES,
    BatchState,
    UploadState,
    hash_sample_file,
    new_id,
    utc_now_iso,
)
from .logging_setup import get_logger

log = get_logger("storage")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id            TEXT PRIMARY KEY,
    scenario_id         TEXT NOT NULL,
    order_id            TEXT NOT NULL,
    component_id        TEXT NOT NULL,
    zone_id             TEXT NOT NULL,
    round               TEXT NOT NULL,
    state               TEXT NOT NULL,
    config_version      TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    source_mode         TEXT NOT NULL,
    camera_source_mode  TEXT NOT NULL,
    position_source     TEXT NOT NULL,
    pairing             TEXT NOT NULL,
    dir_path            TEXT NOT NULL,
    frame_count         INTEGER NOT NULL DEFAULT 0,
    returned_frames     INTEGER NOT NULL DEFAULT 0,
    mark_count          INTEGER NOT NULL DEFAULT 0,
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    upload_state        TEXT NOT NULL DEFAULT 'queued',
    uploaded_bytes      INTEGER NOT NULL DEFAULT 0,
    dataset_hash        TEXT NOT NULL DEFAULT '',
    interrupt_reason    TEXT NOT NULL DEFAULT '',
    diagnosis_frozen    INTEGER NOT NULL DEFAULT 0,
    freeze_reason       TEXT NOT NULL DEFAULT '',
    manifest_committed  INTEGER NOT NULL DEFAULT 0,
    boot_id             TEXT NOT NULL DEFAULT '',
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    updated_at          TEXT NOT NULL,
    payload             TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS files (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    role          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    name          TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT NOT NULL DEFAULT '',
    upload_state  TEXT NOT NULL DEFAULT 'queued',
    remote_id     TEXT NOT NULL DEFAULT '',
    received_offset INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    UNIQUE(batch_id, rel_path)
);

CREATE TABLE IF NOT EXISTS outbox (
    message_id   TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    seq          INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    acked_at     TEXT,
    last_error   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS command_receipts (
    command_id   TEXT NOT NULL,
    action       TEXT NOT NULL,
    state        TEXT NOT NULL,
    error_code   TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    target_batch TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (command_id, state)
);

CREATE TABLE IF NOT EXISTS versions (
    kind         TEXT NOT NULL,
    version      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    active       INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (kind, version)
);

CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    type         TEXT NOT NULL,
    batch_id     TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id    TEXT PRIMARY KEY,
    file_id      TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL DEFAULT '',
    offset       INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'queued',
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    remote_id    TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_batch ON files(batch_id);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(acked_at, seq);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id, seq);
"""


@dataclass
class RecoveredBatch:
    """崩溃恢复时发现的中断批次。"""

    batch_id: str
    component_id: str
    zone_id: str
    returned_frames: int
    marks: int
    dir_path: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batchId": self.batch_id,
            "componentId": self.component_id,
            "zoneId": self.zone_id,
            "returnedFrames": self.returned_frames,
            "marks": self.marks,
            "dirPath": self.dir_path,
            "reason": self.reason,
        }


class Storage:
    """终端本地存储。

    线程模型（重要）：`sqlite3` 的连接默认只能在创建它的线程里使用，而本终端的
    平台客户端跑在**独立网络线程**上（PRD §10 明确要求网络事件循环与 GUI 事件循环
    隔离）。所以这里为每个线程各持一个连接，并用一把可重入锁串行化写：

      · 本地库读多写少、数据量小，串行化的代价可以忽略；
      · 换来"任何线程都能安全调用 storage"，不会出现
        `SQLite objects created in a thread can only be used in that same thread`；
      · WAL + busy_timeout 让多连接共存也不会互相把对方锁死。

    只加一个 `check_same_thread=False` 的连接是**不够**的：那样多线程会同时用同一个
    cursor，出的是更隐蔽的错乱。按线程持有连接、写路径加锁才是正解。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.cfg.ensure_dirs()
        self.db_path = Path(cfg.db_path)
        self.batches_dir = Path(cfg.batches_path)
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._connection()  # 主线程连接：顺便把建表脚本跑一遍
        log.info("本地库就绪：%s", self.db_path)

    # ---- 基础设施 ----

    def _connection(self) -> sqlite3.Connection:
        """取当前线程的连接，没有就新建一个并跑一遍幂等的建表脚本。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def close(self) -> None:
        """关闭所有线程的连接。只应在程序退出时调用。"""
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        try:
            del self._local.conn
        except AttributeError:
            pass

    @contextmanager
    def tx(self):
        """短事务。SQLite + WAL，写少读多，不需要长事务。

        用可重入锁把写路径串起来：多线程各持连接时，靠锁避免
        `database is locked` 的反复重试，比只依赖 busy_timeout 更确定。
        """
        with self._write_lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._connection().execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._connection().execute(
            "INSERT INTO state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utc_now_iso()),
        )

    def log_event(self, type_: str, batch_id: str = "", payload: Optional[Dict[str, Any]] = None) -> int:
        cursor = self._connection().execute(
            "INSERT INTO events(at, type, batch_id, payload) VALUES (?,?,?,?)",
            (utc_now_iso(), type_, batch_id, json.dumps(payload or {}, ensure_ascii=False)),
        )
        return int(cursor.lastrowid or 0)

    # ---- 版本 ----

    def record_version(self, kind: str, version: str, detail: Optional[Dict[str, Any]] = None, active: bool = True) -> None:
        """kind: config / demo_model / controller / app / adapter。

        实际控制器版本与演示模型版本分栏记录，演示回执不覆盖真机状态（PRD §5.6）。
        """
        with self.tx() as conn:
            if active:
                conn.execute("UPDATE versions SET active=0 WHERE kind=?", (kind,))
            conn.execute(
                "INSERT INTO versions(kind, version, detail, active, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(kind, version) DO UPDATE SET detail=excluded.detail, active=excluded.active, updated_at=excluded.updated_at",
                (kind, version, json.dumps(detail or {}, ensure_ascii=False), 1 if active else 0, utc_now_iso()),
            )

    def active_version(self, kind: str) -> Optional[str]:
        row = self._connection().execute(
            "SELECT version FROM versions WHERE kind=? AND active=1 ORDER BY updated_at DESC LIMIT 1", (kind,)
        ).fetchone()
        return row["version"] if row else None

    def all_active_versions(self) -> Dict[str, str]:
        rows = self._connection().execute("SELECT kind, version FROM versions WHERE active=1").fetchall()
        return {row["kind"]: row["version"] for row in rows}

    # ---- 批次 ----

    def batch_dir(self, batch_id: str) -> Path:
        return self.batches_dir / batch_id

    def create_batch_dir(self, batch_id: str, *, keep_existing: bool = False) -> Path:
        path = self.batch_dir(batch_id)
        if path.exists() and not keep_existing:
            # H06：同一样例重复两轮必须用不同 batchId 与会话，无残留数据
            log.warning("批次目录已存在，按新批次重建：%s", path)
            shutil.rmtree(path, ignore_errors=True)
        for sub in ("images",):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path

    def save_batch(self, record, *, manifest_committed: bool = False, boot_id: str = "") -> None:
        """把内存里的 BatchRecord 落库。调用点：建批次、封存、上传状态变化。"""
        payload = record.to_manifest()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO batches(
                    batch_id, scenario_id, order_id, component_id, zone_id, round, state,
                    config_version, model_version, source_mode, camera_source_mode, position_source, pairing,
                    dir_path, frame_count, returned_frames, mark_count, total_bytes, upload_state, uploaded_bytes,
                    dataset_hash, interrupt_reason, diagnosis_frozen, freeze_reason, manifest_committed,
                    boot_id, started_at, finished_at, updated_at, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(batch_id) DO UPDATE SET
                    state=excluded.state, frame_count=excluded.frame_count, returned_frames=excluded.returned_frames,
                    mark_count=excluded.mark_count, total_bytes=excluded.total_bytes,
                    upload_state=excluded.upload_state, uploaded_bytes=excluded.uploaded_bytes,
                    dataset_hash=excluded.dataset_hash, interrupt_reason=excluded.interrupt_reason,
                    diagnosis_frozen=excluded.diagnosis_frozen, freeze_reason=excluded.freeze_reason,
                    manifest_committed=excluded.manifest_committed, model_version=excluded.model_version,
                    config_version=excluded.config_version, finished_at=excluded.finished_at,
                    updated_at=excluded.updated_at, payload=excluded.payload
                """,
                (
                    record.batch_id,
                    record.scenario_id,
                    record.order_id,
                    record.component_id,
                    record.zone_id,
                    record.round,
                    record.state,
                    record.config_version,
                    record.model_version,
                    record.source_mode,
                    record.camera_source_mode,
                    record.position_source,
                    record.pairing,
                    record.dir_path,
                    record.frame_count_expected,
                    record.frames_returned,
                    record.mark_count,
                    record.total_bytes,
                    record.upload_state,
                    record.uploaded_bytes,
                    record.dataset_hash,
                    record.interrupt_reason,
                    1 if record.diagnosis_frozen else 0,
                    record.freeze_reason,
                    1 if manifest_committed else 0,
                    boot_id,
                    record.started_at,
                    record.finished_at,
                    utc_now_iso(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        row = self._connection().execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM batches ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_interrupted(self, current_boot_id: str) -> List[RecoveredBatch]:
        """进程崩溃恢复（PRD §8.2、§13 H14）。

        上次 bootId 不是本次、状态还停在 open 的批次，一律标为 interrupted，
        保留已保存数据，不自动续扫、不自动重传。

        已保存帧数**从 frames.csv 实际数出来**，不用库里的计数字段：库里那个值
        每积累若干帧才同步一次，崩溃时必然偏小，直接报给界面就等于低报了保住的数据。
        """
        rows = self._connection().execute(
            "SELECT * FROM batches WHERE state=? AND boot_id != ?", (BatchState.OPEN, current_boot_id)
        ).fetchall()
        recovered: List[RecoveredBatch] = []
        for row in rows:
            reason = "程序上次未正常退出，批次已恢复为中断态；已保存数据保留，未自动续扫"
            actual_frames = count_frames_on_disk(Path(row["dir_path"] or self.batch_dir(row["batch_id"])))
            reported_frames = max(int(row["returned_frames"] or 0), actual_frames)
            with self.tx() as conn:
                conn.execute(
                    "UPDATE batches SET state=?, interrupt_reason=?, returned_frames=?, finished_at=?, updated_at=? "
                    "WHERE batch_id=?",
                    (BatchState.SEALED, reason, reported_frames, utc_now_iso(), utc_now_iso(), row["batch_id"]),
                )
            self.log_event(
                "batch.recovered",
                row["batch_id"],
                {"reason": reason, "framesOnDisk": actual_frames, "framesInDb": int(row["returned_frames"] or 0)},
            )
            recovered.append(
                RecoveredBatch(
                    batch_id=row["batch_id"],
                    component_id=row["component_id"],
                    zone_id=row["zone_id"],
                    returned_frames=reported_frames,
                    marks=row["mark_count"],
                    dir_path=row["dir_path"],
                    reason=reason,
                )
            )
        if recovered:
            log.warning("恢复 %d 个中断批次：%s", len(recovered), ", ".join(item.batch_id for item in recovered))
        return recovered

    # ---- 文件 ----

    def register_file(
        self,
        batch_id: str,
        role: str,
        rel_path: str,
        *,
        compute_hash: bool = True,
        base_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """登记批次目录里的一个文件；计算摘要用于上传与归档校验。"""
        base = Path(base_dir) if base_dir else self.batch_dir(batch_id)
        full = base / rel_path
        size = full.stat().st_size if full.is_file() else 0
        sha = hash_sample_file(str(full)) if (compute_hash and full.is_file()) else ""
        file_id = f"file-{batch_id}-{abs(hash(rel_path)) % (10**8):08d}"
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO files(id, batch_id, role, rel_path, name, size, sha256, upload_state, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(batch_id, rel_path) DO UPDATE SET
                    role=excluded.role, size=excluded.size, sha256=excluded.sha256, updated_at=excluded.updated_at
                """,
                (file_id, batch_id, role, rel_path, full.name, size, sha, UploadState.QUEUED, utc_now_iso()),
            )
        return {"fileId": file_id, "name": full.name, "role": role, "path": rel_path, "size": size, "sha256": sha}

    def files_for_batch(self, batch_id: str) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM files WHERE batch_id=? ORDER BY rel_path", (batch_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def batch_integrity(self, batch_id: str) -> Dict[str, Any]:
        """批次完整性：文件数、字节数、缺哪些必填文件（PRD §5.5、§9）。"""
        base = self.batch_dir(batch_id)
        files = self.files_for_batch(batch_id)
        total_bytes = sum(item["size"] for item in files)
        missing_required = [name for name in BATCH_REQUIRED_FILES if not (base / name).is_file()]
        missing_dirs = [name for name in BATCH_REQUIRED_DIRS if not (base / name).is_dir()]
        bad = []
        for item in files:
            full = base / item["rel_path"]
            if not full.is_file():
                bad.append({"path": item["rel_path"], "reason": "文件缺失"})
        return {
            "batchId": batch_id,
            "dirPath": str(base),
            "fileCount": len(files),
            "totalBytes": total_bytes,
            "missingRequired": missing_required,
            "missingDirs": missing_dirs,
            "broken": bad,
            "complete": not missing_required and not missing_dirs and not bad,
        }

    def set_file_upload_state(
        self,
        batch_id: str,
        rel_path: str,
        state: str,
        *,
        remote_id: str = "",
        received_offset: Optional[int] = None,
        error: str = "",
    ) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                UPDATE files SET upload_state=?, remote_id=COALESCE(NULLIF(?,''), remote_id),
                    received_offset=COALESCE(?, received_offset), last_error=?, updated_at=?
                WHERE batch_id=? AND rel_path=?
                """,
                (state, remote_id, received_offset, error, utc_now_iso(), batch_id, rel_path),
            )

    # ---- 上传任务 ----

    def upsert_upload(self, job) -> None:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO uploads(upload_id, file_id, batch_id, name, size, sha256, offset, state,
                                    attempts, last_error, remote_id, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(upload_id) DO UPDATE SET
                    offset=excluded.offset, state=excluded.state, attempts=excluded.attempts,
                    last_error=excluded.last_error, remote_id=excluded.remote_id, updated_at=excluded.updated_at
                """,
                (
                    job.upload_id,
                    job.file_id,
                    job.batch_id,
                    job.name,
                    job.size,
                    job.sha256,
                    job.received_offset,
                    job.state,
                    job.attempts,
                    job.last_error,
                    job.remote_file_id,
                    utc_now_iso(),
                ),
            )

    def pending_uploads(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM uploads WHERE state NOT IN (?,?) ORDER BY updated_at LIMIT ?",
            (UploadState.DONE, UploadState.FAILED, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def upload_totals(self) -> Dict[str, int]:
        row = self._connection().execute(
            """
            SELECT
              SUM(CASE WHEN state NOT IN ('done') THEN 1 ELSE 0 END) AS queued,
              SUM(CASE WHEN state NOT IN ('done') THEN MAX(size - offset, 0) ELSE 0 END) AS pending_bytes,
              SUM(offset) AS confirmed_bytes
            FROM uploads
            """
        ).fetchone()
        return {
            "queued": int(row["queued"] or 0),
            "pendingBytes": int(row["pending_bytes"] or 0),
            "confirmedBytes": int(row["confirmed_bytes"] or 0),
        }

    # ---- outbox ----

    def enqueue_event(self, message_id: str, type_: str, payload: Dict[str, Any], seq: int = 0) -> bool:
        """关键业务事件入 outbox（至少一次投递）。messageId 重复则忽略。"""
        try:
            with self.tx() as conn:
                conn.execute(
                    "INSERT INTO outbox(message_id, type, payload, seq, created_at) VALUES (?,?,?,?,?)",
                    (message_id, type_, json.dumps(payload, ensure_ascii=False), seq, utc_now_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def pending_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM outbox WHERE acked_at IS NULL ORDER BY seq, created_at LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_event_count(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) AS n FROM outbox WHERE acked_at IS NULL").fetchone()
        return int(row["n"] or 0)

    def ack_events(self, message_ids: Iterable[str]) -> int:
        ids = list(message_ids)
        if not ids:
            return 0
        with self.tx() as conn:
            count = 0
            for message_id in ids:
                cursor = conn.execute(
                    "UPDATE outbox SET acked_at=? WHERE message_id=? AND acked_at IS NULL",
                    (utc_now_iso(), message_id),
                )
                count += cursor.rowcount
        return count

    def note_event_failure(self, message_id: str, error: str) -> None:
        self._connection().execute(
            "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE message_id=?", (error[:300], message_id)
        )

    def drop_telemetry_events(self, keep_latest: int = 1) -> int:
        """遥测允许覆盖旧样本，不把掉线期间每秒 CPU 数据无限堆积（PRD §8.2）。"""
        rows = self._connection().execute(
            "SELECT message_id FROM outbox WHERE acked_at IS NULL AND type='device.telemetry' "
            "ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (keep_latest,),
        ).fetchall()
        ids = [row["message_id"] for row in rows]
        if not ids:
            return 0
        with self.tx() as conn:
            for message_id in ids:
                conn.execute("DELETE FROM outbox WHERE message_id=?", (message_id,))
        return len(ids)

    # ---- 命令回执 ----

    def save_receipt(self, command_id: str, receipt: Dict[str, Any]) -> None:
        """同一 commandId 的重复回执直接返回已保存结果，不重复执行（PRD §8.1）。"""
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO command_receipts(command_id, action, state, error_code, reason, target_batch, created_at, payload)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(command_id, state) DO UPDATE SET
                    reason=excluded.reason, payload=excluded.payload, created_at=excluded.created_at
                """,
                (
                    command_id,
                    receipt.get("action", ""),
                    receipt.get("state", ""),
                    receipt.get("errorCode", ""),
                    receipt.get("reason", ""),
                    receipt.get("targetBatchId", ""),
                    utc_now_iso(),
                    json.dumps(receipt, ensure_ascii=False),
                ),
            )

    def get_receipt(self, command_id: str, state: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if state:
            row = self._connection().execute(
                "SELECT payload FROM command_receipts WHERE command_id=? AND state=?", (command_id, state)
            ).fetchone()
        else:
            row = self._connection().execute(
                "SELECT payload FROM command_receipts WHERE command_id=? ORDER BY created_at DESC LIMIT 1",
                (command_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def recent_receipts(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._connection().execute(
            "SELECT payload FROM command_receipts ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def recent_events(self, batch_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        if batch_id:
            rows = self._connection().execute(
                "SELECT * FROM events WHERE batch_id=? ORDER BY seq DESC LIMIT ?", (batch_id, limit)
            ).fetchall()
        else:
            rows = self._connection().execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# 批次目录写入
# --------------------------------------------------------------------------- #

class BatchWriter:
    """把一个批次的产物写到独立目录，最后原子提交 manifest。

    写盘顺序固定为：数据文件 → marks/segments/quality/result → manifest.json（原子）。
    manifest 一旦落地就表示"这批数据完整"，之后才允许上传（PRD §8.2）。
    """

    def __init__(self, storage: Storage, batch_id: str) -> None:
        self.storage = storage
        self.batch_id = batch_id
        self.dir = storage.create_batch_dir(batch_id)
        self._frames_path = self.dir / "frames.csv"
        self._frames_handle = None
        self._frames_written = 0
        self._images: List[Dict[str, Any]] = []

    # ---- 帧数据 ----

    def open_frames(self, columns: Iterable[str] = ("frame_index", "sample_index", "amplitude", "t_ms")) -> None:
        self._frames_handle = self._frames_path.open("w", encoding="utf-8", newline="\n")
        self._frames_handle.write(",".join(columns) + "\n")

    def append_frame(self, frame_index: int, amplitudes: Iterable[float], t_ms: Optional[float] = None) -> None:
        """追加一帧的采样点。逐行写，避免把整批数据压在内存里。"""
        if self._frames_handle is None:
            raise RuntimeError("open_frames() 还没调用")
        stamp = frame_index * 100.0 if t_ms is None else t_ms
        buffer = []
        for sample_index, value in enumerate(amplitudes):
            buffer.append(f"{frame_index},{sample_index},{value:.6f},{stamp:.1f}\n")
        self._frames_handle.write("".join(buffer))
        self._frames_written += 1

    def close_frames(self) -> None:
        if self._frames_handle is not None:
            self._frames_handle.flush()
            os.fsync(self._frames_handle.fileno())
            self._frames_handle.close()
            self._frames_handle = None

    # ---- 其它产物 ----

    def write_json(self, name: str, data: Any) -> Path:
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # 原子替换：读到一半崩了也不会留下半个 JSON
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def save_image(self, frame_index: int, image_bytes: bytes, suffix: str = ".jpg") -> Dict[str, Any]:
        """保存一帧相机截图。命名与 frameId 对应，便于平台定位（PRD §9）。"""
        images_dir = self.dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        name = f"frame_{frame_index:05d}{suffix}"
        path = images_dir / name
        path.write_bytes(image_bytes)
        entry = {
            "frameId": f"frame-{frame_index:05d}",
            "file": name,
            "relPath": f"images/{name}",
            "bytes": len(image_bytes),
            "sha256": _quick_hash(image_bytes),
        }
        self._images = [item for item in self._images if item["file"] != name] + [entry]
        return entry

    @property
    def images(self) -> List[Dict[str, Any]]:
        return list(self._images)

    def write_marks_csv(self, marks: List[Dict[str, Any]]) -> None:
        lines = ["mark_id,frame_index,zone_id,operator_label,position_source,device_monotonic_ns"]
        for mark in marks:
            lines.append(
                "{},{},{},{},{},{}".format(
                    mark.get("markId", ""),
                    mark.get("frameIndex", ""),
                    mark.get("zoneId", ""),
                    mark.get("operatorLabel", "") or "-",
                    mark.get("positionSource", ""),
                    mark.get("deviceMonotonicNs", ""),
                )
            )
        self.write_text("marks.csv", "\n".join(lines) + "\n")

    def commit_manifest(self, manifest: Dict[str, Any], file_roles: Dict[str, str]) -> Dict[str, Any]:
        """原子提交 manifest（PRD §8.2）。

        同时算出 datasetHash：对包内除 manifest.json 外所有文件按路径字典序
        取 `path\\0sha256\\n` 再 hash，终端与平台用同一个算法核对"是不是同一份样例"。
        """
        self.close_frames()
        files: List[Dict[str, Any]] = []
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.dir).as_posix()
            if rel in ("manifest.json",) or rel.endswith(".tmp"):
                continue
            files.append(
                {
                    "role": file_roles.get(rel, _guess_role(rel)),
                    "path": rel,
                    "bytes": path.stat().st_size,
                    "sha256": hash_sample_file(str(path)),
                }
            )
        import hashlib

        digest = hashlib.sha256()
        for item in files:
            digest.update(item["path"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(item["sha256"].encode("utf-8"))
            digest.update(b"\n")
        dataset_hash = digest.hexdigest()

        payload = dict(manifest)
        payload["files"] = files
        payload["datasetHash"] = dataset_hash
        payload["manifestCommittedAt"] = utc_now_iso()
        self.write_json("manifest.json", payload)

        # manifest 落地后统一登记文件，之后才允许上传
        for item in files:
            self.storage.register_file(self.batch_id, item["role"], item["path"], compute_hash=False, base_dir=self.dir)
        log.info("批次 %s manifest 已原子提交：%d 个文件，datasetHash=%s", self.batch_id, len(files), dataset_hash[:12])
        return payload


def _guess_role(rel_path: str) -> str:
    """按相对路径判断文件角色。

    先查表再按前缀兜底：`images/index.json` 这种"名字里带目录名的索引文件"
    如果先走 `startswith("images/")` 就会被判成 image，索引与图片混在一个角色里，
    平台侧按角色归档时会出错。
    """
    mapping = {
        "frames.csv": "frames",
        "segments.json": "segments",
        "marks.json": "marks",
        "quality.json": "quality",
        "result.json": "result",
        "config.json": "config",
        "dataset.json": "dataset",
        "plan.json": "plan",
        "events.log": "events",
        "marks.csv": "marks_csv",
        "images/index.json": "image_index",
        "session-events.json": "session_events",
        "batch.json": "batch",
        "manifest.json": "manifest",
    }
    if rel_path in mapping:
        return mapping[rel_path]
    if rel_path.startswith("images/"):
        return "image"
    return "other"


def _quick_hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def count_frames_on_disk(batch_dir: Path) -> int:
    """数一个批次目录里 frames.csv 实际有多少帧。

    崩溃恢复、批次核对都用它：**以磁盘上的真实数据为准**，不以内存或库里的
    计数字段为准。逐行读、只认第一列变化，几十 MB 的文件也很快。
    """
    path = Path(batch_dir) / "frames.csv"
    if not path.is_file():
        return 0
    frames = 0
    last_index = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line or line[0] == "f":  # 表头
                    continue
                head = line.split(",", 1)[0]
                if head == last_index:
                    continue
                last_index = head
                frames += 1
    except OSError as exc:
        log.warning("统计 %s 的帧数失败：%s", path, exc)
        return 0
    return frames
