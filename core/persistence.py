"""Tiny atomic JSON file helpers for snapshots and editable config."""

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any | None:
    """Return the parsed JSON document or ``None`` if unreadable/invalid."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_json(path: Path, payload: Any) -> bool:
    """Write ``payload`` atomically; return ``False`` instead of raising."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(encoded)
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError):
        return False


class JsonStore:
    """One JSON file that warns once per failure streak instead of raising.

    ``path`` is ``None`` when the plugin has no data directory; loading then
    yields nothing and saving reports failure without a warning, because the
    missing directory was already reported once at start-up.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        label: str,
        warn: Callable[[str], None],
    ) -> None:
        self.path = path
        self.label = label
        self._warn = warn
        self._write_failed = False

    @property
    def available(self) -> bool:
        return self.path is not None

    def load(self) -> Any | None:
        """The file's document; None when there is none — or none readable.

        A file that exists but cannot be parsed is set aside under a
        ``.corrupt-<stamp>`` name and reported, so the next save does not
        quietly replace something a person may want to look at (a watch
        list is not recoverable from anywhere else).
        """

        if self.path is None or not self.path.exists():
            return None
        payload = load_json(self.path)
        if payload is not None:
            return payload
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(aside)
            where = f"; moved to {aside.name}"
        except OSError:
            where = ""
        self._warn(
            f"ZmdLogBot could not read the {self.label} file; starting empty{where}."
        )
        return None

    def save(self, payload: Any) -> bool:
        if self.path is None:
            return False
        if save_json(self.path, payload):
            self._write_failed = False
            return True
        if not self._write_failed:
            self._write_failed = True
            self._warn(
                f"ZmdLogBot could not persist the {self.label}; "
                "check the plugin data directory."
            )
        return False
