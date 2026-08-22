"""MCP server exposing the canonical persistent Kleinanzeigen job workflow."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import base64
import hashlib
import hmac
import json
import math
import os
import time
from typing import Annotated, Any, TypeVar

import httpx
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from utils.input_validation import normalize_listing_id
from utils.http_security import LoopbackOnlyMiddleware

from .client import BaseUrlProvider, KleinanzeigenClientError, KleinanzeigenJobClient
from .models import JobResultItem, JobStatus, WaitProgress
from .waiter import collect_search_results, wait_for_job


SCHEMA_VERSION = 1
DEFAULT_WAIT_SECONDS = 600.0
MAX_RESULT_ITEMS = 200
DETAIL_RESULT_CHAR_BUDGET = 450_000
ANTHROPIC_MAX_RESULT_CHARS = 500_000
MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS = 15 * 60.0
MAX_MERGE_SOURCE_JOBS = 32
MAX_MERGE_SNAPSHOT_ITEMS = 10_000
SERVER_INSTRUCTIONS = (
    "Nutze search_listings als Einstieg, wenn Inserate oder Angebote auf "
    "Kleinanzeigen gesucht werden. Es liefert alle freigegebenen Kandidaten bis "
    "200. Wenn alle Kandidaten verlangt sind und continuation.required=true ist, "
    "rufe continue_search mit exakt continuation.token und dem vorherigen "
    "next_cursor auf, bis die Suche vollständig ist. Vervollständige bei mehreren "
    "parallelen Suchjobs bei Bedarf jede Quelle und führe sie anschließend mit "
    "merge_search_results in der gewünschten Prioritätsreihenfolge zusammen; die "
    "erste Quelle liefert bei Duplikaten die kanonischen Daten. Ein Merge-Cursor "
    "bleibt an seinen Snapshot gebunden; starte nach einer späteren "
    "Quellenfortsetzung einen neuen Merge ohne Cursor. Rufe get_listing_details "
    "nur für explizit ausgewählte listing_ids auf. Folge bei laufenden Jobs, "
    "weiteren Ergebnisseiten und needs_attention dem ausgegebenen next_action und "
    "übernimm Token und Cursor exakt. Timeouts und Cooldowns beenden oder "
    "stornieren persistente Jobs nicht; starte dann keinen identischen Job neu. "
    "Verwende cancel_job nur, wenn die Arbeit wirklich abgebrochen werden soll."
)

_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_HEAVY_RESULT_META = {"anthropic/maxResultSizeChars": ANTHROPIC_MAX_RESULT_CHARS}

JobId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description=(
            "Exakte persistente job_id aus einem vorherigen Toolergebnis; "
            "unverändert übernehmen und nicht selbst konstruieren."
        ),
    ),
]
ContinuationToken = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description=(
            "Opaques Token exakt aus job.continuation.token; nur verwenden, wenn "
            "continuation.required=true ist. Nicht verändern und nicht mit "
            "anderen Jobs oder Revisionen mischen."
        ),
    ),
]
ResumeToken = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description=(
            "Opaques Token exakt aus job.resume.token; nur bei "
            "resume.required=true und state=needs_attention verwenden. Derselbe "
            "Token ist retry-sicher, autorisiert aber keinen späteren "
            "Attention-Zyklus."
        ),
    ),
]
ContinuationCursor = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description=(
            "Opaquer Ergebnisanker exakt aus next_cursor der vorherigen "
            "Suchtranche. Muss zum selben Job und continuation_token gehören; "
            "kein Merge-Cursor. Dasselbe Token-Cursor-Paar darf sicher "
            "wiederholt werden."
        ),
    ),
]
ResultCursor = Annotated[str, Field(min_length=1, max_length=512)]
MergeCursor = Annotated[str, Field(min_length=1, max_length=65_536)]
WaitSeconds = Annotated[
    float,
    Field(
        ge=0,
        le=DEFAULT_WAIT_SECONDS,
        description=(
            "Lokale Wartezeit in Sekunden von 0 bis 600. Begrenzt nur diesen "
            "Toolaufruf; der persistente Job läuft bei einem Timeout weiter. "
            "0 liest mindestens den aktuellen Status ohne weiteres Warten."
        ),
    ),
]
PageLimit = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_RESULT_ITEMS,
        description=(
            "Gewünschte maximale Anzahl Ergebnisobjekte dieser Seite von 1 bis "
            "200; Standard ist 200."
        ),
    ),
]
IdempotencyKey = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=_IDEMPOTENCY_PATTERN),
]


class ToolFailure(BaseModel):
    """Stable, non-sensitive failure shape visible to the model."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | int | None = None
    status_code: int | None = None
    suggested_action: str | None = None


class _ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    error: ToolFailure | None = None
    timed_out: bool = False
    next_action: str | None = None


class SearchToolOutput(_ToolOutput):
    job: dict[str, Any] | None = None
    listings: list[dict[str, Any]] = Field(default_factory=list)
    returned_count: int = 0
    has_more_available: bool = False
    next_cursor: str | None = None
    continuation: dict[str, Any] = Field(default_factory=dict)
    resume: dict[str, Any] = Field(default_factory=dict)
    timed_out: bool = False
    complete: bool = False
    idempotent_replay: bool = False
    idempotency_key: str | None = None


class DetailToolOutput(_ToolOutput):
    job: dict[str, Any] | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    requested_count: int = 0
    returned_count: int = 0
    next_cursor: str | None = None
    has_more: bool = False
    effective_limit: int = MAX_RESULT_ITEMS
    resume: dict[str, Any] = Field(default_factory=dict)
    timed_out: bool = False
    complete: bool = False
    idempotent_replay: bool = False
    idempotency_key: str | None = None


