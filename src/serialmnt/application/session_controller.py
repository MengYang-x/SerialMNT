"""单标签串口会话的应用层编排。"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from uuid import uuid4

from serialmnt.application.log_service import LogService
from serialmnt.application.serial_worker import (
    SerialWorker,
    SerialWorkerOptions,
    WorkerEventHandler,
)
from serialmnt.application.ymodem import BLOCK_SIZE_1024, MINIMUM_WRITE_TIMEOUT
from serialmnt.domain import (
    ACTIVE_FILE_TRANSFER_STATES,
    Clock,
    DomainError,
    FileTransferError,
    FileTransferSnapshot,
    FileTransferState,
    InvalidStateTransitionError,
    LogFormat,
    LogIOError,
    LogRecord,
    LogSink,
    RecordBuffer,
    SerialConfig,
    SessionSnapshot,
    SessionState,
    SessionStateMachine,
    Transport,
    TransportIOError,
    UserFacingError,
    ValidationError,
)

SnapshotCallback = Callable[[SessionSnapshot], None]
RecordCallback = Callable[[LogRecord], None]

MAX_SESSION_TITLE_LENGTH = 40
YMODEM_REQUIRED_BYTESIZE = 8
# 1K 数据帧的线上长度（1029 字节）和 8N1 每字节 11 位，用于估算退出等待上限。
YMODEM_TRANSFER_FRAME_BYTES = 1029
YMODEM_FRAME_BITS_PER_BYTE = 11
MAX_TRANSFER_JOIN_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class SessionControllerOptions:
    """SessionController 的可选身份、缓冲和回调参数。"""

    session_id: str | None = None
    title: str | None = None
    buffer: RecordBuffer | None = None
    on_snapshot: SnapshotCallback | None = None
    on_record: RecordCallback | None = None
    worker_options: SerialWorkerOptions | None = None
    log_sink_factory: Callable[[LogFormat], LogSink] | None = None
    auto_log_enabled: bool = False
    auto_log_format: LogFormat = "csv"
    auto_log_directory: str = ""


class SessionController(WorkerEventHandler):
    """维护单个串口会话的状态、缓冲和 Worker 生命周期。"""

    def __init__(
        self,
        *,
        config: SerialConfig,
        transport_factory: Callable[[], Transport],
        clock: Clock,
        options: SessionControllerOptions | None = None,
    ) -> None:
        if not callable(transport_factory):
            raise ValidationError("transport_factory 必须可调用。")
        selected_options = options or SessionControllerOptions()
        self._lock = RLock()
        self._session_id = selected_options.session_id or uuid4().hex
        if not self._session_id.strip():
            raise ValidationError("会话 ID 不能为空。")
        self._title = selected_options.title or config.port or "未选择端口"
        if not self._title.strip():
            raise ValidationError("会话标题不能为空。")
        self._config = config
        self._transport_factory = transport_factory
        self._clock = clock
        self._worker_options = selected_options.worker_options
        self._log_sink_factory = selected_options.log_sink_factory
        self._auto_log_enabled = selected_options.auto_log_enabled
        self._auto_log_format = selected_options.auto_log_format
        self._auto_log_directory = selected_options.auto_log_directory
        self._log_service = (
            LogService(sink_factory=self._log_sink_factory, clock=self._clock)
            if self._log_sink_factory is not None
            else None
        )
        self._buffer = selected_options.buffer or RecordBuffer()
        self._state_machine = SessionStateMachine()
        self._worker: SerialWorker | None = None
        self._last_error: UserFacingError | None = None
        self._transfer: FileTransferSnapshot | None = None
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._on_snapshot = selected_options.on_snapshot
        self._on_record = selected_options.on_record
        self._notify_current_snapshot()

    @property
    def session_id(self) -> str:
        """返回稳定会话 ID。"""
        return self._session_id

    @property
    def title(self) -> str:
        """返回标签标题。"""
        return self._title

    def rename(self, title: str) -> None:
        """重命名会话标签，广播快照但不影响连接状态。"""
        cleaned = title.strip()
        if not cleaned:
            raise ValidationError("会话名称不能为空。")
        if len(cleaned) > MAX_SESSION_TITLE_LENGTH:
            raise ValidationError(f"会话名称不能超过 {MAX_SESSION_TITLE_LENGTH} 个字符。")
        with self._lock:
            if cleaned == self._title:
                return
            self._title = cleaned
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)

    @property
    def config(self) -> SerialConfig:
        """返回当前连接配置。"""
        return self._config

    @property
    def state(self) -> SessionState:
        """返回当前会话状态。"""
        with self._lock:
            return self._state_machine.state

    @property
    def snapshot(self) -> SessionSnapshot:
        """返回线程安全的只读状态快照。"""
        with self._lock:
            return self._build_snapshot()

    @property
    def records(self) -> tuple[LogRecord, ...]:
        """返回当前内存缓冲的不可变快照。"""
        with self._lock:
            return self._buffer.records

    @property
    def transfer(self) -> FileTransferSnapshot | None:
        """返回最近一次 Ymodem 文件发送的进度或结果。"""
        with self._lock:
            return self._transfer

    @property
    def auto_log_enabled(self) -> bool:
        """返回自动日志开关。"""
        return self._auto_log_enabled

    @property
    def auto_log_format(self) -> LogFormat:
        """返回自动日志格式。"""
        return self._auto_log_format

    @property
    def auto_log_directory(self) -> str:
        """返回自动日志目录。"""
        return self._auto_log_directory

    @property
    def log_path(self) -> object | None:
        """返回当前连接正在写入的日志路径。"""
        service = self._log_service
        return service.path if service is not None else None

    def update_log_preferences(
        self, *, enabled: bool, log_format: LogFormat, directory: str
    ) -> None:
        """更新下一次连接使用的自动日志设置。"""
        if type(enabled) is not bool:
            raise ValidationError("自动日志开关必须是布尔值。")
        if self._state_machine.state in {
            SessionState.CONNECTING,
            SessionState.CONNECTED,
            SessionState.DISCONNECTING,
        }:
            raise InvalidStateTransitionError(self._state_machine.state, "update_log_preferences")
        if log_format not in {"csv", "txt"}:
            raise ValidationError("自动日志格式必须是 csv 或 txt。")
        if not isinstance(directory, str):
            raise ValidationError("自动日志开关必须是布尔值。")
        self._auto_log_enabled = enabled
        self._auto_log_format = log_format
        self._auto_log_directory = directory

    def export_records(self, path: Path, log_format: LogFormat) -> int:
        """把当前完整内存缓冲导出为 CSV 或 TXT。"""
        if self._log_sink_factory is None:
            raise LogIOError("未配置日志输出。")
        if log_format not in {"csv", "txt"}:
            raise ValidationError("日志格式必须是 csv 或 txt。")
        records = self.records
        sink = self._log_sink_factory(log_format)
        try:
            sink.open(path)
            for record in records:
                sink.write(record)
            sink.flush()
        finally:
            sink.close()
        return len(records)

    def connect(self, config: SerialConfig | None = None) -> None:
        """校验配置并启动串口 Worker。"""
        selected_config = config or self._config
        selected_config.validate_for_connect()
        with self._lock:
            if self._worker is not None and self._worker.is_alive:
                raise InvalidStateTransitionError(
                    self._state_machine.state,
                    "connect",
                    SessionState.CONNECTING,
                )
            self._config = selected_config
            self._last_error = None
            self._transfer = None
            self._state_machine.connect()
            worker = SerialWorker(
                config=selected_config,
                transport_factory=self._transport_factory,
                event_handler=self,
                options=self._worker_options,
            )
            self._worker = worker
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)
        worker.start()

    def disconnect(self, timeout: float = 1.5) -> bool:
        """请求 Worker 停止、关闭端口并等待清理。"""
        if timeout <= 0:
            raise ValidationError("断开超时必须大于零。")
        with self._lock:
            if self._state_machine.state in {SessionState.DISCONNECTED, SessionState.CLOSED}:
                return True
            if self._state_machine.state is SessionState.CONNECTING:
                raise InvalidStateTransitionError(
                    self._state_machine.state,
                    "disconnect",
                    SessionState.DISCONNECTING,
                )
            if self._state_machine.state is SessionState.CONNECTED:
                self._state_machine.begin_disconnect()
                snapshot = self._build_snapshot()
            else:
                snapshot = None
            worker = self._worker
        if snapshot is not None:
            self._notify_snapshot(snapshot)
        if worker is not None:
            worker.cancel_file_transfer()
            worker.request_stop()
            stopped = worker.join(self._transfer_join_timeout(timeout))
            if not stopped:
                with self._lock:
                    if self._state_machine.state is SessionState.DISCONNECTING:
                        self._state_machine.mark_cleanup_failed()
                    self._last_error = TransportIOError("等待串口线程退出超时。").to_user_facing()
                    error_snapshot = self._build_snapshot()
                self._notify_snapshot(error_snapshot)
                return False
        with self._lock:
            if self._state_machine.state is SessionState.DISCONNECTING:
                self._state_machine.mark_disconnected()
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)
        return True

    def close(self, *, force: bool = False) -> None:
        """关闭标签状态；活动会话需要显式强制关闭。"""
        with self._lock:
            state = self._state_machine.state
        if state in {SessionState.CONNECTED, SessionState.CONNECTING, SessionState.DISCONNECTING}:
            if not force:
                raise InvalidStateTransitionError(state, "close", SessionState.CLOSED)
            worker = self._worker
            if worker is not None:
                worker.cancel_file_transfer()
                worker.request_stop()
                worker.join(self._transfer_join_timeout(1.5))
        with self._lock:
            self._state_machine.close(force=force)
            self._worker = None
            self._transfer = None
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)

    def send(self, data: bytes) -> None:
        """提交一次完整写入。"""
        with self._lock:
            self._require_connected("send")
            self._require_no_transfer("send")
            worker = self._require_worker()
        worker.send(data)

    def set_line_state(self, *, dtr: bool, rts: bool) -> None:
        """修改已连接会话的 DTR/RTS 状态。"""
        with self._lock:
            self._require_connected("set_line_state")
            self._require_no_transfer("set_line_state")
            worker = self._require_worker()
        worker.set_line_state(dtr=dtr, rts=rts)

    def start_file_send(self, path: Path, *, block_size: int = BLOCK_SIZE_1024) -> None:
        """提交一次 Ymodem 单文件发送。"""
        if not isinstance(path, Path):
            raise ValidationError("文件路径必须是 pathlib.Path。")
        if block_size not in {128, BLOCK_SIZE_1024}:
            raise ValidationError("Ymodem 数据块只允许 128 或 1024 字节。")
        with self._lock:
            self._require_connected("start_file_send")
            self._require_no_transfer("start_file_send")
            self._validate_ymodem_config()
            worker = self._require_worker()
            size = self._file_size(path)
            transfer = FileTransferSnapshot(
                state=FileTransferState.WAITING,
                file_name=path.name or "file",
                file_size=size,
                transferred_bytes=0,
                block_number=0,
                retries=0,
                elapsed_seconds=0.0,
                message="等待接收端发起传输。",
            )
            self._transfer = transfer
            record = self._system_record(f"开始发送文件：{transfer.file_name}（{size} 字节）。")
            self._append_record_locked(record)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)
        try:
            worker.start_file_send(path, block_size=block_size)
        except DomainError:
            with self._lock:
                self._transfer = None
                snapshot = self._build_snapshot()
            self._notify_snapshot(snapshot)
            raise

    def cancel_file_send(self) -> bool:
        """请求取消当前 Ymodem 文件发送；没有传输时返回 False。"""
        with self._lock:
            worker = self._worker
        if worker is None:
            return False
        return worker.cancel_file_transfer()

    def on_file_transfer(self, transfer: FileTransferSnapshot) -> None:
        """处理 Worker 线程发布的文件发送进度或终态。"""
        terminal = transfer.state not in ACTIVE_FILE_TRANSFER_STATES
        record = None
        with self._lock:
            self._transfer = transfer
            if terminal:
                record = self._system_record(self._transfer_record_text(transfer))
                self._append_record_locked(record)
            snapshot = self._build_snapshot()
        if record is not None:
            self._notify_record(record)
        self._notify_snapshot(snapshot)

    def clear_buffer(self) -> int:
        """清空当前内存缓冲但不重置累计统计。"""
        with self._lock:
            removed = self._buffer.clear()
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)
        return removed

    def on_opened(self, config: SerialConfig) -> None:
        """处理串口打开成功事件。"""
        with self._lock:
            if self._state_machine.state is SessionState.CONNECTING:
                self._state_machine.mark_connected()
            self._start_log_locked()
            record = self._system_record("串口已连接。")
            self._append_record_locked(record)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)

    def on_open_failed(self, error: DomainError) -> None:
        """处理串口打开失败事件。"""
        with self._lock:
            if self._state_machine.state is SessionState.CONNECTING:
                self._state_machine.mark_connect_failed()
            self._last_error = error.to_user_facing()
            record = self._system_record(error.message)
            self._append_record_locked(record)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)

    def on_bytes_received(self, data: bytes) -> None:
        """处理接收数据。"""
        with self._lock:
            if self._state_machine.state is not SessionState.CONNECTED:
                return
            record = LogRecord.from_bytes(
                timestamp_utc=self._clock.now_utc(),
                direction="rx",
                raw=data,
                encoding=self._config.encoding,
                session_id=self._session_id,
            )
            self._append_record_locked(record)
            self._rx_bytes += len(data)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)

    def on_bytes_sent(self, data: bytes) -> None:
        """处理发送完成数据。"""
        with self._lock:
            record = LogRecord.from_bytes(
                timestamp_utc=self._clock.now_utc(),
                direction="tx",
                raw=data,
                encoding=self._config.encoding,
                session_id=self._session_id,
            )
            self._append_record_locked(record)
            self._tx_bytes += len(data)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)

    def on_io_error(self, error: DomainError) -> None:
        """记录 I/O 错误并启动安全断开。"""
        with self._lock:
            if self._last_error is None:
                self._last_error = error.to_user_facing()
            if self._state_machine.state is SessionState.CONNECTED:
                self._state_machine.begin_disconnect()
            record = self._system_record(error.message)
            self._append_record_locked(record)
            snapshot = self._build_snapshot()
        self._notify_record(record)
        self._notify_snapshot(snapshot)

    def on_stopped(self) -> None:
        """处理 Worker 已结束事件。"""
        with self._lock:
            state = self._state_machine.state
            if state is SessionState.CONNECTING:
                self._state_machine.mark_connect_failed()
                if self._last_error is None:
                    self._last_error = TransportIOError(
                        "串口线程在连接完成前结束。"
                    ).to_user_facing()
            elif state is SessionState.CONNECTED:
                self._state_machine.begin_disconnect()
                self._state_machine.mark_disconnected()
            elif state is SessionState.DISCONNECTING:
                self._state_machine.mark_disconnected()
            record = self._cancel_stale_transfer_locked()
            self._close_log_locked()
            self._worker = None
            snapshot = self._build_snapshot()
        if record is not None:
            self._notify_record(record)
        self._notify_snapshot(snapshot)

    def _append_record_locked(self, record: LogRecord) -> None:
        """同时写入内存缓冲和当前自动日志。"""
        self._buffer.append(record)
        service = self._log_service
        if service is None or not service.is_active:
            return
        try:
            service.write(record)
        except DomainError as exc:
            if self._last_error is None:
                self._last_error = exc.to_user_facing()

    def _start_log_locked(self) -> None:
        """按当前偏好启动本次连接的自动日志。"""
        service = self._log_service
        if service is None or not self._auto_log_enabled:
            return
        try:
            service.start(
                directory=self._auto_log_directory,
                port=self._config.port,
                log_format=self._auto_log_format,
            )
        except DomainError as exc:
            if self._last_error is None:
                self._last_error = exc.to_user_facing()

    def _close_log_locked(self) -> None:
        """安全关闭当前自动日志。"""
        service = self._log_service
        if service is None:
            return
        try:
            service.close()
        except DomainError as exc:
            if self._last_error is None:
                self._last_error = exc.to_user_facing()

    def _require_connected(self, action: str) -> None:
        if self._state_machine.state is not SessionState.CONNECTED:
            raise InvalidStateTransitionError(
                self._state_machine.state,
                action,
                SessionState.CONNECTED,
            )

    def _require_no_transfer(self, action: str) -> None:
        if self._transfer is not None and self._transfer.is_active:
            raise FileTransferError(f"Ymodem 文件传输进行中，暂不能执行 {action}。")

    def _require_worker(self) -> SerialWorker:
        worker = self._worker
        if worker is None or not worker.is_alive:
            raise TransportIOError("串口工作线程未运行。")
        return worker

    def _validate_ymodem_config(self) -> None:
        """Ymodem 是二进制协议，XON/XOFF 或非 8N1 参数会破坏数据。"""
        if (
            self._config.bytesize != YMODEM_REQUIRED_BYTESIZE
            or self._config.parity != "N"
            or self._config.flow_control == "xonxoff"
        ):
            raise FileTransferError(
                "Ymodem 需要 8 数据位、无校验，并关闭 XON/XOFF 流控。请调整串口参数并重新连接。"
            )

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            if not path.is_file():
                raise FileTransferError(f"无法发送文件：{path.name or path} 不是普通文件。")
            return path.stat().st_size
        except OSError as exc:
            raise FileTransferError(f"无法读取文件：{exc.strerror or exc}") from exc

    @staticmethod
    def _transfer_record_text(transfer: FileTransferSnapshot) -> str:
        if transfer.state is FileTransferState.SUCCEEDED:
            return (
                f"Ymodem 发送完成：{transfer.file_name}"
                f"（{transfer.file_size} 字节，用时 {transfer.elapsed_seconds:.1f} 秒）。"
            )
        if transfer.state is FileTransferState.FAILED:
            return f"Ymodem 发送失败：{transfer.message}"
        return f"Ymodem 发送已取消：{transfer.message}"

    def _cancel_stale_transfer_locked(self) -> LogRecord | None:
        """Worker 已退出时兜底结束仍显示为活动中的传输。"""
        if self._transfer is None or not self._transfer.is_active:
            return None
        message = "串口已断开，文件传输中止。"
        self._transfer = replace(
            self._transfer,
            state=FileTransferState.CANCELLED,
            message=message,
        )
        record = self._system_record(f"Ymodem 发送已取消：{message}")
        self._append_record_locked(record)
        return record

    def _transfer_join_timeout(self, fallback: float) -> float:
        """传输中按当前波特率放宽退出等待，覆盖正在写出的 1K 帧。"""
        transfer = self._transfer
        if transfer is None or not transfer.is_active:
            return fallback
        frame_seconds = (
            YMODEM_TRANSFER_FRAME_BYTES * YMODEM_FRAME_BITS_PER_BYTE / max(self._config.baudrate, 1)
        )
        write_timeout = max(MINIMUM_WRITE_TIMEOUT, frame_seconds * 2)
        # 当前帧写完后引擎还要尽力发一次 CAN 取消序列，两者都受写超时约束。
        return max(fallback, min(MAX_TRANSFER_JOIN_TIMEOUT, write_timeout * 2 + 0.5))

    def _build_snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self._session_id,
            title=self._title,
            state=self._state_machine.state,
            config=self._config,
            last_error=self._last_error,
            rx_bytes=self._rx_bytes,
            tx_bytes=self._tx_bytes,
            dropped_records=self._buffer.dropped_records,
            transfer=self._transfer,
        )

    def _system_record(self, message: str) -> LogRecord:
        raw = message.encode("utf-8")
        return LogRecord.from_bytes(
            timestamp_utc=self._clock.now_utc(),
            direction="system",
            raw=raw,
            encoding="utf-8",
            session_id=self._session_id,
        )

    def _notify_current_snapshot(self) -> None:
        with self._lock:
            snapshot = self._build_snapshot()
        self._notify_snapshot(snapshot)

    def _notify_record(self, record: LogRecord) -> None:
        if self._on_record is not None:
            self._on_record(record)

    def _notify_snapshot(self, snapshot: SessionSnapshot) -> None:
        if self._on_snapshot is not None:
            self._on_snapshot(snapshot)
