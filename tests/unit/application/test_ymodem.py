"""测试 Ymodem 发送端协议状态机。"""

from binascii import crc_hqx
from pathlib import Path
from time import monotonic

import pytest
from tests.fixtures.fakes import FakeTransport, YmodemReceiverTransport

from serialmnt.application.ymodem import (
    BLOCK_SIZE_128,
    BLOCK_SIZE_1024,
    SOH,
    STX,
    YmodemOptions,
    YmodemSender,
    build_frame,
    sanitize_ymodem_name,
)
from serialmnt.domain import (
    FileTransferSnapshot,
    FileTransferState,
    SerialConfig,
    TransportIOError,
    ValidationError,
)


def _options(**overrides: object) -> YmodemOptions:
    values: dict[str, object] = {
        "handshake_timeout": 0.5,
        "header_timeout": 0.2,
        "data_timeout": 0.05,
        "eot_timeout": 0.05,
        "final_block_timeout": 0.02,
        "second_c_grace": 0.005,
        "final_c_grace": 0.005,
        "poll_timeout": 0.002,
        "max_retries": 2,
        "progress_interval": 0,
        "max_garbage_bytes": 4096,
    }
    values.update(overrides)
    return YmodemOptions(**values)  # type: ignore[arg-type]


def _open_receiver(**kwargs: object) -> YmodemReceiverTransport:
    transport = YmodemReceiverTransport(**kwargs)  # type: ignore[arg-type]
    transport.open(SerialConfig(port="COM1"))
    return transport


def _send(  # noqa: PLR0913 - 测试辅助函数集中暴露全部场景开关
    transport: FakeTransport,
    path: Path,
    *,
    options: YmodemOptions | None = None,
    block_size: int = BLOCK_SIZE_1024,
    is_cancelled: object = None,
    progress: list[FileTransferSnapshot] | None = None,
) -> FileTransferSnapshot:
    sender = YmodemSender(
        transport=transport,
        monotonic=monotonic,
        baudrate=115200,
        options=options or _options(),
        on_progress=progress.append if progress is not None else None,
        is_cancelled=is_cancelled,  # type: ignore[arg-type]
    )
    return sender.send(path, block_size=block_size)


def _write_file(path: Path, size: int, byte: int = 0x5A) -> bytes:
    data = bytes((byte,)) * size
    path.write_bytes(data)
    return data


def test_sends_file_and_receiver_reconstructs_content(tmp_path: Path) -> None:
    source = _write_file(tmp_path / "firmware_v1.2.bin", 2500)
    transport = _open_receiver()
    progress: list[FileTransferSnapshot] = []

    result = _send(transport, tmp_path / "firmware_v1.2.bin", progress=progress)

    assert result.state is FileTransferState.SUCCEEDED
    assert result.transferred_bytes == len(source)
    assert result.file_size == len(source)
    assert result.block_number == 3
    assert transport.frame_errors == []
    assert transport.received_data() == source
    assert transport.header_name == "firmware_v1.2.bin"
    assert transport.declared_size == len(source)
    states = [item.state for item in progress]
    assert FileTransferState.WAITING in states
    assert FileTransferState.SENDING in states
    assert FileTransferState.FINISHING in states
    assert FileTransferState.SUCCEEDED not in states


def test_header_block_is_always_128_bytes_with_valid_crc(tmp_path: Path) -> None:
    source = _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver()

    _send(transport, tmp_path / "app.bin")

    header = transport.frames[0]
    assert len(header) == 133
    assert header[0] == SOH
    assert header[1] == 0
    assert header[2] == 0xFF
    payload = header[3:131]
    assert payload.startswith(b"app.bin\x0010\x00")
    assert int.from_bytes(header[131:], "big") == crc_hqx(payload, 0)
    assert transport.received_data() == source


@pytest.mark.parametrize(
    ("size", "block_size", "expected_starts", "expected_blocks"),
    [
        (0, BLOCK_SIZE_1024, [], 0),
        (1, BLOCK_SIZE_1024, [STX], 1),
        (1024, BLOCK_SIZE_1024, [STX], 1),
        (1025, BLOCK_SIZE_1024, [STX, STX], 2),
        (128, BLOCK_SIZE_128, [SOH], 1),
        (129, BLOCK_SIZE_128, [SOH, SOH], 2),
    ],
)
def test_data_block_sizes_and_padding(
    tmp_path: Path,
    size: int,
    block_size: int,
    expected_starts: list[int],
    expected_blocks: int,
) -> None:
    source = _write_file(tmp_path / "data.bin", size)
    transport = _open_receiver()

    result = _send(transport, tmp_path / "data.bin", block_size=block_size)

    data_frames = transport.frames[1:-1]
    assert result.state is FileTransferState.SUCCEEDED
    assert [frame[0] for frame in data_frames] == expected_starts
    assert len(data_frames) == expected_blocks
    if data_frames:
        last_payload = data_frames[-1][3 : 3 + block_size]
        actual = size - block_size * (expected_blocks - 1)
        assert last_payload[:actual] == source[-actual:]
        assert set(last_payload[actual:]) <= {0x1A}
    assert transport.received_data() == source


