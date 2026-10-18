"""测试日志面板长路径状态文本的省略显示。"""

from PySide6.QtWidgets import QGridLayout
from tests.fixtures.fakes import FakeClock, FakeTransport

from serialmnt.application import SessionController, SessionControllerOptions
from serialmnt.domain import SerialConfig
from serialmnt.ui import LogPanel

_LONG_STATUS = (
    "日志：C:\\Users\\tester\\Documents\\SerialMNT\\Logs\\SerialMNT_COM1_20261002_101010.csv"
)


def _controller() -> SessionController:
    transport = FakeTransport()
    return SessionController(
        config=SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        clock=FakeClock(),
        options=SessionControllerOptions(session_id="log-panel"),
    )


def test_log_panel_elides_long_status_text(qtbot: object) -> None:
    controller = _controller()
    panel = LogPanel(controller=controller)
    qtbot.addWidget(panel)

    try:
        panel.show()
        panel.resize(220, 160)
        panel._set_status(_LONG_STATUS)

        qtbot.waitUntil(lambda: "…" in panel.status_label.text(), timeout=2000)

        assert panel.status_label.text() != _LONG_STATUS
        assert panel.status_label.toolTip() == _LONG_STATUS
    finally:
        controller.close(force=True)


def test_log_panel_hides_empty_status_row(qtbot: object) -> None:
    controller = _controller()
    panel = LogPanel(controller=controller)
    qtbot.addWidget(panel)

    try:
        panel.apply_snapshot(controller.snapshot)
        assert panel.status_label.isHidden()

        panel._set_status(_LONG_STATUS)
        assert not panel.status_label.isHidden()

        panel._set_status("")
        assert panel.status_label.isHidden()
    finally:
        controller.close(force=True)


def test_log_panel_directory_row_layout(qtbot: object) -> None:
    controller = _controller()
    panel = LogPanel(controller=controller)
    qtbot.addWidget(panel)

    try:
        panel.show()
        panel.resize(272, 200)
        qtbot.wait(50)

        # 面板必须能塞进侧栏最小宽度，否则路径和按钮会被右侧裁掉。
        assert panel.minimumSizeHint().width() <= 260
        # 格式下拉框紧跟勾选框；目录标签从内容左边缘开始并紧贴输入框，不留横向空白。
        assert panel.format_combo.geometry().left() - panel.enabled_check.geometry().right() <= 8
        grid = panel.layout()
        assert isinstance(grid, QGridLayout)
        directory_row = grid.itemAtPosition(1, 0).layout()
        assert directory_row is not None
        directory_label = directory_row.itemAt(0).widget()
        assert directory_label.geometry().left() == panel.enabled_check.geometry().left()
        assert directory_label.geometry().right() < panel.directory_edit.geometry().left()
        assert panel.directory_edit.geometry().left() - directory_label.geometry().right() <= 8
        # 目录行右侧的“…”按钮贴住内容右边缘；导出比打开目录宽。
        content_right = panel.export_button.geometry().right()
        assert panel.browse_button.geometry().right() == content_right
        check_center = panel.enabled_check.geometry().center().y()
        assert check_center == panel.format_combo.geometry().center().y()  # type: ignore[attr-defined]
        assert panel.export_button.width() > panel.open_button.width()
    finally:
        controller.close(force=True)
