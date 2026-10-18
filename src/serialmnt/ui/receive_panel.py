"""接收数据显示面板。"""

from collections.abc import Callable, Sequence

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from serialmnt.domain import (
    DEFAULT_LOG_LEVEL_RULES,
    DisplayMode,
    LogLevelRule,
    LogRecord,
    SessionPreferences,
    TerminalScreen,
    match_log_level,
)
from serialmnt.ui.find_bar import FindBar
from serialmnt.ui.no_wheel_combo import NoWheelComboBox
from serialmnt.ui.terminal_output import TerminalOutput
from serialmnt.ui.theme_manager import CONTROL_HEIGHT_PX, data_colors, data_font, log_level_color

# 内存缓冲仍保留 100,000 条，界面只渲染最近若干条，避免整表重插阻塞主线程。
_MAX_RENDERED_RECORDS = 2000
_DOCUMENT_BLOCK_LIMIT = _MAX_RENDERED_RECORDS + 3

# “接收”标题与格式下拉框之间的固定间隔。
_HEADING_GAP = 12

# 行号按位数右对齐，后接灰色竖线与正文分隔。
_LINE_NUMBER_WIDTH = 6

# 与 splitlines() 一致的行分隔字符，用于判断下一片段是否从新行开始。
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


class ReceivePanel(QWidget):
    """显示当前会话的内存缓冲内容，并在终端模式承接内联输入。"""

    preferences_changed = Signal()

    def __init__(
        self,
        *,
        records_provider: Callable[[], tuple[LogRecord, ...]],
        clear_callback: Callable[[], int],
        preferences: SessionPreferences | None = None,
        log_level_rules: tuple[LogLevelRule, ...] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._records_provider = records_provider
        self._clear_callback = clear_callback
        self._log_level_rules = (
            DEFAULT_LOG_LEVEL_RULES if log_level_rules is None else tuple(log_level_rules)
        )
        self._paused = False
        self._terminal_mode = False
        self._screen: TerminalScreen | None = None
        self._line_number = 0
        self._loading = False
        self._build_ui()
        self._connect_changes()
        if preferences is not None:
            self.apply_preferences(preferences)
        self.render_records()

    def _build_ui(self) -> None:
        header = QHBoxLayout()
        header.setSpacing(6)
        self._header_layout = header
        # 标题行控件必须在创建时就带上父对象：header 是尚未挂载的自由布局，
        # 此时控件还没有父对象，而 Qt 对无父控件调用 setVisible(True) 等同于
        # 弹出一个顶层窗口，会在标题行挂到面板之前闪出小窗。
        heading = QLabel("接收", self)
        heading.setObjectName("panelHeading")
        heading_font = heading.font()
        heading_font.setBold(True)
        heading.setFont(heading_font)
        self.mode_combo = NoWheelComboBox(self)
        self.mode_combo.addItem("文本", "text")
        self.mode_combo.addItem("HEX", "hex")
        self.timestamp_check = QCheckBox("时间戳", self)
        self.timestamp_check.setChecked(True)
        self.line_number_check = QCheckBox("行号", self)
        self.line_number_check.setChecked(True)
        self.pause_button = QPushButton("暂停", self)
        self.pause_button.setCheckable(True)
        self.clear_button = QPushButton("清屏", self)
        self.autoscroll_check = QCheckBox("自动滚动", self)
        self.autoscroll_check.setChecked(True)
        self.highlight_check = QCheckBox("高亮", self)
        self.highlight_check.setChecked(True)
        self.highlight_check.setVisible(False)
        self.wrap_check = QCheckBox("自动换行", self)
        self.wrap_check.setChecked(True)

        self._assemble_header_row(header, heading)

        self.output = TerminalOutput()
        self.output.setPlaceholderText("暂无数据")
        self.output.set_log_level_rules(self._log_level_rules)
        self.output.setFont(data_font())
        self.output.set_wrap_enabled(self.wrap_check.isChecked())
        self.output.document().setMaximumBlockCount(_DOCUMENT_BLOCK_LIMIT)

        self.find_bar = FindBar(self.output, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addLayout(self._display_layout)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.output)

        self.find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.find_shortcut.activated.connect(self._open_find_bar)
        self.find_bar.closed.connect(self._find_bar_closed)

    def _assemble_header_row(self, header: QHBoxLayout, heading: QLabel) -> None:
        """装配两行标题：首行为格式与操作，次行集中显示选项。"""
        header.addWidget(heading)
        header.addSpacing(_HEADING_GAP)
        header.addWidget(self.mode_combo)
        header.addStretch(1)
        header.addWidget(self.pause_button)
        header.addWidget(self.clear_button)
        self._display_layout = QHBoxLayout()
        self._display_layout.setSpacing(6)
        self._display_layout.addWidget(self.timestamp_check)
        self._display_layout.addWidget(self.line_number_check)
        self._display_layout.addWidget(self.autoscroll_check)
        self._display_layout.addWidget(self.wrap_check)
        self._display_layout.addWidget(self.highlight_check)
        self._display_layout.addStretch(1)
        for widget in (
            self.mode_combo,
            self.timestamp_check,
            self.line_number_check,
            self.autoscroll_check,
            self.wrap_check,
            self.highlight_check,
            self.pause_button,
            self.clear_button,
        ):
            widget.setFixedHeight(CONTROL_HEIGHT_PX)

    def add_leading_header_widget(self, widget: QWidget) -> None:
        """在接收标题行最左侧插入控件，例如侧栏折叠按钮。"""
        self._header_layout.insertWidget(0, widget)
        widget.setFixedHeight(CONTROL_HEIGHT_PX)

    def add_trailing_header_widget(self, widget: QWidget) -> None:
        """在接收标题行末尾插入控件，例如视图模式切换按钮。"""
        self._header_layout.addWidget(widget)
        widget.setFixedHeight(CONTROL_HEIGHT_PX)

    def set_terminal_mode(self, enabled: bool) -> None:
        """启用原始终端显示和日志区内联输入。"""
        self._terminal_mode = enabled
        for widget in (self.timestamp_check, self.line_number_check):
            widget.setVisible(not enabled)
        self.highlight_check.setVisible(enabled)
        self.mode_combo.setVisible(not enabled)
        # 终端视图的块状光标会盖住空态文字，切回分栏时再恢复提示。
        self.output.setPlaceholderText("" if enabled else "暂无数据")
        if not enabled:
            self._screen = None
        self.output.set_terminal_mode(enabled)
        self.output.set_highlight_enabled(self.highlight_check.isChecked())
        self.render_records()

    def set_line_submit_handler(self, handler: Callable[[str], bool]) -> None:
        """设置终端行提交回调。"""
        self.output.set_submit_handler(handler)

    def set_history_provider(self, provider: Callable[[], tuple[str, ...]]) -> None:
        """设置终端输入历史提供者。"""
        self.output.set_history_provider(provider)

    def set_terminal_draft(self, text: str) -> None:
        """替换终端输入草稿。"""
        self.output.set_draft(text)

    @property
    def terminal_draft(self) -> str:
        """返回终端输入草稿。"""
        return self.output.draft

    def focus_terminal_input(self) -> None:
        """聚焦终端输入行。"""
        self.output.focus_input()

    def _connect_changes(self) -> None:
        self.mode_combo.currentIndexChanged.connect(self._settings_changed)
        self.timestamp_check.toggled.connect(self._settings_changed)
        self.line_number_check.toggled.connect(self._settings_changed)
        self.autoscroll_check.toggled.connect(self._settings_changed)
        self.highlight_check.toggled.connect(self._settings_changed)
        self.wrap_check.toggled.connect(self._settings_changed)
        self.pause_button.toggled.connect(self._pause_changed)
        self.clear_button.clicked.connect(self._clear)

    def apply_preferences(self, preferences: SessionPreferences) -> None:
        """应用持久化的显示偏好。"""
        self._loading = True
        try:
            with (
                QSignalBlocker(self.mode_combo),
                QSignalBlocker(self.timestamp_check),
                QSignalBlocker(self.line_number_check),
                QSignalBlocker(self.autoscroll_check),
                QSignalBlocker(self.highlight_check),
                QSignalBlocker(self.wrap_check),
            ):
                index = self.mode_combo.findData(preferences.display_mode)
                self.mode_combo.setCurrentIndex(max(index, 0))
                self.timestamp_check.setChecked(preferences.show_timestamp)
                self.line_number_check.setChecked(preferences.show_line_number)
                self.autoscroll_check.setChecked(preferences.autoscroll)
                self.highlight_check.setChecked(preferences.highlight_enabled)
                self.wrap_check.setChecked(preferences.wrap_enabled)
        finally:
            self._loading = False
        self.output.set_autoscroll(self.autoscroll_check.isChecked())
        self.output.set_highlight_enabled(self.highlight_check.isChecked())
        self.output.set_wrap_enabled(self.wrap_check.isChecked())
        self.render_records()

    @property
    def display_mode(self) -> DisplayMode:
        """返回当前文本或 HEX 显示模式。"""
        return self.mode_combo.currentData()  # type: ignore[return-value]

    def append_record(self, record: LogRecord) -> None:
        """在未暂停时追加一条记录。"""
        if self._terminal_mode:
            if record.direction == "rx":
                screen = self._terminal_screen()
                screen.feed(record.text)
                if not self._paused:
                    self.output.render_screen(screen)
            return
        if self._paused:
            return
        self.output.set_autoscroll(self.autoscroll_check.isChecked())
        self._insert_record(record)

    def render_records(self) -> None:
        """重新渲染最近的记录，超出渲染上限的部分用提示行说明。"""
        if self._paused:
            return
        # 查找选区会让终端视图延迟重建，重建前先清除，重建后再重新定位匹配。
        self.find_bar.suspend()
        records = self._records_provider()
        hidden = max(len(records) - _MAX_RENDERED_RECORDS, 0)
        self._line_number = 0
        self.output.set_autoscroll(self.autoscroll_check.isChecked())
        if self._terminal_mode:
            self.output.render_screen(self._terminal_screen())
            self.find_bar.refresh()
            return
        # 分栏视图的重绘必须从空文档开始，否则设置变更、主题切换和规则更新都会
        # 把同一批记录重复追加到文档末尾。
        self.output.clear_content()
        self.output.begin_batch_update()
        if hidden:
            self._insert_hint(f"… 已隐藏较早的 {hidden} 条记录，导出可获取完整缓冲")
        for record in records[hidden:]:
            self._insert_record(record)
        self.output.end_batch_update()
        self.find_bar.refresh()

    def _terminal_screen(self) -> TerminalScreen:
        """返回终端模型；首次进入终端视图时用最近记录重建。"""
        if self._screen is None:
            self._screen = TerminalScreen()
            records = self._records_provider()
            hidden = max(len(records) - _MAX_RENDERED_RECORDS, 0)
            for record in records[hidden:]:
                if record.direction == "rx":
                    self._screen.feed(record.text)
        return self._screen

    def set_data_font_size(self, size: int) -> None:
        """按全局字号更新接收区字体。"""
        self.output.setFont(data_font(size))

    def set_log_level_rules(self, rules: Sequence[LogLevelRule]) -> None:
        """应用新的日志级别规则并重绘当前内容。"""
        self._log_level_rules = tuple(rules)
        self.output.set_log_level_rules(self._log_level_rules)
        self.render_records()

    def refresh_theme(self) -> None:
        """主题切换后按新配色重新渲染。"""
        self.render_records()

    def _insert_hint(self, text: str) -> None:
        """插入一条灰色提示行，不计入记录。"""
        self.output.append_text(text, data_colors().system)

    def _insert_record(self, record: LogRecord) -> None:
        self.output.append_segments(self._record_segments(record))

    def _record_segments(self, record: LogRecord) -> tuple[tuple[str, str, bool], ...]:
        """拆分一行记录：时间戳弱化、方向前缀着色加粗、正文逐行取色。"""
        colors = data_colors()
        if record.direction not in ("rx", "tx"):
            segments: list[tuple[str, str, bool]] = [
                (self._format_record(record), colors.system, False)
            ]
        else:
            segments = []
            if self.timestamp_check.isChecked():
                segments.append((self._timestamp_prefix(record), colors.system, False))
            segments.append((f"{record.direction.upper()}: ", self._color_for(record), True))
            segments.extend(self._payload_segments(record))
        return self._numbered_segments(segments)

    def _numbered_segments(
        self, segments: Sequence[tuple[str, str, bool]]
    ) -> tuple[tuple[str, str, bool], ...]:
        """在每个显示行前插入行号前缀；未开启行号时原样返回且不消耗计数。

        序号只属于当前渲染窗口：追加记录时递增，重绘或清屏后从 1 重新开始。
        正文里的换行会让一条记录占据多行，因此按物理行逐行编号。
        """
        if not self.line_number_check.isChecked():
            return tuple(segments)
        number_color = data_colors().system
        numbered: list[tuple[str, str, bool]] = []
        at_line_start = True
        for text, color, bold in segments:
            for line in text.splitlines(keepends=True):
                if at_line_start:
                    self._line_number += 1
                    prefix = f"{self._line_number:>{_LINE_NUMBER_WIDTH}} │ "
                    numbered.append((prefix, number_color, False))
                    at_line_start = False
                numbered.append((line, color, bold))
                at_line_start = line[-1] in _LINE_BREAKS
        return tuple(numbered)

    def _payload_segments(self, record: LogRecord) -> tuple[tuple[str, str, bool], ...]:
        """拆分正文片段。

        一次串口读取可能包含多行日志，文本模式下必须逐行匹配级别，
        否则同批次里的 Error 会把 Warning、Pass 等行一起染成红色。
        """
        if self.display_mode == "hex" or record.direction != "rx":
            return ((self._payload_text(record), self._payload_color(record), False),)
        text_color = data_colors().text
        return tuple(
            (line, self._line_color(line, text_color), False)
            for line in record.text.splitlines(keepends=True)
        )

    def _line_color(self, line: str, text_color: str) -> str:
        rule = match_log_level(line, self._log_level_rules)
        return text_color if rule is None else log_level_color(rule.color)

    def _payload_color(self, record: LogRecord) -> str:
        """HEX 模式与 TX 正文取色；文本模式 RX 由 ``_payload_segments()`` 逐行处理。"""
        text_color = data_colors().text
        if record.direction != "rx":
            return text_color
        rule = match_log_level(record.text, self._log_level_rules)
        return text_color if rule is None else log_level_color(rule.color)

    def _timestamp_prefix(self, record: LogRecord) -> str:
        local_time = record.timestamp_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return f"[{local_time}] "

    def _payload_text(self, record: LogRecord) -> str:
        return record.hex_text if self.display_mode == "hex" else record.text

    def _format_record(self, record: LogRecord) -> str:
        prefix = self._timestamp_prefix(record) if self.timestamp_check.isChecked() else ""
        prefix += f"{record.direction.upper()}: "
        return prefix + self._payload_text(record)

    def _color_for(self, record: LogRecord) -> str:
        colors = data_colors()
        if record.direction == "rx":
            return colors.rx
        if record.direction == "tx":
            return colors.tx
        return colors.system

    def _open_find_bar(self) -> None:
        """显示查找栏并把焦点移到关键词输入框。"""
        self.find_bar.activate()

    def _find_bar_closed(self) -> None:
        """关闭查找栏后把焦点还给接收区。"""
        if self._terminal_mode:
            self.output.focus_input()
        else:
            self.output.setFocus(Qt.FocusReason.OtherFocusReason)

    def _settings_changed(self) -> None:
        if self._loading:
            return
        self.output.set_autoscroll(self.autoscroll_check.isChecked())
        self.output.set_highlight_enabled(self.highlight_check.isChecked())
        self.output.set_wrap_enabled(self.wrap_check.isChecked())
        self.render_records()
        self.preferences_changed.emit()

    def _pause_changed(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("继续" if paused else "暂停")
        if not paused:
            self.render_records()

    def _clear(self) -> None:
        if (
            self._records_provider()
            and QMessageBox.question(
                self,
                "清空接收区",
                "确定清空当前内存缓冲吗？自动日志文件不会受影响。",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._clear_callback()
        self._line_number = 0
        self.output.clear_content()
        if self._terminal_mode:
            self._screen = None
        self.find_bar.refresh()
        if self._terminal_mode:
            self.render_records()