class MergeToolOutput(_ToolOutput):
    listings: list[dict[str, Any]] = Field(default_factory=list)
    returned_count: int = 0
    total_unique_in_snapshot: int = 0
    has_more: bool = False
    next_cursor: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    all_sources_complete: bool = False


class JobStatusToolOutput(_ToolOutput):
    job: dict[str, Any] | None = None


class JobResultsToolOutput(_ToolOutput):
    job: dict[str, Any] | None = None
    job_id: str | None = None
    type: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    requested_limit: int = 0
    effective_limit: int = 0


class ActionToolOutput(_ToolOutput):
    job: dict[str, Any] | None = None
    timed_out: bool = False
    idempotent_replay: bool = False


class CancelToolOutput(_ToolOutput):
    job_id: str | None = None
    type: str | None = None
    state: str | None = None
    idempotent_replay: bool = False


ClientContextFactory = Callable[[], AbstractAsyncContextManager[KleinanzeigenJobClient]]


@dataclass(frozen=True)
class ServerState:
    client: KleinanzeigenJobClient


OutputT = TypeVar("OutputT", bound=_ToolOutput)


class _StableToolErrorMiddleware:
    """Replace SDK argument-validation text with the public error contract."""

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        result = await call_next(ctx)
        is_unstructured_tool_error = (
            isinstance(result, CallToolResult)
            and result.is_error
            and result.structured_content is None
        ) or (
            isinstance(result, dict)
            and bool(result.get("isError", result.get("is_error", False)))
            and result.get("structuredContent", result.get("structured_content"))
            is None
        )
        if ctx.method != "tools/call" or not is_unstructured_tool_error:
            return result

        params = ctx.params if isinstance(ctx.params, Mapping) else {}
        output_type = _TOOL_OUTPUT_TYPES.get(params.get("name"))
        if output_type is None:
            return result
        return _tool_result(
            output_type(error=_failure(ValueError("invalid tool arguments"))),
            is_error=True,
        )


_TOOL_OUTPUT_TYPES: dict[str, type[_ToolOutput]] = {
    "search_listings": SearchToolOutput,
    "continue_search": SearchToolOutput,
    "merge_search_results": MergeToolOutput,
    "get_listing_details": DetailToolOutput,
    "get_job_status": JobStatusToolOutput,
    "get_job_results": JobResultsToolOutput,
    "resume_job": ActionToolOutput,
    "cancel_job": CancelToolOutput,
}


def network_client_factory(
    base_url: str | None = None,
    *,
    base_url_provider: BaseUrlProvider | None = None,
) -> ClientContextFactory:
    """Create clients for the STDIO adapter without importing the API app."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[KleinanzeigenJobClient]:
        resolved = base_url or os.environ.get(
            "KLEINANZEIGEN_API_BASE_URL", "http://127.0.0.1:8000"
        )
        async with httpx.AsyncClient(trust_env=False) as http_client:
            yield KleinanzeigenJobClient(
                resolved if base_url_provider is None else base_url,
                client=http_client,
                base_url_provider=base_url_provider,
            )

    return factory


def asgi_client_factory(app: Any) -> ClientContextFactory:
    """Create in-process clients which still exercise the canonical REST routes."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[KleinanzeigenJobClient]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as http_client:
            yield KleinanzeigenJobClient("http://127.0.0.1", client=http_client)

    return factory


class _TerminatedSessionCleanupMiddleware:
    """Remove a stateful SDK session after explicit termination.

    MCP SDK 2.0 terminates a transport on ``DELETE`` but leaves it in the
    manager dictionaries until shutdown. Limit the compatibility workaround
    to the session named by that request and only remove terminated transports.
    """

    def __init__(self, app: Any, *, session_manager: Any) -> None:
        self.app = app
        self.session_manager = session_manager

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        session_id = None
        if scope.get("type") == "http" and scope.get("method") == "DELETE":
            session_id = self._session_id(scope.get("headers", []))
        try:
            await self.app(scope, receive, send)
        finally:
            if session_id is not None:
                self._discard_if_terminated(session_id)

    @staticmethod
    def _session_id(headers: list[tuple[bytes, bytes]]) -> str | None:
        for name, value in headers:
            if name.lower() == b"mcp-session-id":
                try:
                    return value.decode("ascii")
                except UnicodeDecodeError:
                    return None
        return None

    def _discard_if_terminated(self, session_id: str) -> None:
        instances = getattr(self.session_manager, "_server_instances", None)
        if not isinstance(instances, dict):
            return
        transport = instances.get(session_id)
        if transport is None or not transport.is_terminated:
            return
        instances.pop(session_id, None)
        owners = getattr(self.session_manager, "_session_owners", None)
        if isinstance(owners, dict):
            owners.pop(session_id, None)


def create_streamable_http_app(
    server: MCPServer,
    *,
    session_idle_timeout_seconds: float = MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS,
) -> Any:
    """Build the bounded, stateful HTTP adapter at exactly ``/mcp``."""

    if (
        isinstance(session_idle_timeout_seconds, bool)
        or not math.isfinite(session_idle_timeout_seconds)
        or session_idle_timeout_seconds <= 0
    ):
        raise ValueError("session_idle_timeout_seconds must be finite and positive")

    http_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        max_request_body_size=64 * 1024,
    )
    # SDK 2.0 implements idle expiry, but its public app builder does not expose
    # the manager constructor's session_idle_timeout argument.
    server.session_manager.session_idle_timeout = float(session_idle_timeout_seconds)
    # Add cleanup first so the loopback guard remains outermost. A remote peer
    # must not be able to terminate a guessed local session ID.
    http_app.add_middleware(
        _TerminatedSessionCleanupMiddleware,
        session_manager=server.session_manager,
    )
    http_app.add_middleware(LoopbackOnlyMiddleware)
    return http_app


