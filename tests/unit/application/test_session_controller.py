"""测试单会话控制器状态、缓冲和资源生命周期。"""

from pathlib import Path
from threading import Condition, Event
from typing import cast

import pytest
from tests.fixtures.fakes import FakeClock, FakeTransport, YmodemReceiverTransport

from serialmnt.application import (
    MAX_SESSION_TITLE_LENGTH,
    SerialWorkerOptions,
    SessionController,
    SessionControllerOptions,
)
from serialmnt.application.ymodem import YmodemOptions
from serialmnt.domain import (
    ErrorCode,
    FileTransferError,
    FileTransferSnapshot,
    FileTransferState,
    InvalidStateTransitionError,
    PortBusyError,
    SerialConfig,
    SessionState,
    TransportIOError,
    ValidationError,
)


def _transfer_worker_options() -> SerialWorkerOptions:
    return SerialWorkerOptions(
        read_timeout=0.005,
        ymodem_options=YmodemOptions(
            handshake_timeout=1.0,
            header_timeout=0.5,
            data_timeout=0.2,
            eot_timeout=0.2,
            final_block_timeout=0.05,
            second_c_grace=0.005,
            final_c_grace=0.005,
            poll_timeout=0.005,
            max_retries=2,
            progress_interval=0,
        ),
    )


class SnapshotRecorder:
    """记录快照并支持条件等待。"""

    def __init__(self) -> None:
        self.condition = Condition()
        self.snapshots = []

    def __call__(self, snapshot: object) -> None:
        with self.condition:
            self.snapshots.append(snapshot)
            self.condition.notify_all()

    def wait_for(self, predicate: object, timeout: float = 2.0) -> bool:
        with self.condition:
            return self.condition.wait_for(
                lambda: any(predicate(item) for item in self.snapshots), timeout
            )


class RecordRecorder:
    """记录串口数据事件。"""

    def __init__(self) -> None:
        self.condition = Condition()
        self.records = []

    def __call__(self, record: object) -> None:
        with self.condition:
            self.records.append(record)
            self.condition.notify_all()

    def wait_for(self, predicate: object, timeout: float = 2.0) -> bool:
        with self.condition:
            return self.condition.wait_for(
                lambda: any(predicate(item) for item in self.records), timeout
            )


def _controller(
    transport: FakeTransport,
    *,
    config: SerialConfig | None = None,
    snapshots: SnapshotRecorder | None = None,
    records: RecordRecorder | None = None,
    worker_options: SerialWorkerOptions | None = None,
) -> SessionController:
    return SessionController(
        config=config or SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        clock=FakeClock(),
        options=SessionControllerOptions(
            session_id="session-1",
            title="测试会话",
            on_snapshot=snapshots,
            on_record=records,
            worker_options=worker_options,
        ),
    )


def test_controller_connects_sends_receives_and_disconnects() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    records = RecordRecorder()
    controller = _controller(transport, snapshots=snapshots, records=records)

    assert controller.state is SessionState.DISCONNECTED
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    controller.send(b"tx")
    assert transport.write_event.wait(1.0)
    assert snapshots.wait_for(lambda snapshot: snapshot.tx_bytes == 2)

    transport.push_read(b"rx")
    assert records.wait_for(lambda record: record.direction == "rx" and record.raw == b"rx")
    assert snapshots.wait_for(lambda snapshot: snapshot.rx_bytes == 2)

    assert controller.disconnect()
    assert controller.state is SessionState.DISCONNECTED
    assert transport.close_count == 1


def test_controller_reports_open_failure_without_losing_config() -> None:
    transport = FakeTransport(open_error=PortBusyError("端口已被占用。"))
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)

    controller.connect()

    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.ERROR)
    snapshot = controller.snapshot
    assert snapshot.last_error is not None
    assert snapshot.last_error.code is ErrorCode.PORT_BUSY
    assert snapshot.config.port == "COM1"


