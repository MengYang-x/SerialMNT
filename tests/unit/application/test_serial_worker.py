"""测试串口 Worker 的命令队列和异常路径。"""

from pathlib import Path
from typing import cast

import pytest
from tests.fixtures.fakes import (
    FakeTransport,
    RecordingWorkerHandler,
    YmodemReceiverTransport,
)

from serialmnt.application import SerialWorker, SerialWorkerOptions
from serialmnt.application.ymodem import YmodemOptions
from serialmnt.domain import (
    FileTransferError,
    FileTransferState,
    InternalError,
    PortBusyError,
    SendQueueFullError,
    SerialConfig,
    TransportIOError,
    ValidationError,
)


def _ymodem_options(**overrides: object) -> YmodemOptions:
    values: dict[str, object] = {
        "handshake_timeout": 1.0,
        "header_timeout": 0.5,
        "data_timeout": 0.2,
        "eot_timeout": 0.2,
        "final_block_timeout": 0.05,
        "second_c_grace": 0.005,
        "final_c_grace": 0.005,
        "poll_timeout": 0.005,
        "max_retries": 2,
        "progress_interval": 0,
    }
    values.update(overrides)
    return YmodemOptions(**values)  # type: ignore[arg-type]


def _worker(
    transport: FakeTransport,
    *,
    options: SerialWorkerOptions | None = None,
) -> tuple[SerialWorker, RecordingWorkerHandler]:
    handler = RecordingWorkerHandler()
    worker = SerialWorker(
        config=SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        event_handler=handler,
        options=options or SerialWorkerOptions(read_timeout=0.005),
    )
    return worker, handler


def test_worker_reads_writes_line_state_and_stops() -> None:
    transport = FakeTransport()
    worker, handler = _worker(transport)
    worker.start()

    assert handler.opened_event.wait(1.0)
    assert worker.is_alive

    worker.send(b"tx")
    assert handler.sent_event.wait(1.0)
    assert transport.writes == [b"tx"]

    transport.push_read(b"rx")
    assert handler.received_event.wait(1.0)
    assert handler.received == [b"rx"]

    worker.set_line_state(dtr=False, rts=True)
    assert transport.line_event.wait(1.0)
    assert transport.line_states == [(False, True)]

    worker.request_stop()
    assert handler.stopped_event.wait(1.0)
    assert worker.join(1.0)
    assert not transport.is_open


def test_worker_reports_open_failure() -> None:
    transport = FakeTransport(open_error=PortBusyError())
    worker, handler = _worker(transport)

    worker.start()

    assert handler.open_failed_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert isinstance(handler.open_error, PortBusyError)
    assert not handler.opened_event.is_set()


def test_worker_maps_transport_factory_exception() -> None:
    handler = RecordingWorkerHandler()

    def failing_factory() -> FakeTransport:
        raise RuntimeError("factory failed")

    worker = SerialWorker(
        config=SerialConfig(port="COM1"),
        transport_factory=failing_factory,
        event_handler=handler,
    )
    worker.start()

    assert handler.open_failed_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert isinstance(handler.open_error, InternalError)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TransportIOError("read failed"), TransportIOError),
        (PortBusyError("write failed"), PortBusyError),
    ],
)
def test_worker_reports_io_errors_and_stops(
    error: Exception,
    expected: type[Exception],
) -> None:
    transport = FakeTransport()
    if isinstance(error, TransportIOError):
        transport.read_error = error
    else:
        transport.write_error = error
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    if isinstance(error, PortBusyError):
        worker.send(b"tx")

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert isinstance(handler.io_errors[0], expected)
    assert worker.join(1.0)


def test_worker_reports_line_state_error() -> None:
    transport = FakeTransport(line_error=TransportIOError("line failed"))
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    worker.set_line_state(dtr=False, rts=False)

    assert handler.io_error_event.wait(1.0)
    assert isinstance(handler.io_errors[0], TransportIOError)


def test_worker_send_queue_is_bounded() -> None:
    worker, _ = _worker(
        FakeTransport(),
        options=SerialWorkerOptions(read_timeout=0.05, write_queue_size=1),
    )
    worker.send(b"first")

    with pytest.raises(SendQueueFullError):
        worker.send(b"second")


def test_worker_validates_input_and_can_start_only_once() -> None:
    worker, _ = _worker(FakeTransport())
    with pytest.raises(ValidationError):
        worker.send(cast(bytes, "bad"))
    with pytest.raises(ValidationError):
        worker.send(b"")
    with pytest.raises(ValidationError):
        worker.set_line_state(dtr=cast(bool, 1), rts=True)

    worker.start()
    with pytest.raises(ValidationError):
        worker.start()
    worker.request_stop()
    assert worker.join(1.0)


