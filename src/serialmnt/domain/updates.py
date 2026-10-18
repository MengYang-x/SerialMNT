"""版本解析、比较和发布信息模型。"""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from .errors import ValidationError

_VERSION_PATTERN = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int]:
    """把 MAJOR.MINOR.PATCH 文本解析为整数元组。

    允许 Release tag 使用一个可选的 ``v`` 前缀。预发布后缀、构建元数据和
    第四段版本号都不受支持；版本比较必须使用返回的整数元组，不能直接比较
    字符串，否则 ``0.10.0`` 会被误判为小于 ``0.9.9``。
    """
    if not isinstance(text, str):
        raise ValidationError("版本号必须是字符串。")
    match = _VERSION_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValidationError("版本号必须为 MAJOR.MINOR.PATCH 格式。")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def normalize_version(text: str) -> str:
    """返回去除可选 ``v`` 前缀和前导零后的规范版本号。"""
    major, minor, patch = parse_version(text)
    return f"{major}.{minor}.{patch}"


def is_newer_version(current: str, latest: str) -> bool:
    """判断 ``latest`` 是否比 ``current`` 更新。"""
    return parse_version(latest) > parse_version(current)


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """一次正式发布的最小信息。"""

    version: str
    release_url: str
    notes: str = ""
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        # 允许调用方直接传入 GitHub 的 vX.Y.Z tag，统一保存规范版本号。
        object.__setattr__(self, "version", normalize_version(self.version))
        if not isinstance(self.release_url, str):
            raise ValidationError("发布页面地址必须是字符串。")
        parsed = urlparse(self.release_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValidationError("发布页面地址必须使用 HTTPS。")
        if not isinstance(self.notes, str):
            raise ValidationError("更新说明必须是字符串。")
        if self.published_at is not None:
            if not isinstance(self.published_at, datetime):
                raise ValidationError("发布时间必须是 datetime 或 None。")
            if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
                raise ValidationError("发布时间必须包含时区信息。")
