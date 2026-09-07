"""Who uploads the most first places: every public account counted over all boards."""

from dataclasses import dataclass

from ..standings import AccountTally
from .common import PageHeader, _as_of_label, _bar_width


@dataclass(frozen=True, slots=True)
class PlayerRowView:
    position: int
    display_name: str
    account_id: str
    first_places: int
    podiums: int
    top_tens: int
    boards: int
    records: int
    main_label: str
    team_label: str
    # First places as a share of all boards, for the in-row bar.
    bar_width: float


@dataclass(frozen=True, slots=True)
class PlayerChampionsPage:
    header: PageHeader
    board_count: int
    as_of_label: str
    top: PlayerRowView | None
    rows: tuple[PlayerRowView, ...]
    # Accounts with records but no top-three finish, folded into chips.
    others: tuple[str, ...]
    window_label: str = ""


def build_player_champions_page(
    tallies: tuple[AccountTally, ...],
    *,
    board_count: int,
    query: str,
    age_seconds: float | None = None,
    window_label: str = "",
    limit: int = 40,
) -> PlayerChampionsPage:
    """One row per account with a podium, most first places first."""

    ranked = [tally for tally in tallies if tally.podiums][:limit]
    rows = tuple(
        PlayerRowView(
            position=index,
            display_name=tally.display_name,
            account_id=tally.account_id,
            first_places=tally.first_places,
            podiums=tally.podiums,
            top_tens=tally.top_tens,
            boards=tally.boards,
            records=tally.records,
            main_label=(
                f"常用主 C {tally.main_c} ×{tally.main_c_count}" if tally.main_c else ""
            ),
            team_label=(
                "常用阵容 " + "、".join(tally.team) + f" ×{tally.team_count}"
                if tally.team
                else ""
            ),
            bar_width=(
                _bar_width(tally.first_places, board_count)
            ),
        )
        for index, tally in enumerate(ranked, start=1)
    )
    scope = "全部榜单" if not window_label else f"{window_label}各榜最快记录"
    return PlayerChampionsPage(
        header=PageHeader(
            title="玩家冠军榜" + (f" · {window_label}" if window_label else ""),
            subtitle=f"公开账号在{scope}拿下的第一名、前三与前十",
            query=query,
            matched_name=f"全部 {board_count} 个榜单 · 玩家冠军榜",
            target_type="玩家冠军榜",
            footer_note="公开榜单 · 第一名记录的上传者算一个冠军",
        ),
        board_count=board_count,
        as_of_label=_as_of_label(age_seconds),
        top=rows[0] if rows else None,
        rows=rows,
        others=tuple(tally.display_name for tally in tallies if not tally.podiums)[
            :60
        ],
        window_label=window_label,
    )