@pytest.mark.parametrize(
    "options",
    [
        {"read_timeout": 0},
        {"read_chunk_size": 0},
        {"write_queue_size": 0},
        {"close_timeout": 0},
    ],
)
def test_worker_options_reject_invalid_values(options: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SerialWorkerOptions(**options)  # type: ignore[arg-type]


def test_worker_rejects_non_callable_transport_factory() -> None:
    with pytest.raises(ValidationError):
        SerialWorker(
            config=SerialConfig(port="COM1"),
            transport_factory=cast(object, None),
            event_handler=RecordingWorkerHandler(),
        )


def test_worker_join_before_start_is_successful() -> None:
    worker, _ = _worker(FakeTransport())
    assert worker.join(0.01)


def test_worker_detects_transport_closed_after_read() -> None:
    transport = FakeTransport(close_on_read=True)
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    transport.push_read(b"last")

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert isinstance(handler.io_errors[0], TransportIOError)
    assert worker.join(1.0)


@pytest.mark.parametrize(
    "error",
    [TransportIOError("line failed"), RuntimeError("line failed")],
)
def test_worker_reports_all_line_state_failures(error: BaseException) -> None:
    transport = FakeTransport(line_error=error)
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    worker.set_line_state(dtr=False, rts=False)

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert worker.join(1.0)


@pytest.mark.parametrize(
    "error",
    [TransportIOError("write failed"), RuntimeError("write failed")],
)
def test_worker_reports_all_unexpected_write_failures(error: BaseException) -> None:
    transport = FakeTransport(write_error=error)
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    worker.send(b"data")

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert worker.join(1.0)


@pytest.mark.parametrize(
    "error",
    [TransportIOError("read failed"), RuntimeError("read failed")],
)
def test_worker_reports_all_unexpected_read_failures(error: BaseException) -> None:
    transport = FakeTransport(read_error=error)
    worker, handler = _worker(transport)
    worker.start()

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert worker.join(1.0)


@pytest.mark.parametrize(
    "error",
    [TransportIOError("close failed"), RuntimeError("close failed")],
)
def test_worker_reports_close_failures(error: BaseException) -> None:
    transport = FakeTransport(close_error=error)
    worker, handler = _worker(transport)
    worker.start()
    assert handler.opened_event.wait(1.0)

    worker.request_stop()

    assert handler.io_error_event.wait(1.0)
    assert handler.stopped_event.wait(1.0)
    assert worker.join(1.0)


def _transfer_worker(
    transport: FakeTransport,
    *,
    ymodem_options: YmodemOptions | None = None,
) -> tuple[SerialWorker, RecordingWorkerHandler]:
    handler = RecordingWorkerHandler()
    worker = SerialWorker(
        config=SerialConfig(port="COM1"),
        transport_factory=lambda: transport,
        event_handler=handler,
        options=SerialWorkerOptions(
            read_timeout=0.005,
            ymodem_options=ymodem_options or _ymodem_options(),
        ),
    )
    return worker, handler


def test_worker_sends_file_and_resumes_normal_send(tmp_path: Path) -> None:
    source = b"ABCDEF" * 500
    path = tmp_path / "firmware.bin"
    path.write_bytes(source)
    transport = YmodemReceiverTransport()
    worker, handler = _transfer_worker(transport)
    worker.start()
    try:
        assert handler.opened_event.wait(1.0)
        worker.start_file_send(path)

        with pytest.raises(FileTransferError):
            worker.send(b"busy")

        assert handler.wait_for_transfer(lambda item: item.state is FileTransferState.SUCCEEDED)
        assert transport.received_data() == source
        assert transport.frame_errors == []

        worker.send(b"after")
        assert handler.sent_event.wait(1.0)
        assert handler.sent[-1] == b"after"
    finally:
        worker.request_stop()
        assert handler.stopped_event.wait(1.0)
        assert worker.join(1.0)


def test_worker_cancels_file_transfer_and_keeps_connection(tmp_path: Path) -> None:
    source = b"0123456789" * 100
    path = tmp_path / "firmware.bin"
    path.write_bytes(source)
    transport = YmodemReceiverTransport(initial_c=False)
    worker, handler = _transfer_worker(transport)
    worker.start()
    try:
        assert handler.opened_event.wait(1.0)
        worker.start_file_send(path)
        assert handler.wait_for_transfer(lambda item: item.state is FileTransferState.WAITING)

        assert worker.cancel_file_transfer()
        assert handler.wait_for_transfer(lambda item: item.state is FileTransferState.CANCELLED)
        assert transport.writes[-1] == b"\x18" * 8
        assert not worker.is_transferring

        worker.send(b"after-cancel")
        assert handler.sent_event.wait(1.0)
        assert handler.sent[-1] == b"after-cancel"
    finally:
        worker.request_stop()
        assert handler.stopped_event.wait(1.0)
        assert worker.join(1.0)


def test_worker_transfer_failure_does_not_stop_worker(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"x" * 1500)
    transport = YmodemReceiverTransport(silent_blocks=frozenset({1}))
    worker, handler = _transfer_worker(
        transport,
        ymodem_options=_ymodem_options(max_retries=1, data_timeout=0.02),
    )
    worker.start()
    try:
        assert handler.opened_event.wait(1.0)
        worker.start_file_send(path)

        assert handler.wait_for_transfer(lambda item: item.state is FileTransferState.FAILED)
        assert worker.is_alive
        assert handler.io_errors == []

        worker.send(b"still-alive")
        assert handler.sent_event.wait(1.0)
    finally:
        worker.request_stop()
        assert handler.stopped_event.wait(1.0)
        assert worker.join(1.0)


def test_worker_validates_file_transfer_requests(tmp_path: Path) -> None:
    path = tmp_path / "firmware.bin"
    path.write_bytes(b"x" * 100)
    transport = YmodemReceiverTransport(initial_c=False)
    worker, handler = _transfer_worker(transport)

    with pytest.raises(ValidationError):
        worker.start_file_send(cast(Path, "not-a-path"))
    with pytest.raises(ValidationError):
        worker.start_file_send(path, block_size=300)

    worker.start()
    try:
        assert handler.opened_event.wait(1.0)
        worker.start_file_send(path)
        with pytest.raises(FileTransferError):
            worker.start_file_send(path)
        with pytest.raises(FileTransferError):
            worker.set_line_state(dtr=False, rts=False)
        assert worker.cancel_file_transfer()
        assert handler.wait_for_transfer(lambda item: item.state is FileTransferState.CANCELLED)
    finally:
        worker.request_stop()
        assert handler.stopped_event.wait(1.0)
        assert worker.join(1.0)
