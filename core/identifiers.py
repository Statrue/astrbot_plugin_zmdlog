"""Safe account and battle identifiers extracted from commands and URLs."""

import re
from urllib.parse import unquote, urlsplit


class PublicReferenceError(ValueError):
    """Raised when a user-supplied public identifier or URL is unsupported."""


_ACCOUNT_ID_PATTERN = re.compile(r"^usr_[A-Za-z0-9_-]{1,124}$")
_BATTLE_ID_PATTERN = re.compile(r"^btl_[A-Za-z0-9_-]{1,124}$")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ",.!?;:，。！？；：、）》】」』"


def is_valid_account_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and _ACCOUNT_ID_PATTERN.fullmatch(value) is not None
    )


def is_valid_battle_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and _BATTLE_ID_PATTERN.fullmatch(value) is not None
    )


def parse_account_reference(value: str, *, web_base_url: str) -> str:
    """Return an account ID from an exact ID or a trusted records URL."""

    normalized = value.strip().rstrip(_TRAILING_URL_PUNCTUATION)
    if is_valid_account_id(normalized):
        return normalized

    segments = _trusted_path_segments(normalized, web_base_url=web_base_url)
    if len(segments) == 2 and segments[0] == "records":
        account_id = unquote(segments[1])
        if is_valid_account_id(account_id):
            return account_id
    raise PublicReferenceError(
        "账号查询目前只支持 accountId 或 ZMDLogs 账号主页链接。"
    )


def parse_battle_reference(value: str, *, web_base_url: str) -> str:
    """Return a battle ID from an exact ID or a trusted battle-related URL."""

    normalized = value.strip().rstrip(_TRAILING_URL_PUNCTUATION)
    if is_valid_battle_id(normalized):
        return normalized

    segments = _trusted_path_segments(normalized, web_base_url=web_base_url)
    if len(segments) >= 2 and segments[0] in {"battle", "share", "axis"}:
        if segments[0] == "axis" and len(segments) not in {2, 3}:
            raise PublicReferenceError("不支持这个 ZMDLogs 战报链接格式。")
        if segments[0] != "axis" and len(segments) != 2:
            raise PublicReferenceError("不支持这个 ZMDLogs 战报链接格式。")
        if len(segments) == 3 and segments[2] != "editor":
            raise PublicReferenceError("不支持这个 ZMDLogs 战报链接格式。")
        battle_id = unquote(segments[1])
        if is_valid_battle_id(battle_id):
            return battle_id
    raise PublicReferenceError(
        "战报查询只支持 battleId 或 ZMDLogs 战报、分享、排轴链接。"
    )


def extract_battle_references(
    message: str,
    *,
    web_base_url: str,
    limit: int = 1,
) -> tuple[str, ...]:
    """Extract unique trusted battle IDs from arbitrary message text."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    result: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(message):
        raw_url = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        try:
            battle_id = parse_battle_reference(
                raw_url,
                web_base_url=web_base_url,
            )
        except PublicReferenceError:
            continue
        if battle_id in seen:
            continue
        result.append(battle_id)
        seen.add(battle_id)
        if len(result) >= limit:
            break
    return tuple(result)


def _trusted_path_segments(value: str, *, web_base_url: str) -> tuple[str, ...]:
    try:
        parsed = urlsplit(value)
        trusted = urlsplit(web_base_url.strip())
    except ValueError:
        return ()
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ()
    if trusted.scheme not in {"http", "https"} or not trusted.netloc:
        return ()
    try:
        # ``.port`` raises ValueError for ":abc" or ":99999"; that is simply
        # not a trusted link, and one bad character in web_base_url must not
        # turn every link parse into an unhandled error either.
        if _origin(parsed) != _origin(trusted):
            return ()
    except ValueError:
        return ()

    trusted_prefix = tuple(segment for segment in trusted.path.split("/") if segment)
    path_segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if path_segments[: len(trusted_prefix)] != trusted_prefix:
        return ()
    return path_segments[len(trusted_prefix) :]


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(parsed) -> tuple[str, str, int | None]:
    scheme = parsed.scheme.casefold()
    port = parsed.port
    if port is None:
        # ``https://host:443/...`` and ``https://host/...`` are the same origin.
        port = _DEFAULT_PORTS.get(scheme)
    return scheme, (parsed.hostname or "").casefold(), port
