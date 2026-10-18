"""Ymodem 发送端协议状态机。"""

import os
import unicodedata
from binascii import crc_hqx
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from serialmnt.domain import (
    DomainError,
    FileTransferError,
    FileTransferSnapshot,
    FileTransferState,
    Transport,
    ValidationError,
)

# 控制字节定义见 Forsberg YMODEM 规范；CRC 使用 XMODEM 的 CRC-16/XMODEM。
SOH = 0x01
STX = 0x02
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = 0x43
YMODEM_G = 0x47
XON = 0x11
XOFF = 0x13
PAD_BYTE = 0x1A

BLOCK_SIZE_128 = 128
BLOCK_SIZE_1024 = 1024
HEADER_BLOCK_SIZE = BLOCK_SIZE_128
CANCEL_BYTES = bytes((CAN,)) * 8
MINIMUM_WRITE_TIMEOUT = 2.0
REMOTE_CANCEL_WINDOW = 2.0
READ_CHUNK_SIZE = 256


@dataclass(frozen=True, slots=True)
class YmodemOptions:
    """Ymodem 发送的超时、重试和进度回调参数。"""

    handshake_timeout: float = 60.0
    header_timeout: float = 30.0
    data_timeout: float = 10.0
    eot_timeout: float = 10.0
    final_block_timeout: float = 5.0
    second_c_grace: float = 0.5
    final_c_grace: float = 0.5
    poll_timeout: float = 0.05
    max_retries: int = 10
    progress_interval: float = 0.1
    max_garbage_bytes: int = 4096

    def __post_init__(self) -> None:
        for name in (
            "handshake_timeout",
            "header_timeout",
            "data_timeout",
            "eot_timeout",
            "final_block_timeout",
            "second_c_grace",
            "final_c_grace",
            "poll_timeout",
        ):
            value = getattr(self, name)
            if type(value) not in {int, float} or value <= 0:
                raise ValidationError(f"{name} 必须是正数。")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValidationError("max_retries 必须是非负整数。")
        if type(self.progress_interval) not in {int, float} or self.progress_interval < 0:
            raise ValidationError("progress_interval 必须是非负数。")
        if type(self.max_garbage_bytes) is not int or self.max_garbage_bytes < 0:
            raise ValidationError("max_garbage_bytes 必须是非负整数。")


class _TransferCancelled(Exception):
    """内部取消信号，最终会转换为 CANCELLED 终态。"""


def build_frame(sequence: int, payload: bytes, block_size: int) -> bytes:
    """构造一个数据帧：起始字节、序号、反码、负载和 CRC 高字节在前。"""
    if block_size not in {BLOCK_SIZE_128, BLOCK_SIZE_1024}:
        raise ValidationError("Ymodem 数据块只允许 128 或 1024 字节。")
    if len(payload) != block_size:
        raise ValidationError("负载长度必须与数据块大小一致。")
    start = SOH if block_size == BLOCK_SIZE_128 else STX
    crc = crc_hqx(payload, 0)
    return (
        bytes((start, sequence & 0xFF, (0xFF - sequence) & 0xFF))
        + payload
        + bytes((crc >> 8, crc & 0xFF))
    )


def sanitize_ymodem_name(name: str) -> str:
    """把文件名净化成 Ymodem 文件头可安全传输的 ASCII 名称。

    Ymodem 没有定义文件名编码，部分 bootloader 只接受 ASCII。这里对名称做
    NFKD 分解后丢弃非 ASCII 字符，保留扩展名，并把空格和特殊字符替换为下划线。
    """
    path = Path(name)
    suffix = _ascii_part(path.suffix)
    stem = _ascii_part(path.stem).lstrip("._") or "file"
    return stem + suffix


def _ascii_part(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in ascii_text)


