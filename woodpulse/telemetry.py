"""真实设备遥测（PRD §6 逐行对应）。

这一版最重要的一条：**这里没有任何随机数**。旧版用 random 生成 CPU、温度、FPS
（PRD §2 表格），本版全部换成真实采集；采不到的字段返回 None 并带 reason，
让界面显示"不可用 + 原因"，而不是显示一个假零值或假曲线。

采集口径按 PRD §6 表格：
  · CPU      psutil.cpu_percent 的非阻塞差分；首样本无效时标 quality=warmup
  · 进程     当前进程 CPU 与 RSS，多核口径单独说明
  · 内存/磁盘 已用/总量/可用；磁盘检测**实际数据目录**所在分区
  · SoC 温度 能力探测后走 vcgencmd 或已识别的 thermal 传感器，并记录来源
  · 降频/欠压 get_throttled；当前标志与历史发生标志**分开**
  · 接口速率 指定接口字节计数差分 ÷ 单调时间差；重连/计数回退时重建基线，
             绝不出负速率或巨大峰值
  · 平台延迟 心跳请求与响应往返
  · 相机 FPS 成功读帧数 ÷ 滑动窗口时长（由 adapters/camera 提供计数）
  · 样例播放率 回放帧计数与进度，sourceMode=replay，不写成雷达实测帧率

psutil 缺失时不是"全不可用"：/proc 与 sysfs 的降级路径会把能读到的部分补上，
读不到的字段照样标 unavailable 并写明原因。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logging_setup import get_logger

log = get_logger("telemetry")

# --------------------------------------------------------------------------- #
# 可选依赖探测
# --------------------------------------------------------------------------- #

try:  # psutil 提供 CPU / 内存 / 网卡计数（PRD §6 引用的就是它的接口）
    import psutil  # type: ignore

    HAS_PSUTIL = True
except Exception:  # noqa: BLE001 - 装不上也要能跑，只是字段变少
    psutil = None  # type: ignore
    HAS_PSUTIL = False

MB = 1024 * 1024


# --------------------------------------------------------------------------- #
# 环境探测结果
# --------------------------------------------------------------------------- #

@dataclass
class ProbeResult:
    """一项能力探测的结果。`available=False` 时 reason 必须写清楚为什么。"""

    available: bool
    value: Optional[Any] = None
    source: str = ""
    reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


def _read_text(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _run(cmd: List[str], timeout: float = 2.0) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"退出码 {proc.returncode}").strip()
    return True, (proc.stdout or "").strip()


# --------------------------------------------------------------------------- #
# 温度 / 降频 / 频率
# --------------------------------------------------------------------------- #

class TemperatureProbe:
    """SoC 温度。按能力探测选择来源，并**记录来源**（PRD §6）。

    树莓派 SoC 温度不能代替木材周围环境温度——环境温度来自平台的仪表记录，
    这里只是设备自身的热状态。
    """

    def __init__(self) -> None:
        self.source = ""
        self.method: Optional[Callable[[], Optional[float]]] = None
        self._thermal_path: Optional[str] = None
        self._probe()

    def _probe(self) -> None:
        ok, out = _run(["vcgencmd", "measure_temp"])
        if ok and "temp=" in out:
            self.source = "vcgencmd"
            self.method = self._from_vcgencmd
            return

        thermal_root = Path("/sys/class/thermal")
        if thermal_root.is_dir():
            candidates: List[Tuple[int, str]] = []
            for zone in sorted(thermal_root.glob("thermal_zone*")):
                kind = _read_text(str(zone / "type")) or ""
                if not kind:
                    continue
                priority = 0
                lowered = kind.lower()
                if "soc" in lowered or "cpu" in lowered or "bcm" in lowered:
                    priority = 2
                elif "thermal" in lowered or "board" in lowered:
                    priority = 1
                candidates.append((priority, str(zone / "temp")))
            if candidates:
                candidates.sort(key=lambda item: -item[0])
                self._thermal_path = candidates[0][1]
                self.source = f"thermal:{Path(self._thermal_path).parent.name}"
                self.method = self._from_sysfs

        if self.method is None:
            try:  # 有的发行版把温度暴露在 psutil 里
                if HAS_PSUTIL and psutil.sensors_temperatures():
                    self.source = "psutil:sensors_temperatures"
                    self.method = self._from_psutil
            except Exception:  # noqa: BLE001
                pass

        if self.method is None:
            self.source = ""
            log.info("未找到可用的 SoC 温度接口，该字段将上报 null（不使用估计值）")

    def probe_result(self) -> ProbeResult:
        if self.method is None:
            return ProbeResult(False, source="", reason="未探测到 vcgencmd 或可识别的 thermal 传感器")
        value = self.method()
        if value is None:
            return ProbeResult(False, source=self.source, reason="温度接口存在但本次读取失败")
        return ProbeResult(True, value=value, source=self.source)

    def _from_vcgencmd(self) -> Optional[float]:
        ok, out = _run(["vcgencmd", "measure_temp"])
        if not ok:
            return None
        try:
            return float(out.split("=")[1].split("'")[0])
        except (IndexError, ValueError):
            return None

    def _from_sysfs(self) -> Optional[float]:
        raw = _read_int(self._thermal_path or "")
        if raw is None:
            return None
        # sysfs 单位是毫摄氏度
        return raw / 1000.0

    def _from_psutil(self) -> Optional[float]:
        try:
            groups = psutil.sensors_temperatures() or {}
        except Exception:  # noqa: BLE001
            return None
        for name in ("cpu_thermal", "coretemp", "soc_thermal", "cpu-thermal"):
            for entry in groups.get(name, []):
                if entry.current:
                    return float(entry.current)
        for entries in groups.values():
            for entry in entries:
                if entry.current:
                    return float(entry.current)
        return None


class ThrottleProbe:
    """`get_throttled` 位解析（PRD §6：当前标志与历史发生标志分开）。

    位定义（树莓派官方文档）：
        0 under-voltage detected            16 under-voltage has occurred
        1 arm frequency capped              17 arm frequency capping has occurred
        2 currently throttled               18 throttling has occurred
        3 soft temperature limit active     19 soft temperature limit has occurred
    """

    BITS = (
        (0, "underVoltageNow", "当前欠压"),
        (1, "freqCappedNow", "当前频率被限制"),
        (2, "throttledNow", "当前降频"),
        (3, "softTempLimitNow", "当前软温度限制"),
        (16, "underVoltageOccurred", "历史发生欠压"),
        (17, "freqCappedOccurred", "历史发生频率限制"),
        (18, "throttledOccurred", "历史发生降频"),
        (19, "softTempLimitOccurred", "历史发生软温度限制"),
    )

    def __init__(self) -> None:
        self.supported = False
        ok, out = _run(["vcgencmd", "get_throttled"])
        self.supported = ok and "throttled=" in out
        if not self.supported:
            log.info("get_throttled 不可用（非树莓派或 vcgencmd 缺失），该字段上报 null")

    def read(self) -> ProbeResult:
        if not self.supported:
            return ProbeResult(False, reason="vcgencmd get_throttled 不可用")
        ok, out = _run(["vcgencmd", "get_throttled"])
        if not ok or "throttled=" not in out:
            return ProbeResult(False, source="vcgencmd", reason="get_throttled 读取失败")
        try:
            raw = int(out.split("=")[1], 16)
        except (IndexError, ValueError):
            return ProbeResult(False, source="vcgencmd", reason="get_throttled 返回值无法解析")
        flags = {name: bool(raw & (1 << bit)) for bit, name, _ in self.BITS}
        flags["raw"] = f"0x{raw:x}"
        labels = [label for bit, name, label in self.BITS if flags[name]]
        return ProbeResult(True, value=flags, source="vcgencmd", detail={"activeLabels": labels})


class CpuFreqProbe:
    """CPU 实际频率（PRD §6：不把频率当负载）。"""

    def __init__(self) -> None:
        self.paths = sorted(str(p) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
        if not self.paths:
            ok, out = _run(["vcgencmd", "measure_clock", "arm"])
            self._use_vcgencmd = ok and "frequency" in out
        else:
            self._use_vcgencmd = False
        if not self.paths and not self._use_vcgencmd:
            log.info("未找到 CPU 频率接口，cpuFreqMhz 上报 null")

    @property
    def supported(self) -> bool:
        return bool(self.paths) or self._use_vcgencmd

    def read(self) -> ProbeResult:
        if self.paths:
            values = [value for value in (_read_int(path) for path in self.paths) if value]
            if not values:
                return ProbeResult(False, source="sysfs:scaling_cur_freq", reason="scaling_cur_freq 读取失败")
            mhz = max(values) / 1000.0  # kHz → MHz
            return ProbeResult(
                True, value=round(mhz, 1), source="sysfs:scaling_cur_freq", detail={"cores": len(values)}
            )
        if self._use_vcgencmd:
            ok, out = _run(["vcgencmd", "measure_clock", "arm"])
            if not ok or "=" not in out:
                return ProbeResult(False, source="vcgencmd", reason="measure_clock 读取失败")
            try:
                hz = int(out.split("=")[1])
            except (IndexError, ValueError):
                return ProbeResult(False, source="vcgencmd", reason="measure_clock 返回值无法解析")
            return ProbeResult(True, value=round(hz / 1_000_000, 1), source="vcgencmd")
        return ProbeResult(False, reason="系统未提供 CPU 频率接口")


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

@dataclass
class InterfaceCounters:
    """接口字节计数的一次采样。"""

    interface: str
    address: str
    is_wireless: bool
    link_up: bool
    tx_bytes: int
    rx_bytes: int
    monotonic: float


class NetworkProbe:
    """接口速率与链路状态（PRD §6）。

    差分口径：`(bytes_now - bytes_prev) / (monotonic_now - monotonic_prev)`。
    接口重置、计数回退、重连时重建基线并返回 0（不是负数，也不是巨大峰值）。
    """

    def __init__(self, preferred: Optional[str] = None) -> None:
        self.preferred = preferred
        self._previous: Optional[InterfaceCounters] = None
        self._interface: Optional[str] = None
        if not HAS_PSUTIL:
            log.warning("未安装 psutil，网络速率改走 /sys/class/net 降级路径")

    def select_interface(self) -> Optional[str]:
        if self._interface:
            return self._interface
        if self.preferred:
            self._interface = self.preferred
            return self._interface
        candidates: List[str] = []
        if HAS_PSUTIL:
            try:
                candidates = [name for name, stats in psutil.net_if_stats().items() if stats.isup and name != "lo"]
            except Exception:  # noqa: BLE001
                candidates = []
        if not candidates:
            net_root = Path("/sys/class/net")
            if net_root.is_dir():
                candidates = [p.name for p in net_root.iterdir() if p.name != "lo" and _read_int(str(p / "flags")) is not None]
        # 优先无线（现场是手持机接路由），其次有线，最后任意
        def rank(name: str) -> int:
            if name.startswith("wl"):
                return 0
            if name.startswith("en") or name.startswith("eth"):
                return 1
            return 2

        candidates.sort(key=rank)
        self._interface = candidates[0] if candidates else None
        if self._interface:
            log.info("遥测选用网络接口：%s", self._interface)
        return self._interface

    def _address_of(self, interface: str) -> str:
        if HAS_PSUTIL:
            try:
                addrs = psutil.net_if_addrs().get(interface) or []
                for entry in addrs:
                    family = getattr(entry, "family", None)
                    # AF_INET 在不同平台上的枚举表示不同，用名字判断更稳
                    if family == socket.AF_INET or str(family).endswith("AF_INET"):
                        return entry.address
            except Exception:  # noqa: BLE001
                pass
        ok, out = _run(["ip", "-4", "-o", "addr", "show", interface])
        if ok:
            for token in out.split():
                if token.count(".") == 3 and "/" in token:
                    return token.split("/")[0]
        return ""

    def _is_wireless(self, interface: str) -> bool:
        return Path(f"/sys/class/net/{interface}/wireless").exists() or interface.startswith("wl")

    def _counters(self, interface: str) -> Optional[Tuple[int, int]]:
        if HAS_PSUTIL:
            try:
                stats = psutil.net_io_counters(pernic=True).get(interface)
                if stats:
                    return int(stats.bytes_sent), int(stats.bytes_recv)
            except Exception:  # noqa: BLE001
                pass
        tx = _read_int(f"/sys/class/net/{interface}/statistics/tx_bytes")
        rx = _read_int(f"/sys/class/net/{interface}/statistics/rx_bytes")
        if tx is None or rx is None:
            return None
        return tx, rx

    def _link_up(self, interface: str) -> bool:
        carrier = _read_text(f"/sys/class/net/{interface}/carrier")
        if carrier is not None:
            return carrier.strip() == "1"
        if HAS_PSUTIL:
            try:
                stats = psutil.net_if_stats().get(interface)
                return bool(stats and stats.isup)
            except Exception:  # noqa: BLE001
                return False
        return False

    def sample(self) -> Dict[str, Any]:
        interface = self.select_interface()
        if not interface:
            return {
                "interface": None,
                "address": None,
                "linkUp": False,
                "txBytesPerSec": None,
                "rxBytesPerSec": None,
                "isWireless": None,
                "quality": "unavailable",
                "reason": "未找到可用网络接口",
            }
        counters = self._counters(interface)
        now = time.monotonic()
        wireless = self._is_wireless(interface)
        address = self._address_of(interface)
        link_up = self._link_up(interface)
        if counters is None:
            return {
                "interface": interface,
                "address": address or None,
                "linkUp": link_up,
                "txBytesPerSec": None,
                "rxBytesPerSec": None,
                "isWireless": wireless,
                "quality": "unavailable",
                "reason": "接口字节计数不可读",
            }

        tx, rx = counters
        tx_rate: Optional[float] = None
        rx_rate: Optional[float] = None
        note = ""
        if self._previous and self._previous.interface == interface:
            elapsed = now - self._previous.monotonic
            if elapsed > 0:
                delta_tx = tx - self._previous.tx_bytes
                delta_rx = rx - self._previous.rx_bytes
                if delta_tx < 0 or delta_rx < 0:
                    note = "接口计数回退（重连或重置），已重建差分基线，本次速率上报 0"
                    delta_tx = max(0, delta_tx)
                    delta_rx = max(0, delta_rx)
                tx_rate = round(delta_tx / elapsed, 1)
                rx_rate = round(delta_rx / elapsed, 1)
        else:
            note = "首次采样，仅建立差分基线"

        self._previous = InterfaceCounters(interface, address, wireless, link_up, tx, rx, now)
        return {
            "interface": interface,
            "address": address or None,
            "linkUp": link_up,
            "txBytesPerSec": tx_rate,
            "rxBytesPerSec": rx_rate,
            "isWireless": wireless,
            "quality": "baseline" if tx_rate is None else "ok",
            "reason": note,
        }

    def wifi_signal(self) -> Dict[str, Any]:
        """Wi-Fi RSSI（PRD §6：接口和权限允许时读 RSSI dBm，不假设都是无线）。"""
        interface = self.select_interface()
        if not interface or not self._is_wireless(interface):
            return {
                "interface": interface,
                "rssiDbm": None,
                "quality": "unavailable",
                "reason": "当前接口不是无线接口" if interface else "没有网络接口",
            }
        ok, out = _run(["iw", "dev", interface, "link"])
        if ok and "signal:" in out:
            for line in out.splitlines():
                if "signal:" in line:
                    try:
                        return {
                            "interface": interface,
                            "rssiDbm": float(line.split("signal:")[1].split("dBm")[0].strip()),
                            "quality": "ok",
                            "reason": "",
                        }
                    except (IndexError, ValueError):
                        break
        wireless_text = _read_text("/proc/net/wireless")
        if wireless_text:
            for line in wireless_text.splitlines():
                if line.strip().startswith(interface):
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            level = float(parts[3].rstrip("."))
                            return {
                                "interface": interface,
                                "rssiDbm": level,
                                "quality": "ok",
                                "reason": "来自 /proc/net/wireless",
                            }
                        except ValueError:
                            break
        return {
            "interface": interface,
            "rssiDbm": None,
            "quality": "unavailable",
            "reason": "无 iw 命令或权限不足，无法读取 RSSI",
        }


# --------------------------------------------------------------------------- #
# 主遥测服务
# --------------------------------------------------------------------------- #

#: 各字段的上报周期（PRD §6“上报建议”列）
CADENCE = {
    "cpu": 1.0,
    "process": 2.0,
    "memory": 2.0,
    "disk": 10.0,
    "socTemp": 2.0,
    "throttled": 5.0,
    "cpuFreq": 2.0,
    "network": 1.0,
    "wifi": 5.0,
    "camera": 1.0,
    "replay": 1.0,
}


class TelemetryService:
    """按周期采集真实指标并组装遥测 payload（PRD §6、§7.3）。

    典型用法（在工作线程里）：
        snap = telemetry.snapshot(camera=camera_stats, replay=replay_stats, uploads=app.upload_summary())
        # snap 就是 device.telemetry 的 payload
    """

    def __init__(self, cfg, *, data_dir: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir) if data_dir else Path(cfg.data_path)
        self.temperature = TemperatureProbe()
        self.throttle = ThrottleProbe()
        self.cpu_freq = CpuFreqProbe()
        self.network = NetworkProbe()
        self._process = None
        self._due = {key: 0.0 for key in CADENCE}
        self._cache: Dict[str, Any] = {}
        self._last_cpu_sample: Optional[float] = None
        self._cpu_warm = True
        self._started = time.monotonic()
        self.platform_latency_ms: Optional[float] = None
        self.capability_notes: Dict[str, str] = {}
        self._prime_cpu()

    # ---- 初始化 ----

    def _prime_cpu(self) -> None:
        """cpu_percent 的第一次调用永远返回 0.0，这里先"点火"一次。

        PRD §6 明确要求：首样本无效时标 warmup，不能把 0 当真实负载报上去。
        """
        if HAS_PSUTIL:
            try:
                psutil.cpu_percent(interval=None)
                self._process = psutil.Process(os.getpid())
                self._process.cpu_percent(interval=None)
            except Exception:  # noqa: BLE001
                self._process = None
        self._warm_started = time.monotonic()

    # ---- 单项采集 ----

    def _cpu(self) -> Dict[str, Any]:
        if not HAS_PSUTIL:
            load1 = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
            if load1 is None:
                return {
                    "cpuPercent": None,
                    "cpuQuality": "unavailable",
                    "cpuReason": "未安装 psutil，且系统不支持 os.getloadavg()",
                }
            cores = os.cpu_count() or 1
            return {
                "cpuPercent": None,
                "cpuQuality": "unavailable",
                "cpuReason": "未安装 psutil；负载均值不能直接当 CPU 百分比",
                "loadAverage1m": round(load1, 2),
                "cpuCount": cores,
            }
        value = float(psutil.cpu_percent(interval=None))
        if self._cpu_warm:
            # 距离点火不足一个采样周期时读数不具代表性
            if time.monotonic() - self._warm_started < 0.9:
                return {"cpuPercent": None, "cpuQuality": "warmup", "cpuReason": "首个采样窗口未结束（预热中）"}
            self._cpu_warm = False
        return {"cpuPercent": round(value, 1), "cpuQuality": "ok", "cpuCount": psutil.cpu_count() or os.cpu_count()}

    def _process_stats(self) -> Dict[str, Any]:
        if not HAS_PSUTIL or self._process is None:
            return {
                "processCpuPercent": None,
                "processRssBytes": None,
                "processQuality": "unavailable",
                "processReason": "未安装 psutil",
            }
        try:
            cpu = float(self._process.cpu_percent(interval=None))
            rss = int(self._process.memory_info().rss)
        except Exception as exc:  # noqa: BLE001
            return {
                "processCpuPercent": None,
                "processRssBytes": None,
                "processQuality": "unavailable",
                "processReason": f"进程信息读取失败：{exc}",
            }
        # 口径说明写进载荷，避免与整机 CPU 混淆
        return {
            "processCpuPercent": round(cpu, 1),
            "processRssBytes": rss,
            "processQuality": "ok",
            "processCpuScope": f"单进程口径，可超过 100%（多核合计），本机 {os.cpu_count()} 核",
        }

    def _memory(self) -> Dict[str, Any]:
        if HAS_PSUTIL:
            try:
                mem = psutil.virtual_memory()
                swap = psutil.swap_memory()
                return {
                    "memory": {
                        "usedBytes": int(mem.used),
                        "totalBytes": int(mem.total),
                        "availableBytes": int(mem.available),
                        "percent": round(float(mem.percent), 1),
                        "swapUsedBytes": int(swap.used),
                        "swapTotalBytes": int(swap.total),
                    },
                    "memoryQuality": "ok",
                }
            except Exception as exc:  # noqa: BLE001
                return {"memory": None, "memoryQuality": "unavailable", "memoryReason": f"内存信息读取失败：{exc}"}
        info = _read_text("/proc/meminfo")
        if not info:
            return {"memory": None, "memoryQuality": "unavailable", "memoryReason": "无 psutil 且 /proc/meminfo 不可读"}
        values: Dict[str, int] = {}
        for line in info.splitlines():
            parts = line.split(":")
            if len(parts) == 2:
                digits = parts[1].strip().split()[0]
                if digits.isdigit():
                    values[parts[0].strip()] = int(digits) * 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if not total or available is None:
            return {"memory": None, "memoryQuality": "unavailable", "memoryReason": "/proc/meminfo 缺少 MemTotal/MemAvailable"}
        used = total - available
        return {
            "memory": {
                "usedBytes": used,
                "totalBytes": total,
                "availableBytes": available,
                "percent": round(used * 100.0 / total, 1),
                "swapUsedBytes": None,
                "swapTotalBytes": None,
            },
            "memoryQuality": "ok",
            "memorySource": "/proc/meminfo",
        }

    def _disk(self) -> Dict[str, Any]:
        """磁盘检测**实际数据目录**所在分区（PRD §6）。"""
        target = self.data_dir
        try:
            usage = shutil.disk_usage(str(target))
        except OSError as exc:
            return {"disk": None, "diskQuality": "unavailable", "diskReason": f"数据目录不可用：{exc}"}
        return {
            "disk": {
                "path": str(target),
                "usedBytes": int(usage.used),
                "totalBytes": int(usage.total),
                "freeBytes": int(usage.free),
                "percent": round(usage.used * 100.0 / usage.total, 1) if usage.total else None,
            },
            "diskQuality": "ok",
        }

    # ---- 组装 ----

    def snapshot(
        self,
        *,
        camera: Optional[Dict[str, Any]] = None,
        replay: Optional[Dict[str, Any]] = None,
        uploads: Optional[Dict[str, Any]] = None,
        versions: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """按到期周期采集一次遥测。`force=True` 用于握手/重连后的即时快照。"""
        now = time.monotonic()
        payload: Dict[str, Any] = {
            "sourceMode": "live",
            "sampleWindowMs": int(self.cfg.platform.telemetry_interval_s * 1000),
            "uptimeSeconds": round(now - self._started, 1),
            "generatedBy": "woodpulse.telemetry",
            "sampledAt": utc_now_iso(),
        }

        def due(key: str) -> bool:
            return force or now >= self._due.get(key, 0.0)

        def mark(key: str) -> None:
            self._due[key] = now + CADENCE[key]

        if due("cpu"):
            payload.update(self._cpu())
            mark("cpu")
        if due("process"):
            payload.update(self._process_stats())
            mark("process")
        if due("memory"):
            payload.update(self._memory())
            mark("memory")
        if due("disk"):
            payload.update(self._disk())
            mark("disk")
        if due("socTemp"):
            result = self.temperature.probe_result()
            payload["socTempC"] = result.value
            payload["socTempSource"] = result.source or None
            payload["socTempQuality"] = "ok" if result.available else "unavailable"
            payload["socTempReason"] = result.reason
            mark("socTemp")
        if due("throttled"):
            result = self.throttle.read()
            payload["throttled"] = result.value
            payload["throttledSupported"] = result.available
            payload["throttledReason"] = result.reason
            payload["throttledLabels"] = result.detail.get("activeLabels", [])
            mark("throttled")
        if due("cpuFreq"):
            result = self.cpu_freq.read()
            payload["cpuFreqMhz"] = result.value
            payload["cpuFreqSource"] = result.source or None
            payload["cpuFreqReason"] = result.reason
            mark("cpuFreq")
        if due("network"):
            payload["network"] = self.network.sample()
            mark("network")
        if due("wifi"):
            payload["wifi"] = self.network.wifi_signal()
            mark("wifi")
        if due("camera"):
            payload["camera"] = camera or {
                "backend": None,
                "device": None,
                "captureFps": None,
                "displayFps": None,
                "droppedFrames": None,
                "lastFrameAgeMs": None,
                "state": "unknown",
                "reason": "本次未提供相机统计",
            }
            mark("camera")
        if due("replay"):
            payload["replay"] = replay or {
                "scenarioId": None,
                "batchId": None,
                "frameIndex": None,
                "frameCount": None,
                "playbackFps": None,
                "datasetHash": "",
                "sourceMode": "replay",
                "reason": "本机当前没有回放任务",
            }
            mark("replay")

        # 这几个字段周期短、变化快，每次快照都带上
        payload["platformLatencyMs"] = self.platform_latency_ms
        payload["upload"] = uploads or {
            "queued": 0,
            "pendingBytes": 0,
            "confirmedBytes": 0,
            "activeFile": None,
            "lastError": None,
        }
        if versions:
            payload["versions"] = versions
        else:
            payload["versions"] = {
                "appVersion": None,
                "adapterVersion": None,
                "controllerVersion": None,
                "demoModelVersion": None,
                "configVersion": None,
            }

        self._cache = payload
        return payload

    # ---- 能力声明（PRD §7.2：能力字段必须根据启动检查生成）----

    def probe_capabilities(self, *, camera_available: bool, camera_reason: str = "") -> Dict[str, Tuple[str, str]]:
        """返回 {能力名: (取值, 原因)}。

        radar 恒为 replay（本机没有真实毫米波回波）；
        imu / battery 在探测不到接口时恒为 unavailable，页面显示"未接入"，不全绿。
        """
        report: Dict[str, Tuple[str, str]] = {}

        report["camera"] = ("live", "") if camera_available else ("unavailable", camera_reason or "未探测到可用相机")
        report["radar"] = ("replay", "本机没有真实毫米波回波，响应序列来自固定检测样例包")
        report["imu"] = self._probe_imu()
        report["battery"] = self._probe_battery()
        report["telemetry"] = self._probe_telemetry()
        report["gpu"] = self._probe_gpu()
        report["preview"] = ("live", "") if camera_available else ("unavailable", "无相机时不产生预览图")
        return report

    def _probe_imu(self) -> Tuple[str, str]:
        if not HAS_PSUTIL:
            return "unavailable", "未安装 psutil，无法枚举传感器；且本机未配置 IMU 驱动"
        try:
            sensors = psutil.sensors_temperatures()  # 仅用于确认 psutil 可用
            _ = sensors
        except Exception:  # noqa: BLE001
            pass
        return "unavailable", "本机未配置 IMU 驱动，不显示枪体倾斜、航向或轨迹"

    def _probe_battery(self) -> Tuple[str, str]:
        power_root = Path("/sys/class/power_supply")
        if power_root.is_dir():
            for entry in power_root.iterdir():
                try:
                    kind = _read_text(str(entry / "type")) or ""
                except OSError:
                    continue
                if kind.lower() in ("battery", "ups"):
                    capacity = _read_text(str(entry / "capacity"))
                    if capacity and capacity.isdigit():
                        return "live", f"检测到电量计：{entry.name}"
                    return "unavailable", f"{entry.name} 是电池接口但无 capacity（无电量计，不能推算续航）"
        return "unavailable", "无电量计或 UPS 接口，普通供电接入不能推算剩余续航"

    def _probe_telemetry(self) -> Tuple[str, str]:
        missing = []
        if not HAS_PSUTIL:
            missing.append("psutil（CPU/内存/网卡计数）")
        if not self.temperature.source:
            missing.append("SoC 温度接口")
        if missing:
            return "live", "部分字段缺少采集接口：" + "、".join(missing)
        return "live", ""

    def _probe_gpu(self) -> Tuple[str, str]:
        """Pi 5 没有可用的 GPU 利用率接口，因此明确不可用而不是显示 0（PRD §5.7）。"""
        if Path("/sys/kernel/debug/dri").is_dir() and _read_text("/sys/kernel/debug/dri/0/gpu_busy_percent"):
            return "live", ""
        return "unavailable", "本机没有可读的 GPU 利用率接口，界面隐藏该字段而不显示假零值"

    # ---- 与平台的往返延迟 ----

    def record_platform_latency(self, milliseconds: Optional[float]) -> None:
        self.platform_latency_ms = None if milliseconds is None else round(milliseconds, 1)


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# 给界面用的格式化（统一单位，避免端与平台各写一套）
# --------------------------------------------------------------------------- #

def format_bytes(value: Optional[float]) -> str:
    """字节 → 人类可读。UI 层才做这个换算（PRD §7.3）。"""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    rate = float(value)
    if rate < 1024:
        return f"{rate:.0f} B/s"
    if rate < 1024 * 1024:
        return f"{rate / 1024:.1f} KB/s"
    return f"{rate / (1024 * 1024):.2f} MB/s"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def system_fingerprint() -> Dict[str, Any]:
    """设备指纹：设备状态页与握手都用它，方便平台区分是哪台机器。"""
    return {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpuCount": os.cpu_count(),
        "psutil": HAS_PSUTIL,
        "pid": os.getpid(),
    }
