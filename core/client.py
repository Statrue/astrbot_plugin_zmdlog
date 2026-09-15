"""Asynchronous client for the public ZMDLogs ranking API."""

import asyncio
import logging
import re
import time
import unicodedata
from typing import Any

import httpx

from .bindings import BINDING_CODE_RE
from .identifiers import is_valid_account_id, is_valid_battle_id
from .models import (
    AccountSearch,
    AccountSearchHit,
    BattleDetailSummary,
    BattleExport,
    BossRanking,
    CharacterBossStatistics,
    CharacterStatistics,
    CharacterType,
    EquipSuit,
    HotBossCard,
    ModelValidationError,
    PublicUserRankings,
    parse_account_search,
    parse_battle_detail,
    parse_battle_export,
    parse_binding_code_account,
    parse_boss_ranking,
    parse_character_boss_statistics,
    parse_character_statistics,
    parse_character_types,
    parse_equip_catalog,
    parse_hot_bosses,
    parse_public_user_rankings,
)

DEFAULT_API_BASE_URL = "https://zmdlogs.com"
# Sent with every request so the site operator can tell this plugin apart and
# name a version when something misbehaves.
DEFAULT_USER_AGENT = "astrbot_plugin_zmdlog"
DEFAULT_REQUEST_TIMEOUT_MS = 10_000
_BOSS_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_REQUEST_ATTEMPTS = 2
_MAX_TOTAL_WAIT_SECONDS = 15.0
_STATS_RANGES = frozenset({"7d", "14d", "30d", "all"})
MIN_ACCOUNT_SEARCH_LENGTH = 2
MAX_ACCOUNT_SEARCH_LENGTH = 64
_CHARACTER_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_STATS_POTENTIALS = frozenset({"0", "1-5", "all"})


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


# httpx logs every request URL at INFO, and AstrBot shows that level. A
# binding code travels as a query parameter and is a bearer credential for
# ten minutes, so the one line that would print it is redacted instead.
_BINDING_CODE_IN_URL = re.compile(r"(code=)ZMD-[0-9A-Z]{4}-[0-9A-Z]{4}", re.IGNORECASE)


