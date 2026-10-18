"""后台更新检查与 UI 线程结果投递。"""

import logging
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from serialmnt.application import UpdateCheckResult, UpdateService, UpdateStatus
from serialmnt.domain import ErrorCode

logger = logging.getLogger(__name__)

_GITHUB_RELEASE_HOSTS = frozenset({"github.com", "www.github.com"})


def is_allowed_release_url(url: str) -> bool:
    """返回发布页地址是否可以交给系统浏览器打开。

    更新说明来自外部服务，只允许打开 GitHub 的 HTTPS 页面，避免把
    ``file:``、``javascript:`` 或其他站点交给系统处理。
    """
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() in _GITHUB_RELEASE_HOSTS


class _TaskSignals(QObject):
    """任务结果信号载体，不依赖界面的生命周期。"""

    finished = Signal(object)


class _UpdateTask(QRunnable):
    """在线程池中执行一次同步检查。"""

    def __init__(self, service: UpdateService, signals: _TaskSignals) -> None:
        super().__init__()
        self._service = service
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._service.check()
        except Exception:
            # 后台任务顶层边界：未预期异常不能传播到 Qt 线程池。
            logger.exception("检查更新时发生未预期错误。")
            result = UpdateCheckResult(
                status=UpdateStatus.FAILED,
                current_version=self._service.current_version,
                error_code=ErrorCode.INTERNAL_ERROR,
                error_message="检查更新时发生未预期错误。",
            )
        self._signals.finished.emit(result)


class UpdateCheckRunner(QObject):
    """保证同一时刻最多一个更新检查任务，并在主线程发布结果。"""

    finished = Signal(object)

    def __init__(self, service: UpdateService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals: _TaskSignals | None = None
        self._is_running = False

    @property
    def is_running(self) -> bool:
        """返回当前是否有检查任务在执行。"""
        return self._is_running

    def start(self) -> bool:
        """启动一次检查；已有任务运行时返回 False。"""
        if self._is_running:
            return False
        self._is_running = True
        # 不设置父对象：窗口先销毁时，任务仍能安全持有并释放信号载体，
        # 接收方已销毁时 Qt 会自动断开连接。
        signals = _TaskSignals()
        signals.finished.connect(self._on_finished)
        self._signals = signals
        self._pool.start(_UpdateTask(self._service, signals))
        return True

    def _on_finished(self, result: object) -> None:
        self._is_running = False
        if self._signals is not None:
            self._signals.deleteLater()
            self._signals = None
        if isinstance(result, UpdateCheckResult):
            self.finished.emit(result)
