"""Tests for nanoclaw.container_runtime."""

from nanoclaw.container_runtime import (
    CONTAINER_HOST_GATEWAY,
    CONTAINER_RUNTIME_BIN,
    host_gateway_args,
    readonly_mount_args,
    stop_container,
)


def test_constants() -> None:
    assert CONTAINER_RUNTIME_BIN == "docker"
    assert CONTAINER_HOST_GATEWAY == "host.docker.internal"


def test_host_gateway_args() -> None:
    args = host_gateway_args()
    assert isinstance(args, list)


def test_readonly_mount_args() -> None:
    args = readonly_mount_args("/host/path", "/container/path")
    assert args == ["-v", "/host/path:/container/path:ro"]


def test_stop_container() -> None:
    cmd = stop_container("nanoclaw-test")
    assert cmd == "docker stop -t 1 nanoclaw-test"
