"""Configuration and environment variables."""

import os
import re
from pathlib import Path

ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Andy")
POLL_INTERVAL = 2.0  # seconds
SCHEDULER_POLL_INTERVAL = 60.0  # seconds

PROJECT_ROOT = Path.cwd()
STORE_DIR = PROJECT_ROOT / "store"
GROUPS_DIR = PROJECT_ROOT / "groups"
DATA_DIR = PROJECT_ROOT / "data"
MAIN_GROUP_FOLDER = "main"

CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "nanoclaw-agent:latest")
CONTAINER_TIMEOUT = int(os.environ.get("CONTAINER_TIMEOUT", "1800000"))  # ms
CONTAINER_MAX_OUTPUT_SIZE = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760"))  # 10MB
IPC_POLL_INTERVAL = 1.0  # seconds
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "1800000"))  # ms — 30min default
MAX_CONCURRENT_CONTAINERS = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))

# Container runtime: "docker" or "container" (Apple Container)
CONTAINER_RUNTIME = os.environ.get("CONTAINER_RUNTIME", "docker")

# HTTP API
HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))


def _escape_regex(s: str) -> str:
    return re.escape(s)


TRIGGER_PATTERN = re.compile(rf"^@{_escape_regex(ASSISTANT_NAME)}\b", re.IGNORECASE)

TIMEZONE = os.environ.get("TZ") or "UTC"
