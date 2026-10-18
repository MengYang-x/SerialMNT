"""测试接收面板标题行控件的挂载顺序和文本查找。"""

from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget

from serialmnt.domain import LogRecord
from serialmnt.ui import ReceivePanel


def _records() -> tuple[LogRecord, ...]:
    timestamp = datetime(2026, 10, 18, tzinfo=timezone.utc)
    return (
        LogRecord.from_bytes(
            timestamp_utc=timestamp,
            direction="rx",
            raw=b"boot complete",
            encoding="utf-8",
            session_id="search-session",
        ),
        LogRecord.from_bytes(
            timestamp_utc=timestamp,
            direction="rx",
            raw=b"ERROR: sensor timeout",
            encoding="utf-8",
            session_id="search-session",
        ),
    )


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


def _open_search(panel: ReceivePanel) -> None:
    """按 Ctrl+F 的绑定触发查找栏，避免依赖离屏环境的窗口焦点。"""
    assert panel.find_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Find)
    assert panel.find_shortcut.context() == Qt.ShortcutContext.WidgetWithChildrenShortcut
    panel.find_shortcut.activated.emit()


def test_ctrl_f_searches_displayed_text_and_escape_closes(qtbot: object) -> None:
    panel = ReceivePanel(records_provider=_records, clear_callback=lambda: 0)
    qtbot.addWidget(panel)
    panel.show()

    _open_search(panel)

    assert panel.find_bar.isVisible()
    qtbot.keyClicks(panel.find_bar.query_edit, "error")
    assert panel.output.textCursor().selectedText() == "ERROR"
    assert panel.find_bar.status_label.text() == "1/1"

    qtbot.keyClick(panel.find_bar.query_edit, Qt.Key.Key_Escape)
    assert not panel.find_bar.isVisible()
    assert not panel.output.textCursor().hasSelection()


def test_ctrl_f_searches_terminal_view(qtbot: object) -> None:
    panel = ReceivePanel(records_provider=_records, clear_callback=lambda: 0)
    qtbot.addWidget(panel)
    panel.show()
    panel.set_terminal_mode(True)

    _open_search(panel)

    assert panel.find_bar.isVisible()
    qtbot.keyClicks(panel.find_bar.query_edit, "timeout")
    assert panel.output.textCursor().selectedText() == "timeout"
