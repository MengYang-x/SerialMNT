"""测试日志级别规则的匹配顺序、整词边界和校验。"""

import pytest

from serialmnt.domain import (
    DEFAULT_LOG_LEVEL_RULES,
    LogLevelRule,
    ValidationError,
    match_log_level,
)


def test_default_rules_prioritize_severity() -> None:
    rule = match_log_level("[Info] [Error] sensor timeout", DEFAULT_LOG_LEVEL_RULES)

    assert rule is not None
    assert rule.field == "Error"


def test_default_rules_match_expected_levels() -> None:
    expected = {
        "[Info] started": "Info",
        "[Warning] voltage low": "Warning",
        "[Error] timeout": "Error",
        "[Pass] self test": "Pass",
        "[Debug] trace": "Debug",
    }
    for text, field in expected.items():
        rule = match_log_level(text, DEFAULT_LOG_LEVEL_RULES)
        assert rule is not None
        assert rule.field == field


def test_match_is_case_insensitive_and_word_bounded() -> None:
    assert match_log_level("[WARNING] voltage low", DEFAULT_LOG_LEVEL_RULES) is not None
    assert match_log_level("information ready", DEFAULT_LOG_LEVEL_RULES) is None
    assert match_log_level("ERROR_CODE=1", DEFAULT_LOG_LEVEL_RULES) is None


def test_custom_rule_matches_chinese_field() -> None:
    rules = (LogLevelRule(field="警告", color="#123456"),)

    assert match_log_level("系统警告：电量低", rules) is not None


def test_empty_rules_and_text_do_not_match() -> None:
    assert match_log_level("Error", ()) is None
    assert match_log_level("", DEFAULT_LOG_LEVEL_RULES) is None


def test_rule_order_is_priority() -> None:
    rules = (LogLevelRule(field="Info"), LogLevelRule(field="Error", color="error"))

    rule = match_log_level("Info and Error", rules)

    assert rule is not None
    assert rule.field == "Info"


@pytest.mark.parametrize("field", ["", "   ", "x" * 65])
def test_invalid_field_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        LogLevelRule(field=field)


@pytest.mark.parametrize("color", ["yellow", "#12345", "#GGGGGG", "INFO"])
def test_invalid_color_is_rejected(color: str) -> None:
    with pytest.raises(ValidationError):
        LogLevelRule(field="Trace", color=color)


def test_rule_field_is_trimmed() -> None:
    assert LogLevelRule(field="  Trace  ").field == "Trace"
