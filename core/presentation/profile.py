"""角色档案: one character's shares and its 通关名次 on every board."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..elements import element_key
from ..loadout import is_raw_item_name
from ..models import CharacterProfile, CharacterType, ProfileShare
from ..professions import normalize_profession
from .common import (
    _ASSET_ID_RE,
    _CHARACTER_AVATAR_PATH,
    _EQUIP_ICON_PATH,
    _RANGE_LABELS,
    _WEAPON_ICON_PATH,
    PageHeader,
    _derived_asset_url,
    _initial,
    _safe_asset_url,
    format_duration,
    format_number,
    investment_view,
)

# Rows a share block lists; the rest are counted under it. Twelve teammates
# or twenty combinations are a long tail of single records.
SHARE_ROWS = 8
SAMPLE_RULE = (
    "只统计带该角色的公开有效通关记录，同一账号在同一榜单只留最快一场"
)
# A combination's key is a Python tuple repr: ``(5, 6)``, ``(5, None)``.
_COMBINATION_KEY = re.compile(r"^\((\d+|None), (\d+|None)\)$")
_UNKNOWN_KEY = "unknown"
_RAW_NAME = "名称未收录"


@dataclass(frozen=True, slots=True)
class ShareRowView:
    label: str
    count: int
    share_label: str
    # The share itself, 0–100: a bar is as long as its share of the sample.
    bar_width: float
    icon_url: str | None = None
    initial: str = ""
    element_key: str | None = None


@dataclass(frozen=True, slots=True)
class ShareBlockView:
    title: str
    note: str
    # ``investment``, ``gear`` or ``teammate``: whether a row has a picture,
    # and whether it is a face.
    kind: str
    rows: tuple[ShareRowView, ...]
    hidden_count: int


@dataclass(frozen=True, slots=True)
class ProfileBoardRowView:
    character_rank: int
    ranked_character_count: int
    boss_name: str
    dungeon_name: str
    sample_count: int
    best_duration: str
    # Whose record the fastest clear is; "" when the board lists none.
    account_display_name: str


@dataclass(frozen=True, slots=True)
class CharacterProfilePage:
    header: PageHeader
    character_initial: str
    character_avatar_url: str | None
    element_key: str | None
    range_label: str
    sample_count: int
    account_count: int
    boss_count: int
    sample_rule: str
    shares: tuple[ShareBlockView, ...]
    boards: tuple[ProfileBoardRowView, ...]


def build_character_profile_page(
    profile: CharacterProfile,
    *,
    character: CharacterType,
    query: str,
    web_base_url: str | None = None,
    elements: Mapping[str, str] | None = None,
    icons: Mapping[str, str] | None = None,
) -> CharacterProfilePage:
    """The four share blocks in the site's order, then the boards, best first.

    Upstream sends the boards most sampled first; the page leads with where
    the character clears fastest, equal ranks keeping upstream's order.
    """

    known = elements or {}
    faces = icons or {}
    boards = sorted(profile.bosses, key=lambda board: board.character_rank)
    return CharacterProfilePage(
        header=PageHeader(
            title=character.name,
            subtitle=normalize_profession(character.profession)
            or character.profession,
            query=query,
            matched_name=f"{character.name} · 角色档案",
            target_type="角色档案",
            footer_note="公开通关记录 · 角色档案",
        ),
        character_initial=_initial(character.name),
        character_avatar_url=_face_url(
            character.icon_path, character.key, web_base_url
        ),
        element_key=element_key(character.element),
        range_label=_RANGE_LABELS.get(profile.range, profile.range),
        sample_count=profile.sample_count,
        account_count=profile.account_count,
        boss_count=profile.boss_count,
        sample_rule=SAMPLE_RULE,
        shares=(
            _block(
                "养成组合",
                "潜能+精炼，占全部样本的比例",
                "investment",
                profile.combinations,
                lambda share: _share_row(share, _combination_label(share)),
            ),
            _block(
                "武器",
                "占全部样本的比例",
                "gear",
                profile.weapons,
                lambda share: _share_row(
                    share,
                    _gear_label(share, unknown="武器未记录"),
                    icon_url=_gear_icon(share, _WEAPON_ICON_PATH, web_base_url),
                ),
            ),
            _block(
                "装备",
                "带上这件的记录占全部样本的比例，一条记录不止一件",
                "gear",
                profile.equipment,
                lambda share: _share_row(
                    share,
                    _gear_label(share, unknown="装备未记录"),
                    icon_url=_gear_icon(share, _EQUIP_ICON_PATH, web_base_url),
                ),
            ),
            _block(
                "常见队友",
                "同队出现的记录占全部样本的比例",
                "teammate",
                profile.teammates,
                lambda share: _share_row(
                    share,
                    share.name,
                    icon_url=_face_url(
                        faces.get(share.name, ""), share.key, web_base_url
                    ),
                    initial=_initial(share.name),
                    ring=element_key(known.get(share.name)),
                ),
            ),
        ),
        boards=tuple(
            ProfileBoardRowView(
                character_rank=board.character_rank,
                ranked_character_count=board.ranked_character_count,
                boss_name=board.boss_name,
                dungeon_name=board.dungeon_name,
                sample_count=board.sample_count,
                best_duration=format_duration(board.best_duration_ms),
                account_display_name=(
                    board.rows[0].account_display_name if board.rows else ""
                ),
            )
            for board in boards
        ),
    )


def _block(
    title: str,
    note: str,
    kind: str,
    shares: tuple[ProfileShare, ...],
    row: Callable[[ProfileShare], ShareRowView],
) -> ShareBlockView:
    """The first ``SHARE_ROWS`` entries with a record, and how many are not shown."""

    listed = [share for share in shares if share.count > 0]
    return ShareBlockView(
        title=title,
        note=note,
        kind=kind,
        rows=tuple(row(share) for share in listed[:SHARE_ROWS]),
        hidden_count=max(0, len(listed) - SHARE_ROWS),
    )


def _share_row(
    share: ProfileShare,
    label: str,
    *,
    icon_url: str | None = None,
    initial: str = "",
    ring: str | None = None,
) -> ShareRowView:
    return ShareRowView(
        label=label,
        count=share.count,
        share_label=f"{format_number(round(share.percent, 1))}%",
        bar_width=round(min(100.0, share.percent), 2),
        icon_url=icon_url,
        initial=initial,
        element_key=ring,
    )


def _combination_label(share: ProfileShare) -> str:
    """``5+6`` as under every avatar, ``5+?`` for an unrecorded weapon.

    The key is read rather than upstream's name (``5 + 未知``), so the pair is
    written the way every other page writes it.
    """

    match = _COMBINATION_KEY.match(share.key)
    if match is None:
        return share.name
    potential, refine = (
        None if value == "None" else int(value) for value in match.groups()
    )
    view = investment_view(potential, refine)
    return "养成未记录" if view is None else view.text


def _gear_label(share: ProfileShare, *, unknown: str) -> str:
    if share.key == _UNKNOWN_KEY:
        return unknown
    if is_raw_item_name(share.name, share.key):
        return _RAW_NAME
    return share.name


def _gear_icon(
    share: ProfileShare, pattern: str, web_base_url: str | None
) -> str | None:
    """Upstream's icon, else the conventional path; an unrecorded piece has none."""

    key = None if share.key == _UNKNOWN_KEY else share.key
    return _derived_asset_url(share.icon_url, pattern, key, web_base_url=web_base_url)


def _face_url(icon_path: str, key: str, web_base_url: str | None) -> str | None:
    """The catalog's portrait when it names one, else the conventional path."""

    if icon_path:
        return _safe_asset_url(icon_path, base_url=web_base_url)
    if not _ASSET_ID_RE.match(key):
        return None
    return _safe_asset_url(
        _CHARACTER_AVATAR_PATH.format(key=key), base_url=web_base_url
    )
