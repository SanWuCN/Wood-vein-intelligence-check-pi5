#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""固定检测样例包生成器（树莓派手持终端的"回波"来源）。

设备没有真实毫米波回波、没有 IMU，终端播放的响应序列全部来自
`samples/<scenarioId>/` 下预制好的 `response-sequence-v1` 包。因此这个生成器
是"剧本口径"的落盘实现：帧数、异常帧号、峰值幅值、结论分数、阶段事件都在这里
一次性写死，终端与平台核对同一个 `datasetHash` 就能确认"看的是不是同一份样例"。

三条硬约束（决定了下面的所有实现细节）：

1. **只用标准库**。树莓派现场不一定有 numpy/PIL，生成器本身也要能在 CI 与
   开发机上直接跑，所以 PNG 用 `zlib` + `struct` 手写，字体用自带的 5×7 点阵。
2. **字节级可复现**。不使用 `time.time()`、`uuid4()`、全局随机状态；所有时间字段
   是固定常量或由帧号推导；随机数一律走 `random.Random(<固定整数种子>)`；
   文本文件统一以 `\\n` 换行写盘（目标机是 Linux）。同一份输入重复生成，
   每个文件的 sha256 必须完全一致 —— `--self-test` 会真的生成两遍来证明这一点。
3. **异常幅值与结果分数必须是同一组数**。平台剧本把三处异常钉成
   0.71 / 0.84 / 0.87（复扫，帧 292 / 330 / 372）。如果样例里另算一套幅值，
   演练时终端画出来的峰高与 result.json 里的分数就会对不上，评审一眼就能看出
   "数据是编的"。所以 `segments.json` 的 `peaks[].amplitude` 与 `result.json`
   的 `findings[].score` 同源同值，`--verify` 会逐个比对（见 `_check_findings`）。

命令行：

    python tools/make_samples.py --out samples --force   # 生成三套包
    python tools/make_samples.py --verify samples        # 只校验，退出码 0/1
    python tools/make_samples.py --self-test             # 临时目录生成→校验→比对→删除

契约对齐说明（与 F:\\1\\pi5\\woodpulse 下的终端实现对齐，README 有完整表格）：

* `frameId`        = `frame-{帧号:05d}`     —— 同 `adapters/replay.py` 的 `ReplayFrame.frame_id`
* 图片文件名        = `frame_{帧号:05d}.png` —— 同 `storage.py` 的 `BatchWriter.save_image`
* `frames.csv`     = 表头 + `amplitude` 6 位小数、`t_ms` 1 位小数（= 帧号 × 100.0）
* `datasetHash`    = 包内除 `manifest.json` 外所有文件按路径字典序拼 `path\\0sha256\\n` 再 sha256
                     —— 同 `storage.py` 的 `BatchWriter.commit_manifest`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shutil
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 0. 控制台编码
# --------------------------------------------------------------------------- #