def test_eot_nak_is_retried_and_missing_final_c_is_tolerated(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 300)
    transport = _open_receiver(eot_nak=True, final_c=False)

    result = _send(transport, tmp_path / "app.bin")

    assert result.state is FileTransferState.SUCCEEDED
    assert result.retries == 1
    # 无 C 时仍发送结束空块，接收端会正常确认。
    assert transport.frames[-1][0] == SOH
    assert not any(transport.frames[-1][3:131])


def test_nak_retransmits_same_block_and_counts_retry(tmp_path: Path) -> None:
    source = _write_file(tmp_path / "app.bin", 1500)
    transport = _open_receiver(nak_blocks=frozenset({1}))

    result = _send(transport, tmp_path / "app.bin")

    data_frames = transport.frames[1:-1]
    assert result.state is FileTransferState.SUCCEEDED
    assert result.retries == 1
    assert data_frames[0] == data_frames[1]
    assert data_frames[0][1] == 1
    assert transport.received_data() == source


def test_retry_exhaustion_fails_transfer(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 1500)
    transport = _open_receiver(silent_blocks=frozenset({1}))

    result = _send(transport, tmp_path / "app.bin", options=_options(max_retries=1))

    assert result.state is FileTransferState.FAILED
    assert "重试" in result.message
    # 失败时尽力发送 CAN 取消序列，串口保持可继续使用。
    assert transport.writes[-1] == b"\x18" * 8


