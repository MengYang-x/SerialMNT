"""日志级别颜色配置对话框。"""

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from serialmnt.domain import DEFAULT_LOG_LEVEL_RULES, LogLevelRule, ValidationError
from serialmnt.ui.theme_manager import log_level_color

_COLOR_COLUMN = 1
_SWATCH_SIZE = 14
_COLOR_LABELS = {
    "default": "默认颜色",
    "warning": "黄色",
    "error": "红色",
    "pass": "绿色",
    "debug": "蓝色",
}
_COLOR_ROLE = Qt.ItemDataRole.UserRole


class LogLevelRulesDialog(QDialog):
    """编辑全局日志级别字段与颜色的模态对话框。

    规则表顺序即匹配优先级：一行日志命中多条规则时使用最靠前的一条。
    """

    def __init__(
        self,
        rules: Sequence[LogLevelRule],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._result: tuple[LogLevelRule, ...] = tuple(rules)
        self._build_ui()
        self._fill_table(self._result)

    def rules(self) -> tuple[LogLevelRule, ...]:
        """返回用户确认后的规则；取消时保持打开前的值。"""
        return self._result

    def _build_ui(self) -> None:
        self.setWindowTitle("日志级别颜色")
        self.resize(460, 380)
        hint = QLabel(
            "接收到的日志行包含“字段”时整行按对应颜色显示。"
            "规则从上到下匹配，一行命中多条时使用最靠前的规则；"
            "双击颜色单元格可以选择自定义颜色。"
        )
        hint.setWordWrap(True)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["字段", "颜色"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            _COLOR_COLUMN, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        add_button = QPushButton("添加")
        add_button.setObjectName("addLogLevelRuleButton")
        add_button.clicked.connect(self._add_rule)
        remove_button = QPushButton("删除")
        remove_button.setObjectName("removeLogLevelRuleButton")
        remove_button.clicked.connect(self._remove_rule)
        default_color_button = QPushButton("使用默认颜色")
        default_color_button.setObjectName("defaultLogLevelColorButton")
        default_color_button.clicked.connect(self._use_default_color)
        restore_button = QPushButton("恢复默认规则")
        restore_button.setObjectName("restoreLogLevelRulesButton")
        restore_button.clicked.connect(self._restore_defaults)
        button_row = QHBoxLayout()
        button_row.addWidget(add_button)
        button_row.addWidget(remove_button)
        button_row.addWidget(default_color_button)
        button_row.addStretch(1)
        button_row.addWidget(restore_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.table, 1)
        layout.addLayout(button_row)
        layout.addWidget(buttons)

    def _fill_table(self, rules: Sequence[LogLevelRule]) -> None:
        self.table.setRowCount(0)
        for rule in rules:
            self._append_rule(rule)

    def _append_rule(self, rule: LogLevelRule) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(rule.field))
        self.table.setItem(row, _COLOR_COLUMN, self._color_item(rule.color))

    def _color_item(self, color: str) -> QTableWidgetItem:
        """构造颜色单元格：显示中文色名或十六进制值，并带色块图标。"""
        item = QTableWidgetItem(_COLOR_LABELS.get(color, color))
        item.setData(_COLOR_ROLE, color)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setIcon(_color_icon(log_level_color(color)))
        return item

    def _update_color_item(self, row: int, color: str) -> None:
        item = self.table.item(row, _COLOR_COLUMN)
        if item is None:
            return
        item.setData(_COLOR_ROLE, color)
        item.setText(_COLOR_LABELS.get(color, color))
        item.setIcon(_color_icon(log_level_color(color)))

    def _on_cell_double_clicked(self, row: int, column: int) -> None:
        if column == _COLOR_COLUMN:
            self._choose_color(row)

    def _choose_color(self, row: int) -> None:
        item = self.table.item(row, _COLOR_COLUMN)
        if item is None:
            return
        current = item.data(_COLOR_ROLE) or "default"
        chosen = QColorDialog.getColor(QColor(log_level_color(current)), self, "选择日志级别颜色")
        if chosen.isValid():
            self._update_color_item(row, chosen.name())

    def _add_rule(self) -> None:
        row = self.table.rowCount()
        self._append_rule(LogLevelRule(field="新字段", color="default"))
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def _remove_rule(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _use_default_color(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self._update_color_item(row, "default")

    def _restore_defaults(self) -> None:
        self._fill_table(DEFAULT_LOG_LEVEL_RULES)

    def accept(self) -> None:
        """确认前校验字段，空字段或重复字段不关闭对话框。"""
        rules = self._collect_rules()
        if rules is None:
            return
        self._result = rules
        super().accept()

    def _collect_rules(self) -> tuple[LogLevelRule, ...] | None:
        rules: list[LogLevelRule] = []
        seen: set[str] = set()
        for row in range(self.table.rowCount()):
            field_item = self.table.item(row, 0)
            color_item = self.table.item(row, _COLOR_COLUMN)
            field = field_item.text().strip() if field_item is not None else ""
            color = "default"
            if color_item is not None:
                color = str(color_item.data(_COLOR_ROLE) or "default")
            if not field:
                QMessageBox.warning(self, "字段无效", f"第 {row + 1} 行的字段不能为空。")
                return None
            if field.lower() in seen:
                QMessageBox.warning(
                    self,
                    "字段重复",
                    f"字段“{field}”重复，靠后的同名规则永远不会生效。",
                )
                return None
            seen.add(field.lower())
            try:
                rules.append(LogLevelRule(field=field, color=color))
            except ValidationError as exc:
                QMessageBox.warning(self, "规则无效", exc.message)
                return None
        return tuple(rules)


def _color_icon(color: str) -> QIcon:
    """生成颜色单元格左侧的小色块。"""
    pixmap = QPixmap(_SWATCH_SIZE, _SWATCH_SIZE)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)
