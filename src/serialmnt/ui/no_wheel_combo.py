"""不响应鼠标滚轮的下拉选择框。"""

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox


class NoWheelComboBox(QComboBox):
    """忽略落在自身上的滚轮事件，避免滚动页面时误改选项。

    展开列表中的滚动仍由弹出视图处理；被忽略的事件会继续向父控件传播，
    因此侧栏和窗口滚动不受影响。
    """

    def wheelEvent(self, event: QWheelEvent) -> None:
        """丢弃滚轮事件，不执行默认的选项切换。"""
        event.ignore()
