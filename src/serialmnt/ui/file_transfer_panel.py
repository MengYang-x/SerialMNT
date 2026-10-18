"""Ymodem 文件发送面板。"""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from serialmnt.application import SessionController
from serialmnt.application.ymodem import BLOCK_SIZE_128, BLOCK_SIZE_1024
from serialmnt.domain import (
    DomainError,
    FileTransferSnapshot,
    FileTransferState,
    SessionSnapshot,
    SessionState,
)
from serialmnt.ui.theme_manager import data_colors, highlight_colors

_BROWSE_BUTTON_SIZE = 26
_PERCENT_LABEL_WIDTH = 38
_PROGRESS_BAR_HEIGHT = 12
_KIB = 1024
_MIB = 1024 * 1024


def _format_bytes(size: float) -> str:
    """把字节数格式化为易读的十进制单位。"""
    if size < _KIB:
        return f"{int(size)} B"
    if size < _MIB:
        return f"{size / _KIB:.1f} KB"
    return f"{size / _MIB:.2f} MB"


class FileTransferPanel(QGroupBox):
    """选择文件、显示 Ymodem 发送进度并请求取消。"""

    def __init__(
        self,
        *,
        controller: SessionController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Ymodem 发送", parent)
        self._controller = controller
        self._path: Path | None = None
        self._selected_size: int | None = None
        self._last_directory = str(Path.home())
        self._cancel_requested = False
        self._connected = False
        self._last_state: FileTransferState | None = None
        self._metrics_text = ""
        self._build_ui()
        self._connect_changes()
        self.apply_snapshot(controller.snapshot)

    def _build_ui(self) -> None:
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("未选择文件")
        self.browse_button = QToolButton()
        self.browse_button.setText("…")
        self.browse_button.setToolTip("选择要发送的文件")
        self.browse_button.setFixedSize(_BROWSE_BUTTON_SIZE, _BROWSE_BUTTON_SIZE)
        self.block_check = QCheckBox("1K 块")
        self.block_check.setChecked(True)
        self.block_check.setToolTip(
            "使用 1024 字节数据块；取消后使用 128 字节块，兼容只支持 128 字节的接收端"
        )
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(_PROGRESS_BAR_HEIGHT)
        self.percent_label = QLabel("0%")
        self.percent_label.setFixedWidth(_PERCENT_LABEL_WIDTH)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.phase_label = QLabel()
        self.metrics_label = QLabel()
        self.metrics_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.metrics_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.start_button = QPushButton("发送文件")
        self.start_button.setProperty("primary", True)
        self.start_button.setToolTip("开始 Ymodem 发送；传输期间普通发送会被暂停")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setToolTip("中止本次传输，并向接收端发送取消序列")

        layout = QGridLayout(self)
        # QSS 的 QGroupBox padding 已提供内边距，布局默认外边距会再叠加 9px，这里归零。
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        # 文件行独占整行：标签从内容左边缘开始，不再与“1K 块”共用列而留下空白。
        file_row = QHBoxLayout()
        file_row.setSpacing(4)
        file_row.addWidget(QLabel("文件"))
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.browse_button)
        layout.addLayout(file_row, 0, 0, 1, 3)
        layout.addWidget(self.block_check, 1, 0)
        layout.addWidget(self.progress_bar, 1, 1)
        layout.addWidget(self.percent_label, 1, 2)
        layout.addWidget(self.phase_label, 2, 0, 1, 3)
        layout.addWidget(self.metrics_label, 3, 0, 1, 3)
        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        action_row.addWidget(self.start_button, 3)
        action_row.addWidget(self.cancel_button, 2)
        layout.addLayout(action_row, 4, 0, 1, 3)
        layout.setColumnStretch(1, 1)

    def _connect_changes(self) -> None:
        self.browse_button.clicked.connect(self._browse)
        self.start_button.clicked.connect(self._start)
        self.cancel_button.clicked.connect(self._cancel)

    @property
    def file_path(self) -> Path | None:
        """返回当前选择的文件。"""
        return self._path

    @property
    def block_size(self) -> int:
        """返回本次发送使用的数据块大小。"""
        return BLOCK_SIZE_1024 if self.block_check.isChecked() else BLOCK_SIZE_128

    def set_file_path(self, path: Path) -> None:
        """设置待发送文件，供文件对话框和测试复用。"""
        self._path = path
        self._last_directory = str(path.parent)
        try:
            self._selected_size = path.stat().st_size
        except OSError:
            self._selected_size = None
        self.file_edit.setText(str(path))
        self.file_edit.setCursorPosition(0)
        self.file_edit.setToolTip(str(path))
        self._refresh_controls()
        transfer = self._controller.transfer
        if transfer is not None and not transfer.is_active:
            # 选择新文件后不再展示上一次传输的结果。
            self._render_transfer(None)
        else:
            self._render_transfer(transfer)

    def _browse(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择要发送的文件",
            self._last_directory,
            "所有文件 (*)",
        )
        if selected:
            self.set_file_path(Path(selected))

    def _start(self) -> None:
        if self._path is None:
            return
        # 先本地点掉按钮，避免信号排队期间重复点击启动第二次传输。
        self.start_button.setEnabled(False)
        try:
            self._controller.start_file_send(self._path, block_size=self.block_size)
        except DomainError as exc:
            QMessageBox.warning(self, "无法发送文件", exc.message)
            self._refresh_controls()

    def _cancel(self) -> None:
        if not self._controller.cancel_file_send():
            return
        self._cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("正在取消…")

    def apply_snapshot(self, snapshot: SessionSnapshot) -> None:
        """按会话快照刷新按钮、进度和状态文本。"""
        transfer = snapshot.transfer
        connected = snapshot.state is SessionState.CONNECTED
        self._connected = connected
        active = transfer is not None and transfer.is_active
        if not active:
            self._cancel_requested = False
        self.file_edit.setEnabled(not active)
        self.browse_button.setEnabled(not active)
        self.block_check.setEnabled(not active)
        self.start_button.setEnabled(connected and not active and self._path is not None)
        self.cancel_button.setEnabled(active and not self._cancel_requested)
        self.cancel_button.setText("正在取消…" if self._cancel_requested else "取消")
        self._render_transfer(transfer)

    def refresh_theme(self) -> None:
        """主题切换后重新应用阶段文字颜色。"""
        self._apply_phase_color(self._last_state)

    def _refresh_controls(self) -> None:
        transfer = self._controller.transfer
        active = transfer is not None and transfer.is_active
        self.start_button.setEnabled(self._connected and not active and self._path is not None)

    def _render_transfer(self, transfer: FileTransferSnapshot | None) -> None:
        percent, phase, metrics = self._transfer_text(transfer)
        self._last_state = transfer.state if transfer is not None else None
        self.progress_bar.setValue(percent)
        self.percent_label.setText(f"{percent}%")
        self.phase_label.setText(phase)
        self._set_metrics(metrics)
        self._apply_phase_color(self._last_state)

    def _transfer_text(self, transfer: FileTransferSnapshot | None) -> tuple[int, str, str]:
        if transfer is None:
            if self._path is None:
                return 0, "未选择文件", "选择文件并连接串口后即可发送"
            size = self._selected_size_text()
            if self._connected:
                return 0, "就绪", f"已选择：{self._path.name}（{size}）"
            return 0, "就绪", f"已选择：{self._path.name}（{size}），请先连接串口"

        percent = self._percent(transfer)
        speed = self._speed(transfer)
        retries = f" · 重试 {transfer.retries}" if transfer.retries else ""
        if transfer.state is FileTransferState.WAITING:
            phase, metrics = "等待接收端 C…", "请让设备进入 Ymodem 接收模式"
        elif transfer.state is FileTransferState.SENDING:
            phase = "发送中"
            metrics = f"块 {transfer.block_number} · {percent}%{retries}" + (
                f" · {speed}" if speed else ""
            )
        elif transfer.state is FileTransferState.FINISHING:
            phase = "完成中…"
            metrics = f"块 {transfer.block_number} · 等待接收端确认结束"
        elif transfer.state is FileTransferState.SUCCEEDED:
            percent = 100
            phase = "发送完成"
            metrics = (
                f"{_format_bytes(transfer.file_size)} · 用时 "
                f"{transfer.elapsed_seconds:.1f} s" + (f" · 平均 {speed}" if speed else "")
            )
        elif transfer.state is FileTransferState.FAILED:
            phase, metrics = "发送失败", transfer.message
        else:
            phase = "已取消"
            metrics = (
                f"已发送 {_format_bytes(transfer.transferred_bytes)} / "
                f"{_format_bytes(transfer.file_size)}"
            )
        return percent, phase, metrics

    def _selected_size_text(self) -> str:
        """返回选择文件时缓存的大小，避免每个快照都在 UI 线程 stat 文件。"""
        if self._selected_size is None:
            return "?"
        return _format_bytes(self._selected_size)

    @staticmethod
    def _percent(transfer: FileTransferSnapshot) -> int:
        if transfer.state is FileTransferState.SUCCEEDED:
            return 100
        if transfer.file_size <= 0:
            return 0
        percent = transfer.transferred_bytes * 100 // transfer.file_size
        return min(percent, 99) if transfer.is_active else percent

    @staticmethod
    def _speed(transfer: FileTransferSnapshot) -> str:
        if transfer.elapsed_seconds <= 0 or transfer.transferred_bytes <= 0:
            return ""
        return f"{_format_bytes(transfer.transferred_bytes / transfer.elapsed_seconds)}/s"

    def _apply_phase_color(self, state: FileTransferState | None) -> None:
        color = ""
        if state is FileTransferState.FAILED:
            color = highlight_colors().error
        elif state is FileTransferState.SUCCEEDED:
            color = data_colors().rx
        self.phase_label.setStyleSheet(f"color: {color};" if color else "")

    def _set_metrics(self, text: str) -> None:
        """保存完整说明，按可用宽度做中间省略并保留 tooltip。"""
        self._metrics_text = text
        self.metrics_label.setToolTip(text)
        self._apply_metrics_elide()

    def _apply_metrics_elide(self) -> None:
        width = self.metrics_label.width()
        if width <= 0:
            elided = self._metrics_text
        else:
            elided = self.metrics_label.fontMetrics().elidedText(
                self._metrics_text,
                Qt.TextElideMode.ElideMiddle,
                width,
            )
        if elided != self.metrics_label.text():
            self.metrics_label.setText(elided)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_metrics_elide()
