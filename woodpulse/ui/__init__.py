"""PyQt 界面层（灰银色工业风，800×480 触摸屏）。

  qt.py             PyQt5 / PySide6 兼容层（界面代码只从这里取 Qt 名字）
  theme.py          PRD §4.1 的颜色表与 §4.2 的尺寸表
  widgets.py        自绘控件：状态条、相机预览、当前曲线、响应序列图、标记列表
  workbench_page.py 任务工作台（PRD §5.1）
  scan_page.py      检测作业首屏（PRD §4.3、§3.2、§3.3）
  environment_page.py 环境配置与自检（PRD §5.2）
  delivery_page.py  数据交付与结果（PRD §5.5）
  update_page.py    更新管理（PRD §5.6）
  status_page.py    设备状态（PRD §5.7、§6）
  state_view.py     界面读取用的状态视图（不依赖 Qt，可单测）
  shell.py          主窗口与定时器
  main.py           入口
"""

from .qt import HAVE_QT, binding_info  # noqa: F401

__all__ = ["HAVE_QT", "binding_info", "run_gui", "headless_report"]


def run_gui(*args, **kwargs):
    from .main import run_gui as _run_gui

    return _run_gui(*args, **kwargs)


def headless_report(*args, **kwargs):
    from .main import headless_report as _headless

    return _headless(*args, **kwargs)
