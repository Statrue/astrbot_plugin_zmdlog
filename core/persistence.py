"""Tiny atomic JSON file helpers for snapshots and editable config."""

import json
import os
import tempfile
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
