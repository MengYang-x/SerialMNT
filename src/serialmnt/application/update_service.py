"""更新检查用例：把外部版本信息转换为界面可直接消费的结果。"""

from dataclasses import dataclass
from enum import Enum

from serialmnt.domain import (
    ErrorCode,
    ReleaseInfo,
    ReleaseSource,
    UpdateCheckError,
    is_newer_version,
    parse_version,
)


class UpdateStatus(str, Enum):
    """一次更新检查的结果状态。"""

    UPDATE_AVAILABLE = "update_available"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"

    def __str__(self) -> str:
        """返回适合日志和展示的稳定字符串值。"""
        return self.value


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """供 UI 线程消费的不可变更新检查结果。"""

    status: UpdateStatus
    current_version: str
    release: ReleaseInfo | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, UpdateStatus):
            raise ValueError("status 必须是 UpdateStatus。")
        parse_version(self.current_version)
        if self.status is UpdateStatus.UPDATE_AVAILABLE:
            if self.release is None:
                raise ValueError("有新版本时必须提供 ReleaseInfo。")
        elif self.release is not None:
            raise ValueError("只有新版本可用时才能提供 ReleaseInfo。")
        if self.status is UpdateStatus.FAILED:
            if self.error_code is None or not self.error_message:
                raise ValueError("检查失败时必须提供错误代码和说明。")
        elif self.error_code is not None or self.error_message is not None:
            raise ValueError("只有检查失败时才能提供错误信息。")


class UpdateService:
    """查询最新版本并比较当前版本。

    ``check()`` 是同步阻塞调用，UI 层必须在线程池中执行；该服务只依赖
    ``ReleaseSource`` 协议，不接触 Qt 或具体网络实现。
    """

    def __init__(self, source: ReleaseSource, *, current_version: str) -> None:
        parse_version(current_version)
        self._source = source
        self._current_version = current_version

    @property
    def current_version(self) -> str:
        """返回当前应用版本。"""
        return self._current_version

    def check(self) -> UpdateCheckResult:
        """执行一次检查；可预期的获取失败转换为 FAILED 结果。"""
        try:
            release = self._source.fetch_latest()
        except UpdateCheckError as exc:
            return UpdateCheckResult(
                status=UpdateStatus.FAILED,
                current_version=self._current_version,
                error_code=exc.code,
                error_message=exc.message,
            )
        if is_newer_version(self._current_version, release.version):
            return UpdateCheckResult(
                status=UpdateStatus.UPDATE_AVAILABLE,
                current_version=self._current_version,
                release=release,
            )
        return UpdateCheckResult(
            status=UpdateStatus.UP_TO_DATE,
            current_version=self._current_version,
        )
