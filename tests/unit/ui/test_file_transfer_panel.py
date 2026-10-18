"""测试 Ymodem 文件发送面板的按钮与进度状态。"""

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QMessageBox
from tests.fixtures.fakes import FakeClock, FakeTransport

from serialmnt.application import SessionController, SessionControllerOptions
from serialmnt.domain import (
    FileTransferError,
    FileTransferSnapshot,
    FileTransferState,
    SerialConfig,
    SessionState,
)
from serialmnt.ui import FileTransferPanel


def _controller() -> SessionController:
    return SessionController(
        config=SerialConfig(port="COM1"),
        transport_factory=FakeTransport,
        clock=FakeClock(),
        options=SessionControllerOptions(session_id="file-panel"),
    )


def _transfer(  # noqa: PLR0913 - 测试辅助函数集中设置快照字段
    state: FileTransferState,
    *,
    sent: int = 0,
    size: int = 1024,
    block: int = 0,
    retries: int = 0,
    message: str = "",
) -> FileTransferSnapshot:
    return FileTransferSnapshot(
        state=state,
        file_name="firmware.bin",
        file_size=size,
        transferred_bytes=sent,
        block_number=block,
        retries=retries,
        elapsed_seconds=1.0,
        message=message,
    )


def test_panel_requires_connection_and_selected_file(qtbot: object, tmp_path: Path) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    try:
        path = tmp_path / "firmware.bin"
        path.write_bytes(b"x" * 100)
        panel.set_file_path(path)
        assert not panel.start_button.isEnabled()

        panel.apply_snapshot(replace(controller.snapshot, state=SessionState.CONNECTED))

        assert panel.start_button.isEnabled()
        assert panel.cancel_button.isEnabled() is False
        assert panel.block_size == 1024
        panel.block_check.setChecked(False)
        assert panel.block_size == 128
    finally:
        controller.close(force=True)


def test_panel_renders_progress_and_terminal_states(qtbot: object, tmp_path: Path) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    try:
        path = tmp_path / "firmware.bin"
        path.write_bytes(b"x" * 1024)
        panel.set_file_path(path)
        connected = replace(controller.snapshot, state=SessionState.CONNECTED)

        panel.apply_snapshot(replace(connected, transfer=_transfer(FileTransferState.WAITING)))
        assert not panel.start_button.isEnabled()
        assert panel.cancel_button.isEnabled()
        assert panel.progress_bar.value() == 0
        assert "等待接收端" in panel.phase_label.text()

        panel.apply_snapshot(
            replace(
                connected,
                transfer=_transfer(FileTransferState.SENDING, sent=512, block=1, retries=2),
            )
        )
        assert panel.progress_bar.value() == 50
        assert panel.percent_label.text() == "50%"
        assert "重试 2" in panel.metrics_label.toolTip()

        panel.apply_snapshot(
            replace(
                connected,
                transfer=_transfer(FileTransferState.SUCCEEDED, sent=1024, block=1),
            )
        )
        assert panel.progress_bar.value() == 100
        assert panel.phase_label.text() == "发送完成"
        assert panel.start_button.isEnabled()
        assert not panel.cancel_button.isEnabled()
    finally:
        controller.close(force=True)


def test_panel_clears_previous_result_when_new_file_selected(qtbot: object, tmp_path: Path) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    try:
        first = tmp_path / "first.bin"
        first.write_bytes(b"x" * 10)
        second = tmp_path / "second.bin"
        second.write_bytes(b"y" * 20)
        panel.set_file_path(first)
        controller.on_file_transfer(_transfer(FileTransferState.SUCCEEDED, sent=10, size=10))
        panel.apply_snapshot(replace(controller.snapshot, state=SessionState.CONNECTED))
        assert panel.phase_label.text() == "发送完成"

        panel.set_file_path(second)

        assert panel.phase_label.text() == "就绪"
        assert panel.progress_bar.value() == 0
        assert "20 B" in panel.metrics_label.toolTip()
    finally:
        controller.close(force=True)


def test_panel_start_passes_selected_block_size(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    calls: list[tuple[Path, int]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        controller,
        "start_file_send",
        lambda path, *, block_size: calls.append((path, block_size)),
    )
    try:
        path = tmp_path / "firmware.bin"
        path.write_bytes(b"x" * 10)
        panel.set_file_path(path)
        panel.apply_snapshot(replace(controller.snapshot, state=SessionState.CONNECTED))
        panel.block_check.setChecked(False)

        panel.start_button.click()

        assert calls == [(path, 128)]
        assert not panel.start_button.isEnabled()
    finally:
        controller.close(force=True)


def test_panel_cancel_button_requests_cancel_once(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    calls: list[bool] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        controller, "cancel_file_send", lambda: calls.append(True) or True
    )
    try:
        path = tmp_path / "firmware.bin"
        path.write_bytes(b"x" * 10)
        panel.set_file_path(path)
        panel.apply_snapshot(
            replace(
                controller.snapshot,
                state=SessionState.CONNECTED,
                transfer=_transfer(FileTransferState.SENDING, sent=512),
            )
        )

        panel.cancel_button.click()

        assert calls == [True]
        assert panel.cancel_button.text() == "正在取消…"
        assert not panel.cancel_button.isEnabled()
    finally:
        controller.close(force=True)


def test_panel_shows_failure_and_start_error(
    qtbot: object, tmp_path: Path, monkeypatch: object
) -> None:
    controller = _controller()
    panel = FileTransferPanel(controller=controller)
    qtbot.addWidget(panel)  # type: ignore[attr-defined]
    warnings: list[str] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text)
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        controller,
        "start_file_send",
        lambda path, *, block_size: (_ for _ in ()).throw(FileTransferError("无法读取文件。")),
    )
    try:
        path = tmp_path / "firmware.bin"
        path.write_bytes(b"x" * 10)
        panel.set_file_path(path)
        connected = replace(controller.snapshot, state=SessionState.CONNECTED)

        panel.apply_snapshot(
            replace(
                connected,
                transfer=_transfer(
                    FileTransferState.FAILED,
                    sent=256,
                    message="第 1 块重试超过上限。",
                ),
            )
        )
        assert panel.phase_label.text() == "发送失败"
        assert panel.metrics_label.toolTip() == "第 1 块重试超过上限。"
        assert panel.progress_bar.value() == 25

        panel.apply_snapshot(connected)
        panel.start_button.click()

        assert warnings == ["无法读取文件。"]
    finally:
        controller.close(force=True)
