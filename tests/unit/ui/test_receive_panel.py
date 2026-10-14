"""测试接收面板标题行控件的挂载顺序。"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

from serialmnt.ui import ReceivePanel


def test_receive_panel_header_widgets_never_show_as_top_level_windows(qtbot: object) -> None:
    """回归：构造标题行时不得把无父控件当成顶层窗口显示。

    Qt 里对没有父对象的控件调用 ``setVisible(True)`` 等同于 ``show()`` 一个顶层窗口。
    标题行控件若在挂到面板之前就被设置可见性，会在启动和新建会话时闪出一个小窗口，
    因此它们必须在创建时就带上父对象。
    """
    flashed: list[QWidget] = []

    class _ShowWatcher(QObject):
        """记录构造期间以顶层窗口身份被显示的控件。"""

        def eventFilter(self, watched: QObject, event: QEvent) -> bool:
            if (
                event.type() is QEvent.Type.Show
                and isinstance(watched, QWidget)
                and watched.parentWidget() is None
            ):
                flashed.append(watched)
            return False

    watcher = _ShowWatcher()
    app = QApplication.instance()
    assert app is not None
    app.installEventFilter(watcher)
    try:
        panel = ReceivePanel(records_provider=lambda: (), clear_callback=lambda: 0)
    finally:
        app.removeEventFilter(watcher)
    qtbot.addWidget(panel)

    assert not flashed, "这些控件被当成顶层窗口显示过：" + "、".join(
        type(widget).__name__ for widget in flashed
    )
