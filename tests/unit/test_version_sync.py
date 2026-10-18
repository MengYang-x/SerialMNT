"""测试版本同步检查器。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "check_version_sync.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_version_sync", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载版本同步检查器。")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _make_tree(
    root: Path,
    *,
    version: str = "0.2.0",
    headers: bool = True,
    entry: bool = True,
    asset_synced: bool = True,
) -> Path:
    (root / "src/serialmnt").mkdir(parents=True)
    (root / "src/serialmnt/__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    guide_text = f"> 软件版本：{version}  \n\n## 10. 软件版本\n\n- V{version}\n    1. 示例变更\n"
    if not entry:
        guide_text = guide_text.replace(f"- V{version}\n    1. 示例变更\n", "")
    docs = root / "docs"
    docs.mkdir()
    for name in ("README.md", "user-guide.md", "developer-guide.md"):
        header = f"> 软件版本：{version}  \n\n" if headers else ""
        text = guide_text if name == "user-guide.md" else header
        (docs / name).write_text(text, encoding="utf-8")
    (docs / "implementation-plan.md").write_text(
        f"- 当前软件版本为 `{version}`。\n", encoding="utf-8"
    )
    assets = root / "src/serialmnt/ui/assets"
    assets.mkdir(parents=True)
    packaged = guide_text if asset_synced else "旧内容"
    (assets / "user-guide.md").write_text(packaged, encoding="utf-8")
    return root


def test_repository_version_is_synchronized() -> None:
    assert checker.find_version_issues(ROOT) == []


def test_missing_version_header_is_reported(tmp_path: Path) -> None:
    _make_tree(tmp_path, headers=False)

    issues = checker.find_version_issues(tmp_path)

    assert any("缺少版本头" in issue for issue in issues)


def test_missing_version_entry_is_reported(tmp_path: Path) -> None:
    _make_tree(tmp_path, entry=False)

    issues = checker.find_version_issues(tmp_path)

    assert any("缺少“- V0.2.0”条目" in issue for issue in issues)


def test_newest_version_entry_must_be_first(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    guide_path = tmp_path / "docs/user-guide.md"
    guide = guide_path.read_text(encoding="utf-8")
    guide = guide.replace(
        "- V0.2.0\n    1. 示例变更\n",
        "- V0.1.0\n    1. 旧变更\n\n- V0.2.0\n    1. 示例变更\n",
    )
    guide_path.write_text(guide, encoding="utf-8")
    (tmp_path / "src/serialmnt/ui/assets/user-guide.md").write_text(guide, encoding="utf-8")

    issues = checker.find_version_issues(tmp_path)

    assert any("新版本需放在最上方" in issue for issue in issues)


def test_implementation_plan_version_must_match(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    plan = tmp_path / "docs/implementation-plan.md"
    plan.write_text("- 当前软件版本为 `0.1.0`。\n", encoding="utf-8")

    issues = checker.find_version_issues(tmp_path)

    assert any("implementation-plan.md" in issue and "0.2.0" in issue for issue in issues)


def test_non_semver_version_is_reported(tmp_path: Path) -> None:
    _make_tree(tmp_path, version="0.2")

    issues = checker.find_version_issues(tmp_path)

    assert any("MAJOR.MINOR.PATCH" in issue for issue in issues)


def test_packaged_guide_must_match_docs(tmp_path: Path) -> None:
    _make_tree(tmp_path, asset_synced=False)

    issues = checker.find_version_issues(tmp_path)

    assert any("逐字节同步" in issue for issue in issues)


def test_missing_version_source_is_reported(tmp_path: Path) -> None:
    issues = checker.find_version_issues(tmp_path)

    assert issues
    assert "__version__" in issues[0]