def _configure_stdio() -> None:
    """输出被重定向时强制 UTF-8。

    树莓派是 UTF-8，Windows 控制台是 GBK；被管道/文件重定向时 Python 会用本地
    代码页编码，报告里的中文就变成乱码。报告是给人看的，这里统一成 UTF-8。
    接在真实控制台上时不改（Python 自己会走 WriteConsoleW，本来就是对的）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 编码设置失败不该影响生成
            pass


# --------------------------------------------------------------------------- #
# 1. 终端契约（优先从 woodpulse.contracts 读，读不到就用同值字面量）
# --------------------------------------------------------------------------- #

# 生成器位于 <项目根>/tools/ 下，项目根要能被 import，才能读到终端的契约模块。
# 注意：只 import 不修改平台工程里的任何文件。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:  # pragma: no cover - 取决于运行位置
    from woodpulse import contracts as _CONTRACTS  # type: ignore
except Exception:  # noqa: BLE001 - 允许生成器脱离终端代码单独运行
    _CONTRACTS = None

CONTRACT_SOURCE = "woodpulse.contracts" if _CONTRACTS is not None else "内置字面量"


def _contract(name: str, fallback: Any) -> Any:
    value = getattr(_CONTRACTS, name, None) if _CONTRACTS is not None else None
    return fallback if value is None else value


SCHEMA_VERSION: str = _contract("SCHEMA_VERSION", "1.0")
SAMPLE_FORMAT: str = _contract("SAMPLE_FORMAT", "response-sequence-v1")
FRAMES_CSV_COLUMNS: Tuple[str, ...] = tuple(
    _contract("FRAMES_CSV_COLUMNS", ("frame_index", "sample_index", "amplitude", "t_ms"))
)
MARKS_CSV_COLUMNS: Tuple[str, ...] = tuple(
    _contract(
        "MARKS_CSV_COLUMNS",
        ("mark_id", "frame_index", "zone_id", "operator_label", "position_source", "device_monotonic_ns"),
    )
)
OPERATOR_LABELS: Tuple[str, ...] = tuple(_contract("OPERATOR_LABELS", ("正面", "右侧", "背面", "左侧", "自定义")))
SEGMENT_KEYS: Tuple[str, ...] = tuple(
    _contract(
        "SEGMENT_KEYS",
        (
            "segmentId", "batchId", "frameId", "frameIndex", "zoneId", "tNs", "deviceMonotonicNs",
            "sampleCount", "axes", "sourceMode", "pairedImage", "quality", "peaks",
        ),
    )
)
MARK_KEYS: Tuple[str, ...] = tuple(
    _contract(
        "MARK_KEYS",
        (
            "markId", "batchId", "frameId", "cameraAssetId", "deviceMonotonicNs",
            "operatorLabel", "positionSource", "note", "createdAt",
        ),
    )
)
BATCH_REQUIRED_FILES: Tuple[str, ...] = tuple(
    _contract("BATCH_REQUIRED_FILES", ("manifest.json", "config.json", "marks.json", "segments.json", "frames.csv", "quality.json"))
)

# --------------------------------------------------------------------------- #
# 2. 剧本口径的固定常量
# --------------------------------------------------------------------------- #

PROJECT_ID = "temple-demo"
ORDER_ID = "SH-2026-0901"
COMPONENT_ID = "Z04"
ZONE_ID = "Z04-lower"
SEGMENT_ID = "segment-02"          # 部位段号（Z04 下部），与异常段号 echo-...-seg-NN 不是一回事
OPERATOR_ID = "rao"                # 饶
DEVICE_ID = "handheld-02"
CONFIG_VERSION = "CFG-02"
POSITION_SOURCE = "operator_tag"   # 没有 IMU/标定定位，绝不猜方位
RADAR_SOURCE_MODE = "replay"       # 响应序列来自预制样例，不是雷达实采
CAMERA_SOURCE_MODE = "live"
PAIRING = "unverified"             # 实拍与样例没有做过时间配对校验

POINT_COUNT = 420                  # 采样点/频点索引轴长度
FPS = 10
IMAGE_EVERY = 10                   # 每 10 帧一张预览图（fps=10 即约 1 张/秒）
IMAGE_WIDTH = 640                  # 与终端相机预览一致（16:9）
IMAGE_HEIGHT = 360

FRAME_ID_DIGITS = 5                # frame-00250；与 replay.py / storage.py 一致
IMAGE_ID_DIGITS = 5                # frame_00250.png

#: 每套包一个独立随机种子常量。用 random.Random(种子 + 帧号*7919) 派生逐帧发生器，
#: 这样"同一帧"的噪声只由帧号决定，与遍历顺序无关，重跑必然一致。
SEED_INITIAL = 20260901
SEED_REFERENCE = 20260902
SEED_RESCAN = 20260903

#: 基线相位种子（三套包各不相同，避免三套曲线看起来是同一份）。
PHASE_INITIAL = 0.37
PHASE_REFERENCE = 1.11
PHASE_RESCAN = 2.03

#: 噪声幅度（归一化幅值上的均匀噪声半宽）。刻意取小：样例要看的是"峰"，
#: 噪声太大反而让演示现场看不出响应区。
NOISE_AMPLITUDE = 0.012
#: 高斯峰的宽度参数（剧本给定：exp(-((x-px)^2)/0.0009)）。
GAUSS_SIGMA_SQ = 0.0009
#: 超过这个窗口的 exp(-16)≈1e-7，直接跳过，省掉无意义的 exp 计算。
GAUSS_WINDOW = 0.12
#: 幅值上下限（归一化）。上限 0.99 而不是 1.0：留一格给"饱和"这种质量状态。
AMP_MIN = 0.02
AMP_MAX = 0.99
#: 峰值只有超过这个高度才写进 segments.peaks（否则每帧都报一堆本底噪声峰）。
PEAK_REPORT_MIN = 0.08
#: 本底电平估计窗口的采样点数，写进 quality.levelSamples。
LEVEL_SAMPLES = 64

#: 固定的时间常量。绝不取系统时间：样例包必须"什么时候生成都长一样"。
CONFIG_PUBLISHED_AT = "2026-09-11T12:41:00Z"
CAPTURE_START_INITIAL = "2026-09-11T12:42:00Z"
CAPTURE_START_REFERENCE = "2026-09-11T13:05:00Z"
CAPTURE_START_RESCAN = "2026-09-11T13:26:00Z"

PRIVACY_NOTE = "所有响应均为预制样例(replay)，不是雷达实采，也不代表木柱内部真实结构"

#: 帧号 → 纳秒。10 fps ⇒ 100 ms/帧。tNs / deviceMonotonicNs / t_ms 共用同一时间轴，
#: 保证 CSV（毫秒）与 segments（纳秒）换算后严格相等。
NS_PER_FRAME = 100_000_000
MS_PER_FRAME = 100.0

#: 平台请求暂停的作用范围（剧本原文，必须逐字出现在 events.log 里）。
PAUSE_REQUEST_TEXT = "平台请求暂停。作用范围：本地采集任务与样例回放，健康遥测与心跳继续运行"
DOMAIN_CHECK_TEXT = "适用域检查未通过：模型 DEMO-M02 缺少该批次木材的标定记录"
DOMAIN_CHECK_FRAME = 279


# --------------------------------------------------------------------------- #
# 3. 5×7 点阵字模（只覆盖 0-9 A-Z 与 - . _ 和空格）
# --------------------------------------------------------------------------- #
#
# 为什么不画真字：树莓派上装字体文件、找 FreeType、处理中文渲染都会把生成器
# 拖进非标准库依赖。标签内容只有固定的机器码（测区 + 帧号 + 样本号），
# 40 个字形足够，点阵表还能顺手保证"同样的字永远像素级一样"。
FONT_5X7: Dict[str, Tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11100", "10010", "10001", "10001", "10001", "10010", "11100"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "10001", "11001", "10101", "10011", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}

RGB = Tuple[int, int, int]

#: 画面配色（都是固定常量，渲染完全确定）。
COL_SILVER: RGB = (222, 222, 228)      # 柱面高光（银灰）
COL_WARM_GRAY: RGB = (132, 120, 106)   # 柱面边缘（暖灰）
COL_GRAIN: RGB = (74, 62, 50)          # 木纹
COL_AMBER: RGB = (245, 176, 65)        # 疑似受潮标记
COL_RED: RGB = (226, 74, 66)           # 疑似空洞标记
COL_STRIP: RGB = (255, 255, 255)       # 底部标签条（白色半透明）
COL_STRIP_TEXT: RGB = (28, 32, 38)
COL_PROGRESS: RGB = (32, 74, 96)
COL_RULER: RGB = (48, 52, 58)

#: 参考样本四个物理样本的边框颜色（用来在画面上区分 S-01..S-04）。
SAMPLE_COLORS: Dict[str, RGB] = {
    "S-01": (63, 163, 77),
    "S-02": (59, 130, 246),
    "S-03": (245, 158, 11),
    "S-04": (139, 92, 246),
}


# --------------------------------------------------------------------------- #
# 4. 极简绘图（内存 RGB 位图 + 手写 PNG）
# --------------------------------------------------------------------------- #

class Canvas:
    """固定 640×360 的 RGB 画布。

    直接在 `bytearray` 上按 (y*width + x)*3 寻址。不上任何图像库，
    因为生成器必须能在只有 CPython 标准库的树莓派上跑起来。
    """

    __slots__ = ("width", "height", "buf")

    def __init__(self, width: int, height: int, buf: Optional[bytearray] = None) -> None:
        self.width = width
        self.height = height
        self.buf = bytearray(width * height * 3) if buf is None else buf

    def copy(self) -> "Canvas":
        return Canvas(self.width, self.height, bytearray(self.buf))

    def set_px(self, x: int, y: int, color: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.buf[offset] = color[0]
            self.buf[offset + 1] = color[1]
            self.buf[offset + 2] = color[2]

    def blend_px(self, x: int, y: int, color: RGB, alpha: float) -> None:
        if alpha <= 0.0 or not (0 <= x < self.width and 0 <= y < self.height):
            return
        if alpha > 1.0:
            alpha = 1.0
        offset = (y * self.width + x) * 3
        for channel in range(3):
            src = self.buf[offset + channel]
            self.buf[offset + channel] = int(src + (color[channel] - src) * alpha + 0.5)

    def fill_rect(self, x0: int, y0: int, x1: int, y1: int, color: RGB, alpha: float = 1.0) -> None:
        for y in range(max(0, y0), min(self.height, y1)):
            for x in range(max(0, x0), min(self.width, x1)):
                self.blend_px(x, y, color, alpha)

    def stroke_rect(self, x0: int, y0: int, x1: int, y1: int, color: RGB, thickness: int = 1, alpha: float = 1.0) -> None:
        for t in range(thickness):
            for x in range(x0, x1):
                self.blend_px(x, y0 + t, color, alpha)
                self.blend_px(x, y1 - 1 - t, color, alpha)
            for y in range(y0, y1):
                self.blend_px(x0 + t, y, color, alpha)
                self.blend_px(x1 - 1 - t, y, color, alpha)

    def vline(self, x: int, y0: int, y1: int, color: RGB, width: int = 1, alpha: float = 1.0) -> None:
        for w in range(width):
            for y in range(max(0, y0), min(self.height, y1)):
                self.blend_px(x + w, y, color, alpha)

    def hline(self, y: int, x0: int, x1: int, color: RGB, width: int = 1, alpha: float = 1.0) -> None:
        for w in range(width):
            for x in range(max(0, x0), min(self.width, x1)):
                self.blend_px(x, y + w, color, alpha)

    def draw_text(self, x: int, y: int, text: str, color: RGB, scale: int = 2, alpha: float = 1.0) -> None:
        """用 5×7 点阵画字符串。不在字模表里的字符按空格处理（不抛异常）。"""
        cursor = x
        for char in text.upper():
            glyph = FONT_5X7.get(char, FONT_5X7[" "])
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                self.blend_px(cursor + col * scale + dx, y + row * scale + dy, color, alpha)
            cursor += (5 + 1) * scale

    def to_png(self) -> bytes:
        """按 PNG 规范拼块输出。

        只写 IHDR/IDAT/IEND：没有 tIME、没有 tEXt，所以同样的像素永远得到
        同样的字节。逐行 filter 固定为"第 0 行 None、其余行 Up(2)"——柱面背景
        主要是横向渐变加少量竖向木纹，相邻两行几乎一样，逐行差分后绝大多数
        字节为 0，压缩率比全 None 高一个数量级。filter 选择本身是确定的，
        不影响可复现性；手写 PNG 而不是调图像库，是为了让"只依赖标准库"成立。
        """
        stride = self.width * 3
        raw = bytearray()
        for y in range(self.height):
            start = y * stride
            if y == 0:
                raw.append(0)  # filter type 0 = None
                raw += self.buf[start:start + stride]
                continue
            raw.append(2)  # filter type 2 = Up
            previous = start - stride
            for offset in range(stride):
                raw.append((self.buf[start + offset] - self.buf[previous + offset]) & 0xFF)
        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)  # 8bit truecolor RGB
        return b"".join(
            (
                b"\x89PNG\r\n\x1a\n",
                _png_chunk(b"IHDR", ihdr),
                _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
                _png_chunk(b"IEND", b""),
            )
        )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def build_background() -> Canvas:
    """柱面渐变 + 竖向木纹的固定背景。

    背景与帧号无关，所以每套包只算一次再逐帧复制：640×360 的逐像素三角函数
    算 400 多次会很慢，算一次只要几十毫秒。

    造型分三层：横向的柱面高光（中间亮、两侧收暗）、逐列的宽窄木纹、
    纵向的柔和明暗。刻意不用逐像素高频噪声：那种"纹理"在 640×360 上看起来
    像摩尔纹而不是木头，而且会让 PNG 几乎无法压缩。
    """
    canvas = Canvas(IMAGE_WIDTH, IMAGE_HEIGHT)
    # 木纹：位置/宽度/深度都写死，不能用随机数，否则背景就不可复现了。
    grains: Tuple[Tuple[int, int, float], ...] = (
        (68, 5, 0.16), (126, 3, 0.09), (188, 7, 0.13), (252, 4, 0.08),
        (318, 6, 0.15), (372, 3, 0.07), (436, 8, 0.12), (502, 4, 0.10),
        (566, 6, 0.14), (612, 3, 0.08),
    )
    # 纵向明暗只算一次（与 x 无关），逐行复用。
    row_shade: List[float] = []
    for y in range(IMAGE_HEIGHT):
        v = y / (IMAGE_HEIGHT - 1)
        shade = 1.0 - 0.18 * abs(v - 0.45) * 2.0          # 上下端略暗
        shade *= 1.0 + 0.04 * math.sin(v * 3.4 + 0.6)     # 一层很缓的打光起伏
        row_shade.append(shade)

    for x in range(IMAGE_WIDTH):
        u = x / (IMAGE_WIDTH - 1)
        # 柱面高光：中间亮、两侧收暗，用 sin 的 0.7 次幂让高光带更宽一些。
        cylinder = math.sin(math.pi * u) ** 0.7
        base = [
            COL_WARM_GRAY[i] + (COL_SILVER[i] - COL_WARM_GRAY[i]) * cylinder
            for i in range(3)
        ]
        grain = 0.0
        for gx, gw, gd in grains:
            delta = (x - gx) / float(gw)
            if abs(delta) < 3.0:
                grain += gd * math.exp(-delta * delta)
        # 宽幅木纹（低频慢变），让柱面不是一根均匀的圆柱。
        figure = 0.03 * math.sin(x * 0.021 + 1.3) + 0.02 * math.sin(x * 0.045 + 0.7)
        attenuation = max(0.0, 1.0 - grain - figure)
        column = [base[i] * attenuation for i in range(3)]
        for y in range(IMAGE_HEIGHT):
            factor = row_shade[y]
            offset = (y * IMAGE_WIDTH + x) * 3
            for channel in range(3):
                value = column[channel] * factor
                if value < 0.0:
                    value = 0.0
                elif value > 255.0:
                    value = 255.0
                canvas.buf[offset + channel] = int(value + 0.5)
    return canvas


def render_image(
    background: Canvas,
    *,
    frame_index: int,
    planned_frames: int,
    label: str,
    markers: Sequence[Tuple[int, int, int, int, RGB]],
    border: Optional[RGB] = None,
    ruler: bool = False,
) -> bytes:
    """在当前帧的画面上叠标记、进度线与标签条，返回 PNG 字节。"""
    canvas = background.copy()

    if ruler:
        # 参考样本的标尺：一条横线 + 每 20px 小刻度、每 100px 大刻度 + 三个刻度值。
        canvas.hline(18, 24, IMAGE_WIDTH - 24, COL_RULER, width=1, alpha=0.75)
        for step in range(0, 13):
            x = 24 + step * 48
            if x > IMAGE_WIDTH - 24:
                break
            major = step % 5 == 0
            canvas.vline(x, 18, 30 if major else 25, COL_RULER, width=2 if major else 1, alpha=0.85)
        for text, x in (("0.0", 20), ("0.5", 296), ("1.0", 572)):
            canvas.draw_text(x, 4, text, COL_RULER, scale=1, alpha=0.9)

    for x0, y0, x1, y1, color in markers:
        canvas.fill_rect(x0, y0, x1, y1, color, alpha=0.32)
        canvas.stroke_rect(x0, y0, x1, y1, color, thickness=2, alpha=0.95)

    if border is not None:
        canvas.stroke_rect(0, 0, IMAGE_WIDTH, IMAGE_HEIGHT, border, thickness=4, alpha=0.95)

    # 进度竖线：位置 = 帧号 / 计划帧数。初扫只到 386/420，线会停在约 92% 处，
    # 一眼就能看出"这批没采完"，不需要额外文字说明。
    progress_x = int(round((frame_index / float(planned_frames)) * (IMAGE_WIDTH - 1))) if planned_frames else 0
    canvas.vline(progress_x, 0, IMAGE_HEIGHT, COL_PROGRESS, width=2, alpha=0.85)
    canvas.fill_rect(progress_x - 3, 0, progress_x + 5, 8, COL_PROGRESS, alpha=0.95)

    # 底部白色半透明标签条（画不出真字，用点阵写机器码）。
    strip_top = IMAGE_HEIGHT - 24
    canvas.fill_rect(0, strip_top, IMAGE_WIDTH, IMAGE_HEIGHT, COL_STRIP, alpha=0.72)
    canvas.hline(strip_top, 0, IMAGE_WIDTH, COL_RULER, width=1, alpha=0.35)
    canvas.draw_text(12, strip_top + 5, label, COL_STRIP_TEXT, scale=2, alpha=0.95)
    return canvas.to_png()


# --------------------------------------------------------------------------- #
# 5. 响应序列模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PeakSpec:
    """一处预制异常响应。

    `target` 是"剧本口径"的峰值幅值，必须与 result.json 的 score 逐位相同；
    `onset/peak_frame/hold_end/decay_end` 决定它在帧轴上的爬坡、保持与衰减。
    """

    sample_index: int
    target: float
    onset: int
    peak_frame: int
    hold_end: int
    decay_end: int
    label: str
    #: 平台剧本固定的证据段号（只有复扫三处异常有）。result.json 的
    #: evidenceSegment 直接引用它，所以这几帧的 segmentId 必须用这个字符串，
    #: 否则包内会出现"指向不存在的段"的悬空引用。
    anchor_segment_id: Optional[str] = None

    @property
    def normalized_x(self) -> float:
        return self.sample_index / float(POINT_COUNT - 1)


@dataclass(frozen=True)
class MarkSpec:
    frame_index: int
    operator_label: str
    note: str


@dataclass(frozen=True)
class PathSpec:
    path_id: str
    direction_deg: int
    start_frame: int
    end_frame: int
    note: str


@dataclass(frozen=True)
class EventSpec:
    frame_index: int
    stage: str
    level: str
    text: str


@dataclass(frozen=True)
class SampleSpec:
    """参考样本包里的一个物理样本。"""

    physical_sample_id: str
    material_source: str
    known_state: str
    label_basis: str
    peak: Optional[PeakSpec]


@dataclass
class Scenario:
    scenario_id: str
    round: str
    batch_id: str
    planned_frames: int
    returned_frames: int
    model_version: str
    state: str
    seed: int
    phase_seed: float
    capture_start: str
    peaks: Tuple[PeakSpec, ...]
    marks: Tuple[MarkSpec, ...]
    events: Tuple[EventSpec, ...]
    paths: Tuple[PathSpec, ...]
    label: str
    quality_note: str
    result: Dict[str, Any]
    image_mode: str                      # plain / single / multi / reference
    interrupt_reason: str = ""
    samples: Tuple[SampleSpec, ...] = ()
    dataset_note: str = ""
    background: Optional[Canvas] = field(default=None, repr=False)


def peak_envelope(frame_index: int, peak: PeakSpec) -> float:
    """异常响应在帧轴上的强度包络。

    用升余弦爬坡而不是线性：线性折点在响应序列图上会看出硬拐角，
    现场讲解时容易被当成"数据突变"。升余弦两端导数为 0，看起来像真实响应
    的生长过程，并且峰值帧返回的**恰好**是 target（不是近似值），
    这样 segments 的幅值与 result 的分数可以按位比对。
    """
    if frame_index < peak.onset or frame_index > peak.decay_end:
        return 0.0
    if peak.peak_frame <= frame_index <= peak.hold_end:
        return peak.target
    if frame_index < peak.peak_frame:
        span = float(peak.peak_frame - peak.onset)
        u = (frame_index - peak.onset) / span if span else 1.0
        return peak.target * 0.5 * (1.0 - math.cos(math.pi * u))
    span = float(peak.decay_end - peak.hold_end)
    u = (frame_index - peak.hold_end) / span if span else 1.0
    return peak.target * 0.5 * (1.0 + math.cos(math.pi * u))


def baseline_amplitude(sample_index: int, phase_seed: float) -> float:
    """剧本给定的基线公式（三个正弦叠加）。

    i 是采样点序号，seed 是逐帧相位种子（由包种子与帧号推导，不是随机数），
    所以基线本身也是逐帧确定、逐帧缓变的，不会出现帧间跳变。
    """
    return (
        0.16
        + 0.05 * math.sin(sample_index * 0.21 + phase_seed)
        + 0.035 * math.sin(sample_index * 0.63 + phase_seed * 2.0)
        + 0.02 * math.sin(sample_index * 1.7 + phase_seed * 3.0)
    )


def active_peaks(scenario: Scenario, frame_index: int) -> List[Tuple[PeakSpec, float]]:
    """该帧上所有还在起作用的异常峰及其当前幅值。"""
    active: List[Tuple[PeakSpec, float]] = []
    for peak in scenario.peaks:
        height = peak_envelope(frame_index, peak)
        if height > 0.0:
            active.append((peak, height))
    return active


def render_frame(scenario: Scenario, frame_index: int, active: Sequence[Tuple[PeakSpec, float]]) -> List[float]:
    """生成一帧的 420 个采样点 = 基线 + 噪声 + 高斯峰（再夹到 [0.02, 0.99]）。

    噪声逐帧独立：random.Random(包种子 + 帧号*7919)。用固定整数种子而不是
    全局 random，是为了让"同一帧"与生成顺序无关 —— 将来若改成多进程生成，
    结果也不会变。
    """
    rng = random.Random(scenario.seed + frame_index * 7919)
    phase_seed = scenario.phase_seed + frame_index * 0.013
    center = [(peak.sample_index / float(POINT_COUNT - 1), height) for peak, height in active]
    values: List[float] = []
    for sample_index in range(POINT_COUNT):
        x = sample_index / float(POINT_COUNT - 1)
        value = baseline_amplitude(sample_index, phase_seed) + (rng.random() * 2.0 - 1.0) * NOISE_AMPLITUDE
        for px, height in center:
            if abs(x - px) < GAUSS_WINDOW:
                value += height * math.exp(-((x - px) ** 2) / GAUSS_SIGMA_SQ)
        if value < AMP_MIN:
            value = AMP_MIN
        elif value > AMP_MAX:
            value = AMP_MAX
        values.append(value)
    return values


# --------------------------------------------------------------------------- #
# 6. 三套包的剧本定义
# --------------------------------------------------------------------------- #

def _rescan_peaks() -> Tuple[PeakSpec, ...]:
    """复扫三处异常：帧 292 / 330 / 372，峰值 0.71 / 0.84 / 0.87。

    sampleIndex 122 / 197 / 260 是平台剧本给定的位置，换算到归一化轴就是
    0.29 / 0.47 / 0.62（`sampleIndex / (pointCount-1)` 四舍五入到 2 位）。
    三处峰在帧轴上刻意错开窗口（前一处衰减到尾段时后一处才开始爬坡），
    这样锚点帧上只有一处主导峰，评审看到的就是"三处"，而不是一团重叠响应。
    """
    return (
        PeakSpec(122, 0.71, onset=254, peak_frame=292, hold_end=302, decay_end=320,
                 label="疑似严重受潮区域", anchor_segment_id="echo-Z04-lower-seg-07"),
        PeakSpec(197, 0.84, onset=298, peak_frame=330, hold_end=340, decay_end=358,
                 label="疑似虫蛀空洞（上部响应区）", anchor_segment_id="echo-Z04-lower-seg-11"),
        PeakSpec(260, 0.87, onset=340, peak_frame=372, hold_end=382, decay_end=402,
                 label="疑似虫蛀空洞（下部响应区）", anchor_segment_id="echo-Z04-lower-seg-13"),
    )


def _initial_peaks() -> Tuple[PeakSpec, ...]:
    """初扫只有一处"中等响应"：帧 200-386，位置 0.62，目标幅值 0.52。

    0.52 刻意压在结论阈值之下：它不足以出结论，只够触发后续的适用域检查，
    这正是剧本里"扫描中平台要求核验适用域"的由来。
    """
    return (
        PeakSpec(260, 0.52, onset=200, peak_frame=260, hold_end=340, decay_end=386,
                 label="中等响应（未达结论阈值）"),
    )


#: 参考样本四个物理样本。S-01 正常、S-02 轻微受潮、S-03 已知缺陷（模拟空洞）、
#: S-04 未知待核验。响应位置沿用复扫的三条轴位，故事上"同一根木柱的同类响应"。
SAMPLE_SPECS: Tuple[SampleSpec, ...] = (
    SampleSpec("S-01", "参考试件（来源卡 SR-01）", "正常", "赛前确认的样本文件", None),
    SampleSpec(
        "S-02", "参考试件（来源卡 SR-02）", "轻微受潮", "赛前确认的样本文件",
        PeakSpec(122, 0.30, onset=0, peak_frame=12, hold_end=20, decay_end=29,
                 label="参考样本 S-02 轻微受潮响应"),
    ),
    SampleSpec(
        "S-03", "参考试件（来源卡 SR-03）", "已知缺陷（模拟空洞）", "赛前确认的样本文件",
        PeakSpec(197, 0.62, onset=0, peak_frame=12, hold_end=20, decay_end=29,
                 label="参考样本 S-03 已知缺陷响应（模拟空洞）"),
    ),
    SampleSpec(
        "S-04", "参考试件（来源卡 SR-04）", "未知待核验", "无标签依据，单列待核验集合",
        PeakSpec(260, 0.22, onset=0, peak_frame=12, hold_end=20, decay_end=29,
                 label="参考样本 S-04 待核验响应（无标签依据）"),
    ),
)

#: 每个物理样本在一段路径里占 30 帧；两条路径各 120 帧，共 240 帧。
SAMPLE_SLOT_FRAMES = 30
REFERENCE_PATH_FRAMES = 120


def _reference_peaks() -> Tuple[PeakSpec, ...]:
    """两条路径 × 四个样本，逐段平移窗口。

    path-02 是"换向重复"：位置与目标幅值和 path-01 完全相同（同一物理样本，
    换 90° 再扫一次应当得到同样的响应），只有噪声不同。dataset.json 里
    S-01..S-04 的 frames 会把两条路径的帧并到同一物理样本下。
    """
    peaks: List[PeakSpec] = []
    for path_index in range(2):
        base = path_index * REFERENCE_PATH_FRAMES
        for slot, sample in enumerate(SAMPLE_SPECS):
            if sample.peak is None:
                continue
            src = sample.peak
            offset = base + slot * SAMPLE_SLOT_FRAMES
            peaks.append(
                PeakSpec(
                    src.sample_index, src.target,
                    onset=offset + src.onset, peak_frame=offset + src.peak_frame,
                    hold_end=offset + src.hold_end, decay_end=offset + src.decay_end,
                    label=src.label,
                )
            )
    return tuple(peaks)


def _progress_events(frames: Iterable[int], planned: int) -> List[EventSpec]:
    return [
        EventSpec(frame, "progress", "INFO", f"已采集 {frame}/{planned} 帧，样例回放按 10 fps 推进")
        for frame in frames
    ]


def build_scenarios() -> Tuple[Scenario, ...]:
    """三套包的完整剧本。所有数值都在这里，别处不再出现魔法数字。"""

    initial = Scenario(
        scenario_id="initial-anomaly-v1",
        round="initial",
        batch_id="scan-Z04-001",
        planned_frames=420,
        returned_frames=386,
        model_version="DEMO-M02",
        state="interrupted_pause",
        seed=SEED_INITIAL,
        phase_seed=PHASE_INITIAL,
        capture_start=CAPTURE_START_INITIAL,
        peaks=_initial_peaks(),
        marks=(
            MarkSpec(100, "正面", "初扫首个关注位置（画面自第 96 帧起出现琥珀标记区）"),
            MarkSpec(200, "右侧", "响应开始抬升的位置"),
            MarkSpec(260, "", "0.62 响应区中心（操作者未选方向，界面只显示标记03）"),
            MarkSpec(340, "背面", "响应区下沿"),
            MarkSpec(380, "左侧", "平台暂停请求生效前最后一次标记"),
        ),
        events=tuple(
            [
                EventSpec(0, "precheck", "INFO",
                          "启动自检通过：样例包 initial-anomaly-v1 就绪，配置 CFG-02 / 模型 DEMO-M02"),
                EventSpec(0, "capture_start", "INFO", "本地采集任务启动，检测样例回放开始（sourceMode=replay）"),
            ]
            + _progress_events((60, 120, 180, 240), 420)
            + [
                EventSpec(DOMAIN_CHECK_FRAME, "domain_check_failed", "WARN", DOMAIN_CHECK_TEXT),
            ]
            + _progress_events((300, 360), 420)
            + [
                EventSpec(384, "pause_requested", "WARN", PAUSE_REQUEST_TEXT),
                EventSpec(386, "capture_paused", "WARN",
                          "采集已暂停：第 386 帧后未继续采集，剩余 34 帧未回传（诊断输出同时冻结）"),
                EventSpec(386, "batch_sealed", "INFO",
                          "批次 scan-Z04-001 已封存：386/420 帧，等待平台核对 datasetHash"),
            ]
        ),
        paths=(
            PathSpec("path-01", 0, 0, 419,
                     "自下而上正向扫描 Z04 下部；暂停请求在第 386 帧后生效，帧 386-419 未采集"),
        ),
        label="初扫（触发适用域待核验）",
        quality_note="因平台请求暂停 / 适用域待核验，第 386 帧后未继续采集，剩余 34 帧未回传",
        result={
            "conclusion": "withheld",
            "reason": "适用域待核验：当前部署模型缺少该批次木材的有效标定记录",
            "findings": [],
            "modelVersion": "DEMO-M02",
            "adequateDomain": False,
        },
        image_mode="single",
        interrupt_reason="平台请求暂停 / 适用域待核验，第 386 帧后未继续采集",
    )

    reference = Scenario(
        scenario_id="reference-samples-v1",
        round="reference",
        batch_id="ref-batch-01",
        planned_frames=240,
        returned_frames=240,
        model_version="DEMO-M02",
        state="finished",
        seed=SEED_REFERENCE,
        phase_seed=PHASE_REFERENCE,
        capture_start=CAPTURE_START_REFERENCE,
        peaks=_reference_peaks(),
        marks=(),  # 参考样本采集不产生人工标记（操作者只按样本标签采集）
        events=(
            EventSpec(0, "path_start", "INFO", "路径 path-01 开始：方向 0°，样本 S-01～S-04 各 30 帧"),
            EventSpec(0, "capture_start", "INFO", "参考样本采集开始（sourceMode=replay，只采集不出结论）"),
            *_progress_events((30, 90), 240),
            EventSpec(REFERENCE_PATH_FRAMES, "path_start", "INFO", "路径 path-02 开始：方向 90°，换向重复采集同一批样本"),
            *_progress_events((150, 210), 240),
            EventSpec(239, "capture_finished", "INFO", "参考样本采集完成：240/240 帧，四个样本 × 两条路径已分组"),
        ),
        paths=(
            PathSpec("path-01", 0, 0, 119, "正向扫描，依次覆盖 S-01 / S-02 / S-03 / S-04，每个样本 30 帧"),
            PathSpec("path-02", 90, 120, 239, "换向 90° 重复采集，样本顺序与 path-01 相同"),
        ),
        label="参考样本采集",
        quality_note="采集完整：240/240 帧回传；两条扫描路径各 120 帧，四个物理样本各 60 帧",
        result={
            "conclusion": "none",
            "reason": "参考样本采集不输出缺陷结论",
            "findings": [],
        },
        image_mode="reference",
        samples=SAMPLE_SPECS,
        dataset_note="同一物理样本的连续扫描不得拆到训练集与测试集",
    )

    rescan_findings = [
        {
            "id": "CUR-Z04-01",
            "label": "疑似严重受潮区域",
            "score": 0.71,
            "sampleIndex": 122,
            "frameIndex": 292,
            "branch": "radar",
            "evidenceSegment": "echo-Z04-lower-seg-07",
        },
        {
            "id": "CUR-Z04-02",
            "label": "疑似虫蛀空洞（上部响应区）",
            "score": 0.84,
            "sampleIndex": 197,
            "frameIndex": 330,
            "branch": "fusion",
            "evidenceSegment": "echo-Z04-lower-seg-11",
        },
        {
            "id": "CUR-Z04-03",
            "label": "疑似虫蛀空洞（下部响应区）",
            "score": 0.87,
            "sampleIndex": 260,
            "frameIndex": 372,
            "branch": "fusion",
            "evidenceSegment": "echo-Z04-lower-seg-13",
        },
    ]

    rescan = Scenario(
        scenario_id="rescan-demo-v1",
        round="rescan",
        batch_id="scan-Z04-002",
        planned_frames=420,
        returned_frames=420,
        model_version="DEMO-M02b",
        state="finished",
        seed=SEED_RESCAN,
        phase_seed=PHASE_RESCAN,
        capture_start=CAPTURE_START_RESCAN,
        peaks=_rescan_peaks(),
        marks=(
            MarkSpec(250, "正面", "复扫起点（画面自第 250 帧起出现三处异常标记）"),
            MarkSpec(290, "自定义", "疑似受潮区域人工标记，与端侧初筛位置一致"),
            MarkSpec(330, "右侧", "疑似空洞（上部响应区）标记"),
            MarkSpec(370, "背面", "疑似空洞（下部响应区）标记"),
            MarkSpec(410, "左侧", "复扫结束位置"),
        ),
        events=tuple(
            [
                EventSpec(0, "precheck", "INFO",
                          "启动自检通过：样例包 rescan-demo-v1 就绪，配置 CFG-02 / 模型 DEMO-M02b"),
                EventSpec(0, "capture_start", "INFO", "复扫开始：沿原路径重扫 Z04 下部（sourceMode=replay）"),
            ]
            + _progress_events((60, 120, 180, 240), 420)
            + [
                EventSpec(292, "anomaly_candidate", "WARN",
                          "异常候选 1/3：响应段 echo-Z04-lower-seg-07 峰值幅值 0.71，"
                          "位置 sampleIndex 122（归一化 0.29），疑似严重受潮区域"),
                EventSpec(330, "anomaly_candidate", "WARN",
                          "异常候选 2/3：响应段 echo-Z04-lower-seg-11 峰值幅值 0.84，"
                          "位置 sampleIndex 197（归一化 0.47），疑似虫蛀空洞（上部响应区）"),
                EventSpec(372, "anomaly_candidate", "WARN",
                          "异常候选 3/3：响应段 echo-Z04-lower-seg-13 峰值幅值 0.87，"
                          "位置 sampleIndex 260（归一化 0.62），疑似虫蛀空洞（下部响应区）"),
                EventSpec(419, "capture_finished", "INFO", "复扫采集结束：420/420 帧已回传"),
                EventSpec(420, "screening_done", "INFO",
                          "端侧初筛完成：3 处异常候选已写入 result.json（conclusion=preliminary），等待平台复核"),
            ]
        ),
        paths=(
            PathSpec("path-01", 0, 0, 419, "沿初扫路径重扫 Z04 下部，末段出现三处异常响应"),
        ),
        label="复扫（三处样例异常）",
        quality_note="采集完整：420/420 帧回传，无缺帧；末段三处异常响应待平台复核",
        result={
            "conclusion": "preliminary",
            "reviewer": "platform",
            "note": "端侧初筛，待平台复核；不输出深度与形状",
            "findings": rescan_findings,
        },
        image_mode="multi",
    )

    return (initial, reference, rescan)


# --------------------------------------------------------------------------- #
# 7. 公共小工具
# --------------------------------------------------------------------------- #

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式摘要：frames.csv 有 6~8MB，不要整个读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def frame_id(frame_index: int) -> str:
    """frame-00250。与 adapters/replay.py 的 ReplayFrame.frame_id 完全一致。"""
    return f"frame-{frame_index:0{FRAME_ID_DIGITS}d}"


def frame_id_to_index(value: str) -> int:
    """从 frameId 反解帧号；格式不对时抛 ValueError（校验里当失败处理）。"""
    match = re.fullmatch(r"frame-(\d{%d})" % FRAME_ID_DIGITS, value or "")
    if not match:
        raise ValueError(f"frameId 格式非法：{value!r}")
    return int(match.group(1))


def image_name(frame_index: int) -> str:
    """frame_00250.png。与 storage.py 的 BatchWriter.save_image 命名一致。"""
    return f"frame_{frame_index:0{IMAGE_ID_DIGITS}d}.png"


def segment_id_for(scenario: Scenario, frame_index: int, peak: Optional[PeakSpec] = None) -> str:
    """段号规则。

    默认按三位帧号编号（echo-Z04-lower-seg-292），编号与 frame 一一对应；
    **只有**复扫三处异常的峰值帧改用平台剧本固定的锚点段号
    （seg-07 / seg-11 / seg-13），因为 result.json 的 evidenceSegment 是剧本里
    写死的字符串，样例包必须让它解析回同一帧，否则包内就是"指向不存在证据段"
    的悬空引用。

    注意锚点段号只给峰值帧：异常窗口内还有几十帧也带着同一个峰（只是幅值更小），
    如果它们都叫 seg-07，段号就不再唯一，证据段也就无法定位到具体帧
    —— 这正是 `--verify` 的"segmentId 重复"检查要拦住的情况。
    """
    if peak is not None and peak.anchor_segment_id and frame_index == peak.peak_frame:
        return peak.anchor_segment_id
    return f"echo-{ZONE_ID}-seg-{frame_index:03d}"


def timestamp_at(base_iso: str, frame_index: int) -> str:
    """由固定基准时刻 + 帧号推导时间戳（100 ms/帧），不读系统时钟。"""
    base = datetime.strptime(base_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (base + timedelta(milliseconds=frame_index * MS_PER_FRAME)).strftime("%Y-%m-%dT%H:%M:%SZ")


def jdump(data: Any) -> str:
    """统一的 JSON 文本形式：缩进 2、保留中文、结尾一个换行。"""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# 8. 生成
# --------------------------------------------------------------------------- #

def _guess_role(rel_path: str) -> str:
    """按路径猜文件角色。表项与 storage.py 的 `_guess_role` 同名同值。

    唯一一处有意不同：storage.py 先判断 `startswith("images/")` 再查表，
    于是表里那条 `"images/index.json": "image_index"` 永远不会命中，索引文件
    会被归成普通图片。这里改成先查表、再用前缀兜底，让索引文件拿到它本来
    就该有的角色；角色写错不会让包不可用，但平台按 role 归档时会少一类文件。
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
        "batch.json": "batch",
    }
    if rel_path in mapping:
        return mapping[rel_path]
    if rel_path.startswith("images/"):
        return "image"
    return "other"


