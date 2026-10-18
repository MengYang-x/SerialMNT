#!/usr/bin/env python3
"""检查代码变更是否在同一提交中同步了相关文档。"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RULE_FILE = ROOT / "docs" / "doc-sync-rules.json"
DOCUMENT_VERSION_PATTERN = re.compile(r"^> 文档版本：\s*(\d+\.\d+)\s*$", re.MULTILINE)
DOCUMENT_PATTERNS = ("AGENT.md", "docs/**/*.md")


@dataclass(frozen=True, slots=True)
class SyncRule:
    """一条源码路径到文档路径的同步约束。"""

    name: str
    sources: tuple[str, ...]
    required_any: tuple[str, ...]

    def matches_source(self, path: str) -> bool:
        """判断路径是否触发该规则。"""
        return _matches_any(path, self.sources)

    def has_documentation_update(self, changed_paths: set[str]) -> bool:
        """判断变更集中是否包含该规则要求的文档。"""
        return any(_matches_any(path, self.required_any) for path in changed_paths)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatchcase(normalized, pattern):
            return True
        # fnmatch treats "**/" as requiring at least one directory. A glob such as
        # "docs/adr/**/*.md" should also match files directly under docs/adr.
        if "**/" in pattern and fnmatch.fnmatchcase(normalized, pattern.replace("**/", "")):
            return True
    return False


def _load_rule_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取文档同步规则 {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("文档同步规则根节点必须是对象。")
    return payload


def _require_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} 必须是非空字符串数组。")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} 必须是非空字符串数组。")
    return tuple(item.strip().replace("\\", "/") for item in value)


def load_rules(path: Path = RULE_FILE) -> tuple[tuple[SyncRule, ...], tuple[str, ...]]:
    """读取并验证文档同步规则。"""
    payload = _load_rule_payload(path)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("rules 必须是非空数组。")
    rules: list[SyncRule] = []
    names: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"rules[{index}] 必须是对象。")
        name = raw_rule.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"rules[{index}].name 必须是非空字符串。")
        if name in names:
            raise ValueError(f"规则名称重复：{name}")
        names.add(name)
        rules.append(
            SyncRule(
                name=name,
                sources=_require_string_tuple(raw_rule.get("sources"), f"rules[{index}].sources"),
                required_any=_require_string_tuple(
                    raw_rule.get("required_any"),
                    f"rules[{index}].required_any",
                ),
            )
        )
    unmapped_fail = _require_string_tuple(payload.get("unmapped_fail"), "unmapped_fail")
    return tuple(rules), unmapped_fail


def _target_exists(root: Path, pattern: str) -> bool:
    if any(character in pattern for character in "*?["):
        return any(root.glob(pattern))
    return (root / pattern).exists()


def validate_rule_targets(
    root: Path = ROOT,
    rules: tuple[SyncRule, ...] | None = None,
) -> list[str]:
    """确认每个规则要求的文档目标至少存在一个文件。"""
    selected_rules = rules if rules is not None else load_rules()[0]
    problems: list[str] = []
    for rule in selected_rules:
        if not any(_target_exists(root, pattern) for pattern in rule.required_any):
            problems.append(f"规则 {rule.name} 的文档目标均不存在：{', '.join(rule.required_any)}")
    return problems


def find_sync_issues(
    changed_paths: set[str],
    rules: tuple[SyncRule, ...],
    unmapped_fail: tuple[str, ...],
) -> list[str]:
    """返回变更集违反同步规则的问题列表。"""
    normalized_paths = {path.replace("\\", "/") for path in changed_paths}
    issues: list[str] = []
    covered_paths: set[str] = set()
    for rule in rules:
        matched_sources = {path for path in normalized_paths if rule.matches_source(path)}
        if not matched_sources:
            continue
        covered_paths.update(matched_sources)
        if not rule.has_documentation_update(normalized_paths):
            issues.append(
                f"{rule.name}：{', '.join(sorted(matched_sources))} 需要同步更新 "
                f"{' 或 '.join(rule.required_any)} 中的至少一个文件。"
            )
    for path in sorted(normalized_paths - covered_paths):
        if _matches_any(path, unmapped_fail):
            issues.append(f"未配置文档同步规则的运行时代码：{path}")
    return issues


def _document_version(text: str | None) -> str | None:
    """从文档头部提取 ``MAJOR.MINOR`` 形式的文档版本。"""
    if text is None:
        return None
    match = DOCUMENT_VERSION_PATTERN.search(text)
    return match.group(1) if match is not None else None


def _version_key(version: str) -> tuple[int, int]:
    major, minor = version.split(".", 1)
    return int(major), int(minor)


def find_document_version_issues(
    current_texts: dict[str, str],
    base_texts: dict[str, str | None],
) -> list[str]:
    """返回文档版本未递增的问题列表。

    只约束原本已有文档版本头的文档；新文档首次引入版本头时只需给出初始版本。
    """
    issues: list[str] = []
    for path in sorted(current_texts):
        base_version = _document_version(base_texts.get(path))
        if base_version is None:
            continue
        current_version = _document_version(current_texts[path])
        if current_version is None:
            issues.append(f"{path} 缺少“> 文档版本：MAJOR.MINOR”头。")
            continue
        if _version_key(current_version) <= _version_key(base_version):
            issues.append(
                f"{path} 的文档版本未递增：{base_version} → {current_version}；"
                "修改文档时必须递增文档版本。"
            )
    return issues


def _read_working_tree_text(path: str) -> str | None:
    try:
        return (ROOT / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _read_git_text(ref: str, path: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{ref}:{path}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout if completed.returncode == 0 else None


def find_changed_document_version_issues(
    changed_paths: set[str],
    base_ref: str,
    content_ref: str | None = None,
) -> list[str]:
    """检查本次变更中的文档是否递增了文档版本。"""
    selected = {path for path in changed_paths if _matches_any(path, DOCUMENT_PATTERNS)}
    current_texts: dict[str, str] = {}
    base_texts: dict[str, str | None] = {}
    for path in selected:
        current = (
            _read_git_text(content_ref, path)
            if content_ref is not None
            else _read_working_tree_text(path)
        )
        if current is not None:
            current_texts[path] = current
        base_texts[path] = _read_git_text(base_ref, path)
    return find_document_version_issues(current_texts, base_texts)


def _run_git_text(*arguments: str) -> str:
    command = ["git", "-C", str(ROOT), *arguments]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知 Git 错误"
        raise RuntimeError(f"Git 命令失败：{' '.join(command)}\n{detail}")
    return completed.stdout


def _run_git(*arguments: str) -> set[str]:
    return {
        line.strip().replace("\\", "/")
        for line in _run_git_text(*arguments).splitlines()
        if line.strip()
    }


def _merge_base(ref: str) -> str:
    """返回给定引用与 HEAD 的共同祖先，作为变更范围比较基线。"""
    base = _run_git_text("merge-base", ref, "HEAD").strip()
    if not base:
        raise RuntimeError(f"无法确定 {ref} 与 HEAD 的共同祖先。")
    return base


def changed_paths(mode: str, base: str | None = None) -> set[str]:
    """按指定模式读取需要检查的变更路径。"""
    common = ("--name-only", "--diff-filter=ACMR")
    if mode == "staged":
        return _run_git("diff", "--cached", *common)
    if mode == "working-tree":
        changed = _run_git("diff", *common, "HEAD")
        changed.update(_run_git("ls-files", "--others", "--exclude-standard"))
        return changed
    if mode == "base":
        if not base:
            raise ValueError("--base 模式必须提供 Git 引用。")
        return _run_git("diff", *common, f"{base}...HEAD")
    raise ValueError(f"不支持的检查模式：{mode}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--staged", action="store_true", help="检查暂存区相对 HEAD 的变更。")
    modes.add_argument("--base", metavar="REF", help="检查 REF...HEAD 范围内的变更。")
    modes.add_argument(
        "--working-tree",
        action="store_true",
        help="检查工作区和暂存区相对 HEAD 的变更。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行文档同步检查并返回进程退出码。"""
    arguments = _build_parser().parse_args(argv)
    try:
        rules, unmapped_fail = load_rules()
        problems = validate_rule_targets(RULE_FILE.parent.parent, rules)
        if arguments.staged:
            mode = "staged"
            base = None
        elif arguments.working_tree:
            mode = "working-tree"
            base = None
        else:
            mode = "base"
            base = arguments.base
        paths = changed_paths(mode, base)
        problems.extend(find_sync_issues(paths, rules, unmapped_fail))
        if base is not None:
            problems.extend(find_changed_document_version_issues(paths, _merge_base(base), "HEAD"))
        else:
            content_ref = ":0" if mode == "staged" else None
            problems.extend(find_changed_document_version_issues(paths, "HEAD", content_ref))
    except (RuntimeError, ValueError) as exc:
        print(f"[doc-sync] 配置或 Git 检查失败：{exc}", file=sys.stderr)
        return 2

    if problems:
        print("[doc-sync] 文档同步检查未通过：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "[doc-sync] 请在同一提交中更新对应文档；规则见 docs/doc-sync-rules.json。",
            file=sys.stderr,
        )
        return 1
    print(f"[doc-sync] 通过，共检查 {len(paths)} 个变更路径。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