class YmodemSender:
    """在独占 Transport 上同步执行一次 Ymodem 单文件发送。

    协议引擎必须由持有 Transport 的串口线程调用；所有协议字节由本类消费，
    不会进入会话的接收缓冲或日志。
    """

    def __init__(  # noqa: PLR0913 - 传输依赖项均为显式注入，便于测试替换
        self,
        *,
        transport: Transport,
        monotonic: Callable[[], float],
        baudrate: int,
        options: YmodemOptions | None = None,
        on_progress: Callable[[FileTransferSnapshot], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if type(baudrate) is not int or baudrate <= 0:
            raise ValidationError("波特率必须是正整数。")
        self._transport = transport
        self._monotonic = monotonic
        self._baudrate = baudrate
        self._options = options or YmodemOptions()
        self._on_progress = on_progress
        self._is_cancelled = is_cancelled

        self._rx = bytearray()
        self._last_can_at: float | None = None
        self._started = False
        self._file_name = "file"
        self._wire_name = "file"
        self._size = 0
        self._sent = 0
        self._block = 0
        self._retries = 0
        self._start = 0.0
        self._last_report = 0.0

    def send(self, path: Path, *, block_size: int = BLOCK_SIZE_1024) -> FileTransferSnapshot:
        """发送一个文件并返回终态快照；协议失败在内部转换为 FAILED。"""
        if block_size not in {BLOCK_SIZE_128, BLOCK_SIZE_1024}:
            raise ValidationError("Ymodem 数据块只允许 128 或 1024 字节。")
        self._file_name = path.name or "file"
        self._start = self._monotonic()
        self._last_report = self._start
        try:
            stream = self._open_stream(path)
        except FileTransferError as exc:
            return self._finish(FileTransferState.FAILED, exc.message)
        try:
            return self._run(stream, block_size)
        finally:
            # 介质被拔出时 close() 也可能抛 OSError，不能让它升级为串口致命错误。
            with suppress(OSError):
                stream.close()

    def _open_stream(self, path: Path) -> BinaryIO:
        try:
            if not path.is_file():
                raise FileTransferError(f"无法发送文件：{self._file_name} 不是普通文件。")
            stream = path.open("rb")
        except OSError as exc:
            raise FileTransferError(f"无法读取文件：{exc.strerror or exc}") from exc
        try:
            self._size = os.fstat(stream.fileno()).st_size
        except OSError as exc:
            with suppress(OSError):
                stream.close()
            raise FileTransferError(f"无法读取文件：{exc.strerror or exc}") from exc
        self._wire_name = sanitize_ymodem_name(self._file_name)
        header_length = len(self._wire_name) + 1 + len(str(self._size)) + 1
        if header_length > HEADER_BLOCK_SIZE:
            with suppress(OSError):
                stream.close()
            raise FileTransferError(
                "文件名过长，无法放入 Ymodem 文件头，请重命名为较短的 ASCII 名称。"
            )
        return stream

    def _run(self, stream: BinaryIO, block_size: int) -> FileTransferSnapshot:
        try:
            self._report(FileTransferState.WAITING, "等待接收端发起传输。", force=True)
            self._wait_for_receiver()
            self._send_header()
            self._send_data(stream, block_size)
            self._finish_transfer()
        except _TransferCancelled as exc:
            self._send_cancel()
            return self._finish(FileTransferState.CANCELLED, str(exc))
        except FileTransferError as exc:
            self._send_cancel()
            return self._finish(FileTransferState.FAILED, exc.message)
        except DomainError as exc:
            self._send_cancel()
            message = f"串口通信失败：{exc.message}"
            self._report(FileTransferState.FAILED, message, force=True)
            raise
        except Exception:
            self._send_cancel()
            self._report(FileTransferState.FAILED, "文件传输发生内部错误。", force=True)
            raise
        return self._finish(FileTransferState.SUCCEEDED, "文件发送完成。")

    def _wait_for_receiver(self) -> None:
        self._purge_input()
        self._started = True
        deadline = self._monotonic() + self._options.handshake_timeout
        garbage = 0
        while True:
            self._check_cancel()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise FileTransferError(
                    "等待接收端发起 Ymodem 传输超时。",
                    suggestion="请让设备进入 Ymodem 接收模式后重新发送。",
                )
            byte = self._read_byte(deadline)
            if byte is None:
                continue
            if byte == CRC_REQUEST:
                return
            if byte == YMODEM_G:
                raise FileTransferError("接收端请求 Ymodem-G，当前仅支持标准 Ymodem。")
            if byte == CAN:
                self._note_can()
                continue
            garbage += 1
            if garbage > self._options.max_garbage_bytes:
                raise FileTransferError("接收数据持续无法识别，Ymodem 传输已中止。")

    def _send_header(self) -> None:
        payload = (
            self._wire_name.encode("ascii") + b"\x00" + str(self._size).encode("ascii") + b"\x00"
        )
        packet = build_frame(0, payload.ljust(HEADER_BLOCK_SIZE, b"\x00"), HEADER_BLOCK_SIZE)
        self._send_packet(packet, self._options.header_timeout, "文件信息")
        self._wait_second_c()

    def _wait_second_c(self) -> None:
        """发送文件头后短暂等待接收端的第二个 C，再开始数据块。"""
        deadline = self._monotonic() + self._options.second_c_grace
        while True:
            self._check_cancel()
            if self._monotonic() >= deadline:
                return
            byte = self._read_byte(deadline)
            if byte is None:
                continue
            if byte == CRC_REQUEST:
                return
            if byte == CAN:
                self._note_can()

    def _send_data(self, stream: BinaryIO, block_size: int) -> None:
        while self._sent < self._size:
            self._check_cancel()
            try:
                chunk = stream.read(min(block_size, self._size - self._sent))
            except OSError as exc:
                raise FileTransferError(f"无法读取文件：{exc.strerror or exc}") from exc
            if not chunk:
                raise FileTransferError("文件在发送过程中被截断，传输已中止。")
            payload = chunk + bytes((PAD_BYTE,)) * (block_size - len(chunk))
            self._block += 1
            packet = build_frame(self._block & 0xFF, payload, block_size)
            self._send_packet(packet, self._options.data_timeout, f"第 {self._block} 块")
            self._sent += len(chunk)
            self._report(FileTransferState.SENDING, f"正在发送第 {self._block} 块。")

    def _send_packet(self, packet: bytes, timeout: float, stage: str) -> None:
        attempt = 0
        while True:
            self._check_cancel()
            self._discard_stale_controls()
            self._transport.write_all(packet, self._write_timeout(len(packet)))
            response = self._wait_response(timeout)
            if response == ACK:
                return
            attempt += 1
            self._retries += 1
            if attempt > self._options.max_retries:
                raise FileTransferError(
                    f"{stage}重试 {self._options.max_retries} 次后仍未得到确认。"
                )
            self._report(
                FileTransferState.SENDING,
                f"{stage}未收到确认，第 {attempt} 次重试。",
                force=True,
            )

    def _finish_transfer(self) -> None:
        self._report(FileTransferState.FINISHING, "正在结束传输。", force=True)
        self._send_eot()
        self._send_final_block()

    def _send_eot(self) -> None:
        for attempt in range(self._options.max_retries + 1):
            self._check_cancel()
            self._discard_stale_controls()
            self._transport.write_all(bytes((EOT,)), self._write_timeout(1))
            if self._wait_response(self._options.eot_timeout) == ACK:
                return
            self._retries += 1
            if attempt == self._options.max_retries:
                raise FileTransferError("结束传输时接收端未确认。")

    def _send_final_block(self) -> None:
        """等待 C 后发送结束空块；没有 C 或未确认都不回滚已完成的文件。"""
        deadline = self._monotonic() + self._options.final_c_grace
        while self._monotonic() < deadline:
            self._check_cancel()
            byte = self._read_byte(deadline)
            if byte is None:
                continue
            if byte == CRC_REQUEST:
                break
            if byte == CAN:
                self._note_can()

        packet = build_frame(0, bytes(HEADER_BLOCK_SIZE), HEADER_BLOCK_SIZE)
        for _ in range(2):
            self._check_cancel()
            self._transport.write_all(packet, self._write_timeout(len(packet)))
            response = self._wait_response(self._options.final_block_timeout)
            if response != NAK:
                return

    def _wait_response(self, timeout: float) -> int | None:
        deadline = self._monotonic() + timeout
        garbage = 0
        while True:
            self._check_cancel()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            byte = self._read_byte(deadline)
            if byte is None:
                continue
            if byte in {ACK, NAK}:
                return byte
            if byte == CAN:
                self._note_can()
                continue
            garbage += 1
            if garbage > self._options.max_garbage_bytes:
                raise FileTransferError("接收数据持续无法识别，Ymodem 传输已中止。")

    def _read_byte(self, deadline: float) -> int | None:
        if self._rx:
            return self._rx.pop(0)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return None
        timeout = min(self._options.poll_timeout, remaining)
        data = self._transport.read(READ_CHUNK_SIZE, timeout)
        if not data:
            return None
        self._rx.extend(data)
        return self._rx.pop(0)

    def _read_available(self) -> None:
        for _ in range(64):
            data = self._transport.read(READ_CHUNK_SIZE, 0.0)
            if not data:
                return
            self._rx.extend(data)

    def _discard_stale_controls(self) -> None:
        """发送新块前丢弃残留 ACK/NAK/C，避免把重复 ACK 当下一块的确认。"""
        self._read_available()
        if not self._rx:
            return
        self._rx = bytearray(
            byte for byte in self._rx if byte not in {ACK, NAK, CRC_REQUEST, XON, XOFF}
        )

    def _purge_input(self) -> None:
        """开始握手前排空启动横幅等残留字节，避免其中的 'C' 误触发传输。"""
        for _ in range(64):
            if not self._transport.read(READ_CHUNK_SIZE, 0.0):
                break
        self._rx.clear()

    def _note_can(self) -> None:
        now = self._monotonic()
        if self._last_can_at is not None and now - self._last_can_at <= REMOTE_CANCEL_WINDOW:
            raise _TransferCancelled("接收端取消了传输。")
        self._last_can_at = now

    def _check_cancel(self) -> None:
        if self._is_cancelled is not None and self._is_cancelled():
            raise _TransferCancelled("用户取消了文件发送。")

    def _send_cancel(self) -> None:
        if not self._started:
            return
        with suppress(DomainError):
            self._transport.write_all(
                CANCEL_BYTES,
                max(0.2, self._write_timeout(len(CANCEL_BYTES))),
            )

    def _write_timeout(self, length: int) -> float:
        # 11 位/字节近似覆盖起始位、8 数据位和停止位；低波特率下必须放宽写超时。
        wire_seconds = length * 11 / self._baudrate
        return max(MINIMUM_WRITE_TIMEOUT, wire_seconds * 2)

    def _report(
        self,
        state: FileTransferState,
        message: str,
        *,
        force: bool = False,
    ) -> None:
        now = self._monotonic()
        if (
            state is FileTransferState.SENDING
            and not force
            and now - self._last_report < self._options.progress_interval
        ):
            return
        self._last_report = now
        if self._on_progress is not None:
            self._on_progress(self._snapshot(state, message))

    def _finish(self, state: FileTransferState, message: str) -> FileTransferSnapshot:
        """构造终态快照；终态由调用方在释放传输锁之后再发布。"""
        return self._snapshot(state, message)

    def _snapshot(self, state: FileTransferState, message: str) -> FileTransferSnapshot:
        return FileTransferSnapshot(
            state=state,
            file_name=self._file_name,
            file_size=self._size,
            transferred_bytes=self._sent,
            block_number=self._block,
            retries=self._retries,
            elapsed_seconds=max(0.0, self._monotonic() - self._start),
            message=message,
        )
