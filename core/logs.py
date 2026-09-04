"""The logger shape ``core`` modules accept.

``core`` stays free of AstrBot imports so it can be unit-tested; AstrBot's
plugin logger is not a :class:`logging.Logger` subclass, so the modules
that need to log take any object with these five methods — the real one in
production, ``logging.getLogger`` in tests.
"""

from typing import Any, Protocol


class LogSink(Protocol):
    def debug(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def info(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def error(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None: ...
