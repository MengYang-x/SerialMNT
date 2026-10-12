"""文档同步检查器单元测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "check_docs_sync.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_sync", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载文档同步检查器。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_ui_change_requires_a_guide() -> None:
    rules = (
        checker.SyncRule(
            name="ui",
            sources=("src/serialmnt/ui/**",),
            required_any=("docs/user-guide.md", "docs/developer-guide.md"),
        ),
    )

    issues = checker.find_sync_issues(
        {"src/serialmnt/ui/main_window.py"},
        rules,
        ("src/serialmnt/**",),
    )

    assert issues == [
        "ui：src/serialmnt/ui/main_window.py 需要同步更新 "
        "docs/user-guide.md 或 docs/developer-guide.md 中的至少一个文件。"
    ]


def test_matching_documentation_passes() -> None:
    rules = (
        checker.SyncRule(
            name="ui",
            sources=("src/serialmnt/ui/**",),
            required_any=("docs/user-guide.md", "docs/developer-guide.md"),
        ),
    )

    issues = checker.find_sync_issues(
        {"src/serialmnt/ui/main_window.py", "docs/user-guide.md"},
        rules,
        ("src/serialmnt/**",),
    )

    assert issues == []


def test_unmapped_source_fails() -> None:
    rules = (
        checker.SyncRule(
            name="ui",
            sources=("src/serialmnt/ui/**",),
            required_any=("docs/user-guide.md",),
        ),
    )

    issues = checker.find_sync_issues(
        {"src/serialmnt/domain/models.py"},
        rules,
        ("src/serialmnt/**",),
    )

    assert issues == ["未配置文档同步规则的运行时代码：src/serialmnt/domain/models.py"]


def test_docs_and_tests_only_changes_pass() -> None:
    rules = (
        checker.SyncRule(
            name="ui",
            sources=("src/serialmnt/ui/**",),
            required_any=("docs/user-guide.md",),
        ),
    )

    issues = checker.find_sync_issues(
        {"docs/user-guide.md", "tests/unit/test_docs_sync.py"},
        rules,
        ("src/serialmnt/**",),
    )

    assert issues == []


def test_rule_loader_and_targets_are_valid() -> None:
    rules, unmapped_fail = checker.load_rules()

    assert rules
    assert unmapped_fail
    assert checker.validate_rule_targets(ROOT, rules) == []


def test_wildcard_document_target_is_found() -> None:
    rules = (
        checker.SyncRule(
            name="adr-only",
            sources=("src/serialmnt/domain/**",),
            required_any=("docs/adr/**/*.md",),
        ),
    )

    assert checker.validate_rule_targets(ROOT, rules) == []


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/serialmnt/ui/**", "src/serialmnt/ui/main_window.py", True),
        ("src/serialmnt/ui/**", r"src\serialmnt\ui\main_window.py", True),
        ("docs/adr/**/*.md", "docs/adr/0001-worker-threading-and-session-boundaries.md", True),
        ("docs/adr/**/*.md", "docs/developer-guide.md", False),
    ],
)
def test_glob_matching(pattern: str, path: str, expected: bool) -> None:
    assert checker._matches_any(path, (pattern,)) is expected
