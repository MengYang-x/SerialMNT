"""测试更新检查后台任务与主窗口交互。"""

import logging
import threading

import pytest
from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication
from tests.fixtures.fakes import FakeClock, FakeTransport

from serialmnt.application import (
    SessionManager,
    UpdateCheckResult,
    UpdateService,
    UpdateStatus,
)
from serialmnt.domain import ErrorCode, PortInfo, ReleaseInfo, UpdateCheckError
from serialmnt.ui import MainWindow, UpdateCheckRunner
from serialmnt.ui.update_check import is_allowed_release_url


def _release(version: str = "0.3.0") -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        release_url=f"https://github.com/MengYang-x/SerialMNT/releases/tag/v{version}",
    )


class _ImmediateSource:
    def __init__(
        self,
        release: ReleaseInfo | None = None,
        error: UpdateCheckError | None = None,
    ) -> None:
        self.release = release
        self.error = error

    def fetch_latest(self) -> ReleaseInfo:
        if self.error is not None:
            raise self.error
        assert self.release is not None
        return self.release


def _service(source: object) -> UpdateService:
    return UpdateService(source, current_version="0.2.3")  # type: ignore[arg-type]


def _window(qtbot: object, source: object) -> MainWindow:
    manager = SessionManager(
        transport_factory=FakeTransport,
        clock=FakeClock(),
    )
    window = MainWindow(
        session_manager=manager,
        port_provider=lambda: (PortInfo(device="COM1"),),
    )
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.show()
    window.configure_update_service(_service(source))
    # 测试直接调用自动/手动入口，避免等待真实的 3 秒启动延迟。
    window._update_timer.stop()
    return window


def test_runner_emits_result_from_worker_thread(qtbot: object) -> None:
    class _ThreadSource:
        def __init__(self) -> None:
            self.thread: QThread | None = None

        def fetch_latest(self) -> ReleaseInfo:
            self.thread = QThread.currentThread()
            return _release()

    source = _ThreadSource()
    runner = UpdateCheckRunner(_service(source))
    with qtbot.waitSignal(runner.finished, timeout=2000) as blocker:  # type: ignore[attr-defined]
        assert runner.start() is True

    result = blocker.args[0]
    assert isinstance(result, UpdateCheckResult)
    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    app = QApplication.instance()
    assert app is not None
    assert source.thread is not app.thread()


def test_runner_rejects_second_start_while_busy(qtbot: object) -> None:
    class _BlockingSource:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def fetch_latest(self) -> ReleaseInfo:
            self.started.set()
            assert self.release.wait(2)
            return _release()

    source = _BlockingSource()
    runner = UpdateCheckRunner(_service(source))
    assert runner.start() is True
    assert source.started.wait(2)
    assert runner.is_running is True
    assert runner.start() is False
    with qtbot.waitSignal(runner.finished, timeout=2000):  # type: ignore[attr-defined]
        source.release.set()
    assert runner.is_running is False


def test_runner_converts_unexpected_exception(
    qtbot: object, caplog: pytest.LogCaptureFixture
) -> None:
    class _BrokenSource:
        def fetch_latest(self) -> ReleaseInfo:
            raise RuntimeError("boom")

    runner = UpdateCheckRunner(_service(_BrokenSource()))
    with (
        caplog.at_level(logging.ERROR),
        qtbot.waitSignal(runner.finished, timeout=2000) as blocker,  # type: ignore[attr-defined]
    ):
        assert runner.start() is True

    result = blocker.args[0]
    assert result.status is UpdateStatus.FAILED
    assert result.error_code is ErrorCode.INTERNAL_ERROR
    assert "检查更新时发生未预期错误" in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0", True),
        ("https://WWW.GITHUB.COM/MengYang-x/SerialMNT", True),
        ("http://github.com/MengYang-x/SerialMNT", False),
        ("https://example.com/release", False),
        ("https://evil@github.com/release", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
    ],
)
def test_is_allowed_release_url(url: str, expected: bool) -> None:
    assert is_allowed_release_url(url) is expected


def test_window_without_service_keeps_check_action_disabled(qtbot: object) -> None:
    manager = SessionManager(
        transport_factory=FakeTransport,
        clock=FakeClock(),
    )
    window = MainWindow(
        session_manager=manager,
        port_provider=lambda: (PortInfo(device="COM1"),),
    )
    qtbot.addWidget(window)  # type: ignore[attr-defined]

    assert window.check_update_action.isEnabled() is False
    assert window._update_timer.isActive() is False


