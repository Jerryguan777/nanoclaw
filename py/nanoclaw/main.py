"""Main orchestrator — state management, message loop, agent invocation."""

import asyncio


async def main() -> None:
    """Entry point for the NanoClaw orchestrator."""


def main_sync() -> None:
    """Synchronous wrapper for CLI entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
