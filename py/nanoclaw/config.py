"""Configuration constants and paths."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from nanoclaw.env import read_env_file

_env_config = read_env_file(["ASSISTANT_NAME", "ASSISTANT_HAS_OWN_NUMBER"])

ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME") or _env_config.get("ASSISTANT_NAME", "Andy")
ASSISTANT_HAS_OWN_NUMBER: bool = (
    os.environ.get("ASSISTANT_HAS_OWN_NUMBER") or _env_config.get("ASSISTANT_HAS_OWN_NUMBER", "")
) == "true"

POLL_INTERVAL: float = 2.0  # seconds
SCHEDULER_POLL_INTERVAL: float = 60.0  # seconds
IPC_POLL_INTERVAL: float = 1.0  # seconds

PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path.home()

MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "nanoclaw" / "mount-allowlist.json"
SENDER_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "nanoclaw" / "sender-allowlist.json"
STORE_DIR: Path = PROJECT_ROOT / "store"
GROUPS_DIR: Path = PROJECT_ROOT / "groups"
DATA_DIR: Path = PROJECT_ROOT / "data"

CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "nanoclaw-agent:latest")
CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))
CONTAINER_MAX_OUTPUT_SIZE: int = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))  # 10MB
CREDENTIAL_PROXY_PORT: int = int(os.environ.get("CREDENTIAL_PROXY_PORT", "3001"))
IDLE_TIMEOUT: int = int(os.environ.get("IDLE_TIMEOUT", "1800000"))  # 30 min
MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))

TRIGGER_PATTERN: re.Pattern[str] = re.compile(
    rf"^@{re.escape(ASSISTANT_NAME)}\b",
    re.IGNORECASE,
)

# Timezone for scheduled tasks — uses system timezone by default
TIMEZONE: str = os.environ.get("TZ") or time.tzname[0]

# Try to get IANA timezone name
try:
    import zoneinfo  # noqa: F401 — only to validate

    _local_tz = time.tzname[0]
    # time.tzname gives abbreviations like "EST"; we need IANA names
    # Use the datetime approach to get the proper IANA name
    from datetime import datetime as _dt

    _tz_name = _dt.now().astimezone().tzinfo
    if _tz_name is not None:
        _name = str(_tz_name)
        if "/" in _name:
            TIMEZONE = _name
except Exception:  # noqa: BLE001
    pass
