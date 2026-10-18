"""测试更新检查用例。"""

import pytest

from serialmnt.application import UpdateCheckResult, UpdateService, UpdateStatus
from serialmnt.domain import ErrorCode, ReleaseInfo, UpdateCheckError, ValidationError


def _release(version: str) -> ReleaseInfo:
    return ReleaseInfo(
        version=version,
        release_url=f"https://github.com/MengYang-x/SerialMNT/releases/tag/v{version}",
    )


class _FakeSource:
    """按预设内容返回或抛出异常的更新源。"""

    def __init__(
        self,
        release: ReleaseInfo | None = None,
        error: UpdateCheckError | None = None,
    ) -> None:
        self.release = release
        self.error = error
        self.calls = 0

    def fetch_latest(self) -> ReleaseInfo:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.release is not None
        return self.release


def test_newer_release_returns_update_available() -> None:
    source = _FakeSource(_release("0.3.0"))
    result = UpdateService(source, current_version="0.2.3").check()

    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert result.release is not None
    assert result.release.version == "0.3.0"
    assert result.error_code is None
    assert source.calls == 1


@pytest.mark.parametrize("version", ["0.2.3", "0.2.2", "0.1.9"])
def test_same_or_older_release_is_up_to_date(version: str) -> None:
    source = _FakeSource(_release(version))
    result = UpdateService(source, current_version="0.2.3").check()

    assert result.status is UpdateStatus.UP_TO_DATE
    assert result.release is None


def test_update_check_error_becomes_failed_result() -> None:
    error = UpdateCheckError("网络不可达。")
    result = UpdateService(_FakeSource(error=error), current_version="0.2.3").check()

    assert result.status is UpdateStatus.FAILED
    assert result.error_code is ErrorCode.UPDATE_CHECK_FAILED
    assert result.error_message == "网络不可达。"


def test_unexpected_exception_is_not_swallowed() -> None:
    class _BrokenSource:
        def fetch_latest(self) -> ReleaseInfo:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        UpdateService(_BrokenSource(), current_version="0.2.3").check()


def test_invalid_current_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        UpdateService(_FakeSource(_release("0.3.0")), current_version="0.2")


def test_status_string_and_current_version() -> None:
    service = UpdateService(_FakeSource(_release("0.3.0")), current_version="0.2.3")

    assert str(UpdateStatus.UPDATE_AVAILABLE) == "update_available"
    assert service.current_version == "0.2.3"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "update_available", "current_version": "0.2.3"},
        {"status": UpdateStatus.UPDATE_AVAILABLE, "current_version": "0.2.3"},
        {
            "status": UpdateStatus.UP_TO_DATE,
            "current_version": "0.2.3",
            "release": _release("0.2.3"),
        },
        {"status": UpdateStatus.FAILED, "current_version": "0.2.3"},
        {
            "status": UpdateStatus.UP_TO_DATE,
            "current_version": "0.2.3",
            "error_code": ErrorCode.INTERNAL_ERROR,
        },
    ],
)
def test_result_rejects_inconsistent_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        UpdateCheckResult(**kwargs)
