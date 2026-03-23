"""Tests for nanoclaw.config."""

from nanoclaw.config import (
    ASSISTANT_NAME,
    CONTAINER_IMAGE,
    CONTAINER_TIMEOUT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAX_CONCURRENT_CONTAINERS,
    POLL_INTERVAL,
    TRIGGER_PATTERN,
)


def test_default_values() -> None:
    assert isinstance(ASSISTANT_NAME, str)
    assert POLL_INTERVAL == 2.0
    assert CONTAINER_TIMEOUT == 1800000
    assert CREDENTIAL_PROXY_PORT == 3001
    assert IDLE_TIMEOUT == 1800000
    assert MAX_CONCURRENT_CONTAINERS >= 1


def test_paths_exist_as_path_objects() -> None:
    assert hasattr(GROUPS_DIR, "exists")
    assert hasattr(DATA_DIR, "exists")


def test_trigger_pattern() -> None:
    name = ASSISTANT_NAME
    assert TRIGGER_PATTERN.match(f"@{name} hello")
    assert TRIGGER_PATTERN.match(f"@{name.upper()} hello")
    assert not TRIGGER_PATTERN.match("hello")
    assert not TRIGGER_PATTERN.match(f"not @{name}")


def test_container_image() -> None:
    assert isinstance(CONTAINER_IMAGE, str)
    assert "nanoclaw" in CONTAINER_IMAGE