def test_handshake_timeout_reports_failure(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver(initial_c=False)

    result = _send(
        transport,
        tmp_path / "app.bin",
        options=_options(handshake_timeout=0.02, poll_timeout=0.002),
    )

    assert result.state is FileTransferState.FAILED
    assert "超时" in result.message
    assert transport.frames == ()


def test_remote_cancel_aborts_and_sends_cancel_sequence(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 100)
    transport = _open_receiver(remote_cancel=True)

    result = _send(transport, tmp_path / "app.bin")

    assert result.state is FileTransferState.CANCELLED
    assert "接收端" in result.message
    assert transport.writes[-1] == b"\x18" * 8


def test_user_cancel_aborts_transfer(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 100)
    transport = _open_receiver()

    result = _send(
        transport,
        tmp_path / "app.bin",
        is_cancelled=lambda: True,
    )

    assert result.state is FileTransferState.CANCELLED
    assert "用户" in result.message
    assert transport.writes[-1] == b"\x18" * 8


def test_missing_file_fails_without_touching_transport(tmp_path: Path) -> None:
    transport = _open_receiver()

    result = _send(transport, tmp_path / "missing.bin")

    assert result.state is FileTransferState.FAILED
    assert "不是普通文件" in result.message
    assert transport.writes == []


def test_empty_file_sends_header_and_final_block_only(tmp_path: Path) -> None:
    _write_file(tmp_path / "empty.bin", 0)
    transport = _open_receiver()

    result = _send(transport, tmp_path / "empty.bin")

    assert result.state is FileTransferState.SUCCEEDED
    assert len(transport.frames) == 2
    assert transport.frames[0].startswith(b"\x01\x00\xffempty.bin\x000\x00")
    assert not any(transport.frames[1][3:131])
    assert transport.received_data() == b""


def test_long_ascii_filename_is_rejected(tmp_path: Path) -> None:
    file_name = "a" * 126 + ".bin"
    _write_file(tmp_path / file_name, 10)
    transport = _open_receiver()

    result = _send(transport, tmp_path / file_name)

    assert result.state is FileTransferState.FAILED
    assert "文件名过长" in result.message
    assert transport.writes == []


def test_sequence_wraps_after_255_data_blocks(tmp_path: Path) -> None:
    size = 256 * BLOCK_SIZE_1024 + 1
    source = _write_file(tmp_path / "large.bin", size)
    transport = _open_receiver()

    result = _send(transport, tmp_path / "large.bin")

    assert result.state is FileTransferState.SUCCEEDED
    assert transport.received_data() == source
    assert transport.frames[255][1] == 0xFF
    assert transport.frames[256][1] == 0x00
    assert transport.frames[257][1] == 0x01
    assert transport.frame_errors == []


def test_transport_error_is_reported_and_reraised(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver()
    transport.write_error = TransportIOError("设备已断开。")
    progress: list[FileTransferSnapshot] = []

    with pytest.raises(TransportIOError):
        _send(transport, tmp_path / "app.bin", progress=progress)

    assert progress[-1].state is FileTransferState.FAILED
    assert "串口通信失败" in progress[-1].message


class _FailingStream:
    """读取时抛出 OSError 的文件对象包装，模拟可移动介质被拔出。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream

    def read(self, count: int) -> bytes:
        raise OSError(5, "设备已移除")

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined,no-any-return]

    def close(self) -> None:
        self._stream.close()  # type: ignore[attr-defined]

    def __enter__(self) -> "_FailingStream":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_single_can_is_ignored_as_noise(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver(initial_noise=b"\x18")

    result = _send(transport, tmp_path / "app.bin")

    assert result.state is FileTransferState.SUCCEEDED
    assert transport.received_data() == b"Z" * 10


class _TruncatingStream:
    """首次读取后模拟文件被截断。"""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._reads = 0

    def read(self, count: int) -> bytes:
        self._reads += 1
        if self._reads > 1:
            return b""
        return self._stream.read(min(count, 100))  # type: ignore[attr-defined,no-any-return]

    def fileno(self) -> int:
        return self._stream.fileno()  # type: ignore[attr-defined,no-any-return]

    def close(self) -> None:
        self._stream.close()  # type: ignore[attr-defined]


def test_file_truncated_during_transfer_fails_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_file(tmp_path / "app.bin", 3000)
    transport = _open_receiver()
    sender = YmodemSender(
        transport=transport,
        monotonic=monotonic,
        baudrate=115200,
        options=_options(),
    )
    real_open = sender._open_stream
    monkeypatch.setattr(
        sender,
        "_open_stream",
        lambda path: _TruncatingStream(real_open(path)),
    )

    result = sender.send(tmp_path / "app.bin")

    assert result.state is FileTransferState.FAILED
    assert "截断" in result.message
    assert transport.writes[-1] == b"\x18" * 8


def test_final_block_nak_is_retried(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver(final_nak_once=True)

    result = _send(transport, tmp_path / "app.bin")

    assert result.state is FileTransferState.SUCCEEDED
    empty_blocks = [frame for frame in transport.frames if not any(frame[3:131])]
    assert len(empty_blocks) >= 2


def test_final_block_timeout_still_succeeds(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver(final_ack=False)

    result = _send(transport, tmp_path / "app.bin")

    assert result.state is FileTransferState.SUCCEEDED


def test_file_read_error_fails_transfer_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver()
    sender = YmodemSender(
        transport=transport,
        monotonic=monotonic,
        baudrate=115200,
        options=_options(),
    )
    real_open = sender._open_stream
    monkeypatch.setattr(
        sender,
        "_open_stream",
        lambda path: _FailingStream(real_open(path)),
    )

    result = sender.send(tmp_path / "app.bin")

    assert result.state is FileTransferState.FAILED
    assert "无法读取文件" in result.message
    # 文件错误只结束传输，不抛出串口级异常。
    assert transport.writes[-1] == b"\x18" * 8


def test_invalid_block_size_is_rejected(tmp_path: Path) -> None:
    _write_file(tmp_path / "app.bin", 10)
    transport = _open_receiver()

    with pytest.raises(ValidationError):
        _send(transport, tmp_path / "app.bin", block_size=256)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("firmware_v1.2.bin", "firmware_v1.2.bin"),
        ("固件 v1.bin", "v1.bin"),
        ("中文", "file"),
        ("app 固件?.bin", "app__.bin"),
        (".hidden", "hidden"),
    ],
)
def test_sanitize_ymodem_name(name: str, expected: str) -> None:
    assert sanitize_ymodem_name(name) == expected


def test_build_frame_validates_and_appends_crc() -> None:
    payload = bytes(range(128))
    frame = build_frame(3, payload, BLOCK_SIZE_128)

    assert frame[:3] == bytes((SOH, 3, 0xFC))
    assert frame[3:131] == payload
    assert int.from_bytes(frame[131:], "big") == crc_hqx(payload, 0)
    with pytest.raises(ValidationError):
        build_frame(0, payload, 256)
    with pytest.raises(ValidationError):
        build_frame(0, payload[:127], BLOCK_SIZE_128)


@pytest.mark.parametrize(
    "overrides",
    [
        {"handshake_timeout": 0},
        {"data_timeout": -1},
        {"poll_timeout": 0},
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"progress_interval": -0.1},
        {"max_garbage_bytes": -1},
    ],
)
def test_options_reject_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        YmodemOptions(**overrides)  # type: ignore[arg-type]
