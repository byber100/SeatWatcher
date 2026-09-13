from __future__ import annotations

import os
import re
from pathlib import Path

LEGACY_KORAIL_KEY = "SEATWATCHER_KORAIL_ID"
KORAIL_MEMBER_NO_KEY = "SEATWATCHER_KORAIL_MEMBER_NO"


def _migrate_legacy_korail_key(env_path: Path) -> None:
    """Rename the old KORAIL ID key in .env without exposing its value."""
    text = env_path.read_text(encoding="utf-8")
    old_pattern = re.compile(rf"^(\s*){re.escape(LEGACY_KORAIL_KEY)}(\s*=)", re.MULTILINE)
    new_pattern = re.compile(rf"^\s*{re.escape(KORAIL_MEMBER_NO_KEY)}\s*=", re.MULTILINE)

    if not old_pattern.search(text):
        return

    if new_pattern.search(text):
        # A new key already exists, so remove only the obsolete duplicate line.
        text = re.sub(
            rf"^\s*{re.escape(LEGACY_KORAIL_KEY)}\s*=.*(?:\r?\n|$)",
            "",
            text,
            flags=re.MULTILINE,
        )
    else:
        text = old_pattern.sub(rf"\1{KORAIL_MEMBER_NO_KEY}\2", text)

    env_path.write_text(text, encoding="utf-8")


def load_project_env(path: Path | None = None) -> None:
    """Load SeatWatcher/.env without overriding already-set OS variables."""
    env_path = path or (Path(__file__).resolve().parent / ".env")
    if not env_path.exists():
        return

    _migrate_legacy_korail_key(env_path)

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
