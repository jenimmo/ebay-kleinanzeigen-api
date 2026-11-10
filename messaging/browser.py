"""Account-isolated, fail-closed browser adapter for Kleinanzeigen messages.

The DOM contracts live here rather than in the durable service. Unknown markup
is an explicit ``ui_unsupported`` result, never an empty successful inbox or a
reason to guess a recipient. The authenticated UI must be checked with the
operator's own test accounts when Kleinanzeigen changes it.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError, async_playwright

from messaging.contracts import (
    MAX_IMAGE_BYTES,
    MAX_TEXT_LENGTH,
    MessagingError,
    validate_id,
)
from utils.input_validation import normalize_listing_id
from utils.outbound_control import OutboundControlError
from utils.url_security import UnsafeOutboundUrlError, validate_public_https_url


ORIGIN = "https://www.kleinanzeigen.de"
INBOX_URL = ORIGIN + "/m-nachrichten.html"
LOGIN_URL = ORIGIN + "/m-einloggen.html"
_COMPOSER = (
    '[data-testid="message-composer"], #viewad-contact-form, '
    '#contact-form, form[action*="nachricht"], .messagebox-write'
)
_EDITOR = 'textarea[name="message"], textarea[name="text"], #viewad-contact-message, textarea, [contenteditable="true"]'
_SEND_NAME = re.compile(r"^(?:Nachricht )?[Ss]enden$")
_MORE_CONVERSATIONS = 'button[data-testid="load-more-conversations"], button[data-action="load-more-conversations"]'
_MORE_MESSAGES = (
    'button[data-testid="load-more-messages"], button[data-action="load-more-messages"]'
)

# Only explicitly current-user/header fields are eligible. Profile links in an
# advertisement or message are the OTHER account and must never bind a login.
_IDENTITY_JS = """() => {
  if (!['https://www.kleinanzeigen.de', 'https://kleinanzeigen.de', 'https://login.kleinanzeigen.de'].includes(location.origin))
    return {ids: [], login_form: false, untrusted_origin: true};
  const ids = new Set();
  const add = value => {if (/^[0-9]{1,20}$/.test(String(value || ''))) ids.add(String(value));};
  for (const e of document.querySelectorAll('[data-current-user-id], [data-logged-in-user-id], meta[name="current-user-id"], meta[name="user-id"]'))
    add(e.getAttribute('data-current-user-id') || e.getAttribute('data-logged-in-user-id') || e.getAttribute('content'));
  for (const e of document.querySelectorAll('#user-menu [data-user-id], #my-menu [data-user-id], [data-testid="my-profile"][data-user-id]')) add(e.getAttribute('data-user-id'));
  for (const e of document.querySelectorAll('#user-menu a[href], #my-menu a[href], a[data-testid="my-profile"]')) {
    const u = new URL(e.href, location.origin);
    if (/\\/s-anzeigen-des-nutzers\\/([0-9]+)$/.test(u.pathname)) add(u.pathname.split('/').pop());
    if (u.pathname === '/s-bestandsliste.html') add(u.searchParams.get('userId'));
  }
  const state = window.__NEXT_DATA__?.props?.pageProps;
  add(state?.currentUser?.id); add(state?.currentUser?.userId);
  try {
    const shared = JSON.parse(document.getElementById('astroSharedData')?.textContent || '{}');
    add(shared.user?.id); add(shared.user?.userId); add(shared.userProfile?.userId);
  } catch (_) {}
  const name = document.querySelector('[data-testid="my-profile"], #user-menu .user-name, #my-menu .user-name')?.textContent?.trim() || null;
  return {ids: [...ids], display_name: name,
    login_form: !!document.querySelector('input[type="password"], form[action*="einloggen"], [data-testid="login-button"]')};
}"""

_INBOX_JS = """() => {
  const result = new Map();
  const userId = e => {
    let v = e.getAttribute('data-recipient-user-id') || e.getAttribute('data-participant-id');
    if (v) return v;
    const a = e.querySelector('a[href*="s-anzeigen-des-nutzers"], a[href*="s-bestandsliste.html"]');
    if (!a) return null;
    const u = new URL(a.href, location.origin);
    return u.searchParams.get('userId') || u.pathname.match(/s-anzeigen-des-nutzers\\/([0-9]+)/)?.[1] || null;
  };
  for (const node of document.querySelectorAll('[data-conversation-id], a[href*="conversationId="]')) {
    if (node.closest('[data-message-id]')) continue;
    const link = node.matches('a[href]') ? node : node.querySelector('a[href*="conversationId="]');
    const u = link ? new URL(link.href, location.origin) : null;
    const id = node.getAttribute('data-conversation-id') || u?.searchParams.get('conversationId');
    if (!id) continue;
    const e = node.closest('[data-conversation-id], li, article') || node;
    result.set(id, {conversation_id: id, recipient_user_id: userId(e),
      listing_id: e.getAttribute('data-ad-id') || e.getAttribute('data-listing-id'),
      title: e.querySelector('[data-testid="conversation-title"], h2, h3, .subject')?.textContent?.trim() || null,
      unread: e.getAttribute('data-unread') === 'true' || e.classList.contains('unread'),
      href: u?.href || null});
  }
  const list = document.querySelector('[data-testid="conversation-list"], [data-conversation-list], #messagebox');
  return {conversations: [...result.values()],
    known_container: !!document.querySelector('#messagebox, [data-testid="conversation-list"], [data-conversation-list]'),
    potentially_truncated: !!list?.querySelector('[data-has-more="true"], [data-virtualized="true"], [aria-busy="true"]') || list?.getAttribute('data-has-more') === 'true' || list?.getAttribute('data-virtualized') === 'true',
    empty: !!document.querySelector('[data-testid="empty-inbox"], [data-empty-inbox="true"]'),
    more: !!document.querySelector('button[data-testid="load-more-conversations"], button[data-action="load-more-conversations"]')};
}"""

_CONVERSATION_JS = """() => {
  const urlId = new URL(location.href).searchParams.get('conversationId');
  const active = document.querySelector('[data-active-conversation-id], [data-conversation-id][data-active="true"], [data-testid="conversation-detail"]');
  const id = active?.getAttribute('data-active-conversation-id') || active?.getAttribute('data-conversation-id') || urlId;
  const header = document.querySelector('[data-testid="conversation-header"], #conversation-header, .messagebox-header') || active;
  const recipients = new Set();
  const add = v => {if (/^[0-9]{1,20}$/.test(String(v || ''))) recipients.add(String(v));};
  if (header) {
    add(header.getAttribute('data-recipient-user-id'));
    for (const e of header.querySelectorAll('[data-recipient-user-id], [data-participant-id]')) add(e.getAttribute('data-recipient-user-id') || e.getAttribute('data-participant-id'));
    for (const a of header.querySelectorAll('a[href*="s-anzeigen-des-nutzers"], a[href*="s-bestandsliste.html"]')) {
      const u = new URL(a.href, location.origin);
      add(u.searchParams.get('userId') || u.pathname.match(/s-anzeigen-des-nutzers\\/([0-9]+)/)?.[1]);
    }
  }
  const scope = active || document.querySelector('#messagebox') || document;
  const messages = [...scope.querySelectorAll('[data-message-id]')].map(e => ({
    message_id: e.getAttribute('data-message-id'),
    sender_user_id: e.getAttribute('data-sender-user-id') || e.getAttribute('data-sender-id'),
    direction: e.getAttribute('data-direction'),
    text: e.querySelector('[data-testid="message-text"], [data-message-text], .message-text')?.textContent || '',
    created_at: e.querySelector('time[datetime]')?.getAttribute('datetime') || e.getAttribute('data-timestamp'),
    images: [...e.querySelectorAll('img[data-attachment-id], [data-testid="message-image"], .message-attachment img')].map(img => ({
      url: img.currentSrc || img.src,
      remote_id: img.getAttribute('data-attachment-id') || img.closest('[data-attachment-id]')?.getAttribute('data-attachment-id') || null,
      filename: img.getAttribute('data-filename') || null}))
  }));
  return {conversation_id: id, recipients: [...recipients], messages,
    empty: !!scope.querySelector('[data-testid="empty-conversation"], [data-empty-conversation="true"]'),
    more: !!scope.querySelector('button[data-testid="load-more-messages"], button[data-action="load-more-messages"]')};
}"""

_LISTING_RECIPIENT_JS = """() => {
  const ids = new Set();
  for (const a of document.querySelectorAll('a.userprofile-vip, .userprofile a[href*="s-anzeigen-des-nutzers"], .userprofile a[href*="s-bestandsliste.html"]')) {
    const u = new URL(a.href, location.origin);
    const id = u.searchParams.get('userId') || u.pathname.match(/s-anzeigen-des-nutzers\\/([0-9]+)/)?.[1];
    if (/^[0-9]{1,20}$/.test(id || '')) ids.add(id);
  }
  return [...ids];
}"""

_SEND_FOLLOW_JS = """() => {
  const links = [];
  const u = new URL(location.href);
  if (u.pathname === '/m-nachrichten.html' && u.searchParams.has('conversationId'))
    links.push({url:u.href, message_id:u.searchParams.get('messageId')});
  for (const root of document.querySelectorAll('[data-testid="message-send-success"], [data-message-send-success="true"], [role="status"], [role="alert"], .successbox')) {
    if (root.closest('[data-message-id]')) continue;
    const explicit = root.matches('[data-testid="message-send-success"], [data-message-send-success="true"]');
    if (!explicit && !/(?:nachricht.*(?:gesendet|verschickt)|erfolgreich.*(?:gesendet|verschickt))/i.test(root.textContent || '')) continue;
    for (const a of root.querySelectorAll('a[href]')) {
      const target = new URL(a.href, location.origin);
      if (target.pathname === '/m-nachrichten.html' && target.searchParams.has('conversationId'))
        links.push({url:target.href, message_id:root.getAttribute('data-message-id') || a.getAttribute('data-message-id') || target.searchParams.get('messageId')});
    }
  }
  return links;
}"""

_DRAFT_JS = """e => ({
  texts:[...e.querySelectorAll('textarea, [contenteditable="true"]')].map(n => n.value ?? n.textContent ?? '').filter(t => t.trim()),
  attachment_ids:[...e.querySelectorAll('[data-attachment-id]')].map(n => n.getAttribute('data-attachment-id')).filter(Boolean),
  unknown_previews:[...e.querySelectorAll('[data-testid="attachment-preview"], .attachment-preview')].some(n => !n.matches('[data-attachment-id]') && !n.querySelector('[data-attachment-id]')),
  filenames:[...e.querySelectorAll('input[type="file"]')].flatMap(n => [...n.files].map(f => f.name))
})"""


def _unsupported(
    message="Die Nachrichtenoberfläche konnte nicht eindeutig erkannt werden.",
):
    return MessagingError("ui_unsupported", message, 409)


def _validate_image_accept(
    file_payload: dict, accepted_types: list[str] | None
) -> None:
    """Check the actual upload MIME/name against the current file input's accept.

    Playwright bypasses the native file chooser's format filter. An absent or
    empty accept attribute therefore cannot establish upstream support and is
    rejected until the UI exposes a verifiable contract. Extension matching is
    against the filename actually uploaded; MIME aliases are not guessed.
    """
    if not isinstance(accepted_types, (list, tuple)) or any(
        not isinstance(token, str) for token in accepted_types
    ):
        raise MessagingError(
            "image_type_unverified",
            "Die erlaubten Bildformate sind in dieser Nachrichtenoberfläche nicht nachweisbar.",
            409,
        )
    accepted = [token.strip().lower() for token in accepted_types if token.strip()]
    if not accepted:
        raise MessagingError(
            "image_type_unverified",
            "Die erlaubten Bildformate sind in dieser Nachrichtenoberfläche nicht nachweisbar.",
            409,
        )
    mime = str(file_payload.get("mimeType", "")).lower()
    filename = str(file_payload.get("name", "")).lower()
    for token in accepted:
        if token == "image/*" and mime.startswith("image/"):
            return
        if re.fullmatch(r"image/[a-z0-9!#$&^_.+-]+", token) and token == mime:
            return
        if re.fullmatch(r"\.[a-z0-9]+", token) and filename.endswith(token):
            return
    raise MessagingError(
        "image_type_unsupported",
        "Dieses Bildformat ist im ausgewählten Nachrichtenformular nicht freigegeben.",
        422,
    )


async def install_messaging_page_routing(page, *, on_blocked=None):
    """Permit the observed login and verification resources with narrow scope.

    The public login flow on 2026-11-04 redirects to login.kleinanzeigen.de and
    loads Auth0 styles from cdn.auth0.com. Its observed device-verification flow
    embeds Kleinanzeigen frames and narrowly scoped online-metrix resources.
    These exceptions never authorize top-level navigation or arbitrary hosts.

    ``on_blocked`` is an optional synchronous diagnostic callback receiving
    only a hostname and resource type. Calls are bounded per page, and callback
    failure never weakens request enforcement.
    """

    blocked_counts = {}

    def report_blocked(url, resource_type):
        if on_blocked is None:
            return
        try:
            host = (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower()
            if not host or len(host) > 253 or not re.fullmatch(r"[a-z0-9.:-]+", host):
                host = "<invalid>"
            elif host.endswith(".e.aa.online-metrix.net"):
                # This hostname prefix encodes a session even when its labels
                # are short. Never publish individual verification hostnames.
                host = "*.online-metrix.net"
            elif any(len(label) > 32 for label in host.split(".")):
                labels = host.split(".")
                suffix = labels[-2:]
                host = (
                    "*." + ".".join(suffix)
                    if len(suffix) == 2 and all(len(label) <= 32 for label in suffix)
                    else "<dynamic-host>"
                )
        except (TypeError, ValueError, UnicodeError):
            host = "<invalid>"
        allowed_types = {
            "document",
            "stylesheet",
            "image",
            "media",
            "font",
            "script",
            "texttrack",
            "xhr",
            "fetch",
            "eventsource",
            "websocket",
            "manifest",
            "other",
        }
        kind = resource_type if resource_type in allowed_types else "other"
        key = (host, kind)
        if key not in blocked_counts and len(blocked_counts) >= 32:
            return
        count = blocked_counts.get(key, 0)
        if count >= 100:
            return
        blocked_counts[key] = count + 1
        try:
            on_blocked(host, kind)
        except Exception:
            pass

    async def guard(route, request):
        try:
            host = urlsplit(request.url).hostname
            resource_type = getattr(request, "resource_type", "other")
            if request.is_navigation_request():
                frame = getattr(request, "frame", None)
                is_subframe = (
                    frame is not None
                    and frame is not getattr(page, "main_frame", None)
                    and getattr(frame, "parent_frame", None) is not None
                )
                if not is_subframe:
                    if host not in {
                        "kleinanzeigen.de",
                        "www.kleinanzeigen.de",
                        "login.kleinanzeigen.de",
                    }:
                        raise ValueError("unexpected top-level navigation host")
                    await validate_public_https_url(
                        request.url,
                        require_kleinanzeigen_host=True,
                        allow_kleinanzeigen_subdomains=True,
                    )
                elif host == "h.online-metrix.net" and resource_type == "document":
                    await validate_public_https_url(
                        request.url, require_kleinanzeigen_host=False
                    )
                else:
                    # Iframe navigation is still navigation in Playwright, but
                    # own device-verification frames are not top-level pages.
                    await validate_public_https_url(
                        request.url,
                        require_kleinanzeigen_host=True,
                        allow_kleinanzeigen_subdomains=True,
                    )
            else:
                verification_script = (
                    host == "h64.online-metrix.net" and resource_type == "script"
                )
                verification_image = (
                    resource_type == "image"
                    and isinstance(host, str)
                    and (
                        host == "h.online-metrix.net"
                        or re.fullmatch(
                            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.e\.aa\.online-metrix\.net",
                            host,
                        )
                        is not None
                    )
                )
                if verification_script or verification_image:
                    await validate_public_https_url(
                        request.url, require_kleinanzeigen_host=False
                    )
                elif host == "cdn.auth0.com":
                    if not urlsplit(request.url).path.startswith("/ulp/"):
                        raise ValueError("unexpected Auth0 resource")
                    await validate_public_https_url(
                        request.url, require_kleinanzeigen_host=False
                    )
                else:
                    await validate_public_https_url(
                        request.url,
                        require_kleinanzeigen_host=True,
                        allow_kleinanzeigen_subdomains=True,
                    )
        except Exception:
            report_blocked(request.url, getattr(request, "resource_type", "other"))
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def guard_socket(socket):
        try:
            url = socket.url
            if (
                not isinstance(url, str)
                or not url.startswith("wss://")
                or urlsplit(url).fragment
            ):
                raise ValueError("only WSS without fragments is supported")
            # Preserve the raw authority/path so the shared validator still
            # rejects credentials, ambiguous separators and control characters.
            await validate_public_https_url(
                "https://" + url[len("wss://") :],
                require_kleinanzeigen_host=True,
                allow_kleinanzeigen_subdomains=True,
            )
            # Playwright delegates to the native WebSocket constructor. The
            # standard handshake rejects redirects; frames pass unchanged and
            # are neither inspected nor recorded by this routing guard.
            socket.connect_to_server()
        except Exception:
            report_blocked(getattr(socket, "url", ""), "websocket")
            await socket.close(
                code=1008, reason="WebSocket target rejected by outbound policy"
            )

    await page.route("**/*", guard)
    await page.route_web_socket("**/*", guard_socket)


def _page_slice(items: list[dict], cursor: str | None, limit: int, key: str):
    if not 1 <= limit <= 200:
        raise MessagingError(
            "invalid_limit", "limit muss zwischen 1 und 200 liegen.", 422
        )
    start = 0
    if cursor:
        if not cursor.startswith("after:"):
            raise MessagingError("invalid_cursor", "Ungültiger Nachrichtencursor.", 422)
        anchor = validate_id(cursor[6:])
        positions = [i for i, item in enumerate(items) if item[key] == anchor]
        if len(positions) != 1:
            raise MessagingError(
                "cursor_expired",
                "Der Anker ist auf der aktuellen Seite nicht mehr vorhanden.",
                409,
            )
        start = positions[0] + 1
    selected = items[start : start + limit]
    more = start + len(selected) < len(items)
    return selected, ("after:" + selected[-1][key] if more and selected else None), more


class MessagingBrowserAdapter:
    def __init__(
        self,
        root: Path,
        outbound_controller,
        login_timeout_seconds: float = 900,
        *,
        browser_channel: str = "chrome",
    ):
        if browser_channel not in {"chrome", "chromium"}:
            raise ValueError("browser_channel must be chrome or chromium")
        self.root = Path(root).absolute()
        self.browser_channel = browser_channel
        self.controller = outbound_controller
        self.login_timeout_seconds = login_timeout_seconds
        self.dom_timeout_seconds = 10.0
        self._runtime = None
        self._contexts = {}
        self._headed = {}
        self._login_pages = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._runtime_lock = asyncio.Lock()
        self._blocked_resources: dict[str, dict[tuple[str, str], int]] = {}

    def _account_id(self, account):
        return validate_id(account["account_id"], account=True)

    @asynccontextmanager
    async def _account_lock(self, account):
        key = self._account_id(account)
        async with self._locks.setdefault(key, asyncio.Lock()):
            yield key

    async def _context(self, account, *, headed=True):
        key = self._account_id(account)
        if key in self._contexts and self._headed[key] != headed:
            await self._contexts.pop(key).close()
            self._headed.pop(key, None)
            self._login_pages.pop(key, None)
        if key not in self._contexts:
            profile = self.root / "profiles" / key
            if profile.is_symlink() or profile.parent.is_symlink():
                raise MessagingError(
                    "unsafe_profile",
                    "Das Browserprofil darf kein symbolischer Link sein.",
                    409,
                )
            profile.mkdir(mode=0o700, parents=True, exist_ok=True)
            profile.chmod(0o700)
            profile.parent.chmod(0o700)
            async with self._runtime_lock:
                if self._runtime is None:
                    self._runtime = await async_playwright().start()
            # Use the installed browser and its native identity. Login and
            # subsequent operations use the same visible mode; no UA override,
            # stealth flags, imported sessions, or silent browser fallback.
            try:
                context = await self._runtime.chromium.launch_persistent_context(
                    str(profile),
                    channel=self.browser_channel,
                    headless=not headed,
                    service_workers="block",
                    accept_downloads=False,
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                )
            except PlaywrightError as exc:
                raise MessagingError(
                    "browser_launch_failed",
                    "Der konfigurierte Nachrichtenbrowser konnte nicht gestartet werden. Browserinstallation und verfügbare Desktop-Sitzung prüfen.",
                    503,
                    {"browser_channel": self.browser_channel},
                ) from exc
            self._contexts[key] = context
            self._headed[key] = headed
        return self._contexts[key]

    async def _wait_snapshot(self, page, script, ready, message):
        deadline = asyncio.get_running_loop().time() + self.dom_timeout_seconds
        while True:
            try:
                value = await page.evaluate(script)
                if isinstance(value, dict) and ready(value):
                    return value
            except PlaywrightError as exc:
                # A navigation between DOM polls is expected during login and
                # SPA transitions. Other driver errors are not silently retried.
                if "Execution context was destroyed" not in str(exc):
                    raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise _unsupported(message)
            await asyncio.sleep(min(0.1, remaining))

    async def _new_page(self, account, *, headed=True):
        context = await self._context(account, headed=headed)
        page = await context.new_page()
        blocked = {}
        self._blocked_resources[self._account_id(account)] = blocked

        def record_blocked(host, resource_type):
            key = (host, resource_type)
            blocked[key] = blocked.get(key, 0) + 1

        await install_messaging_page_routing(page, on_blocked=record_blocked)
        return page

    def browser_diagnostics(self, account_id):
        """Bounded last-page metadata; never include URLs, messages or sessions."""
        validate_id(account_id, account=True)
        return {
            "browser_channel": self.browser_channel,
            "blocked_resources": [
                {"host": host, "resource_type": kind, "count": count}
                for (host, kind), count in sorted(
                    self._blocked_resources.get(account_id, {}).items()
                )
            ],
        }

    async def _identity(self, page, account, *, connecting=False):
        value = await self._wait_snapshot(
            page,
            _IDENTITY_JS,
            lambda v: (
                bool(v.get("ids") or v.get("login_form") or v.get("untrusted_origin"))
                or urlsplit(page.url).hostname == "login.kleinanzeigen.de"
            ),
            "Die angemeldete Kleinanzeigen-Nutzer-ID ist nicht eindeutig erkennbar.",
        )
        if value.get("untrusted_origin"):
            raise MessagingError(
                "unsafe_login_page",
                "Die Anmeldeseite befindet sich nicht auf einem erlaubten Kleinanzeigen-Host.",
                409,
            )
        ids = value["ids"]
        if not ids:
            if (
                value["login_form"]
                or "einloggen" in urlsplit(page.url).path
                or urlsplit(page.url).hostname == "login.kleinanzeigen.de"
            ):
                raise MessagingError(
                    "reauth_required",
                    "Der Account muss im Anmeldefenster angemeldet werden.",
                    409,
                )
            raise _unsupported(
                "Die angemeldete Kleinanzeigen-Nutzer-ID ist nicht eindeutig erkennbar."
            )
        if len(ids) != 1:
            raise _unsupported(
                "Die Oberfläche enthält widersprüchliche Account-Identitäten."
            )
        expected = account.get("user_id")
        if expected and str(expected) != ids[0]:
            raise MessagingError(
                "account_mismatch",
                "Das Browserprofil ist mit einem anderen Account angemeldet.",
                409,
            )
        if not connecting and not expected:
            raise MessagingError(
                "reauth_required", "Der Account wurde noch nicht verifiziert.", 409
            )
        return {"user_id": ids[0], "display_name": value.get("display_name")}

    async def _inspect(self, page, response=None):
        inspect_response = getattr(
            self.controller, "inspect_interaction_response", None
        )
        if inspect_response is not None:
            await inspect_response(page, response)

    async def _navigate(self, account, url, operation, *, headed=True, keep_page=False):
        retained = False

        async def factory():
            return await self._new_page(account, headed=headed)

        async def run_operation(page, response):
            nonlocal retained
            result = await operation(page, response)
            # A login page is handed to the human-input loop only after its
            # initial navigation and setup operation completed successfully.
            retained = keep_page
            return result

        async def cleanup(page):
            if page is not None and not retained:
                await page.close()

        return await self.controller.run_navigation(
            factory,
            url,
            run_operation,
            cleanup=cleanup,
            wait_until="domcontentloaded",
        )

    @staticmethod
    def _login_path(page):
        """Validate the loaded origin without DNS, navigation or DOM access."""
        try:
            raw = page.url
            url = urlsplit(raw)
            if (
                any(ord(char) < 32 or char == "\\" for char in raw)
                or url.scheme != "https"
                or url.hostname
                not in {
                    "www.kleinanzeigen.de",
                    "kleinanzeigen.de",
                    "login.kleinanzeigen.de",
                }
                or url.username is not None
                or url.password is not None
                or url.port not in {None, 443}
            ):
                raise ValueError("untrusted login origin")
            return url.path[:512]
        except (AttributeError, TypeError, ValueError) as exc:
            raise MessagingError(
                "unsafe_login_page",
                "Die Anmeldeseite befindet sich nicht auf einem erlaubten Kleinanzeigen-Host.",
                409,
            ) from exc

    async def _login_error_details(self, page):
        details = {"browser_window_open": False, "identity_present": False}
        if page is None or page.is_closed():
            return details
        details["browser_window_open"] = True
        try:
            details["path"] = self._login_path(page)
            # Return only a boolean. The script independently checks origin,
            # including a manual navigation racing with the Python URL check.
            details["identity_present"] = (
                await page.evaluate(
                    "() => { const value = ("
                    + _IDENTITY_JS
                    + ")(); return value.ids.length === 1; }"
                )
                is True
            )
        except (MessagingError, PlaywrightError):
            pass
        return details

    async def _close_login_context(self, key, page):
        self._login_pages.pop(key, None)
        context = self._contexts.pop(key, None)
        self._headed.pop(key, None)
        for resource in (page, context):
            if resource is not None:
                try:
                    await resource.close()
                except (Exception, asyncio.CancelledError):
                    # Keep the original navigation error/cancellation even if
                    # the user has already closed the browser themselves.
                    pass

    async def connect(self, account):
        async with self._account_lock(account) as key:

            async def open_login(page, _response):
                return page

            page = self._login_pages.get(key)
            retain = page is not None and not page.is_closed()
            try:
                if page is not None and not retain:
                    await self._close_login_context(key, page)
                    page = None
                try:
                    if page is None:
                        page = await self._navigate(
                            account, LOGIN_URL, open_login, headed=True, keep_page=True
                        )
                        self._login_pages[key] = page
                        retain = True
                    # A subsequent explicit connect only rechecks this page.
                    # Human login does not occupy an outbound permit, and a
                    # block never causes an automatic navigation or retry.
                    async with asyncio.timeout(self.login_timeout_seconds):
                        while True:
                            self._login_path(page)
                            await self._inspect(page)
                            self._login_path(page)
                            try:
                                identity = await self._identity(
                                    page, account, connecting=True
                                )
                                identity["capabilities"] = await self._capabilities(
                                    page
                                )
                                return identity
                            except MessagingError as exc:
                                if exc.code not in {
                                    "reauth_required",
                                    "ui_unsupported",
                                }:
                                    raise
                            await asyncio.sleep(1)
                except OutboundControlError as exc:
                    details = await self._login_error_details(page)
                    details["retry_after_seconds"] = exc.retry_after_seconds
                    details["block_indicator"] = getattr(exc, "block_indicator", None)
                    raise MessagingError(
                        exc.error_code,
                        "Kleinanzeigen hat den Zugriff eingeschränkt. Die Anmeldung wurde ohne erneuten Zugriffsversuch angehalten.",
                        exc.http_status,
                        details,
                    ) from exc
                except TimeoutError as exc:
                    raise MessagingError(
                        "reauth_required",
                        "Die Anmeldung wurde nicht rechtzeitig abgeschlossen.",
                        409,
                        await self._login_error_details(page),
                    ) from exc
            except asyncio.CancelledError:
                retain = False
                raise
            finally:
                if not retain:
                    await self._close_login_context(key, page)

    async def disconnect(self, account):
        async with self._account_lock(account) as key:
            self._login_pages.pop(key, None)
            context = self._contexts.pop(key, None)
            self._headed.pop(key, None)
            if context is not None:
                await context.clear_cookies()
                await context.close()
            # Close and remove persisted browser login state, but never touch
            # the independent message archive. A later connect starts cleanly.
            profile = self.root / "profiles" / key
            if profile.exists():
                import shutil

                await asyncio.to_thread(shutil.rmtree, profile)

    async def close(self):
        contexts = list(self._contexts.values())
        self._contexts.clear()
        self._headed.clear()
        self._login_pages.clear()
        for context in contexts:
            try:
                await context.close()
            except Exception:
                pass
        if self._runtime is not None:
            runtime, self._runtime = self._runtime, None
            await runtime.stop()

    async def _inbox(self, page):
        snapshot = await self._wait_snapshot(
            page,
            _INBOX_JS,
            lambda v: bool(v.get("conversations") or v.get("empty")),
            "Die Nachrichtenliste ist nicht eindeutig vorhanden oder noch nicht geladen.",
        )
        for conversation in snapshot["conversations"]:
            validate_id(conversation["conversation_id"])
        return snapshot

    async def _load_more(self, page, reader, snapshot, item_key, id_key, selector):
        controls = page.locator(selector)
        if await controls.count() != 1 or not await controls.is_enabled():
            raise _unsupported(
                "Weitere Nachrichten können derzeit nicht eindeutig nachgeladen werden."
            )
        before_ids = {item[id_key] for item in snapshot[item_key]}
        await controls.click()
        try:
            async with asyncio.timeout(self.dom_timeout_seconds):
                while True:
                    await self._inspect(page)
                    updated = await reader()
                    if not updated["more"] or before_ids != {
                        item[id_key] for item in updated[item_key]
                    }:
                        return updated
                    await asyncio.sleep(0.1)
        except TimeoutError as exc:
            raise _unsupported(
                "Die Nachrichtenliste wurde nicht vollständig nachgeladen."
            ) from exc

    async def _read_page(
        self,
        page,
        reader,
        *,
        item_key,
        id_key,
        cursor,
        limit,
        more_selector,
        reverse=False,
        unread_only=False,
    ):
        """Re-find a stable ID anchor after loading additional visible rows.

        Cursors contain an ID, never a URL or a browser script. For messages,
        newest-first ordering also makes prepended older pages traversable.
        """
        if cursor and not cursor.startswith("after:"):
            raise MessagingError("invalid_cursor", "Ungültiger Nachrichtencursor.", 422)
        anchor = validate_id(cursor[6:]) if cursor else None
        for attempt in range(21):
            snapshot = await reader()
            items = list(snapshot[item_key])
            if reverse:
                items.reverse()
            if unread_only:
                items = [item for item in items if item.get("unread")]
            positions = [i for i, item in enumerate(items) if item[id_key] == anchor]
            start = positions[0] + 1 if positions else 0
            enough = (anchor is None or bool(positions)) and len(items) - start > limit
            if enough or not snapshot["more"]:
                selected, next_cursor, has_more = _page_slice(
                    items, cursor, limit, id_key
                )
                has_more = has_more or snapshot["more"]
                if has_more and selected:
                    next_cursor = "after:" + selected[-1][id_key]
                return snapshot, selected, next_cursor, has_more
            if attempt == 20:
                raise _unsupported(
                    "Weitere Nachrichten können derzeit nicht eindeutig nachgeladen werden."
                )
            await self._load_more(
                page, reader, snapshot, item_key, id_key, more_selector
            )
        raise _unsupported()

    async def list_conversations(
        self, account, cursor=None, limit=50, unread_only=False
    ):
        async with self._account_lock(account):

            async def operation(page, _response):
                await self._identity(page, account)
                snapshot, selected, next_cursor, has_more = await self._read_page(
                    page,
                    lambda: self._inbox(page),
                    item_key="conversations",
                    id_key="conversation_id",
                    cursor=cursor,
                    limit=limit,
                    unread_only=unread_only,
                    more_selector=_MORE_CONVERSATIONS,
                )
                for item in selected:
                    item.pop("href", None)
                return {
                    "conversations": selected,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "coverage": "visible_page",
                    "complete": not snapshot["more"]
                    and not snapshot.get("potentially_truncated", False),
                }

            return await self._navigate(account, INBOX_URL, operation)

    async def _open_conversation(self, page, conversation_id):
        validate_id(conversation_id)
        snapshot = await self._inbox(page)
        for attempt in range(21):
            matches = [
                c
                for c in snapshot["conversations"]
                if c["conversation_id"] == conversation_id
            ]
            if len(matches) == 1:
                break
            if matches:
                raise _unsupported(
                    "Das Gespräch besitzt keine eindeutige Identität in dieser Liste."
                )
            if not snapshot["more"]:
                if snapshot.get("potentially_truncated") or not (
                    snapshot.get("known_container") or snapshot.get("empty")
                ):
                    raise _unsupported(
                        "Das Gespräch ist in der unvollständig erkannten Nachrichtenliste nicht sichtbar."
                    )
                raise MessagingError(
                    "conversation_not_found",
                    "Das Gespräch ist in diesem Account nicht vorhanden.",
                    404,
                )
            if attempt == 20:
                raise _unsupported(
                    "Das Gespräch liegt außerhalb der aktuell vollständig prüfbaren Nachrichtenliste."
                )
            snapshot = await self._load_more(
                page,
                lambda: self._inbox(page),
                snapshot,
                "conversations",
                "conversation_id",
                _MORE_CONVERSATIONS,
            )
        record = matches[0]
        # Selector interpolation is avoided for remote IDs, even validated
        # IDs, since dots/colons have special meanings in CSS selectors.
        rows = page.locator("[data-conversation-id]")
        for i in range(await rows.count()):
            row = rows.nth(i)
            if await row.get_attribute("data-conversation-id") == conversation_id:
                await row.click()
                await self._inspect(page)
                return
        href = record.get("href")
        if not href or urlsplit(href).hostname not in {
            "www.kleinanzeigen.de",
            "kleinanzeigen.de",
        }:
            raise _unsupported("Das Gespräch besitzt keinen eindeutigen Öffnen-Link.")
        links = page.locator('a[href*="conversationId="]')
        for i in range(await links.count()):
            link = links.nth(i)
            raw = await link.get_attribute("href")
            if parse_qs(urlsplit(raw or "").query).get("conversationId") == [
                conversation_id
            ]:
                await link.click()
                await self._inspect(page)
                return
        raise _unsupported("Das Gespräch konnte nicht eindeutig geöffnet werden.")

    async def _conversation(self, page, account, expected_id=None):
        snapshot = await self._wait_snapshot(
            page,
            _CONVERSATION_JS,
            lambda v: (
                bool(v.get("conversation_id"))
                and (expected_id is None or v["conversation_id"] == expected_id)
                and bool(v.get("messages") or v.get("empty"))
                and bool(v.get("recipients"))
            ),
            "Der ausgewählte Nachrichtenverlauf wurde nicht eindeutig geladen.",
        )
        conversation_id = snapshot.get("conversation_id")
        if not conversation_id or (
            expected_id is not None and conversation_id != expected_id
        ):
            raise _unsupported(
                "Das geöffnete Gespräch stimmt nicht mit dem ausgewählten Ziel überein."
            )
        validate_id(conversation_id)
        recipients = set(snapshot["recipients"]) - {str(account["user_id"])}
        if len(recipients) != 1:
            raise _unsupported(
                "Der Gesprächspartner ist nicht eindeutig verifizierbar."
            )
        recipient = recipients.pop()
        messages = snapshot["messages"]
        if not messages and not snapshot["empty"]:
            raise _unsupported(
                "Der Nachrichtenverlauf konnte nicht zuverlässig gelesen werden."
            )
        for message in messages:
            validate_id(message["message_id"])
            if not message.get("sender_user_id"):
                if message.get("direction") == "outgoing":
                    message["sender_user_id"] = str(account["user_id"])
                elif message.get("direction") == "incoming":
                    message["sender_user_id"] = recipient
            message.pop("direction", None)
        snapshot["recipient_user_id"] = recipient
        return snapshot

    async def _composer(self, page):
        composers = page.locator(_COMPOSER)
        visible = [
            composers.nth(i)
            for i in range(await composers.count())
            if await composers.nth(i).is_visible()
        ]
        if len(visible) != 1:
            raise _unsupported("Das Nachrichtenformular ist nicht eindeutig erkennbar.")
        return visible[0]

    async def _capabilities(self, page, *, first_contact=False):
        try:
            composer = await self._composer(page)
        except MessagingError:
            return {
                "scope": "current_page",
                "text_status": "unverified",
                "images_status": "unverified",
                "first_contact_images_status": "unverified",
                "can_send_text": False,
                "can_send_images": False,
                "first_contact_images": False,
            }
        editors = composer.locator(_EDITOR)
        files = composer.locator('input[type="file"]')
        buttons = composer.get_by_role("button", name=_SEND_NAME)
        can_text = await editors.count() == 1 and await buttons.count() == 1
        image_mode = None
        accept = []
        if await files.count() == 1:
            file_input = files.first
            accept = [
                s.strip()
                for s in (await file_input.get_attribute("accept") or "").split(",")
                if s.strip()
            ]
            if await file_input.get_attribute("data-auto-send") == "true":
                image_mode = "auto_send"
            elif (
                await file_input.get_attribute("data-auto-send") == "false"
                and await buttons.count() == 1
            ):
                # An HTML FORM with a send button does NOT establish upload
                # semantics: some clients still auto-send on file selection.
                # Only an explicit UI contract permits the subsequent click.
                image_mode = "explicit_submit"
        maxlength = (
            await editors.first.get_attribute("maxlength")
            if await editors.count() == 1
            else None
        )
        limit = (
            min(int(maxlength), MAX_TEXT_LENGTH)
            if maxlength and maxlength.isdecimal() and int(maxlength) > 0
            else MAX_TEXT_LENGTH
        )
        return {
            "scope": "current_page",
            "text_status": "supported" if can_text else "unverified",
            "images_status": "supported"
            if image_mode is not None
            else "unsupported"
            if can_text and await files.count() == 0
            else "unverified",
            "first_contact_images_status": "supported"
            if first_contact and image_mode is not None
            else "unsupported"
            if first_contact and can_text and await files.count() == 0
            else "unverified",
            "can_send_text": can_text,
            "can_send_images": image_mode is not None,
            "first_contact_images": first_contact and image_mode is not None,
            "image_send_mode": image_mode,
            "accepted_image_types": accept,
            "max_text_length": limit,
            "text_limit_source": "form" if maxlength else "application",
        }

    async def _assert_composer_clean(
        self, composer, kind, text=None, *, attachment_ids=None, filename=None
    ):
        draft = await composer.evaluate(_DRAFT_JS)
        allowed_ids = attachment_ids or set()
        if not isinstance(draft, dict):
            raise _unsupported(
                "Der Nachrichtenentwurf kann nicht vollständig geprüft werden."
            )
        texts = draft.get("texts", [])
        unexpected_text = (
            bool(texts) if kind == "image" else any(value != text for value in texts)
        )
        unexpected_images = bool(
            set(draft.get("attachment_ids", [])) - allowed_ids
        ) or draft.get("unknown_previews", False)
        unexpected_files = any(
            value != filename for value in draft.get("filenames", [])
        )
        if unexpected_text or unexpected_images or unexpected_files:
            raise MessagingError(
                "draft_conflict",
                "Im Nachrichtenformular liegt bereits ein anderer Text- oder Bildentwurf. Er wurde nicht versendet.",
                409,
            )

    async def _download_image(self, page, image):
        from types import SimpleNamespace

        url = image["url"]
        await validate_public_https_url(
            url, require_kleinanzeigen_host=True, allow_kleinanzeigen_subdomains=True
        )
        value = await page.evaluate(
            """async ({url, maxBytes}) => {
          const abort = new AbortController();
          const timer = setTimeout(() => abort.abort(), 30000);
          try {
            const r = await fetch(url, {credentials:'same-origin', redirect:'error', signal:abort.signal});
            // Preserve access/rate-limit evidence before classifying a missing
            // image. Never return response bodies, cookies or arbitrary headers.
            const response = {status:r.status,retry_after:(r.headers.get('retry-after') || '').slice(0,256)};
            if (!r.ok || !r.body) return {...response,error:'image_unavailable'};
            const mime = (r.headers.get('content-type') || '').split(';')[0].toLowerCase();
            if (!mime.startsWith('image/')) return {...response,error:'invalid_image'};
            const reader = r.body.getReader(); const chunks = []; let size=0;
            while (true) {const {done,value} = await reader.read(); if(done) break;
              size += value.byteLength; if(size > maxBytes) {abort.abort(); return {...response,error:'image_too_large'};} chunks.push(value);}
            const bytes = new Uint8Array(size); let offset=0;
            for (const chunk of chunks) {bytes.set(chunk,offset); offset+=chunk.byteLength;}
            let binary=''; for(let i=0;i<bytes.length;i+=8192) binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
            return {...response,data:btoa(binary),mime_type:mime};
          } finally {clearTimeout(timer);}
        }""",
            {"url": url, "maxBytes": MAX_IMAGE_BYTES},
        )
        status = value.get("status")
        retry_after = value.get("retry_after")
        response = SimpleNamespace(
            status=status,
            headers={"retry-after": retry_after}
            if isinstance(retry_after, str) and retry_after
            else {},
        )
        # The same process-wide circuit used by navigation must see the media
        # response too. In particular, 403/429 are NOT per-image placeholders.
        await self._inspect(page, response)
        if status in {403, 429}:
            # Defensive fail-closed behavior if an injected/custom inspector
            # does not raise; production OutboundController already raises.
            raise MessagingError(
                "upstream_rate_limited" if status == 429 else "upstream_blocked",
                "Kleinanzeigen hat den Bildabruf gesperrt oder begrenzt.",
                429 if status == 429 else 503,
            )
        error_code = value.get("error")
        if error_code in {"image_unavailable", "invalid_image", "image_too_large"}:
            raise MessagingError(
                error_code,
                "Dieses Nachrichtenbild konnte nicht geladen werden.",
                404 if error_code == "image_unavailable" else 422,
            )
        return {
            "data": base64.b64decode(value["data"], validate=True),
            "mime_type": value["mime_type"],
            "remote_id": image.get("remote_id"),
        }

    async def get_conversation(self, account, conversation_id, cursor=None, limit=50):
        async with self._account_lock(account):

            async def operation(page, _response):
                await self._identity(page, account)
                await self._open_conversation(page, conversation_id)
                snapshot, selected, next_cursor, has_more = await self._read_page(
                    page,
                    lambda: self._conversation(page, account, conversation_id),
                    item_key="messages",
                    id_key="message_id",
                    cursor=cursor,
                    limit=limit,
                    reverse=True,
                    more_selector=_MORE_MESSAGES,
                )
                for message in selected:
                    downloaded = []
                    for image in message["images"]:
                        try:
                            downloaded.append(await self._download_image(page, image))
                            continue
                        except UnsafeOutboundUrlError:
                            error_code = "image_url_unavailable"
                        except MessagingError as exc:
                            if exc.code not in {
                                "image_unavailable",
                                "invalid_image",
                                "invalid_image_size",
                                "unsupported_image",
                                "image_too_large",
                            }:
                                raise
                            error_code = exc.code
                        except PlaywrightError as exc:
                            # Only failures authored by the bounded image fetch
                            # or its fetch/abort mechanism are media failures.
                            # Closed pages, destroyed execution contexts and
                            # unrelated driver failures must still propagate.
                            detail = str(exc)
                            if not any(
                                marker in detail
                                for marker in (
                                    "Error: image_unavailable",
                                    "Error: invalid_image",
                                    "Error: image_too_large",
                                    "TypeError: Failed to fetch",
                                    "NetworkError when attempting to fetch resource",
                                    "AbortError:",
                                )
                            ):
                                raise
                            error_code = (
                                "image_too_large"
                                if "Error: image_too_large" in detail
                                else "invalid_image"
                                if "Error: invalid_image" in detail
                                else "image_unavailable"
                            )
                        # A missing image must not conceal a newly loaded
                        # access block, logout, or change of account identity.
                        await self._inspect(page)
                        await self._identity(page, account)
                        downloaded.append(
                            {
                                "unavailable": True,
                                "remote_id": image.get("remote_id"),
                                "error": {
                                    "code": error_code,
                                    "message": "Dieses Bild konnte nicht geladen werden.",
                                },
                            }
                        )
                    message["images"] = downloaded
                return {
                    "conversation_id": conversation_id,
                    "recipient_user_id": snapshot["recipient_user_id"],
                    "messages": selected,
                    "next_cursor": next_cursor,
                    "has_more": has_more,
                    "order": "newest_first",
                    "coverage": "visible_page",
                    "complete": not snapshot["more"],
                    "capabilities": await self._capabilities(page),
                }

            return await self._navigate(account, INBOX_URL, operation)

    async def _prepare(self, page, account, target_kind, target_id):
        identity = await self._identity(page, account)
        if target_kind == "conversation":
            await self._open_conversation(page, target_id)
            conversation = await self._conversation(page, account, target_id)
            return {
                "target_kind": target_kind,
                "target_id": target_id,
                "conversation_id": target_id,
                "recipient_user_id": conversation["recipient_user_id"],
                "account_user_id": identity["user_id"],
                "capabilities": await self._capabilities(page),
            }
        await self._verify_listing(page, target_id)
        recipients = await page.evaluate(_LISTING_RECIPIENT_JS)
        if len(recipients) != 1:
            raise _unsupported(
                "Der Anzeigenanbieter ist nicht eindeutig verifizierbar."
            )
        if recipients[0] == identity["user_id"]:
            raise MessagingError(
                "own_listing",
                "Der ausgewählte Account ist selbst der Anzeigenanbieter.",
                409,
            )
        try:
            await self._composer(page)
        except MessagingError:
            buttons = page.get_by_role(
                "button", name=re.compile(r"^Nachricht schreiben$")
            )
            links = page.get_by_role("link", name=re.compile(r"^Nachricht schreiben$"))
            candidates = buttons if await buttons.count() == 1 else links
            if await candidates.count() == 1:
                await candidates.click()
                await self._inspect(page)
        return {
            "target_kind": target_kind,
            "target_id": target_id,
            "listing_id": target_id,
            "recipient_user_id": recipients[0],
            "account_user_id": identity["user_id"],
            "capabilities": await self._capabilities(page, first_contact=True),
        }

    async def _verify_listing(self, page, listing_id):
        """Bind the actual document, not merely the requested URL, to the ad."""
        expected_core = normalize_listing_id(listing_id).split("-", 1)[0]
        parsed = urlsplit(page.url)
        try:
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"kleinanzeigen.de", "www.kleinanzeigen.de"}
                or parsed.port not in {None, 443}
                or parsed.username
                or parsed.password
                or not parsed.path.startswith("/s-anzeige/")
            ):
                raise ValueError()
            actual_core = normalize_listing_id(
                parsed.path.rstrip("/").rsplit("/", 1)[-1]
            ).split("-", 1)[0]
            if actual_core != expected_core:
                raise ValueError()
            visible_id = await page.evaluate(
                "() => document.querySelector('#viewad-ad-id-box > ul > li:nth-child(2)')?.textContent?.trim() || null"
            )
            if visible_id:
                ids = re.findall(r"\b[0-9]{6,20}\b", str(visible_id))
                if ids != [expected_core]:
                    raise ValueError()
        except (ValueError, TypeError) as exc:
            raise MessagingError(
                "listing_mismatch",
                "Die tatsächlich geöffnete Anzeige stimmt nicht mit dem ausgewählten Inserat überein.",
                409,
            ) from exc

    def _target_url(self, kind, target_id):
        if kind == "listing":
            return ORIGIN + "/s-anzeige/" + normalize_listing_id(target_id)
        if kind == "conversation":
            validate_id(target_id)
            return INBOX_URL
        raise MessagingError("invalid_target", "Ungültiger Nachrichtentyp.", 422)

    async def prepare_send(self, account, target_kind, target_id):
        url = self._target_url(target_kind, target_id)
        async with self._account_lock(account):

            async def operation(page, _response):
                return await self._prepare(page, account, target_kind, target_id)

            return await self._navigate(account, url, operation)

    async def _receipt(
        self, page, account, target, before_ids, kind, text, attachment_ids
    ):
        snapshot = await self._conversation(
            page, account, target.get("conversation_id")
        )
        if snapshot["recipient_user_id"] != target["recipient_user_id"]:
            raise MessagingError(
                "outcome_unknown",
                "Die Versandbestätigung gehört nicht eindeutig zum ausgewählten Empfänger.",
                409,
            )
        candidates = []
        for message in snapshot["messages"]:
            if message["message_id"] in before_ids or str(
                message["sender_user_id"]
            ) != str(account["user_id"]):
                continue
            if target.get("_expected_message_id"):
                if message["message_id"] != target["_expected_message_id"]:
                    continue
            elif target.get("_not_before"):
                # A first-contact form has no previous chat snapshot. Following
                # only a conversation link must not mistake an older identical
                # message for a fresh receipt. Without a precise timestamp or
                # an explicit server message ID the outcome stays unknown.
                try:
                    timestamp = datetime.fromisoformat(
                        str(message.get("created_at") or "").replace("Z", "+00:00")
                    )
                    threshold = datetime.fromisoformat(target["_not_before"])
                    if timestamp.tzinfo is None or timestamp < threshold:
                        continue
                except ValueError:
                    continue
            if kind == "text" and message["text"] == text:
                candidates.append(message)
            if (
                kind == "image"
                and attachment_ids
                and attachment_ids.intersection(
                    i.get("remote_id") for i in message["images"]
                )
            ):
                candidates.append(message)
        if len(candidates) == 1:
            return {
                "message_id": candidates[0]["message_id"],
                "conversation_id": snapshot["conversation_id"],
                "recipient_user_id": snapshot["recipient_user_id"],
                "confirmed": True,
                "created_at": candidates[0].get("created_at"),
            }
        return None

    async def _observed_follow_target(self, page):
        raw = await page.evaluate(_SEND_FOLLOW_JS)
        if not isinstance(raw, list):
            return None
        candidates = {}
        for item in raw:
            try:
                url = item["url"]
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname
                    not in {"kleinanzeigen.de", "www.kleinanzeigen.de"}
                    or parsed.port not in {None, 443}
                    or parsed.username
                    or parsed.password
                    or parsed.path != "/m-nachrichten.html"
                ):
                    continue
                values = parse_qs(parsed.query).get("conversationId", [])
                if len(values) != 1:
                    continue
                conversation_id = validate_id(values[0])
                message_id = item.get("message_id")
                if message_id is not None:
                    validate_id(message_id)
                # Repeated links to the same observed conversation are fine;
                # conflicting explicit message IDs are not an acknowledgement.
                previous = candidates.get(conversation_id)
                if (
                    previous
                    and previous.get("message_id")
                    and message_id
                    and previous["message_id"] != message_id
                ):
                    return None
                candidates[conversation_id] = {
                    "url": url,
                    "conversation_id": conversation_id,
                    "message_id": message_id or (previous or {}).get("message_id"),
                }
            except (ValueError, KeyError, TypeError, MessagingError):
                continue
        return next(iter(candidates.values())) if len(candidates) == 1 else None

    async def _image_payload(self, image):
        extensions = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
        }
        mime = image.get("mime_type")
        if mime not in extensions:
            raise MessagingError(
                "invalid_image", "Das bereitgestellte Bildformat ist ungültig.", 422
            )

        def read_staged():
            try:
                with Path(image["path"]).open("rb") as source:
                    data = source.read(MAX_IMAGE_BYTES + 1)
            except (OSError, KeyError) as exc:
                raise MessagingError(
                    "invalid_image",
                    "Die bereitgestellte Bilddatei ist nicht verfügbar.",
                    422,
                ) from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise MessagingError(
                    "invalid_image",
                    "Die bereitgestellte Bilddatei überschreitet die Größenbegrenzung.",
                    422,
                )
            if hashlib.sha256(data).hexdigest() != image.get("sha256"):
                raise MessagingError(
                    "image_changed",
                    "Die bereitgestellte Bilddatei wurde seit dem Upload verändert.",
                    409,
                )
            return data

        data = await asyncio.to_thread(read_staged)
        attachment_id = validate_id(image["attachment_id"])
        return {
            "name": f"{attachment_id}.{extensions[mime]}",
            "mimeType": mime,
            "buffer": data,
        }

    async def send_step(
        self,
        account,
        target,
        kind,
        text=None,
        image=None,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ):
        if before_dispatch is None:
            raise ValueError("before_dispatch is mandatory")
        url = self._target_url(target["target_kind"], target["target_id"])
        async with self._account_lock(account):

            async def operation(page, _response):
                current = await self._prepare(
                    page, account, target["target_kind"], target["target_id"]
                )
                if (
                    current["recipient_user_id"] != target["recipient_user_id"]
                    or current["account_user_id"] != target["account_user_id"]
                ):
                    raise MessagingError(
                        "recipient_mismatch",
                        "Account oder Empfänger hat sich vor dem Versand geändert.",
                        409,
                    )
                for field in (
                    "target_kind",
                    "target_id",
                    "conversation_id",
                    "listing_id",
                ):
                    if current.get(field) != target.get(field):
                        raise MessagingError(
                            "target_mismatch",
                            "Das Nachrichtenziel hat sich vor dem Versand geändert.",
                            409,
                        )
                caps = current["capabilities"]
                composer = await self._composer(page)
                await self._assert_composer_clean(composer, kind, text)
                before_snapshot = await page.evaluate(_CONVERSATION_JS)
                before_ids = {m["message_id"] for m in before_snapshot["messages"]}
                before_follow = await self._observed_follow_target(page)
                button = composer.get_by_role("button", name=_SEND_NAME)
                attachment_ids = set()
                dispatched = False
                dispatched_at = None
                response_tasks = []

                async def inspect_response(response):
                    if response.request.method in {"POST", "PUT", "PATCH"} and urlsplit(
                        response.url
                    ).hostname in {"kleinanzeigen.de", "www.kleinanzeigen.de"}:
                        await self._inspect(page, response)

                def on_response(response):
                    response_tasks.append(
                        asyncio.create_task(inspect_response(response))
                    )

                page.on("response", on_response)
                try:
                    if kind == "text":
                        if (
                            not caps["can_send_text"]
                            or not isinstance(text, str)
                            or not text.strip()
                        ):
                            raise MessagingError(
                                "text_unsupported",
                                "Textversand ist für dieses Ziel nicht verfügbar.",
                                409,
                            )
                        if len(text) > caps["max_text_length"]:
                            raise MessagingError(
                                "text_too_long",
                                "Der Text überschreitet die erlaubte Länge.",
                                422,
                            )
                        await composer.locator(_EDITOR).fill(text)
                        if not await button.is_enabled():
                            raise MessagingError(
                                "send_unavailable",
                                "Die Oberfläche erlaubt den Versand derzeit nicht.",
                                409,
                            )
                        await self._inspect(page)
                        await self._assert_composer_clean(composer, kind, text)
                        await before_dispatch()
                        dispatched = True
                        dispatched_at = datetime.now(timezone.utc).isoformat()
                        if current["target_kind"] == "listing":
                            current["_not_before"] = dispatched_at
                        await button.click()
                    elif kind == "image":
                        if not caps["can_send_images"] or image is None:
                            raise MessagingError(
                                "images_unsupported",
                                "Bildversand ist für dieses Ziel nicht verfügbar.",
                                409,
                            )
                        file_payload = await self._image_payload(image)
                        _validate_image_accept(
                            file_payload, caps.get("accepted_image_types")
                        )
                        await self._inspect(page)
                        # Some UIs send as soon as an upload is selected. The
                        # durable uncertainty boundary therefore precedes upload.
                        await before_dispatch()
                        dispatched = True
                        dispatched_at = datetime.now(timezone.utc).isoformat()
                        if current["target_kind"] == "listing":
                            current["_not_before"] = dispatched_at
                        await composer.locator('input[type="file"]').set_input_files(
                            file_payload
                        )
                        await composer.locator("[data-attachment-id]").first.wait_for(
                            state="attached", timeout=15_000
                        )
                        attachment_ids = set(
                            await composer.locator("[data-attachment-id]").evaluate_all(
                                "nodes => nodes.map(n => n.getAttribute('data-attachment-id')).filter(Boolean)"
                            )
                        )
                        if caps["image_send_mode"] == "explicit_submit":
                            # A matching already-sent attachment suppresses a
                            # second click even if a platform variant auto-sent.
                            try:
                                receipt = await self._receipt(
                                    page,
                                    account,
                                    current,
                                    before_ids,
                                    kind,
                                    text,
                                    attachment_ids,
                                )
                            except MessagingError:
                                receipt = None
                            if receipt:
                                return receipt
                            await self._inspect(page)
                            await self._assert_composer_clean(
                                composer,
                                kind,
                                attachment_ids=attachment_ids,
                                filename=file_payload["name"],
                            )
                            await before_dispatch()
                            await button.click()
                    else:
                        raise MessagingError(
                            "invalid_step", "Unbekannter Versandschritt.", 422
                        )

                    async with asyncio.timeout(30):
                        while True:
                            await self._inspect(page)
                            for task in response_tasks:
                                if task.done():
                                    task.result()
                            observed = await self._observed_follow_target(page)
                            if observed is not None and observed != before_follow:
                                if (
                                    current.get("conversation_id")
                                    and observed["conversation_id"]
                                    != current["conversation_id"]
                                ):
                                    raise MessagingError(
                                        "outcome_unknown",
                                        "Der angezeigte Gesprächslink gehört nicht zum ausgewählten Ziel.",
                                        409,
                                    )
                                return {
                                    "_follow": observed,
                                    "_before_ids": before_ids,
                                    "_attachment_ids": attachment_ids,
                                    "_dispatched_at": dispatched_at,
                                }
                            try:
                                receipt = await self._receipt(
                                    page,
                                    account,
                                    current,
                                    before_ids,
                                    kind,
                                    text,
                                    attachment_ids,
                                )
                            except MessagingError as exc:
                                if exc.code != "ui_unsupported":
                                    raise
                                receipt = None
                            if receipt:
                                return receipt
                            await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if dispatched:
                        raise MessagingError(
                            "outcome_unknown",
                            "Der Versand wurde ausgelöst, konnte aber nicht eindeutig bestätigt werden. Nicht erneut senden.",
                            409,
                        ) from exc
                    raise
                finally:
                    page.remove_listener("response", on_response)
                    for task in response_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*response_tasks, return_exceptions=True)

            result = await self._navigate(account, url, operation)
            if "_follow" not in result:
                return result
            follow = result["_follow"]
            receipt_target = {
                **target,
                "conversation_id": follow["conversation_id"],
                "_expected_message_id": follow.get("message_id"),
            }
            if target["target_kind"] == "listing" and not follow.get("message_id"):
                receipt_target["_not_before"] = result["_dispatched_at"]

            async def confirm(page, _response):
                await self._identity(page, account)
                async with asyncio.timeout(30):
                    while True:
                        await self._inspect(page)
                        receipt = await self._receipt(
                            page,
                            account,
                            receipt_target,
                            result["_before_ids"],
                            kind,
                            text,
                            result["_attachment_ids"],
                        )
                        if receipt:
                            return receipt
                        await asyncio.sleep(0.25)

            try:
                # The first navigation/interaction permit has been released.
                # This is a separate, rate-limited READ of an observed link;
                # no composer or dispatch callback is invoked again.
                return await self._navigate(account, follow["url"], confirm)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise MessagingError(
                    "outcome_unknown",
                    "Die anschließende Gesprächsansicht konnte den Versand nicht eindeutig bestätigen. Nicht erneut senden.",
                    409,
                ) from exc
