"""STDIO entry point for the local Kleinanzeigen MCP adapter."""

from __future__ import annotations

from .backend_launcher import BackendLaunchError, BackendUrlProvider, LauncherSettings
from .server import create_mcp_server, network_client_factory


def main() -> None:
    """Run MCP over STDIO against one shared, independently-lived backend."""

    try:
        provider = BackendUrlProvider(LauncherSettings.from_env())
    except BackendLaunchError as exc:
        raise SystemExit(f"kleinanzeigen-mcp: {exc}") from None
    server = create_mcp_server(network_client_factory(base_url_provider=provider))
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
