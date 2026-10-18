"""终端视图控件：渲染终端网格并提供光标处的内联输入。"""

from collections.abc import Callable, Sequence

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import QApplication, QMenu, QPlainTextEdit, QWidget

from serialmnt.domain import (
    LogLevelRule,
    TerminalCell,
    TerminalScreen,
    TerminalStyle,
    match_log_level,
)
from serialmnt.ui.terminal_highlight import find_highlights
from serialmnt.ui.theme_manager import ansi_color, data_colors, highlight_colors, log_level_color

# 画面只渲染最近若干行，更早的滚动历史由终端模型保留。
_MAX_RENDERED_ROWS = 2000


class TerminalOutput(QPlainTextEdit):
    """渲染终端网格，并把单行输入维护为光标处的可编辑草稿。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._terminal_mode = False
        self._screen: TerminalScreen | None = None
        self._draft = ""
        self._input_start_position = 0
        self._cursor_block_number = 0
        self._history_provider: Callable[[], tuple[str, ...]] = lambda: ()
        self._submit_handler: Callable[[str], bool] | None = None
        self._history_index: int | None = None
        self._autoscroll = True
        self._mouse_pressed = False
        self._batch_update = False
        self._highlight_enabled = True
        self._log_level_rules: tuple[LogLevelRule, ...] = ()
        self._deferred_render = False
        self._rendering = False
        self._scrolling = False
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAcceptDrops(False)
        self.setCursorWidth(0)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.selectionChanged.connect(self._on_selection_changed)

    @property
    def draft(self) -> str:
        """返回当前终端输入草稿。"""
        return self._draft

    def set_terminal_mode(self, enabled: bool) -> None:
        """启用或关闭终端网格渲染与内联输入。"""
        if self._terminal_mode == enabled:
            return
        self._terminal_mode = enabled
        self._screen = None
        self._history_index = None
        self._input_start_position = 0
        self._cursor_block_number = 0
        self._deferred_render = False
        super().clear()
        self.setReadOnly(not enabled)

    def set_submit_handler(self, handler: Callable[[str], bool]) -> None:
        """设置行提交回调；返回 False 时保留输入草稿。"""
        self._submit_handler = handler

    def set_history_provider(self, provider: Callable[[], tuple[str, ...]]) -> None:
        """设置发送历史读取函数。"""
        self._history_provider = provider

    def set_autoscroll(self, enabled: bool) -> None:
        """设置新记录到达时是否自动滚动。"""
        self._autoscroll = enabled

    def set_highlight_enabled(self, enabled: bool) -> None:
        """设置是否在终端视图中叠加地址、链接与关键字高亮。"""
        self._highlight_enabled = enabled

    def set_log_level_rules(self, rules: Sequence[LogLevelRule]) -> None:
        """设置终端视图整行着色的日志级别规则；规则顺序即优先级。"""
        self._log_level_rules = tuple(rules)

    def set_wrap_enabled(self, enabled: bool) -> None:
        """设置超长行是否按控件宽度自动换行。"""
        if enabled:
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        else:
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def set_draft(self, text: str) -> None:
        """替换终端草稿，供历史菜单回填。"""
        self._history_index = None
        self._update_draft(self._single_line(text))
        self.focus_input()

    def focus_input(self) -> None:
        """把焦点和光标移到输入草稿末尾。"""
        if not self._terminal_mode:
            return
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if self._screen is not None:
            self._place_input_cursor()
            self.ensureCursorVisible()

    def begin_batch_update(self) -> None:
        """分栏视图批量追加期间暂停滚动。"""
        self._batch_update = True

    def end_batch_update(self) -> None:
        """结束批量追加并滚动到末尾。"""
        self._batch_update = False
        self._scroll_after_update()

    def clear_content(self) -> None:
        """清空控件内容；终端模式由调用方随后重建画面。"""
        self._deferred_render = False
        super().clear()
        self._input_start_position = 0
        self._cursor_block_number = 0

    def append_text(self, text: str, color: str) -> None:
        """按单色追加一行文本，供提示行等简单调用方使用。"""
        self.append_segments(((text, color, False),))

    def append_segments(self, segments: Sequence[tuple[str, str, bool]]) -> None:
        """按 (文本, 颜色, 是否加粗) 片段追加一行，行尾用默认文本色换行。"""
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for text, color, bold in segments:
            if not text:
                continue
            cursor.setCharFormat(self._format_for(color, bold))
            cursor.insertText(text)
        default_color = self.palette().color(QPalette.ColorRole.Text).name()
        cursor.setCharFormat(self._format_for(default_color))
        cursor.insertText("\n")
        if not self._batch_update:
            self._scroll_after_update()

    def render_screen(self, screen: TerminalScreen) -> None:
        """按终端模型重建画面，并在光标处渲染输入草稿。"""
        if not self._terminal_mode:
            return
        self._screen = screen
        if self.textCursor().hasSelection() or self._mouse_held():
            self._deferred_render = True
            return
        self._render_current_screen()

    def _render_current_screen(self) -> None:
        """重建当前终端画面，供延迟刷新调用。"""
        screen = self._screen
        if screen is None:
            return
        self._deferred_render = False
        self._rendering = True
        try:
            self._rebuild_screen(screen)
        finally:
            self._rendering = False

    def _rebuild_screen(self, screen: TerminalScreen) -> None:
        rows = screen.lines()
        hidden = max(len(rows) - _MAX_RENDERED_ROWS, 0)
        visible = rows[hidden:]
        cursor_row, cursor_col = screen.cursor
        cursor_row -= hidden
        if cursor_row < 0:
            cursor_row = 0
            cursor_col = 0

        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        if hidden:
            cursor.setCharFormat(self._format_for(data_colors().system))
            cursor.insertText(f"… 已隐藏较早的 {hidden} 行\n")
        for index, row in enumerate(visible):
            # 级别颜色是整行基底，关键字/地址高亮在其上局部覆盖。
            row_override = self._log_level_override(row)
            highlights = self._highlight_overrides(row)
            if index == cursor_row:
                self._write_cells(cursor, row, 0, cursor_col, highlights, row_override)
                self._input_start_position = cursor.position()
                cursor.setCharFormat(self._format_for_style(TerminalStyle()))
                if self._draft:
                    cursor.insertText(self._draft)
                self._cursor_block_number = cursor.blockNumber()
            else:
                self._write_cells(cursor, row, 0, None, highlights, row_override)
            if index != len(visible) - 1:
                cursor.insertText("\n")
        cursor.endEditBlock()
        self._place_input_cursor()
        self._scroll_after_update()

    def _write_cells(  # noqa: PLR0913, PLR0917 - 行范围与两层颜色覆盖都是渲染上下文
        self,
        cursor: QTextCursor,
        row: tuple[TerminalCell, ...],
        start: int,
        end: int | None,
        highlights: dict[int, str],
        row_override: str | None = None,
    ) -> None:
        limit = len(row) if end is None else min(end, len(row))
        index = start
        while index < limit:
            cell = row[index]
            if cell.trailing:
                index += 1
                continue
            style = cell.style
            override = highlights.get(index, row_override)
            text = cell.char
            index += 1
            while (
                index < limit
                and not row[index].trailing
                and row[index].style == style
                and highlights.get(index, row_override) == override
            ):
                text += row[index].char
                index += 1
            cursor.setCharFormat(self._format_for_style(style, override))
            cursor.insertText(text)
        if end is not None and end > len(row):
            cursor.setCharFormat(self._format_for_style(TerminalStyle()))
            cursor.insertText(" " * (end - len(row)))

    def _format_for(self, color: str, bold: bool = False) -> QTextCharFormat:
        text_format = QTextCharFormat()
        text_format.setForeground(QColor(color))
        if bold:
            text_format.setFontWeight(QFont.Weight.Bold)
        return text_format

    def _log_level_override(self, row: tuple[TerminalCell, ...]) -> str | None:
        """按整行文本匹配日志级别，返回需要覆盖整行前景的颜色。"""
        if not self._log_level_rules:
            return None
        rule = match_log_level("".join(cell.char for cell in row), self._log_level_rules)
        return None if rule is None else log_level_color(rule.color)

    def _highlight_overrides(self, row: tuple[TerminalCell, ...]) -> dict[int, str]:
        """按行文本匹配地址与链接，返回需要改色的单元格索引到颜色。"""
        if not self._highlight_enabled:
            return {}
        matches = find_highlights("".join(cell.char for cell in row))
        if not matches:
            return {}
        colors = highlight_colors()
        palette = {
            "address": colors.address,
            "link": colors.link,
            "success": colors.success,
            "error": colors.error,
            "warning": colors.warning,
        }
        positions: list[tuple[int, int, int]] = []
        offset = 0
        for cell_index, cell in enumerate(row):
            length = len(cell.char)
            positions.append((offset, offset + length, cell_index))
            offset += length
        overrides: dict[int, str] = {}
        for start, end, kind in matches:
            for cell_start, cell_end, cell_index in positions:
                if cell_start >= end:
                    break
                cell = row[cell_index]
                if cell.trailing or cell.style.fg is not None:
                    continue
                if cell_end > start:
                    overrides[cell_index] = palette[kind]
        return overrides

    def _format_for_style(
        self,
        style: TerminalStyle,
        override: str | None = None,
    ) -> QTextCharFormat:
        text_format = QTextCharFormat()
        palette = self.palette()
        if override is not None:
            text_format.setForeground(QColor(override))
        elif style.fg is None:
            text_format.setForeground(palette.color(QPalette.ColorRole.Text))
        else:
            text_format.setForeground(QColor(ansi_color(style.fg)))
        if style.bg is not None:
            text_format.setBackground(QColor(ansi_color(style.bg)))
        if style.bold:
            text_format.setFontWeight(QFont.Weight.Bold)
        return text_format

    def _draft_bounds(self) -> tuple[int, int]:
        block = self.document().findBlockByNumber(self._cursor_block_number)
        text_end = block.position() + len(block.text())
        start = min(self._input_start_position, text_end)
        return start, text_end

    def paintEvent(self, event: QPaintEvent) -> None:
        """在草稿末尾绘制块状光标，避免污染正文内容。"""
        super().paintEvent(event)
        if not self._terminal_mode or self._screen is None:
            return
        caret = QTextCursor(self.document())
        caret.setPosition(self._draft_bounds()[1])
        rect = self.cursorRect(caret)
        width = max(self.fontMetrics().horizontalAdvance(" "), 1)
        painter = QPainter(self.viewport())
        painter.fillRect(
            QRect(rect.left(), rect.top(), width, rect.height()),
            self.palette().color(QPalette.ColorRole.Text),
        )

    def _place_input_cursor(self) -> None:
        _, end = self._draft_bounds()
        caret = QTextCursor(self.document())
        caret.setPosition(end)
        self.setTextCursor(caret)

    def _scroll_after_update(self) -> None:
        """自动滚动到末尾；存在选中内容、正在滚动或按住鼠标时保持原位。"""
        if not self._autoscroll or self._scrolling or self._mouse_held():
            return
        if self.textCursor().hasSelection():
            return
        self._scrolling = True
        try:
            self.moveCursor(QTextCursor.MoveOperation.End)
            self.ensureCursorVisible()
        finally:
            self._scrolling = False

    def _on_selection_changed(self) -> None:
        """选中被清除后补上暂停期间延迟的渲染与自动滚动。"""
        if (
            self._rendering
            or self._scrolling
            or self._mouse_held()
            or self.textCursor().hasSelection()
        ):
            return
        if self._deferred_render:
            self._render_current_screen()
        self._scroll_after_update()

    def _update_draft(self, text: str) -> None:
        self._draft = text
        if not self._terminal_mode or self._screen is None:
            return
        start, end = self._draft_bounds()
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)
        self._place_input_cursor()
        self._scroll_after_update()

    def _sync_draft_from_document(self) -> None:
        if not self._terminal_mode:
            return
        start, end = self._draft_bounds()
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._draft = cursor.selectedText().replace("\u2029", "\n")

    def _constrain_cursor(self) -> None:
        if not self._terminal_mode:
            return
        start, end = self._draft_bounds()
        cursor = self.textCursor()
        if cursor.hasSelection():
            if cursor.selectionStart() < start or cursor.selectionEnd() > end:
                cursor.clearSelection()
                cursor.setPosition(end)
        elif cursor.position() < start or cursor.position() > end:
            cursor.setPosition(min(max(cursor.position(), start), end))
        self.setTextCursor(cursor)

    def _selection_outside_draft(self) -> bool:
        start, end = self._draft_bounds()
        cursor = self.textCursor()
        return cursor.hasSelection() and (
            cursor.selectionStart() < start or cursor.selectionEnd() > end
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """标记左键按下，让按住期间的更新避开拖动中的选区锚点。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._mouse_pressed = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """左键松开后补上按住期间延迟的渲染与自动滚动。"""
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._mouse_pressed = False
        self._on_selection_changed()

    def _mouse_held(self) -> bool:
        """本控件上按下的左键是否仍按住，用于暂停随数据到达的滚动与重绘。

        终端视图里从选区内部拖动会启动 Qt 内部拖拽，拖拽期间收不到释放事件，
        因此除了本控件按下的标记，还要确认左键在全局仍处于按下状态，避免暂停自我锁死。
        """
        return self._mouse_pressed and bool(QApplication.mouseButtons() & Qt.MouseButton.LeftButton)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """接收区右键只提供复制与全选，避免剪切/删除改到日志内容。"""
        self._build_context_menu().exec(event.globalPos())

    def _build_context_menu(self) -> QMenu:
        """构造受限的右键菜单：只有复制和全选。"""
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)
        select_all_action = menu.addAction("全选")
        select_all_action.triggered.connect(self.selectAll)
        return menu

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """限制编辑范围，并把回车和上下键交给终端命令处理。"""
        if not self._terminal_mode:
            super().keyPressEvent(event)
            return
        if self._handle_terminal_command(event) or self._guard_edit_key(event):
            return
        self._constrain_cursor()
        super().keyPressEvent(event)
        self._sync_draft_from_document()

    def _handle_terminal_command(self, event: QKeyEvent) -> bool:
        handled = False
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self._submit_current()
            handled = True
        elif event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down}:
            self._history_move(older=event.key() == Qt.Key.Key_Up)
            handled = True
        elif event.key() == Qt.Key.Key_Escape:
            self._history_index = None
            self._update_draft("")
            handled = True
        elif event.matches(QKeySequence.StandardKey.Paste):
            self._constrain_cursor()
            self.paste()
            handled = True
        elif event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            handled = True
        elif event.key() == Qt.Key.Key_Home:
            self._move_input_caret(to_end=False)
            handled = True
        elif event.key() == Qt.Key.Key_End:
            self._move_input_caret(to_end=True)
            handled = True
        return handled

    def _move_input_caret(self, *, to_end: bool) -> None:
        start, end = self._draft_bounds()
        cursor = QTextCursor(self.document())
        cursor.setPosition(end if to_end else start)
        self.setTextCursor(cursor)

    def _guard_edit_key(self, event: QKeyEvent) -> bool:
        start, end = self._draft_bounds()
        cursor = self.textCursor()
        blocked = False
        if event.matches(QKeySequence.StandardKey.Cut):
            blocked = self._selection_outside_draft()
        elif event.key() == Qt.Key.Key_Left and not cursor.hasSelection():
            blocked = cursor.position() <= start
        elif event.key() == Qt.Key.Key_Backspace:
            blocked = (
                self._selection_outside_draft()
                if cursor.hasSelection()
                else cursor.position() <= start
            )
        elif event.key() == Qt.Key.Key_Delete:
            blocked = (
                self._selection_outside_draft()
                if cursor.hasSelection()
                else cursor.position() >= end
            )
        return blocked

    def _submit_current(self) -> bool:
        self._sync_draft_from_document()
        if self._submit_handler is None:
            return False
        if self._submit_handler(self._draft):
            self._history_index = None
            self._update_draft("")
            return True
        return False

    def _submit_text(self, text: str) -> bool:
        self._history_index = None
        self._update_draft(self._single_line(text))
        return self._submit_current()

    def _history_move(self, older: bool) -> None:
        history = self._history_provider()
        if not history:
            return
        if self._history_index is None:
            self._history_index = len(history)
        if older:
            self._history_index = max(0, self._history_index - 1)
        else:
            self._history_index = min(len(history), self._history_index + 1)
        value = history[self._history_index] if self._history_index < len(history) else ""
        self._update_draft(self._single_line(value))

    @staticmethod
    def _single_line(text: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.split("\n", 1)[0]

    def insertFromMimeData(self, source: object) -> None:
        """粘贴单行文本；多行文本按非空行依次发送。"""
        if not self._terminal_mode:
            super().insertFromMimeData(source)
            return
        text = source.text().replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in text:
            self._insert_text(text)
            return
        for line in text.split("\n"):
            if line and not self._submit_text(line):
                return
        self._history_index = None
        self._update_draft("")

    def inputMethodEvent(self, event: object) -> None:
        """让输入法文本继续受终端输入区约束。"""
        if not self._terminal_mode:
            super().inputMethodEvent(event)
            return
        self._constrain_cursor()
        super().inputMethodEvent(event)
        self._sync_draft_from_document()

    def _insert_text(self, text: str) -> None:
        self._constrain_cursor()
        cursor = self.textCursor()
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self._sync_draft_from_document()
