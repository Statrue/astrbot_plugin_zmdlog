"""What one command produced, as the host sends it.

Kept apart from ``queries`` because the watch routes answer in the same
shape: ``queries`` imports ``rank_watch``, so the type cannot live in either.

``PageTarget`` travels with it: what a picture is about is known where the
page is chosen, in ``queries``, and used where the reply is sent, in the
host, which offers that thing's ZMDLogs page with the picture. Neither end
knows about the other's platform; ``core/buttons`` turns a target into a
link.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .candidates import CandidateView, PendingCandidates, format_candidates
from .metrics import DEFAULT_METRIC
from .routing import DEFAULT_STATS_POTENTIAL, DEFAULT_STATS_RANGE

if TYPE_CHECKING:
    from .render import RenderedImage


class PageSubject(str, Enum):
    """The kind of thing a result page is about."""

    ACCOUNT = "account"
    BATTLE = "battle"
    BOARD = "board"


@dataclass(frozen=True, slots=True)
class PageTarget:
    """The one thing a result page is about, and which of its pages it is.

    ``key`` names it the way a command and a ZMDLogs link both can: an
    accountId, a battleId or a board slug. ``view`` is the page drawn of it,
    ``RANKING`` being a target's own page (an account's records, a board's
    ranking); the options are the ones it was drawn with that its ZMDLogs
    counterpart reads too. A page about no one thing — help, the statistics
    of every board, a dungeon's podiums, a comparison of two battles — has
    no target.
    """

    subject: PageSubject
    key: str
    view: CandidateView = CandidateView.RANKING
    metric: str = DEFAULT_METRIC
    stats_range: str = DEFAULT_STATS_RANGE
    stats_potential: str = DEFAULT_STATS_POTENTIAL


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
    offer that thing's ZMDLogs page with it. An image from anywhere else —
    AstrBot's fallback renderer — carries neither.
    """

    image_path: str | None = None
    message: str | None = None
    candidates: PendingCandidates | None = None
    candidate_note: str | None = None
    image_scale: int | None = None
    target: PageTarget | None = None

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
