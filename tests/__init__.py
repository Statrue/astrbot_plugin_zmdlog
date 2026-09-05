"""Automated tests for ZmdLogBot.

Importing this package cuts the real network. The suite is meant to be
hermetic: a test that reaches zmdlogs.com passes only while the machine is
online and upstream is healthy, and it silently measures upstream instead
of this plugin. ``httpx.MockTransport`` is untouched, so the client tests
keep working; anything else raises :class:`NetworkAccessInTests` naming
the URL, which points straight at the missing stub.
"""

import httpx


class NetworkAccessInTests(RuntimeError):
    """Raised when a test tries to reach the real network."""


def _refuse(url: object) -> NetworkAccessInTests:
    return NetworkAccessInTests(
        f"a test reached the network: {url}. Stub the data source or the "
        "client method it needs; the suite must run offline."
    )


async def _refuse_async(self, request: httpx.Request) -> httpx.Response:
    raise _refuse(request.url)


def _refuse_sync(self, request: httpx.Request) -> httpx.Response:
    raise _refuse(request.url)


httpx.AsyncHTTPTransport.handle_async_request = _refuse_async
httpx.HTTPTransport.handle_request = _refuse_sync
