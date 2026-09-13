"""检测样例包登记表（PRD §3.4：三套固定样例包）。

终端不随机生成回波：所有响应序列都来自 `samples/<scenarioId>/` 下预制好的
`response-sequence-v1` 包，包里带了 datasetHash，终端与平台核对同一个 hash
就能确认"我们看的是不是同一份样例"。

三套包与剧本的对应关系：
    initial-anomaly-v1     第一幕 S10 手持初扫（扫描中触发适用域待核验）
    reference-samples-v1   第三幕 S13 参考样本采集（样本编号/来源/两条路径）
    rescan-demo-v1         第四幕 S19 复扫（三处样例异常回传）

这个文件是"剧本口径"的单一来源：帧数、异常帧号、结果分数、阶段事件都在这里，
界面与测试都从这里取，避免各处硬编码后对不上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# 剧本口径的固定数值（PRD §5.5：可保留 0.71 / 0.84 / 0.87 的剧本口径）
# --------------------------------------------------------------------------- #

#: 复扫三处样例异常。sampleIndex 是按 pointCount=420 换算后的采样点位置。
RESCAN_FINDINGS: List[Dict[str, Any]] = [
    {
        "id": "CUR-Z04-01",
        "label": "疑似严重受潮区域",
        "score": 0.71,
        "sampleIndex": 122,
        "frameIndex": 292,
        "branch": "radar",
        "evidenceSegment": "echo-Z04-lower-seg-07",
        "nextAction": "先检查周边积水、排水和渗漏来源，再安排复测",
    },
    {
        "id": "CUR-Z04-02",
        "label": "疑似虫蛀空洞（上部响应区）",
        "score": 0.84,
        "sampleIndex": 197,
        "frameIndex": 330,
        "branch": "fusion",
        "evidenceSegment": "echo-Z04-lower-seg-11",
        "nextAction": "安排进一步检测，标注内部异常响应区",
    },
    {
        "id": "CUR-Z04-03",
        "label": "疑似虫蛀空洞（下部响应区）",
        "score": 0.87,
        "sampleIndex": 260,
        "frameIndex": 372,
        "branch": "fusion",
        "evidenceSegment": "echo-Z04-lower-seg-13",
        "nextAction": "安排进一步检测，标注内部异常响应区",
    },
]

#: 初扫在扫描中触发的适用域检查事件（剧本 S12：架构师在采集页看到"适用域待核验"）
DOMAIN_CHECK_FRAME = 279
DOMAIN_CHECK_TEXT = "适用域检查未通过：模型 DEMO-M02 缺少该批次木材的标定记录"

#: 暂停请求的作用范围说明。端上只停本地采集与回放，不断健康遥测与心跳（PRD §5.3）
PAUSE_SCOPE_NOTE = (
    "作用范围：本地采集任务与检测样例回放；健康遥测、心跳与必要的事件通道继续运行。"
    "真实雷达是否停止发射需专用驱动确认。"
)


@dataclass
class ScenarioSpec:
    """一套样例包的登记信息。"""

    scenario_id: str
    round: str
    batch_id: str
    label: str
    frame_count: int
    fps: float = 10.0
    description: str = ""
    expected_interrupt: bool = False
    point_count: int = 420
    #: 该套样例最终会给出的端侧结论（复扫才有）
    findings: List[Dict[str, Any]] = field(default_factory=list)
    #: 事件：frameIndex → (level, text)。阶段推进由它驱动，不由运行时间随机决定。
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.fps if self.fps else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenarioId": self.scenario_id,
            "round": self.round,
            "batchId": self.batch_id,
            "label": self.label,
            "frameCount": self.frame_count,
            "fps": self.fps,
            "pointCount": self.point_count,
            "expectedInterrupt": self.expected_interrupt,
            "description": self.description,
            "findings": list(self.findings),
            "durationS": round(self.duration_s, 1),
        }


SCENARIOS: Dict[str, ScenarioSpec] = {
    "initial-anomaly-v1": ScenarioSpec(
        scenario_id="initial-anomaly-v1",
        round="initial",
        batch_id="scan-Z04-001",
        label="初扫（触发适用域待核验）",
        frame_count=420,
        expected_interrupt=True,
        description="Z04 下部测区首次精扫；扫描中平台请求暂停，诊断输出冻结，剩余 34 帧未采集。",
        findings=[],
        events=[
            {"frameIndex": 0, "level": "INFO", "text": "本地采集任务启动，样例回放开始（配置 CFG-02 / 模型 DEMO-M02）"},
            {"frameIndex": 60, "level": "INFO", "text": "已保存帧 60，标记数量见下方列表"},
            {"frameIndex": DOMAIN_CHECK_FRAME, "level": "WARNING", "text": DOMAIN_CHECK_TEXT},
        ],
    ),
    "reference-samples-v1": ScenarioSpec(
        scenario_id="reference-samples-v1",
        round="reference",
        batch_id="ref-batch-01",
        label="参考样本采集",
        frame_count=240,
        description="四条物理样本、两条扫描路径（0° 与 90° 换向重复）；只采集，不出缺陷结论。",
        findings=[],
        events=[
            {"frameIndex": 0, "level": "INFO", "text": "路径 path-01 开始（方向 0°，样本 S-01～S-04）"},
            {"frameIndex": 120, "level": "INFO", "text": "路径 path-02 开始（方向 90°，换向重复采集）"},
        ],
    ),
    "rescan-demo-v1": ScenarioSpec(
        scenario_id="rescan-demo-v1",
        round="rescan",
        batch_id="scan-Z04-002",
        label="复扫（三处样例异常）",
        frame_count=420,
        description="沿原路径重扫 Z04 下部；末段出现三处预制异常响应，端侧初筛后交平台复核。",
        findings=RESCAN_FINDINGS,
        events=[
            {"frameIndex": 0, "level": "INFO", "text": "复扫开始，沿原路径重扫 Z04 下部（配置 CFG-02 / 模型 DEMO-M02b）"},
            {"frameIndex": 292, "level": "WARNING", "text": "响应段 echo-Z04-lower-seg-07 幅值 0.71，端侧标记为待复核"},
            {"frameIndex": 330, "level": "WARNING", "text": "响应段 echo-Z04-lower-seg-11 幅值 0.84，端侧标记为待复核"},
            {"frameIndex": 372, "level": "WARNING", "text": "响应段 echo-Z04-lower-seg-13 幅值 0.87，端侧标记为待复核"},
            {"frameIndex": 419, "level": "INFO", "text": "复扫采集结束，生成批次记录与标记列表"},
        ],
    ),
}

#: 默认 scenario（按轮次查）
ROUND_SCENARIOS = {
    "initial": "initial-anomaly-v1",
    "reference": "reference-samples-v1",
    "rescan": "rescan-demo-v1",
}

#: 结果揭示策略：包内 result.json 里 findings 出现的时机
REVEAL_POLICY = "findings 在对应 frameIndex 到达时逐条出现，重复排练得到相同帧号与相同分数"


def scenario_for_round(round_name: str) -> Optional[ScenarioSpec]:
    key = ROUND_SCENARIOS.get(round_name)
    return SCENARIOS.get(key) if key else None


def resolve_scenario_root(configured: str = "") -> Path:
    """样例包根目录：优先配置项，其次项目根目录下的 samples/。"""
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "samples"


def discover(root: Path) -> List[Dict[str, Any]]:
    """扫描磁盘上真实存在的样例包，返回可用的 scenarioId 列表。

    自检页要用它回答"检测样例包状态"（PRD §5.2），所以以磁盘为准，
    不拿 SCENARIOS 里的常量当"包已就绪"的证据。
    """
    found: List[Dict[str, Any]] = []
    for spec in SCENARIOS.values():
        package_dir = root / spec.scenario_id
        manifest = package_dir / "manifest.json"
        entry = spec.to_dict()
        entry.update(
            {
                "packageDir": str(package_dir),
                "present": manifest.is_file(),
                "manifestPath": str(manifest),
                "reason": "" if manifest.is_file() else f"缺少 {manifest}",
            }
        )
        found.append(entry)
    return found


def available_scenarios(root: Path) -> List[str]:
    return [item["scenarioId"] for item in discover(root) if item["present"]]
