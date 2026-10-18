"""校验版本号、文档版本头和用户手册版本记录是否同步。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = Path("src/serialmnt/__init__.py")
HEADER_FILES = (
    Path("docs/README.md"),
    Path("docs/user-guide.md"),
    Path("docs/developer-guide.md"),
)
USER_GUIDE = Path("docs/user-guide.md")
IMPLEMENTATION_PLAN = Path("docs/implementation-plan.md")
PACKAGED_GUIDE = Path("src/serialmnt/ui/assets/user-guide.md")
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PLAN_VERSION_PATTERN = re.compile(r"当前软件版本为\s*`([^`]+)`")
VERSION_HEADING = "## 10. 软件版本"
VERSION_ENTRY_PATTERN = re.compile(r"^\s*-\s*V(?P<version>\d+\.\d+\.\d+)\s*$", re.MULTILINE)


def read_version(root: Path) -> str:
    """从唯一版本源读取当前版本号。"""
    try:
        text = (root / VERSION_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法从 {VERSION_FILE.as_posix()} 读取 __version__：{exc}") from exc
    match = VERSION_PATTERN.search(text)
    if match is None:
        raise ValueError(f"未在 {VERSION_FILE.as_posix()} 中找到 __version__。")
    return match.group(1)


def find_version_issues(root: Path) -> list[str]:
    """返回版本号、文档版本头、版本记录和随包手册之间的所有不一致。"""
    try:
        version = read_version(root)
    except (OSError, ValueError) as exc:
        return [str(exc)]

    issues: list[str] = []
    if not SEMVER_PATTERN.fullmatch(version):
        issues.append(f"版本号 {version!r} 不是 MAJOR.MINOR.PATCH 格式。")
    header = f"> 软件版本：{version}"
    for relative in HEADER_FILES:
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(f"无法读取 {relative.as_posix()}：{exc}")
            continue
        if header not in text:
            issues.append(f"{relative.as_posix()} 缺少版本头“{header}”。")

    try:
        plan_text = (root / IMPLEMENTATION_PLAN).read_text(encoding="utf-8")
    except OSError as exc:
        issues.append(f"无法读取 {IMPLEMENTATION_PLAN.as_posix()}：{exc}")
    else:
        plan_version = PLAN_VERSION_PATTERN.search(plan_text)
        if plan_version is None:
            issues.append(f"{IMPLEMENTATION_PLAN.as_posix()} 缺少“当前软件版本为 `x.y.z`”说明。")
        elif plan_version.group(1) != version:
            issues.append(
                f"{IMPLEMENTATION_PLAN.as_posix()} 的当前软件版本为 "
                f"“{plan_version.group(1)}”，应为“{version}”。"
            )

    guide_path = root / USER_GUIDE
    try:
        guide_bytes = guide_path.read_bytes()
        guide_text = guide_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        issues.append(f"无法读取 {USER_GUIDE.as_posix()}：{exc}")
    else:
        issues.extend(_version_entry_issues(guide_text, version))
        try:
            packaged_bytes = (root / PACKAGED_GUIDE).read_bytes()
        except OSError as exc:
            issues.append(f"无法读取 {PACKAGED_GUIDE.as_posix()}：{exc}")
        else:
            if packaged_bytes != guide_bytes:
                issues.append(
                    f"{PACKAGED_GUIDE.as_posix()} 未与 {USER_GUIDE.as_posix()} 逐字节同步。"
                )
    return issues


def _version_entry_issues(guide_text: str, version: str) -> list[str]:
    if VERSION_HEADING not in guide_text:
        return [f"{USER_GUIDE.as_posix()} 缺少“{VERSION_HEADING}”章节。"]
    section = guide_text.split(VERSION_HEADING, 1)[1]
    entries = VERSION_ENTRY_PATTERN.findall(section)
    if version not in entries:
        return [f"{USER_GUIDE.as_posix()} 的“{VERSION_HEADING}”缺少“- V{version}”条目。"]
    if entries[0] != version:
        return [
            f"{USER_GUIDE.as_posix()} 的最新版本条目应为“- V{version}”，"
            f"实际为“- V{entries[0]}”；新版本需放在最上方。"
        ]
    return []


def main() -> int:
    """执行检查并返回进程退出码。"""
    issues = find_version_issues(ROOT)
    if issues:
        print("[version-sync] 版本同步检查未通过：", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"[version-sync] 版本同步检查通过：{read_version(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
