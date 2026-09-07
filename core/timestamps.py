"""ISO-8601 helpers shared by the watch, watch-list and history modules."""

from datetime import UTC, datetime


def utc_now_text() -> str:
    """Timestamp for new entries, in the same shape the payloads store."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 stamp; ``None`` when it cannot be trusted.

    A stamp without an offset cannot be compared against one that has it, so it
    is rejected rather than assumed to be local time.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def later_or_same(value: str, than: str) -> bool:
    """Whether stamp ``value`` is at or after ``than``.

    Compared as instants when both parse — ``+08:00`` and ``Z`` stamps
    mis-order as text — and as text when one does not.
    """

    left, right = parse_timestamp(value), parse_timestamp(than)
    if left is not None and right is not None:
        return left >= right
    return value >= than
