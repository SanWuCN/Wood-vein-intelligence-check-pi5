"""木脉智检手持检测终端（树莓派 Pi 5 / 800×480 触摸屏）。

分层：

    woodpulse/
      config.py           启动配置（默认值 → JSON → 环境变量 → 命令行）
      contracts.py        消息与数据契约：信封、能力、来源标识、样例格式
      app_state.py        任务/批次/配置/更新状态机（不依赖 Qt）
      scenarios.py        三套固定样例包的登记表与剧本口径数值
      logging_setup.py    内存环形缓冲 + 轮转文件日志
      telemetry.py        真实系统遥测（无随机数）
      storage.py          SQLite、批次目录、outbox、崩溃恢复
      platform_client.py  HTTP + 手写 WebSocket + 心跳/重连/回执
      capture.py          采集编排：回放推进、标记、落盘、manifest 原子提交
      selfcheck.py        逐项自检（未接入项显示未接入，不全绿）
      update.py           演示模型包：下载、摘要校验、切换、回验
      adapters/
        camera.py         V4L2 / picamera2 实采，无相机自动降级
        replay.py         固定样例包回放（唯一的"回波"来源）
      ui/                 PyQt5 灰银色 800×480 界面
      app.py              总装（不依赖 Qt，可在无图形环境跑验收脚本）

设计口径见 PRD v0.2：没有真实毫米波回波、没有 IMU；连接、遥测、文件上传、
摘要验证与回执必须真实运行；界面上的每一段数据都要能回答"它从哪来"。
"""

from . import contracts  # noqa: F401

__all__ = ["contracts", "__version__"]

__version__ = "2.0.0-demo"
