"""测试下拉选择框忽略鼠标滚轮。"""

import ast
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from serialmnt.ui import NoWheelComboBox

ROOT = Path(__file__).resolve().parents[3]
UI_DIR = ROOT / "src" / "serialmnt" / "ui"


def _wheel_event(widget: NoWheelComboBox) -> QWheelEvent:
    position = QPointF(widget.width() / 2, widget.height() / 2)
    return QWheelEvent(
        position,
        widget.mapToGlobal(position),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_wheel_does_not_change_selection(qtbot: object) -> None:
    combo = NoWheelComboBox()
    combo.addItems(["9600", "19200", "115200"])
    combo.setCurrentIndex(1)
    combo.resize(120, 24)
    qtbot.addWidget(combo)
    combo.show()
    event = _wheel_event(combo)

    QApplication.sendEvent(combo, event)

    assert combo.currentIndex() == 1
    assert not event.isAccepted(), "滚轮事件应被忽略并继续向父控件传播"


def test_ui_modules_only_create_no_wheel_combo_boxes() -> None:
    """所有下拉框都必须忽略滚轮，避免滚动页面时误改串口参数。"""
    offenders: list[str] = []
    for path in sorted(UI_DIR.glob("*.py")):
        if path.name == "no_wheel_combo.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QComboBox"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "以下位置仍直接创建 QComboBox：" + "、".join(offenders)
