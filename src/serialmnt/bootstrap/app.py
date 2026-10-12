"""SerialMNT 应用启动和依赖组装。"""

import sys

from PySide6.QtWidgets import QApplication

from serialmnt import __version__
from serialmnt.application import SessionManager
from serialmnt.domain import AppConfig, LogFormat, LogSink
from serialmnt.infrastructure import (
    CsvLogSink,
    JsonConfigStore,
    SerialPortScanner,
    SerialTransport,
    SystemClock,
    TxtLogSink,
)
from serialmnt.ui.main_window import MainWindow


def create_log_sink(log_format: LogFormat) -> LogSink:
    """按用户选择创建 CSV 或 TXT 日志输出。"""
    if log_format == "csv":
        return CsvLogSink()
    return TxtLogSink()


def build_main_window(
    *,
    config_store: JsonConfigStore | None = None,
    initial_config: AppConfig | None = None,
) -> MainWindow:
    """组装主窗口所需的真实实现。"""
    store = config_store or JsonConfigStore()
    config = initial_config if initial_config is not None else store.load()
    manager = SessionManager(
        transport_factory=SerialTransport,
        clock=SystemClock(),
        log_sink_factory=create_log_sink,
    )
    scanner = SerialPortScanner()
    return MainWindow(
        session_manager=manager,
        port_provider=scanner.list_ports,
        config_store=store,
        initial_config=config,
    )


def main() -> int:
    """启动 Qt 应用并返回退出码。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName("SerialMNT")
    app.setApplicationVersion(__version__)
    window = build_main_window()
    window.show()
    return app.exec()