def test_controller_reports_fatal_io_error_as_disconnected_snapshot() -> None:
    transport = FakeTransport(read_error=TransportIOError("设备断开。"))
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    assert snapshots.wait_for(
        lambda snapshot: (
            snapshot.state is SessionState.DISCONNECTED and snapshot.last_error is not None
        )
    )
    assert controller.snapshot.last_error is not None


def test_controller_rejects_invalid_operations() -> None:
    empty_config = SerialConfig(port="")
    controller = _controller(FakeTransport(), config=empty_config)

    with pytest.raises(ValidationError):
        controller.connect()
    with pytest.raises(InvalidStateTransitionError):
        controller.send(b"data")
    with pytest.raises(ValidationError):
        controller.disconnect(timeout=0)


def test_controller_clear_buffer_keeps_counters() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    removed = controller.clear_buffer()

    assert removed == 1
    assert controller.records == ()
    assert controller.snapshot.rx_bytes == 0
    assert controller.disconnect()


def test_controller_requires_force_to_close_active_session() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    with pytest.raises(InvalidStateTransitionError):
        controller.close()
    controller.close(force=True)

    assert controller.state is SessionState.CLOSED
    assert transport.close_count == 1


def test_controller_set_line_state() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    controller.set_line_state(dtr=False, rts=True)

    assert transport.line_event.wait(1.0)
    assert transport.line_states[-1] == (False, True)
    assert controller.disconnect()


def test_controller_rejects_blank_session_id() -> None:
    with pytest.raises(ValidationError):
        SessionController(
            config=SerialConfig(port="COM1"),
            transport_factory=FakeTransport,
            clock=FakeClock(),
            options=SessionControllerOptions(session_id="   "),
        )


class BlockingOpenTransport(FakeTransport):
    """在 open 阶段阻塞，用于覆盖连接中的取消边界。"""

    def __init__(self) -> None:
        super().__init__()
        self.open_started = Event()
        self.release_open = Event()

    def open(self, config: SerialConfig) -> None:
        self.open_started.set()
        self.release_open.wait(1.0)
        super().open(config)


def test_controller_rejects_non_callable_transport_factory() -> None:
    with pytest.raises(ValidationError):
        SessionController(
            config=SerialConfig(port="COM1"),
            transport_factory=cast(object, None),  # type: ignore[arg-type]
            clock=FakeClock(),
        )


def test_controller_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        SessionController(
            config=SerialConfig(port=""),
            transport_factory=FakeTransport,
            clock=FakeClock(),
            options=SessionControllerOptions(title="   "),
        )


def test_controller_rejects_duplicate_connect_and_exposes_title() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(transport, snapshots=snapshots)
    assert controller.title == "测试会话"

    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)
    with pytest.raises(InvalidStateTransitionError):
        controller.connect()
    assert controller.disconnect()


def test_disconnect_is_idempotent_and_rejects_connecting_state() -> None:
    idle = _controller(FakeTransport())
    assert idle.disconnect()

    transport = BlockingOpenTransport()
    controller = _controller(transport)
    controller.connect()
    assert transport.open_started.wait(1.0)
    with pytest.raises(InvalidStateTransitionError):
        controller.disconnect()
    transport.release_open.set()
    assert transport.opened_event.wait(1.0)
    controller.close(force=True)


def test_disconnect_timeout_transitions_to_error_without_leaking_thread() -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = SessionController(
        config=SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        clock=FakeClock(),
        options=SessionControllerOptions(
            session_id="timeout-session",
            on_snapshot=snapshots,
            worker_options=SerialWorkerOptions(read_timeout=0.2),
        ),
    )
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

    assert not controller.disconnect(timeout=0.001)
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.ERROR)
    assert transport.closed_event.wait(1.0)


