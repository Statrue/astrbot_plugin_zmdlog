"""Who uploads the most first places: every public account counted over all boards.

玩家排名 is one wide table, never paged: a row an account with a podium,
then the rest by name. A first place is a board's #1 record, credited to
its uploader.
"""

from dataclasses import dataclass

from ..metrics import metric_label
from ..standings import AccountTally
from .common import PageHeader, _bar_width


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
    # The most used main C and how often (``洛茜 ×6``), under the column's
    # 常用主 C; empty when the account fielded none.
    main_label: str
    # First places as a share of all boards, for the in-row bar.
    bar_width: float


@dataclass(frozen=True, slots=True)
class PlayerChampionsPage:
    header: PageHeader
    board_count: int
    # DPS or rDPS, the ranking the first places were counted on.
    metric_label: str
    rows: tuple[PlayerRowView, ...]
    # Accounts with records but no top-three finish, folded into chips:
    # the first sixty by name, ``other_count`` all of them.
    others: tuple[str, ...]
    other_count: int
    window_label: str = ""


def build_player_champions_page(
    tallies: tuple[AccountTally, ...],
    *,
    board_count: int,
    query: str,
    window_label: str = "",
    limit: int = 40,
    metric: str = "dps",
) -> PlayerChampionsPage:
    """One row per account with a podium, most first places first."""

    ranked = [tally for tally in tallies if tally.podiums][:limit]
    rest = [tally.display_name for tally in tallies if not tally.podiums]
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
                f"{tally.main_c} ×{tally.main_c_count}" if tally.main_c else ""
            ),
            bar_width=_bar_width(tally.first_places, board_count),
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
            target_type="玩家排名",
            footer_note="公开榜单 · 第一名上传者",
            metric=metric,
        ),
        board_count=board_count,
        metric_label=metric_label(metric),
        rows=rows,
        others=tuple(rest[:60]),
        other_count=len(rest),
        window_label=window_label,
    )
