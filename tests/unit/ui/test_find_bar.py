"""测试接收区查找栏的匹配、循环和关闭行为。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPlainTextEdit

from serialmnt.ui.find_bar import FindBar


def _bar(qtbot: object) -> tuple[QPlainTextEdit, FindBar]:
    target = QPlainTextEdit()
    target.setPlainText("first error\nsecond OK\nerror again")
    qtbot.addWidget(target)
    bar = FindBar(target)
    qtbot.addWidget(bar)
    bar.show()
    return target, bar


def _selected(target: QPlainTextEdit) -> str:
    return target.textCursor().selectedText()


def test_find_bar_finds_case_insensitively_and_wraps(qtbot: object) -> None:
    target, bar = _bar(qtbot)
    bar.activate()
    qtbot.keyClicks(bar.query_edit, "ERROR")

    assert _selected(target) == "error"
    assert bar.status_label.text() == "1/2"

    bar.find_next()
    assert bar.status_label.text() == "2/2"
    assert target.textCursor().selectionStart() > 6

    bar.find_next()
    assert bar.status_label.text() == "1/2"

    bar.find_previous()
    assert bar.status_label.text() == "2/2"


def test_find_bar_reports_no_match_and_escape_closes(qtbot: object) -> None:
    target, bar = _bar(qtbot)
    bar.activate()
    qtbot.keyClicks(bar.query_edit, "missing")

    assert bar.status_label.text() == "无匹配"
    assert not target.textCursor().hasSelection()

    bar.query_edit.clear()
    qtbot.keyClicks(bar.query_edit, "OK")
    assert _selected(target) == "OK"

    qtbot.keyClick(bar.query_edit, Qt.Key.Key_Escape)
    assert not bar.isVisible()
    assert not target.textCursor().hasSelection()
