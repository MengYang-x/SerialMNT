"""测试日志级别着色的分栏/终端渲染与规则对话框。"""

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from serialmnt.domain import DEFAULT_LOG_LEVEL_RULES, LogLevelRule, LogRecord
from serialmnt.ui import ReceivePanel, data_colors
from serialmnt.ui.log_level_settings import LogLevelRulesDialog
from serialmnt.ui.theme_manager import log_level_color


def _record(raw: bytes, direction: str = "rx") -> LogRecord:
    return LogRecord.from_bytes(
        timestamp_utc=datetime(2026, 10, 18, tzinfo=timezone.utc),
        direction=direction,
        raw=raw,
        encoding="utf-8",
        session_id="log-level",
    )


def _panel(
    qtbot: object,
    records: tuple[LogRecord, ...],
    rules: tuple[LogLevelRule, ...] | None = None,
) -> ReceivePanel:
    panel = ReceivePanel(
        records_provider=lambda: records,
        clear_callback=lambda: 0,
        log_level_rules=rules,
    )
    qtbot.addWidget(panel)
    return panel


def _fragments(panel: ReceivePanel) -> list[tuple[str, str]]:
    """返回文档中非空片段的 (文本, 前景色)。"""
    result: list[tuple[str, str]] = []
    block = panel.output.document().firstBlock()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid() and fragment.text():
                color = fragment.charFormat().foreground().color().name()
                result.append((fragment.text(), color))
            iterator += 1
        block = block.next()
    return result


def _payload_color(panel: ReceivePanel, keyword: str) -> str:
    matches = [color for text, color in _fragments(panel) if keyword in text]
    assert len(matches) == 1, matches
    return matches[0]


def test_warning_record_colors_whole_payload(qtbot: object) -> None:
    panel = _panel(qtbot, (_record(b"[Warning] voltage low"),))

    assert _payload_color(panel, "Warning") == log_level_color("warning")


def test_info_record_uses_default_text_color(qtbot: object) -> None:
    panel = _panel(qtbot, (_record(b"[Info] started"),))

    assert _payload_color(panel, "Info") == data_colors().text


def test_tx_record_keeps_default_payload_color(qtbot: object) -> None:
    panel = _panel(qtbot, (_record(b"[Error] sent command", direction="tx"),))

    assert _payload_color(panel, "Error") == data_colors().text


def test_custom_rule_color_is_applied(qtbot: object) -> None:
    rules = (LogLevelRule(field="TRACE", color="#123456"),)
    panel = _panel(qtbot, (_record(b"trace: sample"),), rules)

    assert _payload_color(panel, "trace") == "#123456"


def test_multiline_record_colors_each_line_independently(qtbot: object) -> None:
    """回归：一次读取包含多行时，每行按自己的级别取色，不能被同批次的 Error 染红。"""
    panel = _panel(
        qtbot,
        (_record(b"[Warning] low\n[Error] timeout\n[Pass] done"),),
    )

    assert _payload_color(panel, "Warning") == log_level_color("warning")
    assert _payload_color(panel, "Error") == log_level_color("error")
    assert _payload_color(panel, "Pass") == log_level_color("pass")


def test_set_log_level_rules_rerenders_existing_records(qtbot: object) -> None:
    panel = _panel(qtbot, (_record(b"[Error] timeout"),))

    panel.set_log_level_rules((LogLevelRule(field="Error", color="#00ff00"),))

    assert _payload_color(panel, "Error") == "#00ff00"


def test_terminal_view_colors_whole_line_and_overrides_ansi(qtbot: object) -> None:
    panel = _panel(qtbot, (_record(b"\x1b[31m[Error] red text\x1b[0m"),))
    panel.set_terminal_mode(True)

    fragments = _fragments(panel)

    assert any("Error" in text for text, _ in fragments)
    assert {color for _, color in fragments} == {log_level_color("error")}


def test_log_level_settings_dialog_collects_rules(qtbot: object) -> None:
    dialog = LogLevelRulesDialog(DEFAULT_LOG_LEVEL_RULES)
    qtbot.addWidget(dialog)

    dialog.accept()

    assert dialog.rules() == DEFAULT_LOG_LEVEL_RULES
    assert dialog.table.item(0, 1).text() == "红色"
    assert dialog.table.item(1, 1).text() == "黄色"


def test_log_level_settings_dialog_adds_and_removes_rules(qtbot: object) -> None:
    dialog = LogLevelRulesDialog(DEFAULT_LOG_LEVEL_RULES)
    qtbot.addWidget(dialog)
    add_button = dialog.findChild(QPushButton, "addLogLevelRuleButton")
    remove_button = dialog.findChild(QPushButton, "removeLogLevelRuleButton")

    qtbot.mouseClick(add_button, Qt.MouseButton.LeftButton)
    dialog.table.setCurrentCell(0, 1)
    dialog.table.item(dialog.table.rowCount() - 1, 0).setText("Trace")
    dialog.accept()

    assert dialog.rules()[-1] == LogLevelRule(field="Trace")

    dialog = LogLevelRulesDialog(DEFAULT_LOG_LEVEL_RULES)
    qtbot.addWidget(dialog)
    remove_button = dialog.findChild(QPushButton, "removeLogLevelRuleButton")
    dialog.table.setCurrentCell(0, 0)
    qtbot.mouseClick(remove_button, Qt.MouseButton.LeftButton)
    dialog.accept()

    assert len(dialog.rules()) == len(DEFAULT_LOG_LEVEL_RULES) - 1
