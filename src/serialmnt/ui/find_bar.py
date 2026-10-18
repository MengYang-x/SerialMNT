"""接收区文本查找栏。"""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QToolButton,
    QWidget,
)

from serialmnt.ui.theme_manager import CONTROL_HEIGHT_PX


class FindBar(QWidget):
    """在接收区当前渲染的文本中查找关键词。

    查找不区分大小写，命中位置用文本选区标出；上一个/下一个到达首尾后循环。
    查找只读取控件中已经渲染的内容，不访问会话缓冲，也不改变原始记录和日志。
    """

    closed = Signal()

    def __init__(self, target: QPlainTextEdit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target = target
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.query_edit = QLineEdit(self)
        self.query_edit.setPlaceholderText("查找接收内容")
        self.query_edit.setClearButtonEnabled(True)
        self.query_edit.setToolTip("在接收区当前显示的文本中查找，不区分大小写")
        self.query_edit.textChanged.connect(self._query_changed)
        self.query_edit.installEventFilter(self)

        self.previous_button = QToolButton(self)
        self.previous_button.setText("上一个")
        self.previous_button.setToolTip("上一个匹配 (Shift+Enter)")
        self.previous_button.clicked.connect(self.find_previous)

        self.next_button = QToolButton(self)
        self.next_button.setText("下一个")
        self.next_button.setToolTip("下一个匹配 (Enter)")
        self.next_button.clicked.connect(self.find_next)

        self.status_label = QLabel(self)
        self.status_label.setMinimumWidth(64)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.close_button = QToolButton(self)
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭查找 (Esc)")
        self.close_button.clicked.connect(self.close_bar)

        layout.addWidget(self.query_edit, 1)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.status_label)
        layout.addWidget(self.close_button)
        for widget in (
            self.query_edit,
            self.previous_button,
            self.next_button,
            self.status_label,
            self.close_button,
        ):
            widget.setFixedHeight(CONTROL_HEIGHT_PX)
        self._set_navigation_enabled(False)

    @property
    def is_active(self) -> bool:
        """返回查找栏当前是否可见。"""
        return self.isVisible()

    def activate(self) -> None:
        """显示查找栏，聚焦输入框并重新定位已有查询。"""
        self.setVisible(True)
        self.query_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query_edit.selectAll()
        if self.query_edit.text():
            self._find(backward=False, from_start=True)

    def close_bar(self) -> None:
        """隐藏查找栏并清除查找选区。"""
        if not self.isVisible():
            return
        self._clear_selection()
        self.setVisible(False)
        self.closed.emit()

    def find_next(self) -> None:
        """跳到下一个匹配，到达末尾后从头循环。"""
        self._find(backward=False)

    def find_previous(self) -> None:
        """跳到上一个匹配，到达开头后从末尾循环。"""
        self._find(backward=True)

    def refresh(self) -> None:
        """接收区内容重建后从开头重新查找。"""
        if not self.is_active or not self.query_edit.text():
            return
        self._find(backward=False, from_start=True)

    def suspend(self) -> None:
        """清除查找选区以便接收区重建内容，查找栏保持可见。"""
        if self.is_active:
            self._clear_selection()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """在查找输入框中把 Esc 转为关闭、回车转为上一个/下一个。"""
        if watched is self.query_edit and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.close_bar()
                return True
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                backward = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._find(backward=backward)
                return True
        return super().eventFilter(watched, event)

    def _query_changed(self, text: str) -> None:
        if not text:
            self._clear_selection()
            self.status_label.clear()
            self._set_navigation_enabled(False)
            return
        self._set_navigation_enabled(True)
        self._find(backward=False, from_start=True)

    def _find(self, *, backward: bool, from_start: bool = False) -> None:
        """从当前选区或文档首尾查找一次，并在未命中时循环。"""
        query = self.query_edit.text()
        if not query:
            return
        document = self._target.document()
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        match = document.find(
            query,
            self._search_cursor(document, from_start=from_start, backward=backward),
            flags,
        )
        if match.isNull():
            wrap = QTextCursor(document)
            wrap.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            match = document.find(query, wrap, flags)
        if match.isNull():
            self._clear_selection()
            self.status_label.setText("无匹配")
            return
        self._target.setTextCursor(match)
        self._target.ensureCursorVisible()
        self._update_status(document, query)

    def _search_cursor(
        self,
        document: QTextDocument,
        *,
        from_start: bool,
        backward: bool,
    ) -> QTextCursor:
        """计算本次查找的起点：文档开头、当前选区边界或光标位置。"""
        cursor = QTextCursor(document)
        if from_start:
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            return cursor
        current = self._target.textCursor()
        if current.hasSelection():
            position = current.selectionStart() if backward else current.selectionEnd()
            cursor.setPosition(position)
        else:
            cursor.setPosition(current.position())
        return cursor

    def _update_status(self, document: QTextDocument, query: str) -> None:
        total, index = self._count_matches(document, query)
        if total == 0:
            self.status_label.setText("无匹配")
        elif index is None:
            self.status_label.setText(f"共 {total} 处")
        else:
            self.status_label.setText(f"{index}/{total}")

    def _count_matches(
        self,
        document: QTextDocument,
        query: str,
    ) -> tuple[int, int | None]:
        """统计全部命中，并返回当前选区是第几个匹配。"""
        current = self._target.textCursor()
        current_start = current.selectionStart() if current.hasSelection() else -1
        current_end = current.selectionEnd() if current.hasSelection() else -1
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        total = 0
        index: int | None = None
        while True:
            match = document.find(query, cursor)
            if match.isNull():
                break
            total += 1
            if match.selectionStart() == current_start and match.selectionEnd() == current_end:
                index = total
            cursor.setPosition(match.selectionEnd())
        return total, index

    def _clear_selection(self) -> None:
        cursor = self._target.textCursor()
        if not cursor.hasSelection():
            return
        cursor.clearSelection()
        self._target.setTextCursor(cursor)

    def _set_navigation_enabled(self, enabled: bool) -> None:
        self.previous_button.setEnabled(enabled)
        self.next_button.setEnabled(enabled)
