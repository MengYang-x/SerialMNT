"""日志级别规则：把日志行中的字段映射为显示颜色。"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from .errors import ValidationError

DEFAULT_LOG_LEVEL_COLOR = "default"
LOG_LEVEL_SEMANTIC_COLORS = ("warning", "error", "pass", "debug")
MAX_LOG_LEVEL_FIELD_LENGTH = 64

_HEX_COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}")
# 字段两侧只排除 ASCII 单词字符：ASCII 日志里 "Error" 不会命中 "Errors" 或 "ERROR_CODE"，
# 中文等非 ASCII 字段仍可正常匹配。
_FIELD_TEMPLATE = r"(?<![0-9A-Za-z_]){field}(?![0-9A-Za-z_])"


@dataclass(frozen=True, slots=True)
class LogLevelRule:
    """把日志中的整词字段映射为一种显示颜色。

    ``color`` 为 ``default``（跟随主题正文色）、语义颜色键（由 UI 层按当前主题
    解析）或 ``#RRGGBB`` 固定颜色。
    """

    field: str
    color: str = DEFAULT_LOG_LEVEL_COLOR

    def __post_init__(self) -> None:
        normalized = self.field.strip()
        if not normalized:
            raise ValidationError("日志级别字段不能为空。")
        if len(normalized) > MAX_LOG_LEVEL_FIELD_LENGTH:
            raise ValidationError("日志级别字段不能超过 64 个字符。")
        if normalized != self.field:
            object.__setattr__(self, "field", normalized)
        if self.color == DEFAULT_LOG_LEVEL_COLOR or self.color in LOG_LEVEL_SEMANTIC_COLORS:
            return
        if not _HEX_COLOR_PATTERN.fullmatch(self.color):
            raise ValidationError("日志级别颜色必须是语义颜色键或 #RRGGBB。")

    def matches(self, text: str) -> bool:
        """判断文本是否包含该字段；不区分大小写且要求整词匹配。"""
        return _field_pattern(self.field).search(text) is not None


# 顺序即匹配优先级：一行命中多条规则时使用最靠前的规则。
DEFAULT_LOG_LEVEL_RULES: tuple[LogLevelRule, ...] = (
    LogLevelRule(field="Error", color="error"),
    LogLevelRule(field="Warning", color="warning"),
    LogLevelRule(field="Pass", color="pass"),
    LogLevelRule(field="Debug", color="debug"),
    LogLevelRule(field="Info", color=DEFAULT_LOG_LEVEL_COLOR),
)


def match_log_level(text: str, rules: Sequence[LogLevelRule]) -> LogLevelRule | None:
    """返回文本命中的第一条规则；没有命中或规则为空时返回 None。"""
    if not text or not rules:
        return None
    for rule in rules:
        if rule.matches(text):
            return rule
    return None


@lru_cache(maxsize=256)
def _field_pattern(field: str) -> re.Pattern[str]:
    return re.compile(_FIELD_TEMPLATE.format(field=re.escape(field)), re.IGNORECASE)
