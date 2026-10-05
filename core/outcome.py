"""What one command produced, as the host sends it.

Kept apart from ``queries`` because the watch and binding routes answer in
the same shape without being queries (``rank_watch``, ``account_binding``).

``PageTarget`` travels with it: what a picture is about is known where the
page is chosen, in ``queries``, and used where the reply is sent, in the
host, which offers that thing's ZMDLogs page with the picture — or, for a
dungeon, which has none, each of its boards. Neither end knows about the
other's platform; ``core/buttons`` turns a target into buttons.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .candidates import CandidateView, PendingCandidates, format_candidates
from .metrics import DEFAULT_METRIC
from .routing import DEFAULT_STATS_POTENTIAL, DEFAULT_STATS_RANGE

if TYPE_CHECKING:
    from .render import RenderedImage


class SitePage(str, Enum):
    """A page of the ZMDLogs site a text reply sends its reader to.

    The value is its path under the site's address.
    """

    # Where a logged-in user makes the code 绑定 asks for.
    BINDING = "account/binding"


class PageSubject(str, Enum):
    """The kind of thing a result page is about."""

    ACCOUNT = "account"
    BATTLE = "battle"
    BOARD = "board"
    CHARACTER = "character"
    # 对比: two battles, neither of whose pages is this one.
    COMPARISON = "comparison"
    DUNGEON = "dungeon"


@dataclass(frozen=True, slots=True)
class PageTarget:
    """The one thing a result page is about, and which of its pages it is.

    ``key`` names it the way a command and a ZMDLogs link both can: an
    accountId, a battleId or a board slug. A character is the exception: a
    link names it by its key and a command by its name, so its ``name``
    carries the second. ``view`` is the page drawn of it,
    ``RANKING`` being a target's own page (an account's records, a board's
    ranking); the options are the ones it was drawn with that its ZMDLogs
    counterpart or its other views read too (``ranking_page`` is the page
    of a board's ranking drawn, a number or ``ALL_PAGES``, None for the
    first). A page about no one thing — help, the statistics of every
    board — has no target.

    ``unavailable`` names the views of this target that drawing it showed
    cannot be drawn — an older upload without loadout, skill statistics or
    casts, an account with no rank trend recorded yet — so the host offers no
    way to one of them.

    A dungeon's podiums are about the dungeon (or the phase) its ``key``
    names, a page the site does not have; what it offers instead is each
    of its ``boards``, ``(slug, name)`` in the board list's order, and the
    ``metric`` the request asked for, which the podiums cannot show but a
    board can.

    A comparison is about two battles and is neither one's page, so it
    has no page on the site either; what it offers is each of its
    ``battles``, ``(battleId, name)`` left side first, the name being what
    the page calls that side. Its ``key`` is the first battle's id.

    A character page cut to one board (角色档案 ``--榜单``) names the board
    in ``boss_slug``; the site's character page reads it too.

    A board's ranking also carries what turning its page needs: the row
    filters it was drawn with (``character_filter`` as typed, which reads
    back the same; ``element_filter`` as the catalog labels it) and
    ``record_count``, the rows they keep — which says whether a next page
    exists, and what the ``全部`` picture left to the site. None where the
    page counted none.
    """

    subject: PageSubject
    key: str
    view: CandidateView = CandidateView.RANKING
    metric: str = DEFAULT_METRIC
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL
    ranking_page: int | str | None = None
    unavailable: frozenset[CandidateView] = frozenset()
    name: str = ""
    boards: tuple[tuple[str, str], ...] = ()
    battles: tuple[tuple[str, str], ...] = ()
    boss_slug: str | None = None
    character_filter: str | None = None
    element_filter: str | None = None
    record_count: int | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    """A rendered image, or a short text instead.

    ``candidates`` is set when the text is a pick list: the entry the list
    was formatted from, so the host can offer the choices by other means
    than the quoted-reply code without re-reading the text. ``candidate_note``
    is the extra line the text carries under the choices, if any, so a list
    offered another way can say the same.

    An image the plugin's renderer drew carries its capture scale, and the
    one thing it is about when there is one (``target``), so the host can
    offer that thing's ZMDLogs page with it (a dungeon's boards, for its
    podiums). An image from anywhere else —
    AstrBot's fallback renderer — carries neither.

    ``site_page`` is set on a text that tells its reader to go and do
    something on the site (make a binding code), so the host can put the
    page one tap away.
    """

    image_path: str | None = None
    message: str | None = None
    candidates: PendingCandidates | None = None
    candidate_note: str | None = None
    image_scale: int | None = None
    target: PageTarget | None = None
    site_page: SitePage | None = None

    @classmethod
    def image(
        cls, rendered: "RenderedImage", *, target: PageTarget | None = None
    ) -> "Outcome":
        """The picture ``rendered`` is, about ``target`` if about one thing."""

        return cls(image_path=rendered.path, image_scale=rendered.scale, target=target)

    @classmethod
    def pick_list(
        cls,
        entry: PendingCandidates,
        *,
        ttl_seconds: float,
        note: str | None = None,
    ) -> "Outcome":
        """The pick list ``entry`` posts, its text written from the entry."""

        return cls(
            message=format_candidates(entry, ttl_seconds=ttl_seconds, note=note),
            candidates=entry,
            candidate_note=note,
        )
