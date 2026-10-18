"""GitHub Releases API 更新源。"""

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from serialmnt import __version__
from serialmnt.domain import ReleaseInfo, UpdateCheckError, ValidationError, normalize_version

logger = logging.getLogger(__name__)

DEFAULT_REPOSITORY = "MengYang-x/SerialMNT"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 256 * 1024
HTTP_OK = 200
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
_API_URL = "https://api.github.com/repos/{repository}/releases/latest"
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GithubReleaseSource:
    """从 GitHub Releases API 读取最新正式版本。

    请求只包含固定地址和必要的请求头，不发送查询参数、Cookie、令牌或设备
    标识。``opener`` 仅用于测试注入；生产默认使用标准库 ``urlopen``。
    """

    def __init__(
        self,
        repository: str = DEFAULT_REPOSITORY,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] = urlopen,  # 外部边界：允许注入签名宽松的 urlopen 替身
    ) -> None:
        if _REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise ValueError("repository 必须为 owner/name 格式。")
        if timeout <= 0:
            raise ValueError("timeout 必须为正数。")
        self._url = _API_URL.format(repository=repository)
        self._timeout = timeout
        self._opener = opener

    def fetch_latest(self) -> ReleaseInfo:
        """返回最新正式版本；网络或响应异常时抛出 UpdateCheckError。"""
        request = Request(
            self._url,
            headers={
                "User-Agent": f"SerialMNT/{__version__}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                self._require_https(response.geturl())
                status = getattr(response, "status", HTTP_OK)
                if status != HTTP_OK:
                    raise UpdateCheckError(f"更新服务返回 HTTP {status}。")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            logger.debug("GitHub Releases API 返回 HTTP %s。", exc.code)
            if exc.code == HTTP_NOT_FOUND:
                raise UpdateCheckError("当前没有可用的正式版本。") from exc
            if exc.code == HTTP_FORBIDDEN:
                raise UpdateCheckError("更新服务访问受限，请稍后重试。") from exc
            raise UpdateCheckError(f"更新服务返回 HTTP {exc.code}。") from exc
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            logger.debug("访问 GitHub Releases API 失败。", exc_info=True)
            raise UpdateCheckError("无法连接更新服务，请检查网络后重试。") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise UpdateCheckError("更新服务响应过大。")
        return self._parse_payload(raw)

    @staticmethod
    def _require_https(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise UpdateCheckError("更新服务返回了不安全的地址。")

    @classmethod
    def _parse_payload(cls, raw: bytes) -> ReleaseInfo:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateCheckError("更新信息格式不正确。") from exc
        if not isinstance(payload, dict):
            raise UpdateCheckError("更新信息格式不正确。")
        tag_name = payload.get("tag_name")
        release_url = payload.get("html_url")
        if not isinstance(tag_name, str) or not tag_name.strip():
            raise UpdateCheckError("更新信息缺少版本号。")
        if not isinstance(release_url, str) or not release_url.strip():
            raise UpdateCheckError("更新信息缺少发布页地址。")
        if payload.get("draft") is True or payload.get("prerelease") is True:
            raise UpdateCheckError("最新版本尚不可用。")
        notes = payload.get("body")
        if notes is None:
            notes = ""
        if not isinstance(notes, str):
            raise UpdateCheckError("更新说明格式不正确。")
        published_at = cls._parse_timestamp(payload.get("published_at"))
        try:
            return ReleaseInfo(
                version=normalize_version(tag_name),
                release_url=release_url,
                notes=notes,
                published_at=published_at,
            )
        except ValidationError as exc:
            raise UpdateCheckError("更新信息中的版本号或地址不合法。") from exc

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed
