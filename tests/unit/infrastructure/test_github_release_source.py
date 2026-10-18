"""测试 GitHub Releases 更新源。"""

import io
import json
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError

import pytest

from serialmnt.domain import UpdateCheckError
from serialmnt.infrastructure import GithubReleaseSource
from serialmnt.infrastructure.github_release_source import MAX_RESPONSE_BYTES

_API_URL = "https://api.github.com/repos/MengYang-x/SerialMNT/releases/latest"
_RELEASE_URL = "https://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0"


class _Response:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str = _API_URL,
        status: int = 200,
    ) -> None:
        self._stream = io.BytesIO(payload)
        self._url = url
        self.status = status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _Opener:
    """记录请求的 urlopen 替身。"""

    def __init__(
        self,
        response: _Response | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, timeout: float) -> _Response:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "tag_name": "v0.3.0",
        "html_url": _RELEASE_URL,
        "body": "更新说明",
        "published_at": "2026-10-18T12:00:00Z",
        "draft": False,
        "prerelease": False,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _source(opener: _Opener) -> GithubReleaseSource:
    return GithubReleaseSource(opener=opener)


def test_request_uses_expected_url_headers_and_timeout() -> None:
    opener = _Opener(_Response(_payload()))
    release = _source(opener).fetch_latest()

    request, timeout = opener.calls[0]
    headers = {name.lower(): value for name, value in request.header_items()}  # type: ignore[attr-defined]
    assert request.full_url == _API_URL  # type: ignore[attr-defined]
    assert request.get_method() == "GET"  # type: ignore[attr-defined]
    assert headers["user-agent"].startswith("SerialMNT/")
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert timeout == 5.0
    assert release.version == "0.3.0"
    assert release.release_url == _RELEASE_URL
    assert release.notes == "更新说明"
    assert release.published_at == datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def test_null_body_becomes_empty_notes() -> None:
    release = _source(_Opener(_Response(_payload(body=None)))).fetch_latest()
    assert release.notes == ""


def test_invalid_published_at_is_ignored() -> None:
    release = _source(_Opener(_Response(_payload(published_at="not-a-date")))).fetch_latest()
    assert release.published_at is None


def test_tag_without_v_prefix_is_accepted() -> None:
    release = _source(_Opener(_Response(_payload(tag_name="0.3.0")))).fetch_latest()
    assert release.version == "0.3.0"


@pytest.mark.parametrize(
    ("tag_name", "html_url"),
    [
        ("0.2", _RELEASE_URL),
        ("0.2.3.4", _RELEASE_URL),
        ("0.2.3-beta", _RELEASE_URL),
        ("v0.3.0", "http://github.com/MengYang-x/SerialMNT/releases/tag/v0.3.0"),
        ("v0.3.0", "https://"),
    ],
)
def test_invalid_tag_or_release_url_is_rejected(tag_name: str, html_url: str) -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(_payload(tag_name=tag_name, html_url=html_url)))).fetch_latest()


@pytest.mark.parametrize("field", ["tag_name", "html_url"])
def test_missing_required_field_is_rejected(field: str) -> None:
    payload = json.loads(_payload())
    payload.pop(field)
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(json.dumps(payload).encode("utf-8")))).fetch_latest()


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_draft_or_prerelease_is_rejected(flag: str) -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(_payload(**{flag: True})))).fetch_latest()


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"\xff\xfe"])
def test_malformed_payload_is_rejected(payload: bytes) -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(payload))).fetch_latest()


def test_non_string_body_is_rejected() -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(_payload(body=3)))).fetch_latest()


def test_oversized_response_is_rejected() -> None:
    payload = b"x" * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(payload))).fetch_latest()


def test_non_https_final_url_is_rejected() -> None:
    response = _Response(_payload(), url="http://api.github.com/releases/latest")
    with pytest.raises(UpdateCheckError):
        _source(_Opener(response)).fetch_latest()


def test_unexpected_status_is_rejected() -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(_Response(_payload(), status=201))).fetch_latest()


@pytest.mark.parametrize("code", [403, 404, 500])
def test_http_error_is_converted(code: int) -> None:
    error = HTTPError(_API_URL, code, "error", {}, None)
    with pytest.raises(UpdateCheckError):
        _source(_Opener(error=error)).fetch_latest()


@pytest.mark.parametrize(
    "error",
    [URLError("offline"), TimeoutError("timed out"), HTTPException("broken response")],
)
def test_network_error_is_converted(error: BaseException) -> None:
    with pytest.raises(UpdateCheckError):
        _source(_Opener(error=error)).fetch_latest()


def test_custom_repository_changes_url() -> None:
    opener = _Opener(_Response(_payload()))
    GithubReleaseSource("owner/repo", opener=opener).fetch_latest()
    request, _ = opener.calls[0]
    assert request.full_url == "https://api.github.com/repos/owner/repo/releases/latest"  # type: ignore[attr-defined]


@pytest.mark.parametrize("repository", ["", "owner", "owner/repo/extra", "../x/y"])
def test_invalid_repository_is_rejected(repository: str) -> None:
    with pytest.raises(ValueError):
        GithubReleaseSource(repository)


def test_invalid_timeout_is_rejected() -> None:
    with pytest.raises(ValueError):
        GithubReleaseSource(timeout=0)
