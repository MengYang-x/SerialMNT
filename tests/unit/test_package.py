"""测试工程骨架的基础包契约。"""

import re
from importlib import import_module
from importlib.metadata import version

import pytest

import serialmnt

PACKAGE_NAMES = (
    "serialmnt.application",
    "serialmnt.bootstrap",
    "serialmnt.domain",
    "serialmnt.infrastructure",
    "serialmnt.ui",
)


def test_package_version_matches_installed_metadata() -> None:
    """确保包内版本与构建元数据使用同一来源。"""
    assert serialmnt.__version__ == version("serialmnt")


def test_package_version_uses_semantic_version_shape() -> None:
    """确保初始版本采用明确的语义版本格式。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", serialmnt.__version__)


@pytest.mark.parametrize("module_name", PACKAGE_NAMES)
def test_core_namespace_can_be_imported(module_name: str) -> None:
    """确保 M0 建立的模块边界可以被 Python 正常导入。"""
    assert import_module(module_name) is not None
