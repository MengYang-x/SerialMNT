"""测试版本解析、比较和发布信息模型。"""

from datetime import datetime, timezone

import pytest

from serialmnt.domain import (
    ReleaseInfo,
    ValidationError,
    is_newer_version,
    normalize_version,
    parse_version,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.2.3", (0, 2, 3)),
        ("v1.10.20", (1, 10, 20)),
        ("V0.0.0", (0, 0, 0)),
        ("  0.3.0  ", (0, 3, 0)),
        ("01.002.0003", (1, 2, 3)),
    ],
)
def test_parse_version_accepts_semver_and_v_prefix(
    text: str, expected: tuple[int, int, int]
) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "v", "0.2", "0.2.3.4", "0.2.x", "0.2.3-beta", "0.2.3+meta", "0.2.3 beta"],
)
def test_parse_version_rejects_non_semver(text: str) -> None:
    with pytest.raises(ValidationError):
        parse_version(text)


def test_parse_version_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        parse_version(3)  # type: ignore[arg-type]


def test_normalize_version_strips_prefix_and_leading_zeros() -> None:
    assert normalize_version("v01.002.0003") == "1.2.3"


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("0.2.3", "0.2.3", False),
        ("0.2.3", "0.2.4", True),
        ("0.2.3", "0.3.0", True),
        ("0.9.9", "0.10.0", True),
        ("1.0.0", "0.99.99", False),
        ("v0.2.3", "0.3.0", True),
    ],
)
def test_is_newer_version_compares_numeric_segments(
    current: str, latest: str, expected: bool
) -> None:
    assert is_newer_version(current, latest) is expected


def test_release_info_normalizes_v_prefix_and_accepts_aware_timestamp() -> None:
    published = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    release = ReleaseInfo(
        version="v0.3.0",
        release_url="https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0",
        notes="更新说明",
        published_at=published,
    )

    assert release.version == "0.3.0"
    assert release.published_at == published


@pytest.mark.parametrize(
    "kwargs",
    [
        {"version": "0.3", "release_url": "https://github.com/x"},
        {"version": "0.3.0", "release_url": "http://github.com/x"},
        {"version": "0.3.0", "release_url": "ftp://github.com/x"},
        {"version": "0.3.0", "release_url": "https://"},
        {"version": 3, "release_url": "https://github.com/x"},
        {"version": "0.3.0", "release_url": 123},
        {"version": "0.3.0", "release_url": "https://github.com/x", "notes": None},
        {
            "version": "0.3.0",
            "release_url": "https://github.com/x",
            "published_at": "2026-10-18",
        },
        {
            "version": "0.3.0",
            "release_url": "https://github.com/x",
            "published_at": datetime(2026, 10, 18),
        },
    ],
)
def test_release_info_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ReleaseInfo(**kwargs)
