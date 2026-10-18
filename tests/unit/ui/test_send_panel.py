"""测试发送历史与周期发送控件。"""

from threading import Event

from PySide6.QtWidgets import QMessageBox
from tests.fixtures.fakes import FakeClock, FakeTransport

from serialmnt.application import SessionController, SessionControllerOptions
from serialmnt.domain import DEFAULT_LINE_ENDING, SerialConfig, SessionPreferences, SessionState
from serialmnt.ui import SendPanel


def _connected_controller(transport: FakeTransport) -> tuple[SessionController, Event]:
    connected = Event()

    def snapshot(value: object) -> None:
        if value.state is SessionState.CONNECTED:
            connected.set()

    controller = SessionController(
        config=SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        clock=FakeClock(),
        options=SessionControllerOptions(session_id="send-session", on_snapshot=snapshot),
    )
    controller.connect()
    assert connected.wait(1.0)
    return controller, connected


def test_send_panel_remembers_history_and_sends_periodically(qtbot: object) -> None:
    transport = FakeTransport()
    controller, _ = _connected_controller(transport)
    preferences = SessionPreferences(
        title="COM1",
        config=controller.config,
        display_mode="text",
        show_timestamp=True,
        show_rx=True,
        show_tx=True,
        autoscroll=True,
        send_history=("old",),
    )
    panel = SendPanel(controller=controller, preferences=preferences)
    qtbot.addWidget(panel)
    assert panel.line_ending == DEFAULT_LINE_ENDING

    panel.editor.setPlainText("first")
    panel.send()
    qtbot.waitUntil(lambda: transport.writes == [b"first\n"], timeout=2000)
    assert panel.history == ("old", "first")

    panel.editor.setPlainText("timer")
    panel.interval_spin.setValue(10)
    panel.periodic_button.setChecked(True)
    qtbot.waitUntil(lambda: b"timer\n" in transport.writes, timeout=2000)
    panel.periodic_button.setChecked(False)
    assert not panel.periodic_button.isChecked()

    controller.disconnect()


def test_send_panel_stops_periodic_send_on_disconnect(qtbot: object) -> None:
    transport = FakeTransport()
    controller, _ = _connected_controller(transport)
    panel = SendPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]

    panel.editor.setPlainText("timer")
    panel.interval_spin.setValue(10)
    panel.periodic_button.setChecked(True)
    assert panel._periodic_timer.isActive()

    panel.set_connected(False)

    assert not panel.periodic_button.isChecked()
    assert not panel._periodic_timer.isActive()
    controller.disconnect()


def test_send_panel_disables_sending_during_file_transfer(qtbot: object) -> None:
    transport = FakeTransport()
    controller, _ = _connected_controller(transport)
    panel = SendPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]

    panel.set_transfer_active(True)

    assert not panel.send_button.isEnabled()
    assert not panel.periodic_button.isEnabled()
    assert panel.send_content("blocked") is False
    assert transport.writes == []

    panel.set_transfer_active(False)
    assert panel.send_button.isEnabled()
    assert panel.send_content("allowed")
    qtbot.waitUntil(lambda: transport.writes == [b"allowed\n"], timeout=2000)
    controller.disconnect()


def test_send_panel_editor_height_follows_content(qtbot: object) -> None:
    """输入框随内容长高，超过上限后不再长高。"""
    transport = FakeTransport()
    controller, _ = _connected_controller(transport)
    panel = SendPanel(controller=controller)
    qtbot.addWidget(panel)

    single_line = panel.editor.height()
    assert single_line < 60  # 单行内容不再占住原来的 120px

    panel.editor.setPlainText("a\nb\nc")
    qtbot.waitUntil(lambda: panel.editor.height() > single_line, timeout=2000)
    three_lines = panel.editor.height()

    panel.editor.setPlainText("\n".join(str(index) for index in range(20)))
    qtbot.waitUntil(lambda: panel.editor.height() > three_lines, timeout=2000)
    capped = panel.editor.height()

    panel.editor.setPlainText("\n".join(str(index) for index in range(60)))
    qtbot.wait(100)
    assert panel.editor.height() == capped

    controller.disconnect()


def test_send_panel_send_content_uses_mode_and_newline(
    qtbot: object,
    monkeypatch: object,
) -> None:
    transport = FakeTransport()
    controller, _ = _connected_controller(transport)
    panel = SendPanel(controller=controller)
    qtbot.addWidget(panel)

    assert panel.line_ending == DEFAULT_LINE_ENDING
    assert panel.send_content("")
    qtbot.waitUntil(lambda: transport.writes == [b"\n"], timeout=2000)
    assert panel.history == ()

    assert panel.send_content("ping")
    qtbot.waitUntil(lambda: transport.writes[-1] == b"ping\n", timeout=2000)
    assert panel.history == ("ping",)

    panel.set_send_mode("hex")
    panel.set_line_ending("none")
    assert panel.send_content("AA 01")
    qtbot.waitUntil(lambda: transport.writes[-1] == b"\xaa\x01", timeout=2000)
    assert panel.history == ("ping", "AA 01")

    panel.set_line_ending("none")
    writes_before = len(transport.writes)
    assert panel.send_content("")
    assert len(transport.writes) == writes_before

    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[2]))
    assert not panel.send_content("GG")
    assert warnings
    assert panel.history == ("ping", "AA 01")

    controller.disconnect()
