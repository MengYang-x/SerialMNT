"""无硬件、可注入时钟和传输测试替身。"""

from binascii import crc_hqx
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Condition, Event, Lock
from time import monotonic

from serialmnt.application.ymodem import ACK, CAN, CRC_REQUEST, EOT, NAK, SOH, STX
from serialmnt.domain import DomainError, FileTransferSnapshot, SerialConfig


class FakeClock:
    """可手动推进的确定性时钟。"""

    def __init__(
        self,
        *,
        now_utc: datetime | None = None,
        monotonic_value: float = 100.0,
    ) -> None:
        self._now_utc = now_utc or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self._monotonic = monotonic_value

    def now_utc(self) -> datetime:
        return self._now_utc

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now_utc += timedelta(seconds=seconds)


class FakeTransport:
    """线程安全的 Transport 测试替身。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        open_error: BaseException | None = None,
        read_error: BaseException | None = None,
        write_error: BaseException | None = None,
        line_error: BaseException | None = None,
        reads: list[bytes] | None = None,
        close_on_read: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self.open_error = open_error
        self.read_error = read_error
        self.write_error = write_error
        self.line_error = line_error
        self.close_on_read = close_on_read
        self.close_error = close_error
        self._reads = deque(reads or [])
        self._condition = Condition()
        self._is_open = False
        self._lock = Lock()
        self.config: SerialConfig | None = None
        self.writes: list[bytes] = []
        self.line_states: list[tuple[bool, bool]] = []
        self.read_timeouts: list[float] = []
        self.open_count = 0
        self.close_count = 0
        self.opened_event = Event()
        self.closed_event = Event()
        self.write_event = Event()
        self.receive_event = Event()
        self.line_event = Event()

    def open(self, config: SerialConfig) -> None:
        if self.open_error is not None:
            raise self.open_error
        with self._condition:
            self.config = config
            self._is_open = True
            self.open_count += 1
            self._condition.notify_all()
        self.opened_event.set()

    def close(self, timeout: float) -> None:
        if self.close_error is not None:
            raise self.close_error
        with self._condition:
            self._is_open = False
            self.close_count += 1
            self._condition.notify_all()
        self.closed_event.set()

    def read(self, max_bytes: int, timeout: float) -> bytes:
        with self._condition:
            self.read_timeouts.append(timeout)
            deadline = monotonic() + timeout
            while self._is_open and not self._reads and self.read_error is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            if self.read_error is not None:
                raise self.read_error
            if not self._is_open or not self._reads:
                return b""
            data = self._reads.popleft()
            if self.close_on_read and not self._reads:
                self._is_open = False
                self._condition.notify_all()
        self.receive_event.set()
        return data[:max_bytes]

    def write_all(self, data: bytes, timeout: float) -> None:
        if self.write_error is not None:
            raise self.write_error
        with self._lock:
            if not self._is_open:
                raise RuntimeError("transport closed")
            self.writes.append(data)
        self.write_event.set()

    def set_line_state(self, *, dtr: bool, rts: bool) -> None:
        if self.line_error is not None:
            raise self.line_error
        with self._lock:
            self.line_states.append((dtr, rts))
        self.line_event.set()

    @property
    def is_open(self) -> bool:
        with self._condition:
            return self._is_open

    def push_read(self, data: bytes) -> None:
        with self._condition:
            self._reads.append(data)
            self._condition.notify_all()


class RecordingWorkerHandler:
    """记录 Worker 事件并提供同步等待点。"""

    def __init__(self) -> None:
        self.opened_event = Event()
        self.open_failed_event = Event()
        self.received_event = Event()
        self.sent_event = Event()
        self.io_error_event = Event()
        self.stopped_event = Event()
        self.opened_config: SerialConfig | None = None
        self.open_error: DomainError | None = None
        self.received: list[bytes] = []
        self.sent: list[bytes] = []
        self.io_errors: list[DomainError] = []
        self.transfers: list[FileTransferSnapshot] = []
        self._lock = Lock()
        self._transfer_condition = Condition()

    def on_opened(self, config: SerialConfig) -> None:
        with self._lock:
            self.opened_config = config
        self.opened_event.set()

    def on_open_failed(self, error: DomainError) -> None:
        with self._lock:
            self.open_error = error
        self.open_failed_event.set()

    def on_bytes_received(self, data: bytes) -> None:
        with self._lock:
            self.received.append(data)
        self.received_event.set()

    def on_bytes_sent(self, data: bytes) -> None:
        with self._lock:
            self.sent.append(data)
        self.sent_event.set()

    def on_io_error(self, error: DomainError) -> None:
        with self._lock:
            self.io_errors.append(error)
        self.io_error_event.set()

    def on_file_transfer(self, transfer: FileTransferSnapshot) -> None:
        with self._transfer_condition:
            self.transfers.append(transfer)
            self._transfer_condition.notify_all()

    def wait_for_transfer(
        self,
        predicate: object,
        timeout: float = 2.0,
    ) -> bool:
        """等待满足条件的传输快照出现。"""
        with self._transfer_condition:
            return self._transfer_condition.wait_for(
                lambda: any(predicate(item) for item in self.transfers),  # type: ignore[operator]
                timeout,
            )

    def on_stopped(self) -> None:
        self.stopped_event.set()


class YmodemReceiverTransport(FakeTransport):
    """在写入时解析 Ymodem 帧并自动返回 ACK/NAK/C 的接收端替身。"""

    def __init__(  # noqa: PLR0913 - 场景开关集中在一个测试替身上便于组合
        self,
        *,
        initial_c: bool = True,
        header_c: bool = True,
        eot_nak: bool = False,
        final_c: bool = True,
        final_ack: bool = True,
        final_nak_once: bool = False,
        initial_noise: bytes = b"",
        nak_blocks: frozenset[int] = frozenset(),
        silent_blocks: frozenset[int] = frozenset(),
        remote_cancel: bool = False,
    ) -> None:
        super().__init__()
        self._initial_c = initial_c
        self._initial_noise = initial_noise
        self._noise_sent = False
        self.final_nak_once = final_nak_once
        self._final_nak_sent = False
        self._initial_c_interval = 0.05
        self._last_initial_c = 0.0
        self._header_received = False
        self.header_c = header_c
        self.eot_nak = eot_nak
        self.final_c = final_c
        self.final_ack = final_ack
        self.nak_blocks = nak_blocks
        self.silent_blocks = silent_blocks
        self.remote_cancel = remote_cancel
        self._lock = Lock()
        self._frames: list[bytes] = []
        self.frame_errors: list[str] = []
        self.payload = bytearray()
        self.declared_size: int | None = None
        self.header_name: str | None = None
        self._expected_sequence = 1
        self._eot_count = 0
        self._eot_acked = False
        self._nak_sent: set[int] = set()
        self._remote_cancelled = False

    def read(self, max_bytes: int, timeout: float) -> bytes:
        # 真实接收端会周期性重发 C，直到收到文件头；这里限速避免正常读循环忙转。
        now = monotonic()
        if not self._header_received and timeout > 0 and not self._noise_sent:
            self._noise_sent = True
            if self._initial_noise:
                self.push_read(self._initial_noise)
        if (
            self._initial_c
            and not self._header_received
            and timeout > 0
            and now - self._last_initial_c >= self._initial_c_interval
        ):
            self._last_initial_c = now
            self.push_read(bytes((CRC_REQUEST,)))
        return super().read(max_bytes, timeout)

    def write_all(self, data: bytes, timeout: float) -> None:
        super().write_all(data, timeout)
        self._respond(data)

    @property
    def frames(self) -> tuple[bytes, ...]:
        """返回接收到的完整协议帧。"""
        return tuple(self._frames)

    def received_data(self) -> bytes:
        """返回接收端按文件头大小截断后的文件内容。"""
        if self.declared_size is None:
            return b""
        return bytes(self.payload[: self.declared_size])

    def _respond(self, data: bytes) -> None:
        with self._lock:
            if data and all(byte == CAN for byte in data):
                return
            if data == bytes((EOT,)):
                self._handle_eot()
                return
            if len(data) < 5 or data[0] not in {SOH, STX}:
                return
            block_size = 128 if data[0] == SOH else 1024
            if len(data) != block_size + 5:
                self.frame_errors.append(f"帧长度错误：{len(data)}")
                return
            sequence = data[1]
            payload = data[3 : 3 + block_size]
            if data[2] != (0xFF - sequence) & 0xFF:
                self.frame_errors.append(f"序号反码错误：{sequence}")
            if int.from_bytes(data[-2:], "big") != crc_hqx(payload, 0):
                self.frame_errors.append(f"CRC 错误：{sequence}")
            self._frames.append(data)
            if sequence == 0 and self._eot_acked:
                self._handle_final_block(payload)
            elif not self._header_received:
                self._handle_header(payload)
            else:
                self._handle_data(sequence, payload)

    def _handle_header(self, payload: bytes) -> None:
        name, _, size_text = payload.partition(b"\x00")
        self.header_name = name.decode("ascii", "replace")
        try:
            self.declared_size = int(size_text.split(b"\x00", 1)[0])
        except ValueError:
            self.frame_errors.append("文件大小字段非法")
            self.declared_size = None
        self._header_received = True
        if self.remote_cancel:
            self._remote_cancelled = True
            self.push_read(bytes((CAN, CAN)))
            return
        self.push_read(bytes((ACK,)))
        if self.header_c:
            self.push_read(bytes((CRC_REQUEST,)))

    def _handle_data(self, sequence: int, payload: bytes) -> None:
        if sequence in self.silent_blocks:
            return
        if sequence in self.nak_blocks and sequence not in self._nak_sent:
            self._nak_sent.add(sequence)
            self.push_read(bytes((NAK,)))
            return
        if sequence != self._expected_sequence:
            self.frame_errors.append(f"序号不连续：期望 {self._expected_sequence}，收到 {sequence}")
        self._expected_sequence = (sequence + 1) & 0xFF
        self.payload.extend(payload)
        self.push_read(bytes((ACK,)))

    def _handle_eot(self) -> None:
        self._eot_count += 1
        if self.eot_nak and self._eot_count == 1:
            self.push_read(bytes((NAK,)))
            return
        self._eot_acked = True
        self.push_read(bytes((ACK,)))
        if self.final_c:
            self.push_read(bytes((CRC_REQUEST,)))

    def _handle_final_block(self, payload: bytes) -> None:
        if any(payload):
            self.frame_errors.append("结束空块不是全零")
        if self.final_nak_once and not self._final_nak_sent:
            self._final_nak_sent = True
            self.push_read(bytes((NAK,)))
            return
        if self.final_ack:
            self.push_read(bytes((ACK,)))
