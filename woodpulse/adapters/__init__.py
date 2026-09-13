"""适配器：相机（实采）与样例回放（唯一的"回波"来源）。

能力声明口径（PRD §7.2、§10）：
    adapters/camera.py   camera  = live / unavailable
    adapters/replay.py   radar   = replay（本机没有真实毫米波回波）
                         imu     = unavailable（本机没有 IMU）
这两个模块都不导入 Qt，便于在无图形环境下单测。
"""

from .camera import CameraFrame, CameraSource, CameraStats  # noqa: F401
from .replay import ReplayError, ReplayLibrary, ReplaySession, SamplePackage  # noqa: F401

__all__ = [
    "CameraSource",
    "CameraFrame",
    "CameraStats",
    "ReplayLibrary",
    "ReplaySession",
    "SamplePackage",
    "ReplayError",
]
