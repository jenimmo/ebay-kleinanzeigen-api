"""Fail-closed URL and DNS checks for browser-based Kleinanzeigen scraping."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit


MAX_URL_LENGTH = 4096
MAX_SCRAPE_PAGES = 20
DNS_RESOLUTION_TIMEOUT_SECONDS = 5.0

ALLOWED_KLEINANZEIGEN_HOSTS = frozenset(
    {
        "kleinanzeigen.de",
        "www.kleinanzeigen.de",
    }
)


class UnsafeOutboundUrlError(ValueError):
    """Raised when an outbound browser URL does not satisfy the SSRF policy."""


@dataclass(frozen=True)
class HttpsTarget:
    parsed: SplitResult
    hostname: str
    port: int


AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]


def _parse_https_target(
    url: str,
    *,
    require_kleinanzeigen_host: bool,
    allow_kleinanzeigen_subdomains: bool = False,
) -> HttpsTarget:
    if not isinstance(url, str) or not url:
        raise UnsafeOutboundUrlError("URL must be a non-empty string")

    try:
        encoded_length = len(url.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise UnsafeOutboundUrlError("URL contains invalid Unicode") from exc

    if encoded_length > MAX_URL_LENGTH:
        raise UnsafeOutboundUrlError("URL is too long")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in url
    ):
        raise UnsafeOutboundUrlError("URL contains unsafe Unicode characters")
    # WHATWG URL parsers treat backslashes as separators for special schemes,
    # while urllib.parse does not always do so. Reject the ambiguous form.
    if "\\" in url:
        raise UnsafeOutboundUrlError("URL contains an ambiguous separator")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeOutboundUrlError("URL authority is invalid") from exc

    if parsed.scheme.lower() != "https":
        raise UnsafeOutboundUrlError("Only HTTPS URLs are allowed")
    if not parsed.netloc or not hostname:
        raise UnsafeOutboundUrlError("URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeOutboundUrlError("URL credentials are not allowed")
    if port not in (None, 443):
        raise UnsafeOutboundUrlError("Only the standard HTTPS port is allowed")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeOutboundUrlError("URL hostname is invalid") from exc

    # IP literals are never needed for Kleinanzeigen or its browser resources.
    # Resolving hostnames below also lets us apply the same policy to aliases.
    try:
        ipaddress.ip_address(ascii_hostname.removeprefix("[").removesuffix("]"))
    except ValueError:
        pass
    else:
        raise UnsafeOutboundUrlError("IP-literal URLs are not allowed")

    if require_kleinanzeigen_host:
        exact_host_allowed = ascii_hostname in ALLOWED_KLEINANZEIGEN_HOSTS
        subdomain_allowed = allow_kleinanzeigen_subdomains and ascii_hostname.endswith(
            ".kleinanzeigen.de"
        )
        if not exact_host_allowed and not subdomain_allowed:
            raise UnsafeOutboundUrlError("URL host is not allowed")

    return HttpsTarget(parsed=parsed, hostname=ascii_hostname, port=port or 443)


def validate_kleinanzeigen_url_syntax(url: str) -> str:
    """Validate a user-provided top-level URL without performing network I/O."""
    _parse_https_target(url, require_kleinanzeigen_host=True)
    return url


def _is_globally_routable(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return (
        parsed.is_global
        and not parsed.is_loopback
        and not parsed.is_link_local
        and not parsed.is_private
        and not parsed.is_reserved
        and not parsed.is_multicast
        and not parsed.is_unspecified
    )


async def resolve_host_addresses(hostname: str, port: int) -> Sequence[str]:
    """Resolve all TCP addresses for a hostname with a short, fail-closed timeout."""

    def _resolve() -> Sequence[str]:
        records = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(dict.fromkeys(record[4][0] for record in records))

    try:
        async with asyncio.timeout(DNS_RESOLUTION_TIMEOUT_SECONDS):
            return await asyncio.to_thread(_resolve)
    except (TimeoutError, OSError, socket.gaierror) as exc:
        raise UnsafeOutboundUrlError(
            "URL hostname could not be safely resolved"
        ) from exc


async def validate_public_https_url(
    url: str,
    *,
    require_kleinanzeigen_host: bool,
    allow_kleinanzeigen_subdomains: bool = False,
    resolver: AddressResolver | None = None,
) -> str:
    """Require an HTTPS URL whose complete DNS answer is globally routable.

    Rejecting an answer when *any* address is unsafe prevents a hostname with a
    mixed public/private answer from selecting a private address in Chromium.
    The check is repeated by the Playwright route handler immediately before
    every request, including redirects and subresources.
    """
    target = _parse_https_target(
        url,
        require_kleinanzeigen_host=require_kleinanzeigen_host,
        allow_kleinanzeigen_subdomains=allow_kleinanzeigen_subdomains,
    )
    address_resolver = resolver or resolve_host_addresses
    addresses = tuple(await address_resolver(target.hostname, target.port))
    if not addresses or any(not _is_globally_routable(item) for item in addresses):
        raise UnsafeOutboundUrlError("URL resolves to a non-public address")
    return url


async def install_safe_page_routing(
    page,
    *,
    resolver: AddressResolver | None = None,
) -> None:
    """Install a fail-closed network guard on a Playwright-like page.

    Every navigation must remain on a canonical Kleinanzeigen hostname.
    Subresources are limited to the Kleinanzeigen-controlled DNS namespace;
    this removes attacker-controlled DNS from Chromium's second resolution and
    makes the public-address check a second layer instead of a racy allow rule.
    Playwright invokes the handler again for redirect hops, so an unsafe
    Location target is aborted before it is sent.
    """

    async def _guard(route, request) -> None:
        try:
            await validate_public_https_url(
                request.url,
                require_kleinanzeigen_host=True,
                allow_kleinanzeigen_subdomains=not request.is_navigation_request(),
                resolver=resolver,
            )
        # The enforcement hook must fail closed even if the resolver or parser
        # fails in an unexpected way. Cancellation remains a BaseException and
        # is therefore still propagated normally.
        except Exception:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _block_web_socket(web_socket_route) -> None:
        # Search-result extraction does not need WebSockets. Blocking the
        # transport entirely avoids a ws:// or wss:// SSRF side channel because
        # Playwright's normal request routing does not intercept WebSockets.
        await web_socket_route.close(
            code=1008,
            reason="Outbound WebSockets are disabled",
        )

    await page.route("**/*", _guard)
    await page.route_web_socket("**/*", _block_web_socket)
