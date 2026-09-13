"""终端启动配置（PRD §7.2）。

配置来源优先级（后者覆盖前者）：
    1. 代码内置默认值
    2. JSON 配置文件（默认 `<dataDir>/config.json`，可用 --config 指定）
    3. 环境变量 `WOODPULSE_*`
    4. 命令行参数

为什么不用 QSettings：配置文件要能被人直接读、被平台下发的通道参数覆盖、
在演示现场用文本编辑器改一行就生效，INI/注册表都不如 JSON 直观。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "木脉智检手持终端"
APP_SLUG = "woodpulse"


def _default_data_dir() -> Path:
    """数据目录。树莓派上装到 /opt 或 ~ 都可能，所以给一个能自动落地的默认值。"""
    env = os.environ.get("WOODPULSE_DATA_DIR")
    if env:
        return Path(env).expanduser()
    for candidate in (Path.home() / "WoodPulse_Data", Path("/var/lib/woodpulse")):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path.cwd() / "WoodPulse_Data"


@dataclass
class CameraConfig:
    """相机适配器配置（PRD §2：保留实际采集，增加设备选择、断连重试、帧率统计）。"""

    #: v4l2 = OpenCV/V4L2 实采；picamera2 = Pi5 官方栈；none = 不采集（能力声明 unavailable）
    backend: str = "v4l2"
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    #: 采集目标帧率。相机给不出这个帧率就按实际来，界面显示实际值。
    fps: int = 15
    #: 断连后重试间隔（秒），指数退避到 retry_max_interval_s
    retry_interval_s: float = 2.0
    retry_max_interval_s: float = 20.0
    #: 预览缩放目标宽度（16:9）
    preview_width: int = 640
    #: 预览 JPEG 质量
    preview_jpeg_quality: int = 70
    #: 显示帧率上限。UI 丢帧是允许的，原始采集队列溢出必须记录（PRD §10）
    display_fps: int = 15


@dataclass
class PlatformConfig:
    """平台通信配置（PRD §7.2 设备配置包含 platformUrl/deviceId/deviceToken/dataDir）。"""

    platform_url: str = "http://127.0.0.1:8080"
    device_id: str = "handheld-02"
    device_token: str = "demo-token"
    demo_session_id: str = "demo-01"
    #: 心跳间隔（PRD §8.2 建议 5 秒）
    heartbeat_interval_s: float = 5.0
    #: 超过这个时间没收到平台任何消息就标延迟，再超就离线（PRD §8.2 约 15 秒）
    offline_after_s: float = 15.0
    #: 重连退避起点与上限（PRD §8.2：1、2、4 秒逐步增加至上限并加抖动）
    reconnect_base_s: float = 1.0
    reconnect_max_s: float = 20.0
    request_timeout_s: float = 10.0
    #: 遥测上报间隔（PRD §6：CPU 1 秒）
    telemetry_interval_s: float = 1.0
    #: 平台不可达时是否允许离线采集（PRD §5.1：没有平台仍可使用已缓存任务采集）
    allow_offline_capture: bool = True
    #: 是否上传低帧率预览图（PRD §4.3、§7.4）
    preview_upload: bool = True
    preview_upload_fps: float = 1.5


@dataclass
class UiConfig:
    """界面配置。800×480 触摸屏按约 5 英寸设计（PRD §4.2）。"""

    width: int = 800
    height: int = 480
    #: 主要触摸按钮高度 64px，次级至少 48px（PRD §4.2）
    primary_button_height: int = 64
    secondary_button_height: int = 48
    #: false = 窗口模式（开发机）；true = 全屏（实机）
    fullscreen: bool = True
    #: 主区域默认呈现：sequence = 响应图优先，camera = 相机优先
    main_view: str = "sequence"


@dataclass
class AppConfig:
    """终端全部配置。字段名与配置文件 JSON 键一一对应（小写下划线）。"""

    device_id: str = ""                     # 留空则用 platform.device_id
    operator_id: str = "rao"                # actorId：操作人，与 deviceId 分开（PRD §7.1）
    data_dir: str = ""
    scenario_root: str = ""                 # 样例包根目录，留空则用 <项目根>/samples
    config_path: str = ""
    log_level: str = "INFO"
    log_dir: str = ""
    log_max_lines: int = 2000               # 界面日志环形缓冲行数（PRD §10）
    log_file_max_bytes: int = 2 * 1024 * 1024
    log_file_backups: int = 3
    #: 启动时是否跑完整自检
    self_check_on_start: bool = True
    #: 演示用：接收平台 pause_capture 后是否真的停样例回放（必须为 True，PRD §8.1）
    obey_pause: bool = True
    #: 无相机时是否允许启动（false 会直接退出，现场不推荐）
    allow_no_camera: bool = True
    #: 是否把预览图上传平台
    upload_preview: bool = True

    camera: CameraConfig = field(default_factory=CameraConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    # ---- 派生值 ----

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir or _default_data_dir()).expanduser()

    @property
    def batches_path(self) -> Path:
        return self.data_path / "batches"

    @property
    def outbox_path(self) -> Path:
        return self.data_path / "outbox"

    @property
    def staging_path(self) -> Path:
        return self.data_path / "staging"

    @property
    def db_path(self) -> Path:
        return self.data_path / "woodpulse.db"

    @property
    def logs_path(self) -> Path:
        return Path(self.log_dir).expanduser() if self.log_dir else self.data_path / "logs"

    @property
    def effective_device_id(self) -> str:
        return self.device_id or self.platform.device_id

    # ---- 序列化 ----

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.batches_path, self.outbox_path, self.staging_path, self.logs_path):
            path.mkdir(parents=True, exist_ok=True)

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path) if path else self.data_path / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #

_ENV_MAP = {
    "WOODPULSE_PLATFORM_URL": ("platform", "platform_url"),
    "WOODPULSE_DEVICE_ID": ("platform", "device_id"),
    "WOODPULSE_DEVICE_TOKEN": ("platform", "device_token"),
    "WOODPULSE_SESSION_ID": ("platform", "demo_session_id"),
    "WOODPULSE_DATA_DIR": ("", "data_dir"),
    "WOODPULSE_SCENARIO_ROOT": ("", "scenario_root"),
    "WOODPULSE_OPERATOR_ID": ("", "operator_id"),
    "WOODPULSE_LOG_LEVEL": ("", "log_level"),
    "WOODPULSE_CAMERA_DEVICE": ("camera", "device"),
    "WOODPULSE_CAMERA_BACKEND": ("camera", "backend"),
    "WOODPULSE_FULLSCREEN": ("ui", "fullscreen"),
    "WOODPULSE_CONFIG": ("", "config_path"),
}


def _coerce(current: Any, raw: str) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    return raw


def _apply_mapping(cfg: AppConfig, data: Dict[str, Any]) -> None:
    """把 dict 写进 dataclass；未知键直接报错，避免拼错字段名后静默失效。"""
    top_level = {f.name for f in fields(AppConfig)}
    for key, value in data.items():
        if key in ("camera", "platform", "ui"):
            sub = getattr(cfg, key)
            valid = {f.name for f in fields(sub)}
            for sub_key, sub_value in (value or {}).items():
                if sub_key not in valid:
                    raise ValueError(f"配置项 {key}.{sub_key} 不存在")
                setattr(sub, sub_key, sub_value)
        elif key in top_level:
            setattr(cfg, key, value)
        else:
            raise ValueError(f"配置项 {key} 不存在")


def _extract_config_path(argv: Optional[list]) -> Optional[str]:
    """从命令行里先挑出 --config / -c。

    必须先单独扫一遍：配置文件要最先读，而 `--config` 本身也只能从命令行来。
    如果把 `--config` 混在"命令行覆盖"那一步处理，就会变成"先按默认路径找文件、
    再发现用户指定了别的路径"，示例配置看似加载成功、实际全用的默认值。
    """
    for index, token in enumerate(argv or []):
        if token in ("--config", "-c") and index + 1 < len(argv):
            return str(argv[index + 1])
        if token.startswith("--config="):
            return token.split("=", 1)[1]
    return None


def load_config(config_path: Optional[str] = None, argv: Optional[list] = None) -> AppConfig:
    """按"默认值 → 文件 → 环境变量 → 命令行"的顺序合成配置。"""
    cfg = AppConfig()
    cfg.data_dir = str(_default_data_dir())

    explicit = config_path or _extract_config_path(argv) or os.environ.get("WOODPULSE_CONFIG")
    candidate = Path(explicit).expanduser() if explicit else cfg.data_path / "config.json"
    if candidate.is_file():
        try:
            _apply_mapping(cfg, json.loads(candidate.read_text(encoding="utf-8")))
            cfg.config_path = str(candidate)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # 配置文件坏了不能让终端起不来：现场还要能采集，所以退回默认值并记下来
            cfg.config_path = f"{candidate}（读取失败：{exc}，已使用默认值）"
    elif explicit:
        # 用户明确指了路径却不存在：要让人知道，不能静默用默认值
        cfg.config_path = f"{candidate}（文件不存在，已使用默认值）"

    for env_key, (section, attr) in _ENV_MAP.items():
        raw = os.environ.get(env_key)
        if raw is None:
            continue
        if section:
            sub = getattr(cfg, section)
            setattr(sub, attr, _coerce(getattr(sub, attr), raw))
        else:
            setattr(cfg, attr, _coerce(getattr(cfg, attr), raw))

    if argv:
        _apply_argv(cfg, argv)

    if not cfg.device_id:
        cfg.device_id = cfg.platform.device_id
    return cfg


def _apply_argv(cfg: AppConfig, argv: list) -> None:
    """只认少量现场常用参数，不做通用 argparse 透传。"""
    index = 0
    while index < len(argv):
        token = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else None
        if token in ("--config", "-c") and value:
            index += 2  # 已经在 _extract_config_path 里读过了，这里只跳过
            continue
        if token.startswith("--config="):
            index += 1
            continue
        if token == "--platform-url" and value:
            cfg.platform.platform_url = value
            index += 2
            continue
        if token == "--device-id" and value:
            cfg.device_id = value
            cfg.platform.device_id = value
            index += 2
            continue
        if token == "--device-token" and value:
            cfg.platform.device_token = value
            index += 2
            continue
        if token == "--data-dir" and value:
            cfg.data_dir = value
            index += 2
            continue
        if token == "--scenario-root" and value:
            cfg.scenario_root = value
            index += 2
            continue
        if token == "--scenario" and value:
            cfg.__dict__["_scenario_override"] = value
            index += 2
            continue
        if token == "--no-fullscreen":
            cfg.ui.fullscreen = False
            index += 1
            continue
        if token == "--fullscreen":
            cfg.ui.fullscreen = True
            index += 1
            continue
        if token == "--camera" and value:
            cfg.camera.backend = value
            index += 2
            continue
        if token == "--log-level" and value:
            cfg.log_level = value
            index += 2
            continue
        if token in ("--help", "-h"):
            cfg.__dict__["_want_help"] = True
            index += 1
            continue
        index += 1


HELP_TEXT = f"""{APP_NAME}（{APP_SLUG}）v2.0.0-demo

用法：
  python -m woodpulse [选项]

选项：
  -c, --config PATH        指定 JSON 配置文件
      --platform-url URL   平台地址，如 http://192.168.1.10:8000
      --device-id ID       设备号（默认 handheld-02）
      --device-token TOKEN 设备令牌
      --data-dir PATH      数据目录（批次、数据库、日志）
      --scenario-root PATH 检测样例包根目录
      --scenario ID        强制使用某个 scenarioId（演示用）
      --camera BACKEND     v4l2 / picamera2 / none
      --fullscreen         全屏（实机默认）
      --no-fullscreen       窗口模式（开发机）
      --log-level LEVEL    DEBUG/INFO/WARNING/ERROR
  -h, --help               显示本帮助

环境变量：{', '.join(sorted(_ENV_MAP))}
配置样例见 docs/部署说明.md。
"""