class PackageWriter:
    """把一个包写进磁盘并登记角色，最后提交 manifest。

    写盘顺序固定：其它文件 → images → manifest.json。manifest 一旦落地就表示
    "这批数据完整"，所以它必须最后写，且不能把自己算进 datasetHash。
    """

    def __init__(self, root: Path, scenario: Scenario) -> None:
        self.scenario = scenario
        self.dir = root / scenario.scenario_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "images").mkdir(exist_ok=True)
        self.roles: Dict[str, str] = {}

    # ---- 写文件 ----

    def write_json(self, name: str, data: Any, role: str) -> None:
        self.write_text(name, jdump(data), role)

    def write_text(self, name: str, text: str, role: str) -> None:
        # newline="\n" 是必须的：Windows 上默认会把 \n 翻成 \r\n，
        # 那样"同一份样例在 Windows 生成、在树莓派生成"就会得到不同 sha256。
        with (self.dir / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self.roles[name] = role

    def write_bytes(self, rel: str, data: bytes, role: str) -> None:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.roles[rel] = role

    # ---- manifest ----

    def commit_manifest(self, manifest: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int]:
        """计算 files 与 datasetHash 并原子落盘 manifest.json。

        datasetHash 算法（与 storage.BatchWriter.commit_manifest 相同）：
        对包内除 manifest.json 外的所有文件，按**路径字典序**拼接
        `path + "\\0" + sha256 + "\\n"`，再取 sha256。
        README 与 `--verify` 用的是同一个算法，谁都不能各写一套。
        """
        entries: List[Dict[str, Any]] = []
        total_bytes = 0
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.dir).as_posix()
            if rel == "manifest.json" or rel.endswith(".tmp"):
                continue
            size = path.stat().st_size
            total_bytes += size
            entries.append(
                {
                    "role": self.roles.get(rel, _guess_role(rel)),
                    "path": rel,
                    "sha256": sha256_file(path),
                    "bytes": size,
                }
            )

        digest = hashlib.sha256()
        for item in entries:
            digest.update(item["path"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(item["sha256"].encode("utf-8"))
            digest.update(b"\n")
        dataset_hash = digest.hexdigest()

        payload = dict(manifest)
        payload["files"] = entries
        payload["datasetHash"] = dataset_hash
        path = self.dir / "manifest.json"
        tmp = self.dir / "manifest.json.tmp"
        tmp.write_text(jdump(payload), encoding="utf-8", newline="\n")
        tmp.replace(path)  # 原子替换：读到一半崩了不会留下半个 JSON
        return payload, len(entries), total_bytes


def _marker_rects(scenario: Scenario, frame_index: int) -> List[Tuple[int, int, int, int, RGB]]:
    """画面上的异常标记矩形（位置固定，便于现场一眼比对）。"""
    rects: List[Tuple[int, int, int, int, RGB]] = []
    if scenario.image_mode == "single" and frame_index >= 96:
        # 初扫：中下部的琥珀色矩形，代表操作者与端侧共同关注的一个区域。
        rects.append((248, 208, 392, 296, COL_AMBER))
    elif scenario.image_mode == "multi" and frame_index >= 250:
        # 复扫：琥珀（中下，疑似受潮）+ 红（左上偏中）+ 红（右下），后两者疑似空洞。
        rects.append((248, 208, 392, 296, COL_AMBER))
        rects.append((96, 56, 232, 152, COL_RED))
        rects.append((408, 216, 544, 312, COL_RED))
    return rects


def _image_label(scenario: Scenario, frame_index: int) -> str:
    """标签条内容。参考样本额外带上物理样本号，便于人工核对分组。"""
    base = f"{ZONE_ID.upper()} F{frame_index:04d}"
    if scenario.image_mode == "reference":
        return f"{base} {_sample_for_frame(frame_index).physical_sample_id}"
    return base


def _sample_for_frame(frame_index: int) -> SampleSpec:
    """帧号 → 物理样本（两条路径各 120 帧，每段 30 帧）。"""
    slot = (frame_index % REFERENCE_PATH_FRAMES) // SAMPLE_SLOT_FRAMES
    return SAMPLE_SPECS[min(slot, len(SAMPLE_SPECS) - 1)]


def _image_kind(scenario: Scenario, frame_index: int) -> str:
    if scenario.image_mode == "reference":
        return "preview_reference"
    if _marker_rects(scenario, frame_index):
        return "preview_marked"
    return "preview"


def generate_package(root: Path, scenario: Scenario) -> Dict[str, Any]:
    """生成一套包，返回摘要（供命令行报告与 --self-test 使用）。"""

    if scenario.background is None:
        scenario.background = build_background()

    # ---- 逐帧生成（一次遍历同时产出 frames.csv / segments.json / quality 统计）----
    #
    # frames.csv 只流式写：420×420 = 176k 行、约 7MB，不能先在内存里拼字符串。
    image_index: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    writer = PackageWriter(root, scenario)
    frames_path = writer.dir / "frames.csv"

    clipped_total = 0
    empty_frames = 0
    max_amplitude_overall = 0.0
    anchor_segment_by_frame: Dict[int, str] = {}

    with frames_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(",".join(FRAMES_CSV_COLUMNS) + "\n")
        buffer: List[str] = []
        for frame_index in range(scenario.returned_frames):
            active = active_peaks(scenario, frame_index)
            values = render_frame(scenario, frame_index, active)

            # t_ms 与 tNs 共用同一条时间轴：100 ms/帧。
            t_ms = frame_index * MS_PER_FRAME
            for sample_index, value in enumerate(values):
                buffer.append(f"{frame_index},{sample_index},{value:.6f},{t_ms:.1f}\n")
            if len(buffer) >= 8192:      # 攒一批再写，避免 17 万次小写调用
                handle.write("".join(buffer))
                buffer.clear()

            clipped = sum(1 for value in values if value >= AMP_MAX)
            clipped_total += clipped
            frame_max = max(values)
            max_amplitude_overall = max(max_amplitude_overall, frame_max)
            if frame_max < 0.05:
                empty_frames += 1

            # 只有真正超过阈值的峰才登记为"响应段"里的一处峰。
            reported = [
                (peak, height) for peak, height in active if height >= PEAK_REPORT_MIN
            ]
            reported.sort(key=lambda item: item[0].sample_index)
            primary = reported[0][0] if reported else None
            seg_id = segment_id_for(scenario, frame_index, primary)
            anchor_segment_by_frame[frame_index] = seg_id
            paired = image_name(frame_index) if frame_index % IMAGE_EVERY == 0 else None

            segments.append(
                {
                    "segmentId": seg_id,
                    "batchId": scenario.batch_id,
                    "frameId": frame_id(frame_index),
                    "frameIndex": frame_index,
                    "zoneId": ZONE_ID,
                    "tNs": frame_index * NS_PER_FRAME,
                    "deviceMonotonicNs": frame_index * NS_PER_FRAME,
                    "sampleCount": POINT_COUNT,
                    "axes": {"x": "frame_index", "y": "sample_index"},
                    "sourceMode": RADAR_SOURCE_MODE,
                    "pairedImage": paired,
                    "quality": {
                        "saturationPct": round(100.0 * clipped / POINT_COUNT, 4),
                        "emptyFrame": frame_max < 0.05,
                        "clipped": clipped > 0,
                        "maxAmplitude": round(frame_max, 6),
                    },
                    "peaks": [
                        {
                            "sampleIndex": peak.sample_index,
                            "normalizedX": round(peak.normalized_x, 2),
                            "amplitude": round(height, 6),
                            "label": peak.label,
                        }
                        for peak, height in reported
                    ],
                }
            )
        if buffer:
            handle.write("".join(buffer))
    writer.roles["frames.csv"] = "frames"   # 流式写的文件也要登记角色

    # ---- 预览图：每 10 帧一张，标签条与标记按帧叠加 ----
    for frame_index in range(0, scenario.returned_frames, IMAGE_EVERY):
        border = None
        ruler = False
        if scenario.image_mode == "reference":
            border = SAMPLE_COLORS[_sample_for_frame(frame_index).physical_sample_id]
            ruler = True
        png = render_image(
            scenario.background,
            frame_index=frame_index,
            planned_frames=scenario.planned_frames,
            label=_image_label(scenario, frame_index),
            markers=_marker_rects(scenario, frame_index),
            border=border,
            ruler=ruler,
        )
        rel = f"images/{image_name(frame_index)}"
        writer.write_bytes(rel, png, "image")
        image_index.append(
            {
                "frameId": frame_id(frame_index),
                "file": image_name(frame_index),
                "relPath": rel,
                "sha256": sha256_bytes(png),
                "bytes": len(png),
                "width": IMAGE_WIDTH,
                "height": IMAGE_HEIGHT,
                "kind": _image_kind(scenario, frame_index),
            }
        )

    # ---- batch.json ----
    duration_s = round(scenario.returned_frames / float(FPS), 1)
    batch = {
        "schemaVersion": SCHEMA_VERSION,
        "batchId": scenario.batch_id,
        "scenarioId": scenario.scenario_id,
        "round": scenario.round,
        "label": scenario.label,
        "projectId": PROJECT_ID,
        "orderId": ORDER_ID,
        "componentId": COMPONENT_ID,
        "segmentId": SEGMENT_ID,
        "zoneId": ZONE_ID,
        "operatorId": OPERATOR_ID,
        "deviceId": DEVICE_ID,
        "configVersion": CONFIG_VERSION,
        "modelVersion": scenario.model_version,
        "format": SAMPLE_FORMAT,
        "radarSourceMode": RADAR_SOURCE_MODE,
        "cameraSourceMode": CAMERA_SOURCE_MODE,
        "positionSource": POSITION_SOURCE,
        "pairing": PAIRING,
        "plannedFrames": scenario.planned_frames,
        "returnedFrames": scenario.returned_frames,
        "fps": FPS,
        "state": scenario.state,
        "startedAt": scenario.capture_start,
        "finishedAt": timestamp_at(scenario.capture_start, scenario.returned_frames),
        "durationS": duration_s,
        "markCount": len(scenario.marks),
        "interruptReason": scenario.interrupt_reason,
        "privacyNote": PRIVACY_NOTE,
    }
    writer.write_json("batch.json", batch, "batch")

    # ---- config.json：三套包共用同一份环境与补偿配置快照 ----
    writer.write_json(
        "config.json",
        {
            "configVersion": CONFIG_VERSION,
            "source": "platform",
            "publishedAt": CONFIG_PUBLISHED_AT,
            "airTempC": 26.4,
            "relativeHumidityPct": 78.0,
            "windSpeedMs": 1.6,
            "instrumentId": "THM-2207 / ANE-3310",
            "position": "四柱区域入口，距 Z04 2.4m，离地 1.1m",
            "compensation": {
                "baselineOffsetDb": -1.8,
                "normalization": "reference_normalized",
                "functionVersion": "comp-v1.4",
            },
            "notes": {
                "wind": "风速仅作采集稳定性记录，不代入 HH 模型",
                "emc": "HH 估计是环境先验，不是木柱实测含水率",
            },
        },
        "config",
    )

    # ---- plan.json ----
    writer.write_json(
        "plan.json",
        {
            "zoneId": ZONE_ID,
            "paths": [
                {
                    "pathId": item.path_id,
                    "directionDeg": item.direction_deg,
                    "startFrame": item.start_frame,
                    "endFrame": item.end_frame,
                    "note": item.note,
                }
                for item in scenario.paths
            ],
            "expectedFrames": scenario.planned_frames,
            "note": (
                f"{scenario.label}：计划 {scenario.planned_frames} 帧，实际回传 "
                f"{scenario.returned_frames} 帧；路径与帧段按上表执行，停采位置以 quality.json 为准"
            ),
        },
        "plan",
    )

    # ---- dataset.json：只有参考样本包需要（物理样本分组）----
    if scenario.samples:
        groups = []
        for slot, sample in enumerate(scenario.samples):
            frames: List[int] = []
            for path_index in range(2):
                base = path_index * REFERENCE_PATH_FRAMES + slot * SAMPLE_SLOT_FRAMES
                frames.extend(range(base, base + SAMPLE_SLOT_FRAMES))
            groups.append(
                {
                    "physicalSampleId": sample.physical_sample_id,
                    "materialSource": sample.material_source,
                    "knownState": sample.known_state,
                    "labelBasis": sample.label_basis,
                    "frames": frames,
                    "groupId": f"G-SAMPLE-{slot + 1:02d}",
                }
            )
        writer.write_json(
            "dataset.json",
            {"groups": groups, "note": scenario.dataset_note},
            "dataset",
        )

    # ---- segments.json ----
    writer.write_json("segments.json", segments, "segments")

    # ---- marks.json / marks.csv ----
    marks_json: List[Dict[str, Any]] = []
    for order, mark in enumerate(scenario.marks, start=1):
        marks_json.append(
            {
                "markId": f"mark-{scenario.batch_id}-{order:02d}",
                "batchId": scenario.batch_id,
                "frameId": frame_id(mark.frame_index),
                "cameraAssetId": image_name(mark.frame_index),
                "deviceMonotonicNs": mark.frame_index * NS_PER_FRAME,
                "operatorLabel": mark.operator_label,
                "positionSource": POSITION_SOURCE,
                "note": mark.note,
                "createdAt": timestamp_at(scenario.capture_start, mark.frame_index),
                # frameIndex / imageOk 不在 contracts.MARK_KEYS 里，但终端的
                # Mark.to_dict() 会写、BatchWriter.write_marks_csv 会读 frameIndex，
                # 少了它 marks.csv 的 frame_index 列就空了。
                "frameIndex": mark.frame_index,
                "imageOk": True,
            }
        )
    writer.write_json("marks.json", marks_json, "marks")
    lines = [",".join(MARKS_CSV_COLUMNS)]
    for item in marks_json:
        lines.append(
            ",".join(
                str(value)
                for value in (
                    item["markId"],
                    item["frameIndex"],
                    ZONE_ID,
                    item["operatorLabel"],
                    item["positionSource"],
                    item["deviceMonotonicNs"],
                )
            )
        )
    writer.write_text("marks.csv", "\n".join(lines) + "\n", "marks_csv")

    # ---- quality.json ----
    total_samples = scenario.returned_frames * POINT_COUNT
    writer.write_json(
        "quality.json",
        {
            "frameCount": scenario.returned_frames,
            "expectedFrames": scenario.planned_frames,
            "missingFrames": scenario.planned_frames - scenario.returned_frames,
            "saturationPct": round(100.0 * clipped_total / total_samples, 4) if total_samples else 0.0,
            "emptyFrames": empty_frames,
            "nanValues": 0,
            "duplicateFrames": 0,
            "levelSamples": LEVEL_SAMPLES,
            "note": scenario.quality_note,
        },
        "quality",
    )

    # ---- result.json（剧本给定的端侧初筛结果）----
    writer.write_json("result.json", scenario.result, "result")

    # ---- events.log（NDJSON，每行一个事件，tMs 单调不减）----
    event_lines = []
    for event in scenario.events:
        event_lines.append(
            json.dumps(
                {
                    "tMs": int(event.frame_index * MS_PER_FRAME),
                    "stage": event.stage,
                    "level": event.level,
                    "text": event.text,
                },
                ensure_ascii=False,
            )
        )
    writer.write_text("events.log", "\n".join(event_lines) + "\n", "events")

    # ---- images/index.json ----
    writer.write_json("images/index.json", image_index, "image_index")

    # ---- manifest.json（最后写）----
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "batchId": scenario.batch_id,
        "projectId": PROJECT_ID,
        "orderId": ORDER_ID,
        "componentId": COMPONENT_ID,
        "segmentId": SEGMENT_ID,
        "zoneId": ZONE_ID,
        "positionSource": POSITION_SOURCE,
        "radarSourceMode": RADAR_SOURCE_MODE,
        "cameraSourceMode": CAMERA_SOURCE_MODE,
        "pairing": PAIRING,
        "format": SAMPLE_FORMAT,
        "axis": {"x": "frame_index", "y": "sample_index"},
        "configVersion": CONFIG_VERSION,
        "modelVersion": scenario.model_version,
        "scenarioId": scenario.scenario_id,
        "round": scenario.round,
        "operatorId": OPERATOR_ID,
        "deviceId": DEVICE_ID,
        "frameCount": scenario.planned_frames,
        "pointCount": POINT_COUNT,
        "fps": FPS,
        "returnedFrames": scenario.returned_frames,
        "state": scenario.state,
        "privacyNote": PRIVACY_NOTE,
        "createdAt": scenario.capture_start,
    }
    payload, file_count, total_bytes = writer.commit_manifest(manifest)

    return {
        "scenarioId": scenario.scenario_id,
        "batchId": scenario.batch_id,
        "frameCount": scenario.planned_frames,
        "returnedFrames": scenario.returned_frames,
        "pointCount": POINT_COUNT,
        "fileCount": file_count,
        "imageCount": len(image_index),
        "totalBytes": total_bytes,
        "datasetHash": payload["datasetHash"],
        "dir": str(writer.dir),
        "state": scenario.state,
        "maxAmplitude": round(max_amplitude_overall, 6),
    }


def generate_all(out_dir: Path, force: bool) -> List[Dict[str, Any]]:
    """生成三套包。`--force` 只删这三套包的目录，不动 samples/ 下的其它内容
    （比如 README.md）—— 否则每次重新生成都会把说明文件一起删掉。"""
    scenarios = build_scenarios()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not force:
        existing = [s.scenario_id for s in scenarios if (out_dir / s.scenario_id / "manifest.json").is_file()]
        if existing:
            raise SystemExit(
                f"目标目录已存在样例包：{', '.join(existing)}\n"
                f"确认要覆盖请加 --force（只会删除这三套包的目录，不动其它文件）"
            )

    summaries: List[Dict[str, Any]] = []
    for scenario in scenarios:
        target = out_dir / scenario.scenario_id
        if target.exists():
            shutil.rmtree(target)
        summaries.append(generate_package(out_dir, scenario))
    return summaries


# --------------------------------------------------------------------------- #
# 9. 校验
# --------------------------------------------------------------------------- #

@dataclass
class Check:
    ok: bool
    name: str
    detail: str = ""
    warn: bool = False


class Report:
    """一个包的检查结果集合。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.checks: List[Check] = []

    def add(self, ok: bool, name: str, detail: str = "", warn: bool = False) -> bool:
        self.checks.append(Check(bool(ok), name, detail, warn))
        return bool(ok)

    @property
    def failures(self) -> List[Check]:
        return [item for item in self.checks if not item.ok and not item.warn]

    @property
    def warnings(self) -> List[Check]:
        return [item for item in self.checks if not item.ok and item.warn]

    def render(self) -> List[str]:
        lines = [f"  包：{self.title}"]
        for item in self.checks:
            tag = "[OK]  " if item.ok else ("[WARN]" if item.warn else "[FAIL]")
            suffix = f"  {item.detail}" if item.detail else ""
            lines.append(f"    {tag} {item.name}{suffix}")
        return lines


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_package(package_dir: Path, expected_scenario: Optional[Scenario] = None) -> Report:
    """校验一套包。所有检查都写进 Report，调用方按 failures 决定退出码。"""
    report = Report(str(package_dir))

    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        report.add(False, "manifest.json 存在", str(manifest_path))
        return report

    try:
        manifest = _load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        report.add(False, "manifest.json 可解析", str(exc))
        return report

    # ---- 1. 必需文件 ----
    required = list(BATCH_REQUIRED_FILES)
    for extra in ("batch.json", "plan.json", "result.json", "events.log", "marks.csv", "images/index.json"):
        if extra not in required:
            required.append(extra)
    missing = [name for name in required if not (package_dir / name).is_file()]
    report.add(not missing, "必需文件齐全", f"{len(required) - len(missing)}/{len(required)}" + (f"，缺 {missing}" if missing else ""))
    if missing:
        return report
    report.add((package_dir / "images").is_dir(), "images 目录存在")

    if expected_scenario is not None:
        report.add(
            expected_scenario.scenario_id == manifest.get("scenarioId"),
            "scenarioId 与目录名一致",
            f"目录={package_dir.name} manifest={manifest.get('scenarioId')}",
        )
        report.add(expected_scenario.batch_id == manifest.get("batchId"), "batchId 与剧本一致", str(manifest.get("batchId")))

    # ---- 2. 契约必填字段 ----
    manifest_required = (
        "schemaVersion", "batchId", "projectId", "orderId", "componentId", "segmentId", "zoneId",
        "positionSource", "radarSourceMode", "cameraSourceMode", "pairing", "format", "axis",
        "configVersion", "modelVersion", "scenarioId", "round", "operatorId", "deviceId",
        "frameCount", "pointCount", "fps", "returnedFrames", "state", "privacyNote", "createdAt",
        "files", "datasetHash",
    )
    lacked = [key for key in manifest_required if key not in manifest]
    report.add(not lacked, "manifest 必填字段", f"{len(manifest_required) - len(lacked)}/{len(manifest_required)}" + (f"，缺 {lacked}" if lacked else ""))
    for key, want in (
        ("projectId", PROJECT_ID), ("orderId", ORDER_ID), ("componentId", COMPONENT_ID),
        ("zoneId", ZONE_ID), ("operatorId", OPERATOR_ID), ("deviceId", DEVICE_ID),
        ("configVersion", CONFIG_VERSION), ("format", SAMPLE_FORMAT),
        ("positionSource", POSITION_SOURCE), ("radarSourceMode", RADAR_SOURCE_MODE),
        ("cameraSourceMode", CAMERA_SOURCE_MODE), ("pairing", PAIRING),
        ("axis", {"x": "frame_index", "y": "sample_index"}), ("pointCount", POINT_COUNT), ("fps", FPS),
    ):
        report.add(manifest.get(key) == want, f"manifest.{key} 取值", f"{manifest.get(key)!r}")
    report.add(manifest.get("state") in ("finished", "interrupted_pause"), "manifest.state 取值", repr(manifest.get("state")))
    try:
        datetime.strptime(str(manifest.get("createdAt")), "%Y-%m-%dT%H:%M:%SZ")
        report.add(True, "manifest.createdAt 是合法固定时刻", str(manifest.get("createdAt")))
    except ValueError:
        report.add(False, "manifest.createdAt 是合法固定时刻", f"{manifest.get('createdAt')!r} 不是合法 ISO-8601（小时/日期越界）")

    # ---- 3. manifest.files 与磁盘一致 ----
    disk_files: List[str] = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(package_dir).as_posix()
            if rel == "manifest.json" or rel.endswith(".tmp"):
                continue
            disk_files.append(rel)
    listed = {item["path"]: item for item in manifest.get("files") or []}
    report.add(set(listed) == set(disk_files), "manifest.files 与磁盘文件集合一致",
               f"磁盘 {len(disk_files)} 个 / 清单 {len(listed)} 个"
               + (f"，差异 {sorted(set(listed) ^ set(disk_files))}" if set(listed) != set(disk_files) else ""))

    bad_hash = []
    for rel in disk_files:
        item = listed.get(rel)
        if not item:
            continue
        path = package_dir / rel
        if item.get("sha256") != sha256_file(path) or item.get("bytes") != path.stat().st_size:
            bad_hash.append(rel)
    report.add(not bad_hash, "逐文件 sha256 与字节数一致", f"{len(disk_files)} 个文件" + (f"，不符 {bad_hash}" if bad_hash else ""))

    # 角色写错不会让包不可用，但平台按 role 归档时会少一类文件，所以也校验。
    wrong_role = [
        rel for rel in disk_files
        if listed.get(rel) and listed[rel].get("role") != _guess_role(rel)
    ]
    report.add(not wrong_role, "manifest.files[].role 与文件类型一致",
               f"{len(disk_files)} 个文件" + (f"，不符 {wrong_role}" if wrong_role else ""))

    # ---- 4. datasetHash 重算 ----
    digest = hashlib.sha256()
    for rel in sorted(disk_files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(package_dir / rel).encode("utf-8"))
        digest.update(b"\n")
    recomputed = digest.hexdigest()
    report.add(recomputed == manifest.get("datasetHash"), "datasetHash 重算一致",
               f"{recomputed[:16]}…" if recomputed == manifest.get("datasetHash") else f"重算 {recomputed[:16]}… ≠ manifest {str(manifest.get('datasetHash'))[:16]}…")

    # ---- 5. frames.csv ----
    returned = int(manifest.get("returnedFrames") or 0)
    planned = int(manifest.get("frameCount") or 0)
    point_count = int(manifest.get("pointCount") or 0)
    frames_path = package_dir / "frames.csv"
    with frames_path.open("r", encoding="utf-8", newline="") as handle:
        header = handle.readline().strip()
        rows = 0
        bad_rows: List[str] = []
        seen_frames: Dict[int, int] = {}
        amp_pattern = re.compile(r"^\d+\.\d{6}$")
        for line in handle:
            rows += 1
            parts = line.rstrip("\n").split(",")
            if len(parts) != len(FRAMES_CSV_COLUMNS):
                if len(bad_rows) < 3:
                    bad_rows.append(f"列数 {len(parts)}")
                continue
            frame_index = int(parts[0])
            sample_index = int(parts[1])
            amp_text = parts[2]
            seen_frames[frame_index] = seen_frames.get(frame_index, 0) + 1
            if len(bad_rows) < 3:
                if not amp_pattern.match(amp_text):
                    bad_rows.append(f"幅值格式 {amp_text}")
                else:
                    value = float(amp_text)
                    if not (AMP_MIN - 1e-9 <= value <= AMP_MAX + 1e-9):
                        bad_rows.append(f"幅值越界 {value}")
                if parts[3] != f"{frame_index * MS_PER_FRAME:.1f}":
                    bad_rows.append(f"t_ms {parts[3]}")
                if not (0 <= sample_index < point_count):
                    bad_rows.append(f"sample_index {sample_index}")
    report.add(header == ",".join(FRAMES_CSV_COLUMNS), "frames.csv 表头", header)
    report.add(rows == returned * point_count, "frames.csv 行数 == returnedFrames × pointCount",
               f"{rows} 行，期望 {returned}×{point_count}={returned * point_count}")
    report.add(len(seen_frames) == returned and all(count == point_count for count in seen_frames.values()),
               "帧号连续且每帧点数完整",
               f"{len(seen_frames)} 帧" + (f"，异常 {[k for k, v in seen_frames.items() if v != point_count][:3]}" if any(v != point_count for v in seen_frames.values()) else ""))
    report.add(not bad_rows, "frames.csv 行内容合法（6 位小数/范围内/t_ms 对齐）", "; ".join(bad_rows))

    # ---- 6. segments.json ----
    segments = _load_json(package_dir / "segments.json")
    if not isinstance(segments, list):
        report.add(False, "segments.json 是数组")
        segments = []
    else:
        report.add(True, "segments.json 是数组", f"{len(segments)} 条")
    report.add(len(segments) == returned, "segments 条数 == returnedFrames", f"{len(segments)} / {returned}")

    seg_problem: List[str] = []
    seg_ids: Dict[str, int] = {}
    seg_by_frame: Dict[int, Dict[str, Any]] = {}
    for item in segments:
        lacked_keys = [key for key in SEGMENT_KEYS if key not in item]
        if lacked_keys:
            seg_problem.append(f"{item.get('segmentId')} 缺 {lacked_keys}")
            continue
        try:
            index = frame_id_to_index(str(item["frameId"]))
        except ValueError as exc:
            seg_problem.append(str(exc))
            continue
        if not (0 <= index < returned):
            seg_problem.append(f"frameId 越界 {item['frameId']}")
        if int(item["frameIndex"]) != index:
            seg_problem.append(f"{item['frameId']} 与 frameIndex={item['frameIndex']} 不一致")
        if int(item["sampleCount"]) != point_count:
            seg_problem.append(f"{item['segmentId']} sampleCount={item['sampleCount']}")
        if item["axes"] != {"x": "frame_index", "y": "sample_index"}:
            seg_problem.append(f"{item['segmentId']} axes 不对")
        if item["sourceMode"] != RADAR_SOURCE_MODE:
            seg_problem.append(f"{item['segmentId']} sourceMode={item['sourceMode']}")
        if int(item["deviceMonotonicNs"]) != index * NS_PER_FRAME or int(item["tNs"]) != index * NS_PER_FRAME:
            seg_problem.append(f"{item['segmentId']} 时间轴不对")
        paired = item.get("pairedImage")
        if paired and not (package_dir / "images" / str(paired)).is_file():
            seg_problem.append(f"{item['segmentId']} pairedImage 不存在 {paired}")
        if len(str(item.get("segmentId"))) == 0:
            seg_problem.append("segmentId 为空")
        if item["segmentId"] in seg_ids:
            seg_problem.append(f"segmentId 重复 {item['segmentId']}")
        seg_ids[item["segmentId"]] = index
        seg_by_frame[index] = item
        for peak in item.get("peaks") or []:
            if abs(round(peak["sampleIndex"] / float(point_count - 1), 2) - float(peak["normalizedX"])) > 1e-9:
                seg_problem.append(f"{item['segmentId']} normalizedX 与 sampleIndex 不一致")
            if not (0.0 < float(peak["amplitude"]) <= AMP_MAX + 1e-9):
                seg_problem.append(f"{item['segmentId']} 峰值幅值越界")
    report.add(not seg_problem, "segments 字段完整且自洽（frameId 在范围内、时间轴、峰位置）", "; ".join(seg_problem[:3]))

    # ---- 7. marks ----
    marks = _load_json(package_dir / "marks.json")
    if not isinstance(marks, list):
        report.add(False, "marks.json 是数组")
        marks = []
    else:
        report.add(True, "marks.json 是数组", f"{len(marks)} 条")
    mark_problem: List[str] = []
    for mark in marks:
        lacked_keys = [key for key in MARK_KEYS if key not in mark]
        if lacked_keys:
            mark_problem.append(f"{mark.get('markId')} 缺 {lacked_keys}")
            continue
        if mark["positionSource"] != POSITION_SOURCE:
            mark_problem.append(f"{mark['markId']} positionSource={mark['positionSource']}")
        label = mark.get("operatorLabel")
        if label not in OPERATOR_LABELS and label != "":
            mark_problem.append(f"{mark['markId']} operatorLabel 非法 {label!r}")
        asset = str(mark.get("cameraAssetId") or "")
        if not asset or not (package_dir / "images" / asset).is_file():
            mark_problem.append(f"{mark['markId']} cameraAssetId 不存在 {asset!r}")
        try:
            index = frame_id_to_index(str(mark.get("frameId")))
        except ValueError as exc:
            mark_problem.append(str(exc))
            continue
        if not (0 <= index < returned):
            mark_problem.append(f"{mark['markId']} frameId 越界 {mark.get('frameId')}")
        if "frameIndex" in mark and int(mark["frameIndex"]) != index:
            mark_problem.append(f"{mark['markId']} frameIndex 与 frameId 不一致")
        if int(mark.get("deviceMonotonicNs", -1)) != index * NS_PER_FRAME:
            mark_problem.append(f"{mark['markId']} deviceMonotonicNs 不对")
        try:
            datetime.strptime(str(mark.get("createdAt")), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            mark_problem.append(f"{mark['markId']} createdAt 非法")
    report.add(not mark_problem, "marks 字段完整且自洽（frameId 在范围内、无方位猜测）", "; ".join(mark_problem[:3]))

    # ---- 8. marks.csv 与 marks.json 对齐 ----
    marks_csv = (package_dir / "marks.csv").read_text(encoding="utf-8").strip().split("\n")
    csv_header = marks_csv[0] if marks_csv else ""
    report.add(csv_header == ",".join(MARKS_CSV_COLUMNS), "marks.csv 列名", csv_header)
    report.add(len(marks_csv) - 1 == len(marks), "marks.csv 行数 == marks.json 条数", f"{len(marks_csv) - 1} / {len(marks)}")
    if len(marks_csv) - 1 == len(marks) and marks:
        mismatch = [
            f"{line.split(',')[0]}"
            for line, mark in zip(marks_csv[1:], marks)
            if line.split(",")[0] != mark["markId"] or line.split(",")[1] != str(mark.get("frameIndex"))
        ]
        report.add(not mismatch, "marks.csv 与 marks.json 逐行一致", "; ".join(mismatch[:3]))

    # ---- 9. quality.json ----
    quality = _load_json(package_dir / "quality.json")
    quality_required = ("frameCount", "expectedFrames", "missingFrames", "saturationPct",
                        "emptyFrames", "nanValues", "duplicateFrames", "levelSamples", "note")
    lacked = [key for key in quality_required if key not in quality]
    report.add(not lacked, "quality 必填字段", f"缺 {lacked}" if lacked else f"{len(quality_required)} 项齐全")
    report.add(int(quality.get("expectedFrames", -1)) == planned, "quality.expectedFrames == manifest.frameCount",
               f"{quality.get('expectedFrames')} / {planned}")
    report.add(int(quality.get("frameCount", -1)) == returned, "quality.frameCount == manifest.returnedFrames",
               f"{quality.get('frameCount')} / {returned}")
    report.add(int(quality.get("missingFrames", -1)) == planned - returned, "quality.missingFrames == 计划 - 实际",
               f"{quality.get('missingFrames')} / {planned - returned}")
    report.add(int(quality.get("nanValues", -1)) == 0 and int(quality.get("duplicateFrames", -1)) == 0,
               "quality.nanValues / duplicateFrames 为 0")
    report.add(bool(str(quality.get("note", "")).strip()), "quality.note 非空", str(quality.get("note", ""))[:40] + "…")

    # ---- 10. result.json 与 segments 的"同一组数"----
    result = _load_json(package_dir / "result.json")
    report.add("conclusion" in result and "findings" in result, "result 必填字段",
               f"conclusion={result.get('conclusion')!r} findings={len(result.get('findings') or [])}")
    report.add(result.get("conclusion") in ("withheld", "none", "preliminary"),
               "result.conclusion 取值合法", repr(result.get("conclusion")))
    finding_problem: List[str] = []
    for finding in result.get("findings") or []:
        for key in ("id", "label", "score", "sampleIndex", "frameIndex", "branch", "evidenceSegment"):
            if key not in finding:
                finding_problem.append(f"{finding.get('id')} 缺 {key}")
        segment = seg_by_frame.get(int(finding.get("frameIndex", -1)))
        if segment is None:
            finding_problem.append(f"{finding.get('id')} 找不到 frameIndex={finding.get('frameIndex')} 的响应段")
            continue
        if segment["segmentId"] != finding["evidenceSegment"]:
            finding_problem.append(f"{finding['id']} evidenceSegment 指向 {segment['segmentId']}，与 result 不一致")
        peaks = segment.get("peaks") or []
        if not peaks:
            finding_problem.append(f"{finding['id']} 对应响应段没有峰")
            continue
        matched = [peak for peak in peaks if int(peak["sampleIndex"]) == int(finding["sampleIndex"])]
        if not matched:
            finding_problem.append(f"{finding['id']} 响应段里没有 sampleIndex={finding['sampleIndex']} 的峰")
            continue
        if abs(float(matched[0]["amplitude"]) - float(finding["score"])) > 1e-9:
            finding_problem.append(
                f"{finding['id']} 峰值 {matched[0]['amplitude']} ≠ 分数 {finding['score']}"
            )
    report.add(not finding_problem, "result.findings 与 segments 对齐（峰值幅值 == 分数，证据段可解析）",
               "; ".join(finding_problem[:3]) if finding_problem else f"{len(result.get('findings') or [])} 条 finding 全部可回溯")

    # ---- 11. 图片索引 ----
    index = _load_json(package_dir / "images" / "index.json")
    if not isinstance(index, list):
        report.add(False, "images/index.json 是数组")
        index = []
    else:
        report.add(True, "images/index.json 是数组", f"{len(index)} 张")
    expected_images = len(range(0, returned, IMAGE_EVERY))
    report.add(len(index) == expected_images, "预览图数量 == 每 10 帧一张",
               f"{len(index)} / {expected_images}")
    image_problem: List[str] = []
    for item in index:
        for key in ("frameId", "file", "sha256", "width", "height", "kind"):
            if key not in item:
                image_problem.append(f"缺字段 {key}")
        path = package_dir / "images" / str(item.get("file"))
        if not path.is_file():
            image_problem.append(f"{item.get('file')} 不存在")
            continue
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            image_problem.append(f"{item['file']} 不是 PNG")
        if item.get("sha256") != sha256_bytes(data):
            image_problem.append(f"{item['file']} sha256 不符")
        if (int(item.get("width", 0)), int(item.get("height", 0))) != (IMAGE_WIDTH, IMAGE_HEIGHT):
            image_problem.append(f"{item['file']} 尺寸不是 {IMAGE_WIDTH}×{IMAGE_HEIGHT}")
        if b"IEND" not in data[-16:]:
            image_problem.append(f"{item['file']} 缺 IEND")
        try:
            if frame_id_to_index(str(item.get("frameId"))) % IMAGE_EVERY != 0:
                image_problem.append(f"{item['file']} frameId 不是每 10 帧")
        except ValueError as exc:
            image_problem.append(str(exc))
    report.add(not image_problem, "图片存在、sha256/尺寸正确", "; ".join(image_problem[:3]))

    # ---- 12. events.log ----
    event_lines = (package_dir / "events.log").read_text(encoding="utf-8").strip().split("\n")
    event_problem: List[str] = []
    last_t = -1
    for line in event_lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            event_problem.append(f"第 {len(event_problem) + 1} 行不是 JSON：{exc}")
            continue
        for key in ("tMs", "stage", "level", "text"):
            if key not in item:
                event_problem.append(f"缺字段 {key}")
        if int(item.get("tMs", -1)) < last_t:
            event_problem.append(f"tMs 不单调：{item.get('tMs')}")
        last_t = int(item.get("tMs", last_t))
        if int(item.get("tMs", 0)) < 0 or int(item.get("tMs", 0)) > planned * MS_PER_FRAME:
            event_problem.append(f"tMs 越界 {item.get('tMs')}")
    report.add(not event_problem, "events.log 是合法 NDJSON 且时间单调", f"{len(event_lines)} 条事件")

    # ---- 13. plan.json ----
    plan = _load_json(package_dir / "plan.json")
    plan_problem: List[str] = []
    if not isinstance(plan.get("paths"), list) or not plan["paths"]:
        plan_problem.append("paths 为空")
    else:
        for path in plan["paths"]:
            for key in ("pathId", "directionDeg", "startFrame", "endFrame", "note"):
                if key not in path:
                    plan_problem.append(f"{path.get('pathId')} 缺 {key}")
            if not (0 <= int(path.get("startFrame", -1)) <= int(path.get("endFrame", -1)) < planned):
                plan_problem.append(f"{path.get('pathId')} 帧段越界")
    if int(plan.get("expectedFrames", -1)) != planned:
        plan_problem.append(f"expectedFrames={plan.get('expectedFrames')} ≠ {planned}")
    report.add(not plan_problem, "plan.json 路径与预计帧数", "; ".join(plan_problem[:3]) if plan_problem else f"{len(plan.get('paths') or [])} 条路径")

    # ---- 14. dataset.json（仅参考样本包）----
    dataset_path = package_dir / "dataset.json"
    if manifest.get("round") == "reference":
        if not dataset_path.is_file():
            report.add(False, "参考样本包必须有 dataset.json")
        else:
            dataset = _load_json(dataset_path)
            groups = dataset.get("groups") or []
            problem: List[str] = []
            if len(groups) != 4:
                problem.append(f"分组数 {len(groups)} ≠ 4")
            covered: List[int] = []
            for group in groups:
                for key in ("physicalSampleId", "materialSource", "knownState", "labelBasis", "frames", "groupId"):
                    if key not in group:
                        problem.append(f"{group.get('physicalSampleId')} 缺 {key}")
                frames = [int(value) for value in group.get("frames") or []]
                if any(not (0 <= value < returned) for value in frames):
                    problem.append(f"{group.get('physicalSampleId')} 帧号越界")
                covered.extend(frames)
            if sorted(covered) != list(range(returned)):
                problem.append("四个分组的帧号未恰好覆盖全部帧（重叠或缺失）")
            unknown = [g for g in groups if g.get("physicalSampleId") == "S-04"]
            if unknown and unknown[0].get("labelBasis") != "无标签依据，单列待核验集合":
                problem.append("S-04 的 labelBasis 不是待核验口径")
            report.add(not problem, "dataset.json 分组完整且帧号不重不漏", "; ".join(problem[:3]) if problem else "4 组 × 60 帧 = 240 帧")
    else:
        report.add(not dataset_path.is_file(), "非参考样本包不带 dataset.json")

    # ---- 15. 契约模块一致性 ----
    report.add(
        CONTRACT_SOURCE == "woodpulse.contracts",
        "已按 woodpulse.contracts 校验（未读到则用内置字面量）",
        f"来源：{CONTRACT_SOURCE}",
        warn=CONTRACT_SOURCE != "woodpulse.contracts",
    )

    return report


def verify_root(root: Path) -> Tuple[List[Report], int]:
    """校验一个样例根目录（或单个包目录）。返回 (报告列表, 失败数)。"""
    scenarios = {item.scenario_id: item for item in build_scenarios()}
    if (root / "manifest.json").is_file():
        reports = [verify_package(root, scenarios.get(root.name))]
        return reports, sum(len(report.failures) for report in reports)

    reports: List[Report] = []
    for scenario in build_scenarios():
        package_dir = root / scenario.scenario_id
        if not package_dir.is_dir():
            report = Report(str(package_dir))
            report.add(False, "样例包目录存在", str(package_dir))
            reports.append(report)
            continue
        reports.append(verify_package(package_dir, scenario))
    failures = sum(len(report.failures) for report in reports)
    return reports, failures


# --------------------------------------------------------------------------- #
# 10. 命令行
# --------------------------------------------------------------------------- #

def _print_generation(summaries: Sequence[Dict[str, Any]]) -> None:
    total = 0
    print("生成结果：")
    for item in summaries:
        total += item["totalBytes"]
        print(
            f"  [OK]   {item['scenarioId']:<22} batchId={item['batchId']:<13} "
            f"帧 {item['returnedFrames']}/{item['frameCount']}  文件 {item['fileCount']} 个"
            f"（图片 {item['imageCount']}）  {item['totalBytes'] / 1048576:.2f} MB  "
            f"state={item['state']}  datasetHash={item['datasetHash'][:16]}…"
        )
    print(f"  合计：3 套包，{total / 1048576:.2f} MB")


def _print_reports(reports: Sequence[Report]) -> None:
    for report in reports:
        for line in report.render():
            print(line)


def cmd_generate(out: Path, force: bool) -> int:
    summaries = generate_all(out, force)
    _print_generation(summaries)
    print(f"输出目录：{out.resolve()}")
    return 0


def cmd_verify(root: Path) -> int:
    if not root.exists():
        print(f"[FAIL] 目录不存在：{root}")
        return 1
    print("样例包校验报告")
    print(f"  根目录：{root.resolve()}")
    reports, failures = verify_root(root)
    _print_reports(reports)
    ok = sum(1 for report in reports if not report.failures)
    warns = sum(len(report.warnings) for report in reports)
    print(f"  汇总：{len(reports)} 个包，通过 {ok}，失败 {len(reports) - ok}，警告项 {warns}")
    print(f"退出码 {1 if failures else 0}")
    return 1 if failures else 0


def _tree_hashes(root: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256_file(path)
    return result


def cmd_self_test() -> int:
    """CI 入口：生成 → 校验 → 再生成一份比对字节 → 删除临时目录。

    第二次生成不是浪费：它证明"同样输入每次生成字节完全一致"这条硬要求
    真的成立（PNG 里混进时间戳、字典顺序不稳、浮点格式化不一致都会在这里暴露）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="woodpulse-samples-"))
    print(f"自检临时目录：{tmp}")
    try:
        first = tmp / "a"
        second = tmp / "b"
        print("步骤 1/3：生成第一份")
        summaries = generate_all(first, force=True)
        _print_generation(summaries)
        print("步骤 2/3：校验第一份")
        reports, failures = verify_root(first)
        _print_reports(reports)
        if failures:
            print(f"自检失败：校验未通过（{failures} 项）")
            return 1
        print("步骤 3/3：再生成一份并逐文件比对 sha256")
        generate_all(second, force=True)
        left = _tree_hashes(first)
        right = _tree_hashes(second)
        if set(left) != set(right):
            print(f"[FAIL] 两次生成的文件集合不同：{sorted(set(left) ^ set(right))[:5]}")
            return 1
        diff = [name for name in left if left[name] != right[name]]
        if diff:
            print(f"[FAIL] 两次生成有 {len(diff)} 个文件字节不同：{diff[:5]}")
            return 1
        print(f"[OK]   {len(left)} 个文件两次生成字节完全一致（字节级可复现）")
        print("自检通过，退出码 0")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"临时目录已删除：{tmp}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_samples.py",
        description="木脉智检手持终端：固定检测样例包生成器（只用标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/make_samples.py --out samples --force\n"
            "  python tools/make_samples.py --verify samples\n"
            "  python tools/make_samples.py --self-test\n"
        ),
    )
    parser.add_argument("--out", default="samples", help="生成输出根目录（默认 samples）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的三套样例包目录")
    parser.add_argument("--verify", metavar="DIR", help="只校验已有包，不生成（退出码 0/1）")
    parser.add_argument("--self-test", action="store_true", help="生成到临时目录→校验→字节比对→删除")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    actions = [bool(args.verify), bool(args.self_test)]
    if sum(actions) > 1:
        parser.error("--verify 与 --self-test 不能同时使用")
    if args.self_test:
        return cmd_self_test()
    if args.verify:
        return cmd_verify(Path(args.verify).expanduser())
    return cmd_generate(Path(args.out).expanduser(), args.force)


if __name__ == "__main__":
    raise SystemExit(main())