def test_startup_timer_triggers_single_check(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager(
        transport_factory=FakeTransport,
        clock=FakeClock(),
    )
    window = MainWindow(
        session_manager=manager,
        port_provider=lambda: (PortInfo(device="COM1"),),
    )
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    window.show()
    presented: list[UpdateCheckResult] = []
    monkeypatch.setattr(window, "_present_update", presented.append)

    window.configure_update_service(_service(_ImmediateSource(_release())))
    # setInterval() 会按新间隔重启正在运行的定时器，测试无需等待 3 秒。
    window._update_timer.setInterval(10)
    qtbot.waitUntil(lambda: bool(presented), timeout=2000)  # type: ignore[attr-defined]

    assert presented[0].status is UpdateStatus.UPDATE_AVAILABLE
    assert window._update_timer.isActive() is False


def test_update_result_is_ignored_while_closing(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, _ImmediateSource(_release()))
    presented: list[UpdateCheckResult] = []
    monkeypatch.setattr(window, "_present_update", presented.append)
    result = UpdateCheckResult(
        status=UpdateStatus.UPDATE_AVAILABLE,
        current_version="0.2.3",
        release=_release(),
    )

    window._is_closing = True
    window._on_update_checked(result)

    assert presented == []


def test_startup_check_presents_new_version(qtbot: object, monkeypatch: pytest.MonkeyPatch) -> None:
    window = _window(qtbot, _ImmediateSource(_release()))
    presented: list[UpdateCheckResult] = []
    monkeypatch.setattr(window, "_present_update", presented.append)

    window._auto_check_updates()
    qtbot.waitUntil(lambda: bool(presented), timeout=2000)  # type: ignore[attr-defined]

    assert presented[0].status is UpdateStatus.UPDATE_AVAILABLE
    assert presented[0].release is not None
    assert presented[0].release.version == "0.3.0"
    assert window.check_update_action.isEnabled() is True


def test_startup_check_is_silent_when_up_to_date(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, _ImmediateSource(_release("0.2.3")))
    presented: list[UpdateCheckResult] = []
    monkeypatch.setattr(window, "_present_update", presented.append)

    window._auto_check_updates()
    runner = window._update_runner
    assert runner is not None
    qtbot.waitUntil(lambda: not runner.is_running, timeout=2000)  # type: ignore[attr-defined]

    assert presented == []
    assert window.statusBar().currentMessage() == ""


def test_manual_check_reports_up_to_date(qtbot: object) -> None:
    window = _window(qtbot, _ImmediateSource(_release("0.2.3")))

    window._manual_check_updates()
    qtbot.waitUntil(  # type: ignore[attr-defined]
        lambda: "当前已是最新版本 0.2.3" in window.statusBar().currentMessage(),
        timeout=2000,
    )


def test_manual_check_reports_failure(qtbot: object) -> None:
    window = _window(qtbot, _ImmediateSource(error=UpdateCheckError("离线")))

    window._manual_check_updates()
    qtbot.waitUntil(  # type: ignore[attr-defined]
        lambda: "检查更新失败" in window.statusBar().currentMessage(),
        timeout=2000,
    )


def test_update_dialog_uses_plain_text_and_shows_versions(qtbot: object) -> None:
    window = _window(qtbot, _ImmediateSource(_release()))
    result = UpdateCheckResult(
        status=UpdateStatus.UPDATE_AVAILABLE,
        current_version="0.2.3",
        release=ReleaseInfo(
            version="0.3.0",
            release_url="https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0",
            notes="<b>不要渲染</b>",
        ),
    )

    box, download_button = window._build_update_dialog(result)
    assert box.windowTitle() == "发现新版本"
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert "当前版本：0.2.3" in box.text()
    assert "最新版本：0.3.0" in box.text()
    assert box.detailedText() == "<b>不要渲染</b>"
    assert download_button.text() == "前往下载"
    box.deleteLater()


def test_open_release_page_uses_desktop_services(
    qtbot: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _window(qtbot, _ImmediateSource(_release()))
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toString()) or True),
    )

    window._open_release_page("https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0")

    assert opened == ["https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0"]
