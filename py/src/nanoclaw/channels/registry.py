"""Channel factory registry.

Channels self-register at startup by calling register_channel().
Also provides dynamic gateway management for multi-tenant mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from nanoclaw.channels.gateway import ChannelGateway
    from nanoclaw.core.types import Channel, OnChatMetadata, OnInboundMessage, RegisteredGroup


@dataclass
class ChannelOpts:
    """Options passed to channel factories during initialization."""

    on_message: OnInboundMessage
    on_chat_metadata: OnChatMetadata
    registered_groups: Callable[[], dict[str, RegisteredGroup]]


ChannelFactory = "Callable[[ChannelOpts], Channel | None]"

_registry: dict[str, Callable[[ChannelOpts], Channel | None]] = {}

# Gateway registry for multi-tenant mode (one gateway per channel type)
_gateways: dict[str, ChannelGateway] = {}


def register_channel(name: str, factory: Callable[[ChannelOpts], Channel | None]) -> None:
    """Register a channel factory by name."""
    _registry[name] = factory


def get_channel_factory(name: str) -> Callable[[ChannelOpts], Channel | None] | None:
    """Look up a channel factory by name."""
    return _registry.get(name)


def get_registered_channel_names() -> list[str]:
    """List all registered channel names."""
    return list(_registry.keys())


# --- Gateway management ---


def register_gateway(channel_type: str, gateway: ChannelGateway) -> None:
    """Register a channel gateway for multi-tenant mode."""
    _gateways[channel_type] = gateway


def get_gateway(channel_type: str) -> ChannelGateway | None:
    """Look up a gateway by channel type."""
    return _gateways.get(channel_type)


def get_all_gateways() -> dict[str, ChannelGateway]:
    """Get all registered gateways."""
    return dict(_gateways)