def test_controller_callback_branch_guards() -> None:
    controller = _controller(FakeTransport())
    config = SerialConfig(port="COM1")

    controller.on_opened(config)
    controller.on_open_failed(PortBusyError())
    controller.on_bytes_received(b"ignored")
    controller.on_io_error(TransportIOError("first"))
    controller.on_io_error(PortBusyError("second"))
    controller.on_stopped()

    assert controller.state is SessionState.DISCONNECTED
    assert controller.records[-1].text == "second"


def test_controller_force_close_without_worker_and_missing_worker_error() -> None:
    controller = _controller(FakeTransport())
    controller.close()
    assert controller.state is SessionState.CLOSED
    with pytest.raises(TransportIOError):
        controller._require_worker()


def test_controller_rename_trims_title_and_broadcasts_snapshot() -> None:
    snapshots = SnapshotRecorder()
    controller = _controller(FakeTransport(), snapshots=snapshots)

    controller.rename("  水泵  ")

    assert controller.title == "水泵"
    assert snapshots.wait_for(lambda snapshot: snapshot.title == "水泵")


def test_controller_rename_rejects_empty_and_too_long_titles() -> None:
    controller = _controller(FakeTransport())

    with pytest.raises(ValidationError, match="会话名称不能为空"):
        controller.rename("   ")
    with pytest.raises(ValidationError, match="不能超过"):
        controller.rename("x" * (MAX_SESSION_TITLE_LENGTH + 1))

    assert controller.title == "测试会话"


def test_controller_rename_to_same_title_does_not_broadcast() -> None:
    snapshots = SnapshotRecorder()
    controller = _controller(FakeTransport(), snapshots=snapshots)
    snapshots.snapshots.clear()

    controller.rename("测试会话")

    assert snapshots.snapshots == []


def test_controller_transfers_file_and_publishes_result(tmp_path: Path) -> None:
    source = b"YMODEM-DATA-" * 200
    path = tmp_path / "firmware.bin"
    path.write_bytes(source)
    transport = YmodemReceiverTransport()
    snapshots = SnapshotRecorder()
    records = RecordRecorder()
    controller = _controller(
        transport,
        snapshots=snapshots,
        records=records,
        worker_options=_transfer_worker_options(),
    )
    try:
        controller.connect()
        assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

        controller.start_file_send(path)

        assert controller.snapshot.transfer is not None
        assert controller.snapshot.transfer.state is FileTransferState.WAITING
        assert snapshots.wait_for(
            lambda snapshot: (
                snapshot.transfer is not None
                and snapshot.transfer.state is FileTransferState.SUCCEEDED
            )
        )
        transfer = controller.snapshot.transfer
        assert transfer is not None
        assert transfer.transferred_bytes == len(source)
        assert transfer.file_size == len(source)
        assert transport.received_data() == source
        assert records.wait_for(lambda record: "Ymodem 发送完成" in getattr(record, "text", ""))
        assert any("开始发送文件" in record.text for record in controller.records)

        controller.send(b"after")
        assert snapshots.wait_for(lambda snapshot: snapshot.tx_bytes == 5)
        assert transport.writes[-1] == b"after"
    finally:
        controller.close(force=True)


def test_controller_rejects_send_and_second_transfer_while_busy(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"x" * 100)
    transport = YmodemReceiverTransport(initial_c=False)
    snapshots = SnapshotRecorder()
    controller = _controller(
        transport,
        snapshots=snapshots,
        worker_options=_transfer_worker_options(),
    )
    try:
        controller.connect()
        assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

        controller.start_file_send(path)
        with pytest.raises(FileTransferError):
            controller.send(b"busy")
        with pytest.raises(FileTransferError):
            controller.start_file_send(path)
        with pytest.raises(FileTransferError):
            controller.set_line_state(dtr=False, rts=False)

        assert controller.cancel_file_send()
        assert snapshots.wait_for(
            lambda snapshot: (
                snapshot.transfer is not None
                and snapshot.transfer.state is FileTransferState.CANCELLED
            )
        )
        assert not controller.cancel_file_send()
    finally:
        controller.close(force=True)


