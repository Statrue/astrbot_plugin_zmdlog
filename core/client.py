"""Asynchronous client for the public ZMDLogs ranking API."""

import asyncio
import re
import time
from typing import Any

import httpx

from .identifiers import is_valid_account_id, is_valid_battle_id
from .models import (
    BattleDetailSummary,
    BossRanking,
    HotBossCard,
    ModelValidationError,
    PublicUserRankings,
    parse_battle_detail,
    parse_boss_ranking,
    parse_hot_bosses,
    parse_public_user_rankings,
)

DEFAULT_API_BASE_URL = "https://zmdlogs.com"
DEFAULT_REQUEST_TIMEOUT_MS = 10_000
_BOSS_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_REQUEST_ATTEMPTS = 2
_MAX_TOTAL_WAIT_SECONDS = 15.0


class ZmdLogsClientError(Exception):
    """Base exception for safe, expected client failures."""


class InvalidBossSlugError(ZmdLogsClientError):
    """Raised before a request when a boss slug is unsafe or malformed."""


class InvalidPublicIdentifierError(ZmdLogsClientError):
    """Raised before a request when an account or battle ID is malformed."""


class ZmdLogsAPIError(ZmdLogsClientError):
    """An error response returned by ZMDLogs."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ZmdLogsProtocolError(ZmdLogsClientError):
    """Raised when a successful response violates the public API contract."""


def is_valid_boss_slug(value: str) -> bool:
    """Return whether ``value`` is safe to place in the ranking path."""

    return isinstance(value, str) and _BOSS_SLUG_PATTERN.fullmatch(value) is not None


class ZmdLogsClient:
    """Fetch and adapt the public ZMDLogs APIs used by the plugin."""

    def __init__(
        self,
        api_base_url: str = DEFAULT_API_BASE_URL,
        request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base_url = _validate_base_url(api_base_url)
        timeout_seconds = _validate_timeout(request_timeout_ms) / 1000
        self._request_timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "astrbot_plugin_zmdlog",
            },
        )

    async def list_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Return every card from ``GET /api/home/hot-bosses`` in API order."""

        payload = await self._get_json("api/home/hot-bosses")
        try:
            return parse_hot_bosses(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("hot-bosses response is invalid") from exc

    async def get_boss_rankings(self, boss_slug: str) -> BossRanking:
        """Return one complete DPS ranking without sending a metric parameter."""

        if not is_valid_boss_slug(boss_slug):
            raise InvalidBossSlugError("invalid boss slug")

        payload = await self._get_json(f"api/bosses/{boss_slug}/rankings")
        try:
            return parse_boss_ranking(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("boss ranking response is invalid") from exc

    async def get_public_user_rankings(
        self,
        account_id: str,
    ) -> PublicUserRankings:
        """Return one exact public account's best records."""

        if not is_valid_account_id(account_id):
            raise InvalidPublicIdentifierError("invalid account id")
        payload = await self._get_json(
            f"api/battles/users/{account_id}/rankings"
        )
        try:
            return parse_public_user_rankings(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError(
                "public user rankings response is invalid"
            ) from exc

    async def get_battle_detail(self, battle_id: str) -> BattleDetailSummary:
        """Return the compact fields needed from one full public battle."""

        if not is_valid_battle_id(battle_id):
            raise InvalidPublicIdentifierError("invalid battle id")
        payload = await self._get_json(f"api/battles/{battle_id}")
        try:
            return parse_battle_detail(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("battle detail response is invalid") from exc

    async def close(self) -> None:
        """Close the underlying connection pool."""

        await self._client.aclose()

    async def _get_json(self, path: str) -> Any:
        deadline = time.monotonic() + _MAX_TOTAL_WAIT_SECONDS
        last_request_error: Exception | None = None
        for attempt in range(_MAX_REQUEST_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts_left = _MAX_REQUEST_ATTEMPTS - attempt
            attempt_timeout = min(
                self._request_timeout_seconds,
                remaining / attempts_left,
            )
            try:
                async with asyncio.timeout(attempt_timeout):
                    response = await self._client.get(
                        path,
                        timeout=httpx.Timeout(attempt_timeout),
                    )
            except (httpx.RequestError, TimeoutError) as exc:
                last_request_error = exc
                continue

            if response.status_code >= 500 and attempt + 1 < _MAX_REQUEST_ATTEMPTS:
                continue
            return _decode_response(response)

        raise ZmdLogsClientError("ZMDLogs request failed") from last_request_error


def _decode_response(response: httpx.Response) -> Any:
    """Decode one final response after retry policy has been applied."""

    if not 200 <= response.status_code < 300:
        code, message = _read_api_error(response)
        raise ZmdLogsAPIError(response.status_code, code, message)

    try:
        return response.json()
    except ValueError as exc:
        raise ZmdLogsProtocolError("ZMDLogs returned invalid JSON") from exc


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("api_base_url must be a string")
    try:
        url = httpx.URL(value.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("api_base_url is invalid") from exc

    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError("api_base_url must be an absolute HTTP(S) URL")
    if url.query or url.fragment:
        raise ValueError("api_base_url cannot contain a query or fragment")

    return str(url.copy_with(path=f"{url.path.rstrip('/')}/"))


def _validate_timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("request_timeout_ms must be a positive integer")
    return value


def _read_api_error(response: httpx.Response) -> tuple[str, str]:
    default_code = f"http_{response.status_code}"
    default_message = "ZMDLogs 暂时无法完成请求。"
    try:
        payload = response.json()
    except ValueError:
        return default_code, default_message

    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return default_code, default_message

    error = payload["error"]
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else default_code,
        message if isinstance(message, str) else default_message,
    )
