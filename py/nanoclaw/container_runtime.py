"""Container runtime abstraction (Docker).

All runtime-specific logic lives here so swapping runtimes means changing one file.
"""

from __future__ import annotations

import contextlib
import os
import platform
import subprocess
from pathlib import Path

from nanoclaw.logger import get_logger

logger = get_logger()

CONTAINER_RUNTIME_BIN: str = "docker"
CONTAINER_HOST_GATEWAY: str = "host.docker.internal"


def _detect_proxy_bind_host() -> str:
    """Detect the appropriate bind host for the credential proxy.

    Docker Desktop (macOS): 127.0.0.1 — the VM routes host.docker.internal to loopback.
    Docker (Linux): bind to the docker0 bridge IP so only containers can reach it.
    """
    if platform.system() == "Darwin":
        return "127.0.0.1"

    # WSL uses Docker Desktop — loopback is correct
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        return "127.0.0.1"

    # Bare-metal Linux: try to find docker0 bridge IP
    try:
        import fcntl
        import socket
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            addr = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", b"docker0"[:15]))  # SIOCGIFADDR
            return socket.inet_ntoa(addr[20:24])
        except OSError:
            pass
        finally:
            sock.close()
    except ImportError:
        pass

    return "0.0.0.0"


PROXY_BIND_HOST: str = os.environ.get("CREDENTIAL_PROXY_HOST") or _detect_proxy_bind_host()


def host_gateway_args() -> list[str]:
    """CLI args needed for the container to resolve the host gateway."""
    if platform.system() == "Linux":
        return ["--add-host=host.docker.internal:host-gateway"]
    return []


def readonly_mount_args(host_path: str, container_path: str) -> list[str]:
    """Returns CLI args for a readonly bind mount."""
    return ["-v", f"{host_path}:{container_path}:ro"]


def stop_container(name: str) -> str:
    """Returns the shell command to stop a container by name."""
    return f"{CONTAINER_RUNTIME_BIN} stop -t 1 {name}"


def ensure_container_runtime_running() -> None:
    """Ensure the container runtime is running."""
    try:
        subprocess.run(
            [CONTAINER_RUNTIME_BIN, "info"],
            capture_output=True,
            timeout=10,
            check=True,
        )
        logger.debug("Container runtime already running")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.error("Failed to reach container runtime", error=str(exc))
        msg = (
            "\n╔════════════════════════════════════════════════════════════════╗\n"
            "║  FATAL: Container runtime failed to start                      ║\n"
            "║                                                                ║\n"
            "║  Agents cannot run without a container runtime. To fix:        ║\n"
            "║  1. Ensure Docker is installed and running                     ║\n"
            "║  2. Run: docker info                                           ║\n"
            "║  3. Restart NanoClaw                                           ║\n"
            "╚════════════════════════════════════════════════════════════════╝\n"
        )
        raise RuntimeError(msg) from exc


def cleanup_orphans() -> None:
    """Kill orphaned NanoClaw containers from previous runs."""
    try:
        result = subprocess.run(
            [CONTAINER_RUNTIME_BIN, "ps", "--filter", "name=nanoclaw-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        orphans = [name for name in result.stdout.strip().splitlines() if name]
        for name in orphans:
            with contextlib.suppress(OSError):
                subprocess.run(stop_container(name).split(), capture_output=True, check=False)
        if orphans:
            logger.info("Stopped orphaned containers", count=len(orphans), names=orphans)
    except OSError as exc:
        logger.warning("Failed to clean up orphaned containers", error=str(exc))
