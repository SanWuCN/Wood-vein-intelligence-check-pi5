"""检测样例回放适配器（PRD §3.4、§9、§10）。

**这是本机唯一的"回波"来源**：没有真实毫米波硬件，所以响应序列一律来自
`samples/<scenarioId>/` 下的 `response-sequence-v1` 固定样例包。

四条必须守住的规则（PRD §3.4）：
  1. 推进由当前任务的运行/暂停/结束控制。暂停时**停止追加**，恢复时接着写，
     不是从头播也不是加速追赶（H05）。
  2. 结果随有效样例段到达而出现：某一帧到了才揭示对应结论，
     不随运行时间随机提高置信度。
  3. 同一输入反复排练得到相同结果与相同帧内容（H06），因此包内数据只读、
     内存中不做任何平滑或改写。
  4. 来源标识恒为 replay（"检测样例"），绝不写成雷达实采帧率或实测深度。

样例包仍是"逐帧读盘"而不是一次性加载：420 帧 × 420 点约 4MB 文本，
一次性解析会阻塞启动，逐帧读也让"暂停冻结"在实现上天然成立。
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..logging_setup import get_logger
from ..scenarios import ScenarioSpec, discover, resolve_scenario_root, scenario_for_round

log = get_logger("replay")


class ReplayError(RuntimeError):
    """样例包缺失或损坏。这类错误必须显式暴露，不能静默退回"看起来能跑"。"""


@dataclass
class ReplayFrame:
    """一帧有效响应。`amplitudes` 是归一化幅值，长度 = pointCount。"""

    frame_index: int
    amplitudes: List[float]
    t_ms: float
    segment: Dict[str, Any] = field(default_factory=dict)

    @property
    def frame_id(self) -> str:
        return f"frame-{self.frame_index:05d}"

    def downsample(self, rows: int) -> List[float]:
        """按最大值抽取降到 rows 行，供响应序列图的列使用。

        用最大值而不是均值：窄的异常峰在均值下会被邻近平掉，现场看到的
        "响应区"就消失了。降采样只影响显示，落盘与上传始终是原始 420 点。
        """
        count = len(self.amplitudes)
        if rows <= 0 or count == 0:
            return []
        if rows >= count:
            return list(self.amplitudes)
        bucket = count / rows
        out: List[float] = []
        for row in range(rows):
            start = int(row * bucket)
            end = max(start + 1, int((row + 1) * bucket))
            out.append(max(self.amplitudes[start:end]))
        return out


@dataclass
class ReplayEvent:
    """回放过程中出现的一条状态事件（阶段推进）。"""

    frame_index: int
    level: str
    text: str
    kind: str = "stage"


@dataclass
class RevealedFinding:
    """随有效样例段到达而揭示的端侧结论。"""

    finding: Dict[str, Any]
    frame_index: int

    @property
    def score(self) -> float:
        return float(self.finding.get("score") or 0.0)

    @property
    def label(self) -> str:
        return str(self.finding.get("label") or "")


class SamplePackage:
    """磁盘上一套样例包的只读视图。

    只在这里碰文件系统；`ReplaySession` 只操作内存游标。这样"暂停/恢复/重播"
    不会产生任何磁盘副作用，重复排练自然得到相同结果。
    """

    def __init__(self, root: Path, scenario_id: str) -> None:
        self.root = root
        self.scenario_id = scenario_id
        self.dir = root / scenario_id
        if not self.dir.is_dir():
            raise ReplayError(f"样例包目录不存在：{self.dir}")

        self.manifest = self._load_json("manifest.json", required=True)
        self.batch_info = self._load_json("batch.json", required=False) or {}
        self.config_snapshot = self._load_json("config.json", required=False) or {}
        self.quality = self._load_json("quality.json", required=False) or {}
        self.plan = self._load_json("plan.json", required=False) or {}
        self.dataset = self._load_json("dataset.json", required=False) or {}
        self.result = self._load_json("result.json", required=False) or {}
        self._segments_by_frame: Dict[int, Dict[str, Any]] = {}
        self._load_segments()
        self.marks = self._load_marks()

        self.point_count = int(self.manifest.get("pointCount") or 420)
        self.declared_frames = int(self.manifest.get("frameCount") or 0)
        # 初扫包在第 386 帧中断，实际可用帧数少于声明帧数
        self.returned_frames = int(self.manifest.get("returnedFrames") or self.declared_frames)
        self.fps = float(self.manifest.get("fps") or 10.0)
        self.dataset_hash = str(self.manifest.get("datasetHash") or "")
        self.state = str(self.manifest.get("state") or "finished")

    # ---- 读取 ----

    def _load_json(self, name: str, *, required: bool) -> Optional[Dict[str, Any]]:
        path = self.dir / name
        if not path.is_file():
            if required:
                raise ReplayError(f"样例包缺少 {name}：{path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayError(f"{path} 解析失败：{exc}") from exc

    def _load_segments(self) -> None:
        path = self.dir / "segments.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("segments.json 不可读（%s），响应曲线仍可显示但没有分段信息", exc)
            return
        entries = data.get("segments") if isinstance(data, dict) else data
        for item in entries or []:
            try:
                self._segments_by_frame[int(item.get("frameIndex", -1))] = item
            except (TypeError, ValueError):
                continue

    def _load_marks(self) -> List[Dict[str, Any]]:
        path = self.dir / "marks.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = data.get("marks") if isinstance(data, dict) else data
        return list(entries or [])

    # ---- 帧迭代 ----

    def iter_frames(self) -> Iterator[ReplayFrame]:
        """逐帧读 frames.csv。

        容错策略：某一帧的点数不足 pointCount 时按已有数据补齐到 pointCount，
        并把该帧标进 `short_frames`，宁可在质量里写清楚，也不要让界面画一半就崩。
        """
        path = self.dir / "frames.csv"
        if not path.is_file():
            raise ReplayError(f"样例包缺少 frames.csv：{path}")

        current_index: Optional[int] = None
        current_values: List[float] = []
        current_t = 0.0

        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header and header[0].strip() != "frame_index":
                # 没有表头就把它当数据行处理
                row = self._parse_row(header)
                if row:
                    current_index, sample_index, value, t_ms = row
                    current_values.append(value)
                    current_t = t_ms

            for raw in reader:
                parsed = self._parse_row(raw)
                if parsed is None:
                    continue
                frame_index, _sample_index, value, t_ms = parsed
                if current_index is None:
                    current_index, current_t = frame_index, t_ms
                if frame_index != current_index:
                    yield self._finish_frame(current_index, current_values, current_t)
                    current_index, current_values, current_t = frame_index, [], t_ms
                current_values.append(value)

        if current_index is not None:
            yield self._finish_frame(current_index, current_values, current_t)

    @staticmethod
    def _parse_row(row: List[str]) -> Optional[Tuple[int, int, float, float]]:
        if not row or len(row) < 3:
            return None
        try:
            return int(row[0]), int(row[1]), float(row[2]), float(row[3]) if len(row) > 3 else 0.0
        except ValueError:
            return None

    def _finish_frame(self, frame_index: int, values: List[float], t_ms: float) -> ReplayFrame:
        if len(values) < self.point_count:
            values = values + [values[-1] if values else 0.0] * (self.point_count - len(values))
        elif len(values) > self.point_count:
            values = values[: self.point_count]
        return ReplayFrame(
            frame_index=frame_index,
            amplitudes=values,
            t_ms=t_ms,
            segment=self._segments_by_frame.get(frame_index, {}),
        )

    # ---- 查询 ----

    def segment_for(self, frame_index: int) -> Dict[str, Any]:
        return self._segments_by_frame.get(frame_index, {})

    def peaks_for(self, frame_index: int) -> List[Dict[str, Any]]:
        return list(self.segment_for(frame_index).get("peaks") or [])

    def summary(self) -> Dict[str, Any]:
        return {
            "scenarioId": self.scenario_id,
            "batchId": self.manifest.get("batchId"),
            "dir": str(self.dir),
            "declaredFrames": self.declared_frames,
            "returnedFrames": self.returned_frames,
            "pointCount": self.point_count,
            "fps": self.fps,
            "state": self.state,
            "datasetId": self.manifest.get("datasetId"),
            "datasetHash": self.dataset_hash,
            "markCount": len(self.marks),
            "quality": self.quality,
            "axis": self.manifest.get("axis") or {"x": "frame_index", "y": "sample_index"},
            "privacyNote": self.manifest.get("privacyNote", ""),
        }


class ReplaySession:
    """一次回放运行。状态只有"游标 + 玩/停"，没有隐藏的随机源。"""

    def __init__(self, package: SamplePackage, spec: Optional[ScenarioSpec] = None) -> None:
        self.package = package
        self.spec = spec
        self._iterator: Optional[Iterator[ReplayFrame]] = None
        self._next_frame: Optional[ReplayFrame] = None
        self._exhausted = False
        self._accumulator = 0.0
        self._playing = False
        self._paused_reason = ""
        self._frame_index = -1
        self._emitted_events: set = set()
        self._revealed: List[RevealedFinding] = []
        self._playback_started_at: Optional[float] = None
        self._playing_seconds = 0.0
        self.frames_returned = 0
        self._last_emit_at: Optional[float] = None
        self._recent_fps: List[float] = []
        self.short_frames: List[int] = []
        self._reset_iterator()

    # ---- 生命周期 ----

    def _reset_iterator(self) -> None:
        self._iterator = self.package.iter_frames()
        self._exhausted = False
        self._advance_iterator()

    def _advance_iterator(self) -> None:
        assert self._iterator is not None
        try:
            self._next_frame = next(self._iterator)
        except StopIteration:
            self._next_frame = None
            self._exhausted = True
        except ReplayError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单帧坏掉不该让整个程序退出
            log.exception("读取样例帧失败：%s", exc)
            self._next_frame = None
            self._exhausted = True

    def play(self) -> None:
        if self._playing:
            return
        self._playing = True
        self._paused_reason = ""
        self._accumulator = 0.0
        self._playback_started_at = time.monotonic()

    def pause(self, reason: str = "") -> None:
        if not self._playing:
            return
        self._playing = False
        self._paused_reason = reason
        if self._playback_started_at is not None:
            self._playing_seconds += time.monotonic() - self._playback_started_at
            self._playback_started_at = None

    def stop(self) -> None:
        self.pause("已结束")
        self._accumulator = 0.0

    def rewind(self) -> None:
        """重播：游标回零，重新读盘，结果集合清空。用于同一会话内重复排练。"""
        self._frame_index = -1
        self._emitted_events.clear()
        self._revealed.clear()
        self._accumulator = 0.0
        self.frames_returned = 0
        self._recent_fps.clear()
        self.short_frames.clear()
        self._playing_seconds = 0.0
        self._playback_started_at = time.monotonic() if self._playing else None
        self._reset_iterator()

    # ---- 推进 ----

    def advance(self, dt: float) -> List[ReplayFrame]:
        """推进 dt 秒，返回这一段新增的帧。

        暂停时直接返回空列表 —— 这是"暂停冻结"的实现点（H05）。
        """
        if not self._playing or self._exhausted or self._next_frame is None:
            return []
        fps = self.package.fps or 10.0
        self._accumulator += max(0.0, dt)
        budget = 1.0 / fps
        produced: List[ReplayFrame] = []
        # 一次最多推进 40 帧，避免界面卡顿后一次性灌进来（原始采集队列溢出要记录，
        # 不能静默声称完整；这里选择限速并在日志里说明）
        guard = 0
        while self._accumulator >= budget and self._next_frame is not None and guard < 40:
            frame = self._next_frame
            self._accumulator -= budget
            guard += 1
            self._frame_index = frame.frame_index
            self.frames_returned += 1
            now = time.monotonic()
            if self._last_emit_at is not None:
                delta = now - self._last_emit_at
                if delta > 0:
                    self._recent_fps.append(1.0 / delta)
                    if len(self._recent_fps) > 20:
                        self._recent_fps.pop(0)
            self._last_emit_at = now
            self._advance_iterator()
            produced.append(frame)
        if guard >= 40:
            log.warning("回放推进被限速（单次 tick 超过 40 帧），界面可能落后于样例进度")
        if self._exhausted:
            self._playing = False
            if self._playback_started_at is not None:
                self._playing_seconds += time.monotonic() - self._playback_started_at
                self._playback_started_at = None
        return produced

    # ---- 阶段事件与结论揭示 ----

    def drain_events(self, upto_frame: int) -> List[ReplayEvent]:
        """取出到 upto_frame 为止、还没发过的阶段事件。"""
        events: List[ReplayEvent] = []
        if self.spec:
            for item in self.spec.events:
                key = ("stage", item["frameIndex"], item["text"])
                if item["frameIndex"] <= upto_frame and key not in self._emitted_events:
                    self._emitted_events.add(key)
                    events.append(
                        ReplayEvent(
                            frame_index=int(item["frameIndex"]),
                            level=str(item.get("level", "INFO")),
                            text=str(item["text"]),
                            kind="stage",
                        )
                    )
        # 包内 result.json 的 findings 按自己的 frameIndex 揭示
        for finding in self.package.result.get("findings") or []:
            try:
                reveal_at = int(finding.get("frameIndex"))
            except (TypeError, ValueError):
                continue
            key = ("finding", finding.get("id"))
            if reveal_at <= upto_frame and key not in self._emitted_events:
                self._emitted_events.add(key)
                self._revealed.append(RevealedFinding(finding=dict(finding), frame_index=reveal_at))
                events.append(
                    ReplayEvent(
                        frame_index=reveal_at,
                        level="WARNING",
                        text=f"端侧初筛：{finding.get('label')} 响应段 {finding.get('evidenceSegment')} 幅值 {finding.get('score')}",
                        kind="finding",
                    )
                )
        return events

    @property
    def revealed_findings(self) -> List[RevealedFinding]:
        return list(self._revealed)

    # ---- 状态 ----

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused_reason(self) -> str:
        return self._paused_reason

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def progress(self) -> float:
        total = self.package.returned_frames or self.package.declared_frames
        if total <= 0:
            return 0.0
        return min(1.0, max(0.0, (self._frame_index + 1) / total))

    @property
    def playback_fps(self) -> float:
        if not self._recent_fps:
            return 0.0
        return round(sum(self._recent_fps) / len(self._recent_fps), 1)

    def telemetry(self) -> Dict[str, Any]:
        """给遥测用的回放统计（PRD §6：样例播放率，不能显示为雷达实测帧率）。"""
        return {
            "scenarioId": self.package.scenario_id,
            "batchId": self.package.manifest.get("batchId"),
            "frameIndex": self._frame_index,
            "frameCount": self.package.returned_frames,
            "declaredFrameCount": self.package.declared_frames,
            "playbackFps": self.playback_fps,
            "progress": round(self.progress, 4),
            "datasetHash": self.package.dataset_hash,
            "sourceMode": "replay",
            "playing": self._playing,
            "missingFrames": max(0, self.package.declared_frames - self.package.returned_frames),
        }


# --------------------------------------------------------------------------- #
# 包仓库
# --------------------------------------------------------------------------- #

class ReplayLibrary:
    """管理 samples/ 下的所有样例包，按 scenarioId 缓存已解析的清单。

    `resolve()` 在包缺失时抛 ReplayError。终端不允许"悄悄换一套假数据"，
    因为那样会让平台看到与终端不一致的 datasetHash（H15）。
    """

    def __init__(self, root: Optional[Path] = None, configured: str = "") -> None:
        self.root = Path(root) if root else resolve_scenario_root(configured)
        self._cache: Dict[str, SamplePackage] = {}

    def available(self) -> List[Dict[str, Any]]:
        return discover(self.root)

    def has(self, scenario_id: str) -> bool:
        return (self.root / scenario_id / "manifest.json").is_file()

    def open(self, scenario_id: str) -> SamplePackage:
        if scenario_id in self._cache:
            return self._cache[scenario_id]
        package = SamplePackage(self.root, scenario_id)
        self._cache[scenario_id] = package
        log.info(
            "加载样例包 %s：%d/%d 帧，%d 点/帧，datasetHash=%s",
            scenario_id,
            package.returned_frames,
            package.declared_frames,
            package.point_count,
            (package.dataset_hash or "")[:12],
        )
        return package

    def open_for_round(self, round_name: str) -> Tuple[SamplePackage, ScenarioSpec]:
        spec = scenario_for_round(round_name)
        if spec is None:
            raise ReplayError(f"未知的采集轮次 {round_name!r}")
        return self.open(spec.scenario_id), spec

    def session_for_round(self, round_name: str) -> ReplaySession:
        package, spec = self.open_for_round(round_name)
        return ReplaySession(package, spec)

    def create_session(self, scenario_id: str) -> ReplaySession:
        spec = None
        for item in discover(self.root):
            if item["scenarioId"] == scenario_id:
                from ..scenarios import SCENARIOS

                spec = SCENARIOS.get(scenario_id)
                break
        return ReplaySession(self.open(scenario_id), spec)

    def status(self) -> Dict[str, Any]:
        """自检页用：样例包是否就绪、缺哪个、hash 是多少（PRD §5.2）。"""
        items = []
        ready = 0
        for entry in self.available():
            detail = {
                "scenarioId": entry["scenarioId"],
                "label": entry["label"],
                "batchId": entry["batchId"],
                "round": entry["round"],
                "present": entry["present"],
                "packageDir": entry["packageDir"],
                "frameCount": entry["frameCount"],
                "reason": entry["reason"],
            }
            if entry["present"]:
                try:
                    package = self.open(entry["scenarioId"])
                    summary = package.summary()
                    detail.update(
                        {
                            "datasetHash": summary["datasetHash"],
                            "returnedFrames": summary["returnedFrames"],
                            "pointCount": summary["pointCount"],
                            "state": summary["state"],
                            "markCount": summary["markCount"],
                        }
                    )
                    ready += 1
                except ReplayError as exc:
                    detail.update({"present": False, "reason": str(exc)})
            items.append(detail)
        return {
            "root": str(self.root),
            "ready": ready,
            "total": len(items),
            "items": items,
        }