def create_mcp_server(client_factory: ClientContextFactory) -> MCPServer:
    """Create one MCP server; HTTP and STDIO share this exact tool catalog."""

    # Request IDs are scoped to one MCP connection and are routinely reused
    # after a STDIO client restarts.  A server-instance component prevents a
    # deliberately new request in that new connection from replaying an old
    # durable API job while retaining retry stability inside this instance.
    server_instance_id = os.urandom(16).hex()

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[ServerState]:
        async with client_factory() as client:
            yield ServerState(client=client)

    server = MCPServer(
        name="kleinanzeigen",
        title="Kleinanzeigen Agent",
        description=(
            "Lokaler MCP-Server für die Suche nach Inseraten, gebrauchten Artikeln "
            "und Fahrzeugen auf Kleinanzeigen.de, das Zusammenführen paralleler "
            "Suchen und den Abruf vollständiger Inserat- und Verkäuferdetails "
            "über persistente Browserjobs."
        ),
        instructions=SERVER_INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
        middleware=[_StableToolErrorMiddleware()],
    )

    @server.tool(
        name="search_listings",
        title="Kleinanzeigen durchsuchen",
        description=(
            "Nutze dieses Tool als Einstieg, wenn der Nutzer Inserate oder "
            "Kaufangebote auf Kleinanzeigen finden möchte. Es startet per Browser "
            "einen persistenten Suchjob mit vollständiger Such-URL oder "
            "strukturierten Filtern, wartet höchstens wait_timeout_seconds und "
            "liefert alle freigegebenen vollständigen Suchkarten bis 200. Bei "
            "timed_out=true läuft der Job weiter: nicht neu starten, sondern "
            "next_action folgen. Weitere Tranches werden nie automatisch "
            "freigegeben."
        ),
        annotations=_annotations(open_world=True),
        meta=_HEAVY_RESULT_META,
    )
    async def search_listings(
        ctx: Context,
        url: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=4096,
                description=(
                    "Vollständige Kleinanzeigen-Such-URL unter "
                    "https://kleinanzeigen.de oder https://www.kleinanzeigen.de "
                    "einschließlich gewünschter Kategorie und Filter. Nicht mit "
                    "query, location, radius, min_price oder max_price "
                    "kombinieren; min_publish_date darf zusätzlich gesetzt werden."
                ),
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                max_length=512,
                description=(
                    "Freier Suchbegriff für eine strukturierte Suche. Nicht "
                    "zusammen mit url verwenden; weglassen statt leer übergeben."
                ),
            ),
        ] = None,
        location: Annotated[
            str | None,
            Field(
                max_length=512,
                description=(
                    "Von Kleinanzeigen verstandener Ort oder Postleitzahl für die "
                    "strukturierte Suche. Nicht zusammen mit url verwenden."
                ),
            ),
        ] = None,
        radius: Annotated[
            int | None,
            Field(
                ge=0,
                description=(
                    "Nichtnegativer Suchradius in Kilometern um location. Nicht "
                    "zusammen mit url verwenden."
                ),
            ),
        ] = None,
        min_price: Annotated[
            int | None,
            Field(
                ge=0,
                description=(
                    "Nichtnegativer Mindestpreis in ganzen Euro. Nicht zusammen "
                    "mit url verwenden; darf max_price nicht überschreiten."
                ),
            ),
        ] = None,
        max_price: Annotated[
            int | None,
            Field(
                ge=0,
                description=(
                    "Nichtnegativer Höchstpreis in ganzen Euro. Nicht zusammen "
                    "mit url verwenden; darf min_price nicht unterschreiten."
                ),
            ),
        ] = None,
        min_publish_date: Annotated[
            datetime | None,
            Field(
                description=(
                    "Inklusive Veröffentlichungs-Untergrenze als "
                    "ISO-8601-Zeitstempel. Zeitstempel ohne Offset gelten als "
                    "Europe/Berlin; Zeitstempel mit Offset werden entsprechend "
                    "umgerechnet. Ältere Karten werden entfernt, Karten ohne "
                    "erkannten Zeitpunkt bleiben enthalten. Darf auch mit url "
                    "verwendet werden."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            IdempotencyKey | None,
            Field(
                description=(
                    "Optionaler stabiler Schlüssel für bewusste Wiederholungen "
                    "desselben Suchjob-Starts. Nie für andere Argumente oder einen "
                    "anderen Jobtyp wiederverwenden. Normalerweise weglassen; der "
                    "Server erzeugt und liefert dann einen Schlüssel."
                )
            ),
        ] = None,
        wait_timeout_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
    ) -> Annotated[CallToolResult, SearchToolOutput]:
        try:
            payload = _search_payload(
                url=url,
                query=query,
                location=location,
                radius=radius,
                min_price=min_price,
                max_price=max_price,
                min_publish_date=min_publish_date,
            )
            used_key = idempotency_key or _derived_idempotency_key(
                ctx,
                "search",
                payload,
                server_instance_id=server_instance_id,
            )
            client = _client(ctx)
            accepted = await client.create_search_job(payload, idempotency_key=used_key)
            waited = await _wait(client, accepted.job_id, wait_timeout_seconds, ctx)
            snapshot = await collect_search_results(
                client, accepted.job_id, max_results=MAX_RESULT_ITEMS
            )
            job = _job_view(waited.status)
            output = SearchToolOutput(
                job=job,
                listings=[_item_dict(item) for item in snapshot.items],
                returned_count=len(snapshot.items),
                has_more_available=snapshot.has_more,
                next_cursor=snapshot.cursor.api_cursor,
                continuation=waited.status.continuation,
                resume=waited.status.resume,
                timed_out=waited.timed_out,
                complete=waited.status.state == "completed",
                idempotent_replay=accepted.idempotent_replay,
                idempotency_key=used_key,
                next_action=_next_action(waited.status),
            )
            return _tool_result(output)
        except Exception as exc:
            return _tool_error(SearchToolOutput, exc)

    @server.tool(
        name="continue_search",
        title="Suche fortsetzen",
        description=(
            "Nutze dieses Tool nur, wenn alle Kandidaten verlangt sind und der "
            "Suchjob continuation.required=true meldet. Übergib dessen exakte "
            "job_id, continuation.token und das next_cursor der vorherigen "
            "Tranche; dadurch wird genau die nächste Tranche mit höchstens 200 "
            "neuen Treffern autorisiert und gegebenenfalls Browserarbeit "
            "gestartet. Dieselbe Token-Cursor-Kombination ist retry-sicher. Bei "
            "einem Timeout läuft der Job weiter."
        ),
        annotations=_annotations(idempotent=True, open_world=True),
        meta=_HEAVY_RESULT_META,
    )
    async def continue_search(
        ctx: Context,
        job_id: JobId,
        continuation_token: ContinuationToken,
        after_cursor: ContinuationCursor,
        wait_timeout_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
    ) -> Annotated[CallToolResult, SearchToolOutput]:
        try:
            client = _client(ctx)
            accepted = await client.continue_job(
                job_id,
                continuation_token,
                after_cursor=after_cursor,
            )
            waited = await _wait(client, job_id, wait_timeout_seconds, ctx)
            snapshot = await collect_search_results(
                client,
                job_id,
                cursor=after_cursor,
                max_results=MAX_RESULT_ITEMS,
            )
            output = SearchToolOutput(
                job=_job_view(waited.status),
                listings=[_item_dict(item) for item in snapshot.items],
                returned_count=len(snapshot.items),
                has_more_available=snapshot.has_more,
                next_cursor=snapshot.cursor.api_cursor,
                continuation=waited.status.continuation,
                resume=waited.status.resume,
                timed_out=waited.timed_out,
                complete=waited.status.state == "completed",
                idempotent_replay=accepted.idempotent_replay,
                next_action=_next_action(waited.status),
            )
            return _tool_result(output)
        except Exception as exc:
            return _tool_error(SearchToolOutput, exc)

    @server.tool(
        name="merge_search_results",
        title="Parallele Suchen zusammenführen",
        description=(
            "Nutze dieses Tool, um Ergebnisse mehrerer paralleler Suchjobs lokal "
            "zusammenzuführen. Es verursacht keinen Browserzugriff, behält die "
            "Reihenfolge der Quellen bei, dedupliziert nach listing_id und nutzt "
            "bei Überschneidungen die Daten der ersten Quelle; alle Fundstellen "
            "bleiben in source_job_ids erhalten. Seiten desselben Snapshots sind "
            "retry-sicher. Nach einer späteren Quellenfortsetzung einen neuen "
            "Merge ohne Cursor beginnen."
        ),
        annotations=_annotations(read_only=True, idempotent=True),
        meta=_HEAVY_RESULT_META,
    )
    async def merge_search_results(
        ctx: Context,
        search_job_ids: Annotated[
            list[JobId],
            Field(
                min_length=1,
                max_length=MAX_MERGE_SOURCE_JOBS,
                description=(
                    "1 bis 32 eindeutige Suchjob-IDs in gewünschter "
                    "Prioritätsreihenfolge. Nur Suchjobs angeben; bei derselben "
                    "listing_id in mehreren Quellen liefert der zuerst genannte "
                    "Job die kanonischen Kartendaten."
                ),
            ),
        ],
        cursor: Annotated[
            MergeCursor | None,
            Field(
                description=(
                    "Opaques next_cursor eines vorherigen "
                    "merge_search_results-Aufrufs. Mit exakt derselben geordneten "
                    "search_job_ids-Liste verwenden. Weglassen, um nach späteren "
                    "Quellfortsetzungen einen neuen Snapshot zu beginnen; kein "
                    "Job-Ergebnis-Cursor."
                )
            ),
        ] = None,
        limit: PageLimit = MAX_RESULT_ITEMS,
    ) -> Annotated[CallToolResult, MergeToolOutput]:
        try:
            if len(search_job_ids) != len(set(search_job_ids)):
                raise ValueError("search_job_ids must be unique")
            client = _client(ctx)
            state = (
                await _new_merge_state(client, search_job_ids)
                if cursor is None
                else _decode_merge_cursor(cursor, search_job_ids)
            )
            merged, statuses = await _materialize_merge(client, state)
            offset = state["offset"]
            if offset > len(merged):
                raise ValueError("invalid merge cursor")
            end = min(len(merged), offset + limit)
            page = merged[offset:end]
            has_more = end < len(merged)
            next_cursor = None
            if has_more:
                next_cursor = _encode_merge_cursor(
                    {
                        "v": 1,
                        "sources": state["sources"],
                        "watermarks": state["watermarks"],
                        "offset": end,
                    }
                )
            sources = [
                {
                    "job_id": status.job_id,
                    "state": status.state,
                    "snapshot_watermark": state["watermarks"][index],
                    "available_now": _available_results(status),
                    "continuation": status.continuation,
                    "resume": status.resume,
                    "job": _job_view(status),
                }
                for index, status in enumerate(statuses)
            ]
            output = MergeToolOutput(
                listings=page,
                returned_count=len(page),
                total_unique_in_snapshot=len(merged),
                has_more=has_more,
                next_cursor=next_cursor,
                sources=sources,
                all_sources_complete=all(
                    status.state == "completed" for status in statuses
                ),
                next_action=(
                    "merge_search_results" if has_more else _next_merge_action(statuses)
                ),
            )
            return _tool_result(output)
        except Exception as exc:
            return _tool_error(MergeToolOutput, exc)

    @server.tool(
        name="get_listing_details",
        title="Inseratdetails abrufen",
        description=(
            "Nutze dieses Tool erst, nachdem konkrete Inserate aus Suchergebnissen "
            "ausgewählt wurden. Es normalisiert und reihenfolgenstabil "
            "dedupliziert die expliziten listing_ids, startet dafür einen "
            "persistenten Browserjob und liefert vollständige Detailzustände ohne "
            "Kürzung einzelner Objekte. Bei timed_out=true oder weiteren Seiten "
            "den bestehenden Job mit get_job_status beziehungsweise "
            "get_job_results weiterverfolgen, nicht neu starten."
        ),
        annotations=_annotations(open_world=True),
        meta=_HEAVY_RESULT_META,
    )
    async def get_listing_details(
        ctx: Context,
        listing_ids: Annotated[
            list[str],
            Field(
                min_length=1,
                description=(
                    "Explizit ausgewählte listing_id-Werte aus Suchtreffern, keine "
                    "Inserat-URLs: numerische Anzeigen-ID oder kanonisches "
                    "ad-id-category-id-location-id-Segment. Reihenfolge bleibt "
                    "erhalten, Duplikate werden entfernt. Keine fachliche "
                    "Anzahlgrenze; bei payload_too_large selbst in Teilmengen "
                    "aufteilen."
                ),
            ),
        ],
        idempotency_key: Annotated[
            IdempotencyKey | None,
            Field(
                description=(
                    "Optionaler stabiler Schlüssel für bewusste Wiederholungen "
                    "desselben Detailjob-Starts. Nie für andere Argumente oder "
                    "einen anderen Jobtyp wiederverwenden. Normalerweise "
                    "weglassen; der Server erzeugt und liefert dann einen "
                    "Schlüssel."
                )
            ),
        ] = None,
        wait_timeout_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
    ) -> Annotated[CallToolResult, DetailToolOutput]:
        try:
            normalized = _normalise_listing_ids(listing_ids)
            used_key = idempotency_key or _derived_idempotency_key(
                ctx,
                "details",
                {"listing_ids": normalized},
                server_instance_id=server_instance_id,
            )
            client = _client(ctx)
            accepted = await client.create_detail_job(
                listing_ids=normalized,
                idempotency_key=used_key,
            )
            waited = await _wait(client, accepted.job_id, wait_timeout_seconds, ctx)
            requested_limit = min(MAX_RESULT_ITEMS, len(normalized))
            page, effective_limit = await _bounded_results_page(
                client,
                accepted.job_id,
                cursor=None,
                limit=requested_limit,
            )
            output = DetailToolOutput(
                job=_job_view(waited.status),
                items=[_item_dict(item) for item in page.items],
                requested_count=len(normalized),
                returned_count=len(page.items),
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                effective_limit=effective_limit,
                resume=waited.status.resume,
                timed_out=waited.timed_out,
                complete=waited.status.state == "completed",
                idempotent_replay=accepted.idempotent_replay,
                idempotency_key=used_key,
                next_action=_next_after_results(waited.status, page.has_more),
            )
            return _tool_result(output)
        except Exception as exc:
            return _tool_error(DetailToolOutput, exc)

    @server.tool(
        name="get_job_status",
        title="Jobstatus lesen",
        description=(
            "Nutze dieses Tool, um einen gestarteten Such- oder Detailjob nach "
            "einem Timeout, während eines Cooldowns oder bei unklarem Fortschritt "
            "zu prüfen. Es liest ausschließlich den persistenten lokalen Zustand "
            "einschließlich Fortschritt, Ergebnismengen, Continue-/Resume-Token "
            "und nächster Aktion; es verursacht keinen Browserzugriff und ist "
            "retry-sicher."
        ),
        annotations=_annotations(read_only=True, idempotent=True),
    )
    async def get_job_status(
        ctx: Context,
        job_id: JobId,
    ) -> Annotated[CallToolResult, JobStatusToolOutput]:
        try:
            status = await _client(ctx).get_job(job_id)
            return _tool_result(
                JobStatusToolOutput(
                    job=_job_view(status),
                    next_action=_next_action(status),
                )
            )
        except Exception as exc:
            return _tool_error(JobStatusToolOutput, exc)

    @server.tool(
        name="get_job_results",
        title="Persistierte Jobergebnisse lesen",
        description=(
            "Nutze dieses Tool, um Ergebnisse eines vorhandenen Jobs seitenweise "
            "zu lesen, insbesondere nach timed_out=true oder solange has_more=true "
            "ist. Es verursacht keinen Browserzugriff. Suchresultate sind "
            "unveränderlich; laufende Detailjobs liefern einen Change-Feed mit "
            "möglicherweise wiederholter listing_id, während ein terminaler "
            "Detailjob ohne Cursor den aktuellen Snapshot liefert. Folge nur bei "
            "has_more=true dem exakten next_cursor."
        ),
        annotations=_annotations(read_only=True, idempotent=True),
        meta=_HEAVY_RESULT_META,
    )
    async def get_job_results(
        ctx: Context,
        job_id: JobId,
        cursor: Annotated[
            ResultCursor | None,
            Field(
                description=(
                    "Opaques next_cursor der vorherigen get_job_results-Seite "
                    "desselben Jobs. Exakt übernehmen und niemals selbst "
                    "konstruieren; für die erste Seite beziehungsweise einen "
                    "aktuellen terminalen Detail-Snapshot weglassen."
                )
            ),
        ] = None,
        limit: Annotated[
            PageLimit,
            Field(
                description=(
                    "Gewünschte maximale Anzahl Ergebnisobjekte von 1 bis 200. "
                    "Bei großen Detailobjekten kann effective_limit zum Schutz "
                    "der Antwort kleiner sein."
                )
            ),
        ] = MAX_RESULT_ITEMS,
    ) -> Annotated[CallToolResult, JobResultsToolOutput]:
        try:
            client = _client(ctx)
            # Read status first. A terminal status is committed only after all
            # result/events are durable, so the following page cannot miss a
            # final commit while simultaneously telling the agent to stop.
            status = await client.get_job(job_id)
            page, effective_limit = await _bounded_results_page(
                client, job_id, cursor=cursor, limit=limit
            )
            output = JobResultsToolOutput(
                job=_job_view(status),
                job_id=page.job_id,
                type=page.type,
                items=[_item_dict(item) for item in page.items],
                next_cursor=page.next_cursor,
                has_more=page.has_more,
                requested_limit=limit,
                effective_limit=effective_limit,
                next_action=_next_after_results(status, page.has_more),
            )
            return _tool_result(output)
        except Exception as exc:
            return _tool_error(JobResultsToolOutput, exc)

    @server.tool(
        name="resume_job",
        title="Job wiederaufnehmen",
        description=(
            "Nutze dieses Tool nur, wenn ein Job state=needs_attention und "
            "resume.required=true meldet. Übergib die exakte job_id und den "
            "ausgegebenen resume.token; dadurch wird genau ein "
            "Wiederaufnahmezyklus autorisiert und gegebenenfalls Browserarbeit "
            "gestartet. Derselbe Token ist retry-sicher. Bei einem Timeout läuft "
            "der Job weiter."
        ),
        annotations=_annotations(idempotent=True, open_world=True),
    )
    async def resume_job(
        ctx: Context,
        job_id: JobId,
        resume_token: ResumeToken,
        wait_timeout_seconds: WaitSeconds = DEFAULT_WAIT_SECONDS,
    ) -> Annotated[CallToolResult, ActionToolOutput]:
        try:
            client = _client(ctx)
            accepted = await client.resume_job(job_id, resume_token)
            waited = await _wait(client, job_id, wait_timeout_seconds, ctx)
            return _tool_result(
                ActionToolOutput(
                    job=_job_view(waited.status),
                    timed_out=waited.timed_out,
                    idempotent_replay=accepted.idempotent_replay,
                    next_action=_next_action(waited.status),
                )
            )
        except Exception as exc:
            return _tool_error(ActionToolOutput, exc)

    @server.tool(
        name="cancel_job",
        title="Job abbrechen",
        description=(
            "Nutze dieses Tool nur, wenn ein persistenter Job wirklich dauerhaft "
            "abgebrochen werden soll. Es setzt den Job auf abgebrochen und wartet, "
            "bis eine gerade laufende Browseroperation beendet ist. Die Aktion ist "
            "destruktiv, aber für dieselbe job_id retry-sicher."
        ),
        annotations=_annotations(
            destructive=True,
            idempotent=True,
        ),
    )
    async def cancel_job(
        ctx: Context,
        job_id: JobId,
    ) -> Annotated[CallToolResult, CancelToolOutput]:
        try:
            accepted = await _client(ctx).cancel_job(job_id)
            return _tool_result(
                CancelToolOutput(
                    job_id=accepted.job_id,
                    type=accepted.type,
                    state=accepted.state,
                    idempotent_replay=accepted.idempotent_replay,
                )
            )
        except Exception as exc:
            return _tool_error(CancelToolOutput, exc)

    return server


def _annotations(
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


def _client(ctx: Context) -> KleinanzeigenJobClient:
    state = ctx.request_context.lifespan_context
    if not isinstance(state, ServerState):
        raise RuntimeError("MCP backend client is unavailable")
    return state.client


def _search_payload(
    *,
    url: str | None,
    query: str | None,
    location: str | None,
    radius: int | None,
    min_price: int | None,
    max_price: int | None,
    min_publish_date: datetime | None,
) -> dict[str, Any]:
    structured = (query, location, radius, min_price, max_price)
    if url is not None and any(value is not None for value in structured):
        raise ValueError("url cannot be combined with structured search filters")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("min_price must not exceed max_price")
    payload: dict[str, Any] = {}
    for name, value in (
        ("url", url),
        ("query", query),
        ("location", location),
        ("radius", radius),
        ("min_price", min_price),
        ("max_price", max_price),
    ):
        if value is not None:
            payload[name] = value
    if min_publish_date is not None:
        payload["min_publish_date"] = min_publish_date.isoformat()
    return payload


def _normalise_listing_ids(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_listing_id(value)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if not result:
        raise ValueError("listing_ids must not be empty")
    return result


def _derived_idempotency_key(
    ctx: Context,
    operation: str,
    payload: dict[str, Any],
    *,
    server_instance_id: str,
) -> str:
    headers = ctx.headers or {}
    session_id = headers.get("mcp-session-id")
    client_params = ctx.session.client_params
    if client_params is None:
        client_identity = "anonymous"
    else:
        client_identity = (
            f"{client_params.client_info.name}:{client_params.client_info.version}"
        )
    connection_identity = session_id or f"{server_instance_id}:{client_identity}"
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(
        f"{connection_identity}:{ctx.request_id}:{operation}:{canonical}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"mcp:{operation}:{digest}"


async def _wait(
    client: KleinanzeigenJobClient,
    job_id: str,
    timeout_seconds: float,
    ctx: Context,
):
    last_signature: str | None = None
    last_sent_at = 0.0

    async def progress_callback(progress: WaitProgress) -> None:
        nonlocal last_signature, last_sent_at
        signature = json.dumps(
            {
                "state": progress.state,
                "progress": progress.status.progress,
                "results": progress.status.results,
                "cooldown": progress.status.cooldown,
            },
            sort_keys=True,
            default=str,
        )
        now = time.monotonic()
        if signature == last_signature and now - last_sent_at < 15:
            return
        last_signature = signature
        last_sent_at = now
        done, total = _progress_numbers(progress.status, progress.poll_count)
        message = (
            f"Job {progress.job_id}: {progress.state}; "
            f"{progress.remaining_seconds:.0f}s lokale Wartezeit verbleiben"
        )
        try:
            await ctx.report_progress(done, total, message)
        except Exception:
            # Progress is advisory; an older client must not fail the job call.
            return

    return await wait_for_job(
        client,
        job_id,
        timeout_seconds=timeout_seconds,
        progress_callback=progress_callback,
    )


def _progress_numbers(status: JobStatus, poll_count: int) -> tuple[float, float | None]:
    progress = status.progress
    for key in ("processed_items", "processed", "completed", "complete"):
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            done = float(value)
            break
    else:
        done = float(poll_count)
    for key in ("total_items", "total"):
        value = progress.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return done, float(value)
    return done, None


def _job_view(status: JobStatus) -> dict[str, Any]:
    result = status.model_dump(mode="json", exclude_none=True)
    result["terminal"] = status.terminal
    if status.state == "awaiting_continuation":
        result["action_required"] = "continue"
    elif status.state == "needs_attention":
        result["action_required"] = "resume"
    else:
        result["action_required"] = None
    return result


def _next_action(status: JobStatus) -> str | None:
    if status.state == "awaiting_continuation":
        return "continue_search"
    if status.state == "needs_attention":
        return "resume_job"
    if status.state in {"queued", "running", "waiting_cooldown"}:
        return "get_job_status"
    if status.state == "completed" and status.type == "search":
        return "get_listing_details für explizit ausgewählte listing_ids"
    if status.state == "completed" and status.type == "details":
        return "get_job_results"
    return None


def _next_after_results(status: JobStatus, has_more: bool) -> str | None:
    if has_more:
        return "get_job_results"
    if status.state == "completed" and status.type == "details":
        return None
    return _next_action(status)


def _next_merge_action(statuses: Sequence[JobStatus]) -> str | None:
    if any(status.state == "awaiting_continuation" for status in statuses):
        return "continue_search für die unvollständigen Quellen"
    if any(status.state == "needs_attention" for status in statuses):
        return "resume_job für die betroffenen Quellen"
    if any(not status.terminal for status in statuses):
        return "get_job_status für die laufenden Quellen"
    return "get_listing_details für explizit ausgewählte listing_ids"


def _item_dict(item: JobResultItem) -> dict[str, Any]:
    return item.model_dump(mode="json", exclude_none=True)


async def _bounded_results_page(
    client: KleinanzeigenJobClient,
    job_id: str,
    *,
    cursor: str | None,
    limit: int,
):
    effective_limit = limit
    while True:
        page = await client.get_results(job_id, cursor=cursor, limit=effective_limit)
        serialized = json.dumps(
            [_item_dict(item) for item in page.items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized) <= DETAIL_RESULT_CHAR_BUDGET or effective_limit == 1:
            return page, effective_limit
        effective_limit = max(1, effective_limit // 2)


async def _new_merge_state(
    client: KleinanzeigenJobClient,
    search_job_ids: list[str],
) -> dict[str, Any]:
    statuses = [await client.get_job(job_id) for job_id in search_job_ids]
    if any(status.type != "search" for status in statuses):
        raise ValueError("merge_search_results accepts search jobs only")
    watermarks = [_available_results(status) for status in statuses]
    _validate_merge_workload(search_job_ids, watermarks)
    return {
        "v": 1,
        "sources": list(search_job_ids),
        "watermarks": watermarks,
        "offset": 0,
    }


async def _materialize_merge(
    client: KleinanzeigenJobClient,
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[JobStatus]]:
    _validate_merge_workload(state["sources"], state["watermarks"])
    statuses: list[JobStatus] = []
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for source_index, job_id in enumerate(state["sources"]):
        status = await client.get_job(job_id)
        if status.type != "search":
            raise ValueError("merge_search_results accepts search jobs only")
        watermark = state["watermarks"][source_index]
        if _available_results(status) < watermark:
            raise ValueError("search snapshot watermark is no longer available")
        statuses.append(status)
        source_items = await _read_search_items(client, job_id, watermark)
        for item in source_items:
            position = positions.get(item.listing_id)
            if position is None:
                value = _item_dict(item)
                value["source_job_ids"] = [job_id]
                positions[item.listing_id] = len(merged)
                merged.append(value)
            else:
                sources = merged[position]["source_job_ids"]
                if job_id not in sources:
                    sources.append(job_id)
    return merged, statuses


async def _read_search_items(
    client: KleinanzeigenJobClient,
    job_id: str,
    watermark: int,
) -> list[JobResultItem]:
    items: list[JobResultItem] = []
    cursor: str | None = None
    while len(items) < watermark:
        page = await client.get_results(
            job_id,
            cursor=cursor,
            limit=min(MAX_RESULT_ITEMS, watermark - len(items)),
        )
        if page.type != "search":
            raise ValueError("merge_search_results accepts search jobs only")
        items.extend(page.items)
        if len(items) >= watermark:
            break
        if not page.items or page.next_cursor is None or page.next_cursor == cursor:
            raise ValueError("search result snapshot is inconsistent")
        cursor = page.next_cursor
    return items[:watermark]


def _available_results(status: JobStatus) -> int:
    value = status.results.get("available", status.results.get("released", 0))
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("job status contains an invalid available result count")
    return value


def _validate_merge_workload(
    source_job_ids: Sequence[str],
    watermarks: Sequence[int],
) -> None:
    if len(source_job_ids) > MAX_MERGE_SOURCE_JOBS:
        raise KleinanzeigenClientError(
            code="merge_snapshot_too_large",
            message=(
                "The merge snapshot contains too many source jobs; split it into "
                "smaller groups"
            ),
        )
    if sum(watermarks) > MAX_MERGE_SNAPSHOT_ITEMS:
        raise KleinanzeigenClientError(
            code="merge_snapshot_too_large",
            message=(
                "The merge snapshot contains too many visible results; merge "
                "smaller or less-continued search jobs"
            ),
        )


def _encode_merge_cursor(state: dict[str, Any]) -> str:
    body = json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    envelope = {
        "p": base64.urlsafe_b64encode(body).decode("ascii").rstrip("="),
        "c": hashlib.sha256(body).hexdigest()[:24],
    }
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_merge_cursor(
    cursor: str,
    expected_sources: list[str],
) -> dict[str, Any]:
    try:
        raw = _urlsafe_decode(cursor)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"p", "c"}:
            raise ValueError
        body = _urlsafe_decode(envelope["p"])
        checksum = hashlib.sha256(body).hexdigest()[:24]
        if not hmac.compare_digest(checksum, envelope["c"]):
            raise ValueError
        state = json.loads(body)
        if (
            not isinstance(state, dict)
            or set(state) != {"v", "sources", "watermarks", "offset"}
            or state["v"] != 1
            or state["sources"] != expected_sources
            or not isinstance(state["watermarks"], list)
            or len(state["watermarks"]) != len(expected_sources)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in state["watermarks"]
            )
            or isinstance(state["offset"], bool)
            or not isinstance(state["offset"], int)
            or state["offset"] < 0
        ):
            raise ValueError
        _validate_merge_workload(state["sources"], state["watermarks"])
        return state
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("invalid merge cursor") from exc


def _urlsafe_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _tool_result(output: OutputT, *, is_error: bool = False) -> CallToolResult:
    payload = output.model_dump(mode="json", exclude_none=True)
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=is_error,
    )


def _tool_error(output_type: type[OutputT], exc: Exception) -> CallToolResult:
    failure = _failure(exc)
    return _tool_result(output_type(error=failure), is_error=True)


def _failure(exc: Exception) -> ToolFailure:
    if isinstance(exc, KleinanzeigenClientError):
        code = exc.code
        message = exc.message
        retryable = exc.retryable
        retry_after = exc.retry_after_seconds
        status_code = exc.status_code
    elif isinstance(exc, ValueError):
        code = "invalid_argument"
        message = "Die Toolargumente sind ungültig"
        retryable = False
        retry_after = None
        status_code = None
    else:
        code = "internal_error"
        message = "Der MCP-Server konnte die Anfrage nicht verarbeiten"
        retryable = False
        retry_after = None
        status_code = None
    suggested = {
        "invalid_argument": "Argumente korrigieren und erneut aufrufen",
        "invalid_cursor": "Ohne Cursor neu lesen",
        "job_not_found": "Job-ID prüfen",
        "job_conflict": "get_job_status aufrufen",
        "payload_too_large": "listing_ids in selbst gewählte Teilmengen aufteilen",
        "merge_snapshot_too_large": (
            "Weniger oder kleinere Suchjobs je Merge-Snapshot verwenden"
        ),
        "rate_limited": "Nach retry_after_seconds erneut versuchen",
        "service_unavailable": "Lokale API und /health prüfen",
        "job_manager_unavailable": "Lokale API und /health prüfen",
        "transport_error": "Lokale API unter 127.0.0.1 starten oder prüfen",
        "transport_timeout": "Jobstatus später erneut lesen",
    }.get(code)
    if suggested is None and status_code == 429:
        suggested = "Nach retry_after_seconds erneut versuchen"
    elif suggested is None and status_code == 503:
        suggested = "Lokale API und /health prüfen"
    elif suggested is None and status_code in {401, 403}:
        suggested = "API-Konfiguration und Berechtigung prüfen"
    elif suggested is None and retryable:
        suggested = "Aufruf später erneut versuchen"
    return ToolFailure(
        code=code,
        message=message,
        retryable=retryable,
        retry_after_seconds=retry_after,
        status_code=status_code,
        suggested_action=suggested,
    )


__all__ = [
    "ANTHROPIC_MAX_RESULT_CHARS",
    "ClientContextFactory",
    "DEFAULT_WAIT_SECONDS",
    "MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS",
    "SERVER_INSTRUCTIONS",
    "ServerState",
    "asgi_client_factory",
    "create_mcp_server",
    "create_streamable_http_app",
    "network_client_factory",
]
