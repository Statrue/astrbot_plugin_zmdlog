"""Answer a thing once per chat for a while: the claim behind both of the
plugin's duplicate guards.

A group's auto-expanded battle link and a group's tapped callback button
are each answered for everyone in the chat, so the same one arriving again
soon after — the link reposted, the button tapped by a second member while
the first picture is still on its way — would only post the same picture
twice. Whoever arrives first claims the key for ``seconds``; a later
arrival with the same key is dropped until the claim runs out.

A claim is taken before the work starts, not after it ends, so a second
arrival while the first is still fetching and rendering is dropped too —
that is the case two people tapping together produce. The caller releases
the claim when what it sent was a failure a retry could plausibly fix, so
the next arrival gets that retry instead of silence.

Checking and taking a claim never awaits, so on the one event loop no two
callers can both find the key free.
"""

import time
from collections.abc import Callable, Hashable


class Cooldown:
    """Keys claimed for ``seconds`` each; expired claims are dropped as
    claims are taken, so the table holds only the live ones."""

    def __init__(
        self, seconds: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until: dict[Hashable, float] = {}

    def claim(self, key: Hashable) -> bool:
        """Take ``key`` if no live claim holds it; False when one does."""

        now = self._clock()
        self._until = {
            held: expires_at
            for held, expires_at in self._until.items()
            if expires_at > now
        }
        if key in self._until:
            return False
        self._until[key] = now + self._seconds
        return True

    def release(self, key: Hashable) -> None:
        """Give ``key`` up early, so the next arrival is answered."""

        self._until.pop(key, None)
