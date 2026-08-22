"""STDIO entry point for the local Kleinanzeigen MCP adapter."""

from __future__ import annotations

from .server import create_mcp_server, network_client_factory


def main() -> None:
    """Run MCP over STDIO while the durable FastAPI backend stays separate."""

    server = create_mcp_server(network_client_factory())
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
