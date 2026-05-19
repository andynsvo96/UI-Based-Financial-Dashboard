from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import AUDIT_LOG_PATH


def _clean(value: Any) -> str:
    text = str(value)
    blocked_fragments = ["password", "cookie", "token", "secret", "session="]
    lowered = text.lower()
    if any(fragment in lowered for fragment in blocked_fragments):
        return "[redacted]"
    return text.replace("\r", " ").replace("\n", " ").strip()


def log_event(event: str, message: str = "", **details: Any) -> None:
    """Append a human-readable audit line without storing credentials or tokens."""
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    parts = [timestamp, _clean(event)]
    if message:
        parts.append(_clean(message))
    for key, value in details.items():
        parts.append(f"{_clean(key)}={_clean(value)}")
    with Path(AUDIT_LOG_PATH).open("a", encoding="utf-8") as handle:
        handle.write(" | ".join(parts) + "\n")