def test_controller_rejects_transfer_with_incompatible_serial_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"x" * 10)
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(
        transport,
        config=SerialConfig(port="COM1", parity="E"),
        snapshots=snapshots,
        worker_options=_transfer_worker_options(),
    )
    try:
        controller.connect()
        assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

        with pytest.raises(FileTransferError, match="8 数据位"):
            controller.start_file_send(path)
        assert controller.snapshot.transfer is None
    finally:
        controller.close(force=True)


def test_controller_rejects_missing_file_without_transfer_state(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    snapshots = SnapshotRecorder()
    controller = _controller(
        transport,
        snapshots=snapshots,
        worker_options=_transfer_worker_options(),
    )
    try:
        controller.connect()
        assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)

        with pytest.raises(FileTransferError, match="不是普通文件"):
            controller.start_file_send(tmp_path / "missing.bin")
        assert controller.snapshot.transfer is None
    finally:
        controller.close(force=True)


def test_controller_disconnect_cancels_active_transfer(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"x" * 100)
    transport = YmodemReceiverTransport(initial_c=False)
    snapshots = SnapshotRecorder()
    controller = _controller(
        transport,
        snapshots=snapshots,
        worker_options=_transfer_worker_options(),
    )
    controller.connect()
    assert snapshots.wait_for(lambda snapshot: snapshot.state is SessionState.CONNECTED)
    controller.start_file_send(path)
    assert snapshots.wait_for(
        lambda snapshot: (
            snapshot.transfer is not None and snapshot.transfer.state is FileTransferState.WAITING
        )
    )

    assert controller.disconnect()
    assert controller.state is SessionState.DISCONNECTED
    transfer = controller.snapshot.transfer
    assert transfer is not None
    assert transfer.state is FileTransferState.CANCELLED


def test_controller_stopped_worker_cancels_stale_transfer() -> None:
    records = RecordRecorder()
    controller = _controller(FakeTransport(), records=records)
    controller.on_file_transfer(
        FileTransferSnapshot(
            state=FileTransferState.WAITING,
            file_name="firmware.bin",
            file_size=100,
            transferred_bytes=0,
            block_number=0,
            retries=0,
            elapsed_seconds=0.0,
            message="等待接收端发起传输。",
        )
    )

    controller.on_stopped()

    transfer = controller.snapshot.transfer
    assert transfer is not None
    assert transfer.state is FileTransferState.CANCELLED
    assert "串口已断开" in transfer.message
    assert any("Ymodem 发送已取消" in record.text for record in controller.records)
    assert records.wait_for(lambda record: "Ymodem 发送已取消" in getattr(record, "text", ""))


def test_controller_transfer_join_timeout_scales_with_baudrate() -> None:
    transfer = FileTransferSnapshot(
        state=FileTransferState.SENDING,
        file_name="firmware.bin",
        file_size=1024,
        transferred_bytes=512,
        block_number=1,
        retries=0,
        elapsed_seconds=1.0,
    )
    fast = _controller(FakeTransport())
    assert fast._transfer_join_timeout(1.5) == 1.5
    fast.on_file_transfer(transfer)
    # 115200 下帧写超时为下限 2 s，退出等待覆盖当前帧和取消序列。
    assert fast._transfer_join_timeout(1.5) == 4.5

    slow = _controller(FakeTransport(), config=SerialConfig(port="COM1", baudrate=9600))
    slow.on_file_transfer(transfer)
    # 9600 下计算值超过 5 s，按上限截断。
    assert slow._transfer_join_timeout(1.5) == 5.0

    slow.on_file_transfer(
        FileTransferSnapshot(
            state=FileTransferState.SUCCEEDED,
            file_name="firmware.bin",
            file_size=1024,
            transferred_bytes=1024,
            block_number=1,
            retries=0,
            elapsed_seconds=1.0,
        )
    )
    assert slow._transfer_join_timeout(1.5) == 1.5
