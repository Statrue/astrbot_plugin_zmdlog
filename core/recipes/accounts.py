"""账号: one public account's best record on every board."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..characters import account_roster_names
from ..models import BossRankingRow, PublicUserRankings

if TYPE_CHECKING:
    from ..datasource import ZmdLogsDataSource
    from ..render import LongImageRenderer


@dataclass(frozen=True, slots=True)
class AccountRecipe:
    account: PublicUserRankings
    # The index's rows of the same battles: the user endpoint names a
    # roster but carries no avatars, professions or main C.
    rows_by_battle: Mapping[str, BossRankingRow]
    # Every board slug the index lists, None while it is incomplete; with it
    # the page tells a retired board from one not read yet.
    listed_boards: frozenset[str] | None
    elements: Mapping[str, str]
    icons: Mapping[str, str]
    query: str
    web_base_url: str

    async def draw(self, renderer: "LongImageRenderer") -> str:
        return await renderer.render_account(
            self.account,
            query=self.query,
            web_base_url=self.web_base_url,
            rows_by_battle=self.rows_by_battle,
            listed_boards=self.listed_boards,
            elements=self.elements,
            icons=self.icons,
        )


async def prepare_account(
    data: "ZmdLogsDataSource", account_id: str, *, query: str, web_base_url: str
) -> AccountRecipe:
    """The account page's reads; an unknown or private account raises."""

    account = await data.get_public_user_rankings(account_id)
    rows, listed = await data.index_rows_for(
        row.battle_id for row in account.rankings
    )
    names = account_roster_names(account)
    return AccountRecipe(
        account=account,
        rows_by_battle=rows,
        listed_boards=listed,
        elements=await data.character_elements(names=names),
        icons=await data.character_icons(names=names),
        query=query,
        web_base_url=web_base_url,
    )
