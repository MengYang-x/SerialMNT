"""串口会话与文件传输状态。"""

from enum import Enum


class SessionState(str, Enum):
    """会话状态机可处于的稳定状态。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    ERROR = "error"
    CLOSED = "closed"

    def __str__(self) -> str:
        """返回适合日志和展示的稳定字符串值。"""
        return self.value


class FileTransferState(str, Enum):
    """一次 Ymodem 文件发送所处的阶段。"""

    WAITING = "waiting"
    SENDING = "sending"
    FINISHING = "finishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        """返回适合日志和展示的稳定字符串值。"""
        return self.value


# 只有这些状态表示传输仍占用串口线程；终态用于向界面展示最后一次结果。
ACTIVE_FILE_TRANSFER_STATES = frozenset(
    {
        FileTransferState.WAITING,
        FileTransferState.SENDING,
        FileTransferState.FINISHING,
    }
)