class _RedactBindingCode(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a malformed record is not ours
            return True
        if "binding-code" in message:
            record.msg = _BINDING_CODE_IN_URL.sub(r"\1ZMD-****-****", message)
            record.args = ()
        return True


def install_log_redaction() -> None:
    """Keep binding codes out of httpx's request log; idempotent.

    Matched by class name, not identity: a plugin reload imports this module
    again, and the filter a previous load installed on the process-wide
    logger is an instance of the previous class object.
    """

    logger = logging.getLogger("httpx")
    name = _RedactBindingCode.__name__
    if not any(type(entry).__name__ == name for entry in logger.filters):
        logger.addFilter(_RedactBindingCode())


def searchable_nickname(query: str) -> str | None:
    """The form of ``query`` the account search accepts, or None if it would refuse.

    Upstream measures the NFKC-normalised, stripped text; every caller that
    decides whether a nickname is worth a request must measure the same way,
    or a fullwidth nickname passes here and is refused there.
    """

    normalized = unicodedata.normalize("NFKC", query).strip()
    if MIN_ACCOUNT_SEARCH_LENGTH <= len(normalized) <= MAX_ACCOUNT_SEARCH_LENGTH:
        return normalized
    return None


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
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        base_url = _validate_base_url(api_base_url)
        timeout_seconds = _validate_timeout(request_timeout_ms) / 1000
        self._request_timeout_seconds = timeout_seconds
        install_log_redaction()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            verify=_ssl_context(),
            headers={
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
        )

    async def list_hot_bosses(self) -> tuple[HotBossCard, ...]:
        """Return every card from ``GET /api/home/hot-bosses`` in API order."""

        cards, _ = await self.list_hot_bosses_with_payload()
        return cards

    async def list_hot_bosses_with_payload(
        self,
    ) -> tuple[tuple[HotBossCard, ...], Any]:
        """Like :meth:`list_hot_bosses` but also return the raw JSON payload.

        The payload lets callers persist a snapshot that can be re-parsed with
        :func:`parse_hot_bosses` when the upstream API is unreachable.
        """

        payload = await self._get_json("api/home/hot-bosses")
        try:
            return parse_hot_bosses(payload), payload
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("hot-bosses response is invalid") from exc

    async def get_equip_catalog(self) -> tuple[EquipSuit, ...]:
        """Return every gear suit the game data catalog names.

        Static game data, public and unauthenticated, roughly two dozen
        entries. It is the authority on which suit an item belongs to;
        ``suitName`` inside a battle is filled per upload and contradicts
        itself, sometimes naming a different suit outright.
        """

        payload = await self._get_json("api/game-data/equip")
        try:
            return parse_equip_catalog(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("equip catalog response is invalid") from exc

    async def get_character_types(self) -> tuple[CharacterType, ...]:
        """Return every character's element and weapon type.

        Static game data, public and unauthenticated, about thirty entries;
        the only place upstream states a character's element outside a
        battle's own roster.
        """

        payload = await self._get_json("api/game-data/character")
        try:
            return parse_character_types(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError(
                "character catalog response is invalid"
            ) from exc

    async def get_boss_rankings(self, boss_slug: str) -> BossRanking:
        """Return one complete DPS ranking without sending a metric parameter."""

        if not is_valid_boss_slug(boss_slug):
            raise InvalidBossSlugError("invalid boss slug")

        payload = await self._get_json(f"api/bosses/{boss_slug}/rankings")
        try:
            return parse_boss_ranking(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("boss ranking response is invalid") from exc

    async def get_character_statistics(
        self,
        boss_slug: str | None,
        *,
        time_range: str = "all",
        potential: str = "all",
    ) -> CharacterStatistics:
        """Return DPS character statistics for one board or for all boards.

        ``boss_slug=None`` targets the global endpoint. The ``metric`` query
        parameter is deliberately never sent so the upstream DPS default applies.
        """

        if time_range not in _STATS_RANGES:
            raise ValueError("invalid statistics range")
        if potential not in _STATS_POTENTIALS:
            raise ValueError("invalid statistics potential filter")
        if boss_slug is None:
            path = "api/bosses/character-statistics"
        else:
            if not is_valid_boss_slug(boss_slug):
                raise InvalidBossSlugError("invalid boss slug")
            path = f"api/bosses/{boss_slug}/character-statistics"

        payload = await self._get_json(
            path,
            params={"range": time_range, "potential": potential},
        )
        try:
            return parse_character_statistics(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError(
                "character statistics response is invalid"
            ) from exc

    async def search_public_accounts(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> AccountSearch:
        """Search public accounts by nickname substring (2-64 chars)."""

        normalized = searchable_nickname(query)
        if normalized is None:
            raise InvalidPublicIdentifierError("invalid account search query")
        if not 1 <= limit <= 20:
            raise ValueError("account search limit must be between 1 and 20")

        payload = await self._get_json(
            "api/battles/users/search",
            params={"query": normalized, "limit": str(limit)},
        )
        try:
            return parse_account_search(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError(
                "account search response is invalid"
            ) from exc

    async def get_binding_code_account(self, code: str) -> AccountSearchHit:
        """The public account a binding code was issued to.

        ``GET /api/battles/users/binding-code?code=`` is anonymous and
        read-only: it neither consumes nor extends the code, so the caller
        must remember redeemed codes itself. Never cached and never logged
        with the code (see :func:`install_log_redaction`). A 404
        ``binding_code_invalid`` means unknown, expired, replaced or a
        disabled account; a 404 with any other code means the deployment
        behind ``api_base_url`` (a mirror, an old build) has no such endpoint.
        """

        if not BINDING_CODE_RE.match(code):
            raise InvalidPublicIdentifierError("invalid binding code")
        payload = await self._get_json(
            "api/battles/users/binding-code", params={"code": code}
        )
        try:
            return parse_binding_code_account(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("binding code response is invalid") from exc

    async def get_character_boss_statistics(
        self,
        character_key: str,
        *,
        time_range: str = "all",
        potential: str = "all",
    ) -> CharacterBossStatistics:
        """Return one character's DPS distribution on every statistics board.

        The ``metric`` query parameter is deliberately never sent so the
        upstream DPS default applies.
        """

        if time_range not in _STATS_RANGES:
            raise ValueError("invalid statistics range")
        if potential not in _STATS_POTENTIALS:
            raise ValueError("invalid statistics potential filter")
        if not _CHARACTER_KEY_PATTERN.match(character_key):
            raise InvalidBossSlugError("invalid character key")

        payload = await self._get_json(
            f"api/characters/{character_key}/boss-statistics",
            params={"range": time_range, "potential": potential},
        )
        try:
            return parse_character_boss_statistics(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError(
                "character boss statistics response is invalid"
            ) from exc

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

    async def get_battle_export(self, battle_id: str) -> BattleExport:
        """Return the cast sequence of one public battle.

        The export endpoint is the read-only contract upstream keeps for
        external axis tools: public battles only, per-IP rate limited, and a
        422 ``battle_export_unsupported`` for uploads too old to carry casts.
        """

        if not is_valid_battle_id(battle_id):
            raise InvalidPublicIdentifierError("invalid battle id")
        payload = await self._get_json(f"api/v1/battles/{battle_id}/export")
        try:
            return parse_battle_export(payload)
        except ModelValidationError as exc:
            raise ZmdLogsProtocolError("battle export response is invalid") from exc

    async def close(self) -> None:
        """Close the underlying connection pool."""

        await self._client.aclose()

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
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
                        params=params,
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


_SSL_CONTEXT = None


def _ssl_context():
    """One verified SSL context per process.

    Building one loads the system trust store (about 150 ms); httpx builds
    a fresh one per client, and every plugin reload makes a client.
    """

    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT
