"""What one command produced, as the host sends it.

Kept apart from ``queries`` because the watch routes answer in the same
shape: ``queries`` imports ``rank_watch``, so the type cannot live in either.
"""

from dataclasses import dataclass

from .candidates import PendingCandidates, format_candidates


@dataclass(frozen=True, slots=True)
class Outcome:
    """A rendered image, or a short text instead.

    ``candidates`` is set when the text is a pick list: the entry the list
    was formatted from, so the host can offer the choices by other means
    than the quoted-reply code without re-reading the text. ``candidate_note``
    is the extra line the text carries under the choices, if any, so a list
    offered another way can say the same.
    """

    image_path: str | None = None
    message: str | None = None
    candidates: PendingCandidates | None = None
    candidate_note: str | None = None

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
