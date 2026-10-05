"""账号: one public account's best record on every board."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..characters import account_roster_names
from ..models import BossRankingRow, PublicUserRankings

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer, RenderedImage


@dataclass(frozen=True, slots=True)
class AccountRecipe:
    account: PublicUserRankings
    # The index's rows of the same battles: the user endpoint carries no
    # main C, and an older response no roster beyond the names.
    rows_by_battle: Mapping[str, BossRankingRow]
    # 全部榜单's slugs, None when the board list could not be read; with them
    # the page tells a 未收录榜单 from a board the index has not read yet.
    listed_boards: frozenset[str] | None
    icons: Mapping[str, str]
    query: str
    web_base_url: str

    async def draw(self, renderer: "LongImageRenderer") -> "RenderedImage":
        return await renderer.render_account(
            self.account,
            query=self.query,
            web_base_url=self.web_base_url,
            rows_by_battle=self.rows_by_battle,
            listed_boards=self.listed_boards,
            icons=self.icons,
        )


async def prepare_account(
    data: "ZmdLogsDataSource", account_id: str, *, query: str, web_base_url: str
) -> AccountRecipe:
    """The account page's reads; an unknown or private account raises.

    Off the ranking index when it holds the account, else from the endpoint;
    see :meth:`ZmdLogsDataSource.account_rankings_for_page`.
    """

    account, rows, listed = await data.account_rankings_for_page(account_id)
    names = account_roster_names(account)
    return AccountRecipe(
        account=account,
        rows_by_battle=rows,
        listed_boards=listed,
        icons=await data.character_icons(names=names),
        query=query,
        web_base_url=web_base_url,
    )
