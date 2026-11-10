# Ebay Kleinanzeigen API

<div align="center">
  <h3 align="center">Ebay Kleinanzeigen API</h3>

  <p align="center">
    A powerful API interface for Ebay-Kleinanzeigen.de that enables you to fetch listings and specific data.
  </p>

  <p align="center">
    <b>🚀 Looking for a ready-to-use solution?</b>
    <br />
    Try it at <a href="https://kleinanzeigen-agent.de/features/developer-api"><strong>kleinanzeigen-agent.de »</strong></a>
    <br />
    ✓ Automated Search Agents
    <br />
    ✓ Search & Detail API
    <br />
    <a href="https://github.com/DanielWTE/ebay-kleinanzeigen-api/issues">Report Bug</a>
    ·
    <a href="https://github.com/DanielWTE/ebay-kleinanzeigen-api/issues">Request Feature</a>
  </p>
</div>

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Getting Started

### Want to skip the setup?

Visit [kleinanzeigen-agent.de](https://kleinanzeigen-agent.de/features/developer-api) for our hosted solution with additional features and zero configuration required.

### Prerequisites

- Python 3.12 or higher
- pip (Python package manager)
- Playwright

### Installation

1. Clone the repository

```sh
git clone https://github.com/DanielWTE/ebay-kleinanzeigen-api.git
cd ebay-kleinanzeigen-api
```

2. Install dependencies

```sh
pip install -r requirements.txt
playwright install chromium
```

3. Start the API

```sh
uvicorn main:app --reload
```

The API will be available at `http://localhost:8000`

#### Browser mode

The public listing browser always launches Chromium in headless mode. A
`HEADLESS` environment switch is not implemented. The separate messaging browser
uses visible Google Chrome; see the messaging configuration below.

### Installation using UV

#### Prerequisites

- uv package manager

#### Installation of dependencies

```sh
uv sync
```

#### Start the API

```sh
uv run uvicorn main:app --reload
```

### Installation using Docker

1. Build the Docker image

```sh
docker build -t ebay-kleinanzeigen-api .
```

2. Run the Docker container

```sh
docker run -p 8000:8000 -v kleinanzeigen-jobs:/app/data ebay-kleinanzeigen-api
```

The API will be available at `http://localhost:8000`

### Outbound control and production limits

API calls now create durable SQLite jobs instead of keeping a request open while
browser work waits. A fixed worker pool schedules one navigation per job and
round, so several agents can submit work concurrently without one large job
monopolizing the outbound connection. Accepted jobs have no queue-age or total
runtime timeout and survive client disconnects and API restarts.

Every search page, detail page, retry, legacy adapter, and half-open probe still
passes through one process-wide outbound controller. Its concurrency permit is
held from page allocation through `page.goto()`, extraction, and cleanup. The
SQLite queue therefore stores waiting work without creating one coroutine per
listing, while the controller remains the only production code allowed to call
`page.goto()`.

The following environment variables are read and validated during application
startup. Invalid or out-of-range values abort startup with a clear error instead
of silently weakening the limits:

| Environment variable | Default | Purpose |
|---|---:|---|
| `KLEINANZEIGEN_OUTBOUND_MAX_CONCURRENCY` | `1` | Maximum full page lifecycles active at once. |
| `KLEINANZEIGEN_OUTBOUND_MIN_INTERVAL_SECONDS` | `3.0` | Minimum interval between navigation starts. |
| `KLEINANZEIGEN_OUTBOUND_JITTER_SECONDS` | `1.0` | Maximum additive start jitter. |
| `KLEINANZEIGEN_OUTBOUND_QUEUE_CAPACITY` | `20` | Compatibility limit for direct legacy controller callers; durable jobs do not use this queue. |
| `KLEINANZEIGEN_OUTBOUND_QUEUE_TIMEOUT_SECONDS` | `30` | Compatibility timeout for direct legacy controller callers; durable jobs have no queue timeout. |
| `KLEINANZEIGEN_OUTBOUND_NAVIGATION_TIMEOUT_SECONDS` | `60` | Maximum duration of one navigation attempt. |
| `KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET` | `1` | One shared transient retry budget for every navigation, including durable search/detail jobs and legacy helpers. |
| `KLEINANZEIGEN_OUTBOUND_COOLDOWN_SECONDS` | `120` | Base cooldown after a block signal. |
| `KLEINANZEIGEN_OUTBOUND_MAX_COOLDOWN_SECONDS` | `3600` | Maximum local exponential cooldown; a longer valid `Retry-After` remains authoritative up to 24 hours. |

Durable job settings:

| Environment variable | Default | Purpose |
|---|---:|---|
| `KLEINANZEIGEN_JOB_DATABASE_PATH` | `data/kleinanzeigen_jobs.sqlite3` | SQLite database and adjacent exclusive process lock. The Docker image overrides this with `/app/data/kleinanzeigen_jobs.sqlite3`. |
| `KLEINANZEIGEN_JOB_WORKER_COUNT` | `1` | Fixed number of scheduler workers. |
| `KLEINANZEIGEN_JOB_LEASE_SECONDS` | `180` | Recoverable work-item lease. |
| `KLEINANZEIGEN_JOB_HEARTBEAT_SECONDS` | `30` | Lease heartbeat interval. |
| `KLEINANZEIGEN_JOB_RETRY_BUDGET` | `1` | Deprecated compatibility alias. If present, it must equal `KLEINANZEIGEN_OUTBOUND_RETRY_BUDGET`; the outbound value is authoritative. |
| `KLEINANZEIGEN_JOB_POLL_INTERVAL_SECONDS` | `0.25` | Idle scheduler polling ceiling. |
| `KLEINANZEIGEN_JOB_MIN_DISPATCH_INTERVAL_SECONDS` | `3.0` | Persistent scheduler start floor. |
| `KLEINANZEIGEN_JOB_DISPATCH_JITTER_SECONDS` | `1.0` | Maximum additive scheduler jitter. |
| `KLEINANZEIGEN_JOB_COOLDOWN_INITIAL_SECONDS` | `120` | Fallback cooldown when no controller signal is available. |
| `KLEINANZEIGEN_JOB_COOLDOWN_MAX_SECONDS` | `3600` | Maximum fallback exponential cooldown. |
| `KLEINANZEIGEN_JOB_PAGE_SETUP_TIMEOUT_SECONDS` | `30` | Context/page setup deadline per attempt. |
| `KLEINANZEIGEN_JOB_EXTRACTION_TIMEOUT_SECONDS` | `30` | DOM extraction deadline per attempt. |
| `KLEINANZEIGEN_JOB_CLEANUP_TIMEOUT_SECONDS` | `30` | Cleanup deadline per attempt. |
| `KLEINANZEIGEN_JOB_RETENTION_DAYS` | `7` | Retention for completed or cancelled jobs. |
| `KLEINANZEIGEN_JOB_MIN_FREE_DISK_BYTES` | `536870912` | Free-space reserve required before accepting a new job; `512MiB` syntax is also accepted. |

These conservative defaults protect the service from accidental concurrency
spikes. They are **not** a guaranteed "safe" or permitted request rate, and
technical throttling is neither authorization nor approval by Kleinanzeigen.
Operators remain responsible for the applicable terms, permissions, and legal
requirements.

Job submission normally returns `202 Accepted`, including while work is backed
up or the upstream circuit is cooling down. A submission returns `503` only
before acceptance when the job manager or SQLite storage is unavailable, or
when the configured disk reserve is not available. Already accepted work is not
discarded. A client or proxy
disconnect does not cancel a job; only `DELETE /jobs/{job_id}` does.

The controller evaluates the response returned by Playwright navigation and
detects HTTP `403`, HTTP `429`, `Retry-After`, and known captcha, challenge, or
block-page signals. Such a response is never interpreted as a successful empty
search. It persistently pauses all jobs without consuming their transient retry
budget. Before the stored deadline there are no further navigation starts;
afterwards exactly one half-open probe is allowed. Repeated blocks extend the
cooldown exponentially. Captchas and access restrictions are never bypassed.

`GET /outbound-status` returns a non-sensitive, process-local snapshot of the
configured limits, active controller work, durable job counts, oldest queue age,
circuit state, cooldown, and aggregate counters. It does not expose URLs,
queries, headers, cookies, or response bodies.

The controller is global **within one API process only**. An exclusive lock
prevents two local processes from operating the same job database, but separate
containers can still multiply the effective rate. Run exactly one Uvicorn worker
and one container, and mount `/app/data` when using Docker:

```sh
docker run -p 8000:8000 -v kleinanzeigen-jobs:/app/data ebay-kleinanzeigen-api
```

All agents, including the MCP server, must call this shared API process and
must not start their own browser manager. Horizontal scaling requires a later
Redis-backed distributed queue, rate limiter, and circuit state.

### Local MCP server

The local MCP integration is another interface to the same durable job API. It
must not create a second browser, scheduler, or SQLite owner. The recommended
STDIO setup lets Codex or Claude start the shared FastAPI backend automatically
on the first actual tool call. Concurrent clients and subagents discover the
same detached backend and therefore share its browser pool, outbound limits,
jobs, and persisted results.

#### One-time installation

Install the project and Chromium once:


```sh
cd "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API"
uv sync --no-editable
uv run --no-editable playwright install chromium
```

The repository already contains the project-scoped client configurations
`.codex/config.toml` and `.mcp.json`. Restart Codex or Claude Code after the
one-time installation. Trust this project in Codex; Claude Code additionally
asks once whether the checked-in MCP server is trusted. These are client
security approvals, not server-start steps. If the repository is moved, update
the absolute executable and working-directory paths in both files.

Both clients then launch this STDIO command themselves:

```sh
/Users/matthias/Documents/ChatGPT/Kleinanzeigen\ API/.venv/bin/kleinanzeigen-mcp
```

#### Python-Änderungen in die lokale MCP-Installation übernehmen

Der installierte Entry-Point lädt eine normale Wheel-Kopie aus
`.venv/lib/python3.12/site-packages`. Nach Änderungen am Python-Code muss diese
Kopie aus dem Projektordner aktualisiert werden:

```sh
# Im Projektstamm ausführen
uv pip install --python .venv/bin/python --offline --no-deps --reinstall .
```

Dieser geprüfte Befehl verwendet Build-Isolation und den vorhandenen lokalen
Build-Cache. Er aktualisiert nur das Projektpaket in der Projekt-Venv; die
Runtime-Venv benötigt dafür kein separat installiertes `setuptools`.
Die editierbare Installation funktioniert in dieser Umgebung nicht zuverlässig:
Ihre erzeugte `__editable__*.pth` trug das macOS-Dateiflag `UF_HIDDEN`, und der
verwendete CPython 3.12.13 überspringt solche Startupdateien. Es wurden keine
globalen Python- oder Dateiflag-Regeln geändert.

Tests oder `python -m` aus dem Projektordner können den aktuellen Quellcode
laden, während der ausführbare MCP-Entry-Point noch eine ältere Wheel-Kopie
verwendet. Deshalb nach einer Neuinstallation zusätzlich aus einem fremden
Arbeitsverzeichnis prüfen: Importpfad und installierten Code mit dem Source
vergleichen und den echten `.venv/bin/kleinanzeigen-mcp`-Entry-Point testen.
Am 4. November 2026 wurden aus `/private/tmp` mit deaktiviertem Autostart
Initialisierung, alle 18 Werkzeuge und der rein lokale Aufruf `list_accounts`
über STDIO sowie HTTP erfolgreich geprüft; der installierte Launcher entsprach
dem Source einschließlich des Socket-Neustartfixes.

Bereits laufende MCP-Adapter und das Backend behalten ihre geladenen Module.
Die Neuinstallation ersetzt daher keinen anschließenden geordneten Neustart
dieser Prozesse. Vor einem Backendwechsel müssen laufende Versandversuche
abgeschlossen beziehungsweise eindeutig als unklar gespeichert sein; unklare
Aufträge werden nicht automatisch erneut gesendet.

#### Startverhalten

The MCP handshake and tool discovery complete without waiting for Chromium.
The first actual tool call reuses a healthy compatible backend when one exists.
If none exists, it reserves the configured loopback port, or a free `127.0.0.1`
port when no explicit address is set, acquires a cross-process
startup lock, starts exactly one detached Uvicorn process with one worker, and
waits for backend readiness up to the configured startup timeout.
Uvicorn output is written to a private rotating log instead of MCP `stdout`.
Closing the client stops only its STDIO adapter: the shared backend and accepted
jobs continue running for other clients and later sessions. If that backend
later exits, an existing STDIO session safely resolves or starts it once more
and retries the idempotent job operation exactly once.

Runtime discovery data, the startup lock, and backend log are stored under
`data/.mcp-runtime/` by default. They contain no MCP results. The launcher never
signals, replaces, or adopts an incompatible process which already owns an
explicit address.

The relevant launcher settings are:

| Environment variable | Default | Purpose |
|---|---:|---|
| `KLEINANZEIGEN_MCP_AUTOSTART` | `true` | Start or discover the shared local backend for STDIO. Set to `false` for a separately managed API. |
| `KLEINANZEIGEN_MCP_STARTUP_TIMEOUT_SECONDS` | `90` | Maximum wait for the startup lock and backend readiness. |
| `KLEINANZEIGEN_MCP_HEALTH_TIMEOUT_SECONDS` | `2` | Timeout for an individual local compatibility/readiness probe. |
| `KLEINANZEIGEN_MCP_RUNTIME_DIR` | `data/.mcp-runtime` | Absolute override for state, lock, and the default log. |
| `KLEINANZEIGEN_MCP_BACKEND_LOG_PATH` | `backend.log` in the runtime directory | Absolute path or runtime-directory-relative backend log path. |
| `KLEINANZEIGEN_MCP_BACKEND_LOG_MAX_BYTES` | `1048576` | Rotate the backend log at the next start after this size is reached. |
| `KLEINANZEIGEN_MCP_BACKEND_DIRECTORY` | detected project root | Absolute working directory containing `main.py`. |
| `KLEINANZEIGEN_API_BASE_URL` | dynamically discovered | Reuse or start one explicit compatible loopback API; with autostart disabled, the fallback is `http://127.0.0.1:8000`. |

#### Dauerhafte Safari-Konfiguration

Die Projektkonfigurationen `.mcp.json` und `.codex/config.toml` verwenden den
Safari-Adapter und starten das Backend bei Bedarf auf `http://127.0.0.1:50489`.
Passe die absoluten Pfade in der Startkonfiguration an deine Installation an.
Verwenden mehrere Clients oder ein Personal-Plugin denselben Connector, brauchen
sie dieselbe API-Adresse und denselben Archivpfad. Das Arbeitsverzeichnis ist
der Projektstamm. Die Suchjobs liegen unter `data/kleinanzeigen_jobs.sqlite3`,
die Laufzeitdatei und die Backend-Logs unter `data/.mcp-runtime/`.

Der Connector läuft im **Normalbetrieb** mit
`KLEINANZEIGEN_MESSAGING_TEST_MODE=false`. Neue beauftragte Nachrichten können
über die verbundenen Accounts an reguläre Gesprächspartner gesendet werden.
Für einen Versandtest konfigurierst du die beiden eigenen Accounts, ihre
verifizierten Nutzer-IDs und die eigene Testanzeige lokal beim Backendstart.
Die Einstellungen stehen unter [Nachrichtentests ausschließlich eigener Accounts](#nachrichtentests-ausschließlich-eigener-accounts).

Der erste Werkzeugaufruf verwendet das laufende Backend oder startet es nach
einem Mac-Neustart. Eine geordnete Übergabe von einem manuell gestarteten Backend
an den Autostart wurde über STDIO mit `list_accounts` geprüft.

Am gleichen Port und Datenpfad verwendet Safari den bestehenden privaten
Verbindungsschlüssel weiter; ein normaler Backendneustart erfordert keinen
erneuten Import der Kopplungsdatei. Nach einem Safari-Neustart können Tab-IDs
wechseln: `connect_account` versucht im bereits gebundenen Profil genau einen
passenden Konto- oder Nachrichten-Tab wiederzufinden und dessen Nutzer-ID frisch
zu prüfen. Fehlt ein eindeutiger Tab, bleibt die manuelle Zuordnung über
„Diesen Tab verbinden“ erforderlich. Archivzugriff benötigt keinen offenen Chat.

Das Fenster **Kleinanzeigen Safari** dient nur zur Einrichtung und zum Import der
Kopplungsdatei. Es darf nach der Kopplung geschlossen werden; beim Schließen des
letzten Fensters beendet sich die Host-App. Die aktivierte Erweiterung arbeitet
unabhängig davon in Safari weiter. Für Live-Zugriffe müssen Safari und die
zugeordneten Kontotabs verfügbar sein. Ein dauerhaft geöffnetes Host-Fenster oder
laufendes Dockprogramm ist nicht erforderlich; ein angehefteter Dockeintrag ist
nur eine Verknüpfung. Archivdaten und exportierte HTML-Dateien bleiben ohne
Safari-Anmeldung lesbar.

Die eigenen App- und Erweiterungsicons zeigen eine Sprechblase mit Archivablage.
Vektorvorlagen, ICNS und monochrome Toolbar-Varianten liegen in
`safari-extension/assets/`. Der eng begrenzte Ressourcen-Updateweg ist in der
[Safari-Dokumentation](safari-extension/README.md#icons-und-host-fenster) beschrieben.

Der laufende HTTP-MCP stellt 18 Werkzeuge bereit, einschließlich der bisherigen
acht. Zeigt ein bestehender Client nach einem Pluginupdate weiterhin nur acht,
muss dessen MCP-Verbindung beziehungsweise Werkzeugkatalog neu geladen werden;
dafür kein zweites Backend starten. Die gespeicherte Konfiguration und die
Offline-Prüfungen ersetzen noch keine bestandene Nachrichtenabnahme.

#### Optional manually managed HTTP transport

For direct HTTP MCP use, start the shared API yourself, bind only to loopback,
run exactly one worker, and do not use `--reload`:

```sh
cd "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API"
uv run --no-editable uvicorn main:app \
  --host 127.0.0.1 --port 8000 --workers 1
curl -fsS http://127.0.0.1:8000/health
```

Streamable HTTP is then available at `http://127.0.0.1:8000/mcp`. In addition
to the SDK's Host-header protection, the MCP route rejects non-loopback network
peers even if the surrounding API was accidentally bound more broadly.

Configure either HTTP or STDIO under the MCP name `kleinanzeigen`, not both at
the same time. Both expose the identical tool catalog and use the same
FastAPI job scheduler.

#### MCP tools

The existing search and detail workflow exposes these eight tools. The messaging
tools are described separately below:

| Tool | Purpose |
|---|---|
| `search_listings` | Create a durable search job from structured filters or one validated Kleinanzeigen search URL. |
| `continue_search` | Submit the current continuation token and release/schedule the next tranche of at most 200 unique search results. |
| `merge_search_results` | Merge persisted results from ordered, unique search job IDs without browser access. The first source wins duplicates; output includes `source_job_ids`, stable snapshot pagination, and source states. |
| `get_listing_details` | Create a durable detail job from an explicit list of selected listing IDs. |
| `get_job_status` | Read progress, continuation/resume data, cooldown, and errors without contacting Kleinanzeigen. |
| `get_job_results` | Read cursor-paginated persisted results without contacting Kleinanzeigen. |
| `resume_job` | Retry exactly the current persisted `needs_attention` cycle with its resume token. |
| `cancel_job` | Durably cancel accepted work and drain any currently running browser operation. |

Search jobs release at most 200 unique listings per tranche. When a job reaches
`awaiting_continuation`, pass the unchanged token and the exact `next_cursor`
from the preceding tranche to `continue_search`. Token and cursor are bound to
that tranche; retrying the same pair is idempotent and cannot release another
tranche.

Detail jobs have no application-level item-count limit. Explicit
`listing_ids` are normalized and deduplicated, but every HTTP request is still
subject to the global 64 KiB request-body limit. A payload larger than 65,536
bytes returns `413`; split a very large ID list into self-selected batches.

`merge_search_results` accepts up to 32 ordered, unique `search_job_ids`, an
optional snapshot cursor, and `limit` from 1 through 200 (default 200). A single
snapshot may contain at most 10,000 visible source items, which bounds local
memory and CPU work; split unusually large merges into smaller groups. The tool
preserves source order followed by each result's sequence, deduplicates by
envelope `listing_id`, and returns `has_more`, `next_cursor`, watermarks, and
source states. It never starts browser work.

#### Codex

Use STDIO so Codex starts the adapter and, when necessary, the shared backend.
After the project is trusted, the checked-in `.codex/config.toml` contains:

```toml
[mcp_servers.kleinanzeigen]
command = "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API/.venv/bin/kleinanzeigen-mcp"
cwd = "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API"
startup_timeout_sec = 120
tool_timeout_sec = 900
```

To make the server available outside this repository too, create the base entry
at user scope with the CLI and then add the timeout and working directory to
`~/.codex/config.toml`:

```sh
codex mcp add kleinanzeigen -- \
  "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API/.venv/bin/kleinanzeigen-mcp"
codex mcp list
```

Codex stores timeout values in seconds. Restart the Codex client after changing
the configuration, then use `/mcp` to inspect the connection and available tools.
For a separately managed HTTP backend, replace the STDIO entry with:

```toml
[mcp_servers.kleinanzeigen]
url = "http://127.0.0.1:8000/mcp"
tool_timeout_sec = 900
```

#### Claude Code

The checked-in `.mcp.json` registers the auto-starting STDIO command for this
project and sets its per-server tool-call timeout to 900,000 milliseconds.
Confirm it once when Claude Code presents its project-server security prompt:

```sh
claude mcp list
```

To make the server available outside this repository too, register the same
command at user scope:

```sh
claude mcp add-json --scope user kleinanzeigen \
  '{"type":"stdio","command":"/Users/matthias/Documents/ChatGPT/Kleinanzeigen API/.venv/bin/kleinanzeigen-mcp","args":[],"timeout":900000}'
```

Use `/mcp` inside Claude Code to inspect the server. For a separately managed
HTTP backend, remove the STDIO entry and add the HTTP endpoint instead:

```sh
claude mcp remove kleinanzeigen --scope project
# Run this too only if the optional user-scoped entry was added:
claude mcp remove kleinanzeigen --scope user
claude mcp add --transport http --scope project kleinanzeigen \
  http://127.0.0.1:8000/mcp
```

#### Claude Desktop (classic STDIO configuration)

On macOS, add the following to
`~/Library/Application Support/Claude/claude_desktop_config.json`, preserving
any other configured servers, and restart Claude Desktop:

```json
{
  "mcpServers": {
    "kleinanzeigen": {
      "type": "stdio",
      "command": "/Users/matthias/Documents/ChatGPT/Kleinanzeigen API/.venv/bin/kleinanzeigen-mcp",
      "args": []
    }
  }
}
```

Restart Claude Desktop after saving. It launches the STDIO adapter itself; no
terminal with Uvicorn has to remain open.

### Nachrichten, Bilder und mehrere Accounts

Die Nachrichtenfunktionen ergänzen die bestehende Suche im selben lokalen
Backend. Sie unterstützen getrennte angemeldete Accounts, Gesprächsübersichten,
Chatverläufe, Erstnachrichten über eine Anzeige und Antworten mit Text und Bildern.
Es gibt keine automatischen Antworten und keine regelmäßige Vollsynchronisierung.

Die Offline-Verträge und Versandzustände sind getestet. Die private
Kleinanzeigen-Oberfläche muss vor dem produktiven Einsatz mit den eigenen
angemeldeten Testaccounts abgeglichen werden. Unbekannte Formulare oder nicht
eindeutig erkennbare Account-/Empfänger-IDs ergeben `ui_unsupported`; die
Erweiterung sendet dann nicht auf Verdacht.

#### Accounts anmelden

1. Mit `connect_account(account_id="privat", label="Privat")` einen eigenen
   lokalen Accountnamen anlegen. Für einen zweiten Account einen anderen Namen
   verwenden, beispielsweise `zweiter-account`.
2. Im geöffneten Browserfenster selbst bei Kleinanzeigen anmelden und eventuell
   angeforderte SMS-, E-Mail- oder App-Bestätigungen durchführen. Zugangsdaten
   werden nicht als Toolparameter übermittelt.
3. Mit `get_message_operation` den Anmeldeauftrag und mit `list_accounts` die
   gebundene Kleinanzeigen-Nutzer-ID prüfen. Bei abgelaufener Sitzung meldet das
   Backend `reauth_required`; denselben Account erneut über `connect_account`
   anmelden.

Jede Nachrichtenoperation nennt den Account ausdrücklich. Ein bereits gebundenes
Profil darf bei einer erneuten Anmeldung nicht unbemerkt zu einem anderen
Kleinanzeigen-Nutzer wechseln. Die Browserprofile sind vom anonymen Suchbrowser
getrennt. Die manuelle Anmeldung benötigt eine verfügbare grafische Desktop-Sitzung.
`disconnect_account` trennt die lokale Sitzung; das Nachrichtenarchiv bleibt erhalten.

Der Nachrichtenbereich verwendet standardmäßig den installierten Google Chrome
in einem eigenen Profil pro Account. Anmeldung und spätere Nachrichtenzugriffe
laufen im sichtbaren Browser mit dessen unveränderter Browserkennung. Der
öffentliche Suchbrowser bleibt davon unabhängig. Chrome muss lokal installiert
sein; ein fehlgeschlagener Start wechselt nicht automatisch den Browser.
`KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL=chromium` wählt ausdrücklich das von
Playwright installierte Chromium; der Standardwert ist `chrome`. Ein Wechsel zu
einer älteren Browserversion mit demselben Profil wird nicht empfohlen.
Browserprofile oder Cookies aus Safari beziehungsweise persönlichen Chrome-Profilen
werden nicht übernommen.

Mit `KLEINANZEIGEN_MESSAGING_BROWSER_CHANNEL=safari` verwendet der
Nachrichtenbereich die lokale Safari-Erweiterung. Diese arbeitet in den normalen,
manuell angemeldeten Safari-Profilen. Für jeden Account ist ein eigenes Profil
erforderlich. Safari selbst liefert der nativen Erweiterung die Profilkennung;
die API bindet sie erst nach Prüfung der angemeldeten Kleinanzeigen-Nutzer-ID an
`account_id`. Cookies und Passwörter verbleiben in Safari. Der öffentliche
Suchbrowser wird durch diese Einstellung nicht verändert.

Build und Einrichtung stehen in [safari-extension/README.md](safari-extension/README.md).
Beim Start des Safari-Backends entsteht die private Datei
`data/messaging/safari-pairing.json`. Sie wird einmal in die signierte lokale
Safari-Host-App importiert und enthält einen lokalen Verbindungsschlüssel, keine
Kleinanzeigen-Sitzungsdaten. Nicht weitergeben oder einchecken. Der native Host
spricht ausschließlich die konfigurierte Loopback-Route
`/messaging/safari-bridge/exchange` an. Normale Webseiten dürfen diese Route
auch mit gleicher Origin nicht verwenden; der JSON-Grenzwert bleibt 64 KiB.
Bilddaten werden in begrenzten Abschnitten übertragen.

Bei manuellem API-Start muss `KLEINANZEIGEN_MESSAGING_SAFARI_ENDPOINT` zur
tatsächlichen Adresse passen, zum Beispiel
`http://127.0.0.1:8000/messaging/safari-bridge/exchange`. Der MCP-Autostart setzt
die Adresse aus dem tatsächlich reservierten lokalen Port. Bei einem Portwechsel
muss die neu erzeugte Verbindungsdatei erneut importiert werden; der private
Verbindungsschlüssel bleibt bei einem normalen Neustart erhalten.

`connect_account` stellt bei vorhandener verifizierter Profilbindung zunächst
deren Tab-Zuordnung lesend wieder her. Es akzeptiert dafür genau eine bekannte
Konto- oder Nachrichtenseite und dieselbe frisch geprüfte Nutzer-ID. Ohne diesen
eindeutigen Nachweis wartet der Auftrag auf die manuelle Zuordnung: Im passenden
Safari-Profil auf Kleinanzeigen anmelden, die Erweiterung öffnen und den
wartenden Account zuordnen. Safari-Fenster bleiben beim Trennen oder Beenden
des Backends geöffnet; `disconnect_account` widerruft nur die API-Zuordnung.
Die Erweiterung fragt lediglich nach lokalen API-Aufträgen und synchronisiert
das Postfach nicht regelmäßig im Hintergrund. Nicht eindeutig erkannte
Chat-Strukturen oder Bildfunktionen liefern eine ausdrückliche Einschränkung.
Ein ausgelieferter Versandauftrag wird auch nach Verbindungsabbruch niemals
automatisch erneut zugestellt.

Ein Bildversand wird erst nach einer separaten, frischen Chatnavigation
bestätigt. Die echte Server-Nachrichten-ID, der Empfänger und das dort eindeutig
zugeordnete Bild müssen zum Versandbeleg passen. Der Hash der von Kleinanzeigen
erzeugten JPEG-Vorschau wird getrennt vom Hash des ursprünglichen Uploads
gespeichert. Vorschauhash und gegebenenfalls ein begrenzter, exakt definierter
Pixelbeleg werden vor dem einzigen Sendeklick dauerhaft bestätigt. Der spätere
Beleg muss entweder dieselben Bildbytes oder exakt dasselbe Pixelraster enthalten;
eine bloß ähnliche Darstellung genügt nicht. Uploadoriginale und tatsächlich
abgerufene Serveranhänge bleiben im Archiv getrennt. Unklare Bildzuordnungen
bleiben ausdrücklich unverfügbar; ein
fehlender Beleg führt niemals zu einem zweiten automatischen Sendeklick.

Diese Erweiterung ist für macOS vorgesehen und verwendet in Manifest V2 eine
persistente Hintergrundseite. So kann sie lokale API-Aufträge auch bei
geschlossenem Erweiterungsmenü empfangen; die nichtpersistente Variante wurde
von Safari zwischen Aufträgen entladen. Apple unterstützt
[persistenten Hintergrundinhalt nur unter macOS](https://developer.apple.com/documentation/webkit/wkwebextension/haspersistentbackgroundcontent)
und beschreibt das
[Entladen nichtpersistenter Hintergrundseiten](https://developer.apple.com/documentation/safariservices/optimizing-your-web-extension-for-safari).
Im Leerlauf wird ausschließlich die lokale Queue alle fünf Sekunden geprüft,
bei wartender Anmeldung oder unmittelbar nach einem Auftrag jede Sekunde.
Content-Skripte benötigen keinen Heartbeat. Ohne API-Auftrag erfolgen keine
Kleinanzeigen-Abfragen; dies ist kein periodischer Postfachabgleich.

Private und gewerbliche Accounts können unterschiedliche Oberflächen verwenden:
privat `/m-meine-anzeigen.html` und `/m-nachrichten.html`, im beobachteten
Business-Portal `/portal/anzeigen` und `/portal/nachrichten`. Die Erweiterung
akzeptiert nur diesen ausdrücklich geprüften Wechsel der eigenen Anzeigen-Seite
und speichert den erkannten Pfad zusammen mit der verifizierten Nutzer-ID im
jeweiligen Safari-Profil. Das ist keine Freigabe beliebiger Weiterleitungen.
Die Safari-Abnahme mit zwei eigenen Testaccounts umfasste die Profilbindung,
das Wiederfinden von Kontotabs, die Postfachpaginierung und den Austausch von
Text und Bildern. Beim Erstkontakt lieferte die API eine Gesprächs-ID. Zum
Prüfen der Bildantwort wurden Vorschau- und Pixelbelege vor dem Sendeklick
gespeichert und mit dem später abgerufenen Bild verglichen. Nach einem
Backendneustart ließen sich die archivierten Nachrichten und Bilder über
STDIO und HTTP lesen.

`GET /messaging/safari-bridge/status` liefert für die lokale Fehlerdiagnose den
letzten bestätigten Poll der nativen Erweiterung sowie Anzahlen und Zustände
wartender Aufträge. Die Route führt keine Browseraktion aus und gibt weder
Verbindungsschlüssel noch Nutzer-IDs, URLs oder Nachrichteninhalte zurück.

Eine während der manuellen Anmeldung erkannte Zugriffssperre wird als `blocked`
mit dem konkreten Sperrfehler gemeldet. Sie wird nicht als falsches Passwort oder
gewöhnlich abgelaufene Sitzung behandelt; die Anmeldung wird nicht automatisch
wiederholt.
Nach erfolgreicher Anmeldung bleibt das Accountfenster geöffnet. Wird während
der manuellen Anmeldung eine Sperre erkannt, bleibt die geladene Seite zur
Prüfung sichtbar, während die Automatik anhält. Ein erneuter ausdrücklicher
`connect_account`-Aufruf prüft die vorhandene Anmeldeseite nur lesend und setzt
keine Zugriffssperre zurück. `disconnect_account` oder das Beenden des Backends
schließt die zugehörigen Browserfenster.
Versteckte CAPTCHA-Elemente allein gelten nicht als aktive Sperrseite. Bei einer
erkannten Sperre nennt `block_indicator` einen festen Erkennungsgrund, ohne
Seiteninhalte, Anmeldedaten oder vollständige URLs zu speichern.

Der Ressourcenfilter unterscheidet das Hauptfenster von eingebetteten
Anmeldekomponenten. Beobachtete Prüfressourcen und sichere Kleinanzeigen-WebSockets
sind eng begrenzt zugelassen; private Netzwerkziele und fremde Hauptnavigationen
bleiben gesperrt. Service Worker bleiben deaktiviert, damit die Request-Prüfung
auch für Hintergrundanfragen greift.

#### Nachrichtenwerkzeuge

| MCP-Werkzeug | Verwendung |
|---|---|
| `list_accounts` | Accountstatus, gebundene Nutzer-IDs und serverseitige Testfreigaben lesen. |
| `connect_account` | Eine manuelle Anmeldung oder erneute Anmeldung im Browser starten. |
| `disconnect_account` | Lokale Accountsitzung trennen. |
| `list_conversations` | Gesprächsübersichten seitenweise abrufen, optional nur ungelesene Gespräche. |
| `get_conversation` | Nachrichten und Bildreferenzen eines Gesprächs live oder aus dem Archiv lesen. |
| `get_message_image` | Ein archiviertes Bild als darstellbaren Bildinhalt abrufen. |
| `upload_message_images` | Ausdrücklich angegebene lokale Bilddateien bereitstellen und Anhangs-IDs erhalten. |
| `start_conversation` | Eine beauftragte erste Nachricht an eine konkrete Anzeige senden. |
| `send_message` | Eine beauftragte Nachricht in einem bestehenden Gespräch senden. |
| `get_message_operation` | Den Zustand eines Anmelde- oder Versandauftrags prüfen. |

Für Bilder zunächst `upload_message_images` mit Account und absoluten lokalen
Dateipfaden aufrufen; danach die zurückgegebenen `attachment_ids` an den
Versandauftrag übergeben. Lokale Schutzgrenzen sind 20 MiB pro Datei, 40 Millionen
Pixel, maximal 20 Anhänge und 20.000 Textzeichen. PNG-, JPEG-, GIF- und WebP-Inhalte
werden auf tatsächliches Bildformat geprüft. Das sind **lokale Schutzgrenzen**,
keine Zusicherung der Kleinanzeigen-Grenzen. Die angemeldete Oberfläche muss das
Format und den Bildversand am konkreten Ziel zusätzlich unterstützen. Bei einer
nicht erkannten oder nicht angebotenen Funktion wird ein entsprechender Fehler
zurückgegeben, nicht stillschweigend ein Bild weggelassen.

Wenn die Anzeige Bilder erst im bestehenden Chat zulässt, kann der Erstkontakt
aus einem Textschritt und anschließenden Bildschritten bestehen. Ein erfolgreicher
Text mit fehlgeschlagenem Bild wird als Teilerfolg gemeldet. `operation_id` und
Schrittzustände deshalb aufbewahren und mit `get_message_operation` prüfen.

Jeder Versand benötigt einen stabilen `idempotency_key`. Derselbe Schlüssel mit
demselben Account, Ziel und Inhalt bezeichnet denselben Auftrag, auch nach einem
Neustart. Ein veränderter Inhalt mit demselben Schlüssel wird abgewiesen. Bei
`outcome_unknown` kann Kleinanzeigen bereits gesendet haben: Den vorhandenen
Auftrag und das Gespräch prüfen, **keinen neuen Versandauftrag erzeugen**. Der
Service wiederholt einen möglicherweise ausgeführten Sendeschritt nicht
automatisch. Wiederholung eines bestätigten Textschritts ist auch bei einem
fehlgeschlagenen Folgebild ausgeschlossen.

#### Lokales Archiv und REST-Zugriff

Standardmäßig liegen die privaten Daten im Projekt unter `data/messaging/`:
`messaging.sqlite3`, getrennte Browserprofile und Bilddateien.
`KLEINANZEIGEN_MESSAGING_DIR` kann diesen Datenordner überschreiben. Der Ordner
wird nicht in Git aufgenommen und mit privaten Dateirechten angelegt. Sitzungsdaten
und Nachrichteninhalte gehören nicht in Logs oder Projektberichte.

Das Archiv bewahrt tatsächlich abgerufene Nachrichten und zugehörige Bilder
dauerhaft auf, ohne automatische Löschfrist. Die Postfachauflistung allein lädt
weiterhin keine vollständigen Gesprächsverläufe. Ein ausdrücklich gestarteter
Accountimport kann alle zu diesem Zeitpunkt erreichbaren Gespräche und deren
Nachrichtenseiten nacheinander sichern.
Live- und Archivantworten kennzeichnen ihre Quelle; `source="archive"` liest
bereits gespeicherte Daten ohne Kleinanzeigen-Abruf. Eine später entfernte Anzeige
oder Nachricht löscht das lokale Archiv nicht. Das Öffnen eines Live-Chats kann
ihn bei Kleinanzeigen als gelesen markieren.

Beim ersten Live-Seitenabruf eines Gesprächs über Safari werden zusätzlich
zuordenbare Anbieterinformationen aus der verknüpften Anzeige, deren
eingeklapptem Impressum und dem öffentlichen Profil des Gegenübers gelesen.
`provider_context` enthält gespeicherte Momentaufnahmen mit Quelle, Abrufzeit
und verifizierter Gegenüber-ID sowie den letzten Prüfstatus je Quelle.
Veränderte Angaben erzeugen weitere Momentaufnahmen; fehlende oder später
gelöschte Seiten entfernen keine vorhandenen Angaben. Gehört die Anzeige dem
eigenen Account, wird dessen Impressum nicht dem Gesprächspartner zugeschrieben.
Nicht erreichbare oder nicht eindeutig zuordenbare Informationen werden
ausdrücklich gekennzeichnet. Plattformereignisse werden als `system_events`
getrennt von den eigentlichen Nachrichten gesichert; bei nicht unterstützten
Inhalten wird keine vollständige Erfassung behauptet.

Die ergänzenden öffentlichen Anzeigen- und Profilseiten werden ohne Cookies
abgerufen (`credentials: "omit"`). PRO-Anbieter mit einer eigenen Profiladresse
werden über deren exakte kanonische URL und zwei übereinstimmende numerische
Nutzerkennungen in bekannten Profilkomponenten der Gesprächspartei zugeordnet.
Die gefundenen PRO-Angaben und die Daten des zusätzlichen numerischen
Nutzerprofils werden gemeinsam in einem begrenzten Profil-Snapshot gesichert;
jeder Textabschnitt nennt seine eigene Quell-URL. Größenbegrenzungen und noch
nicht unterstützte Profilstrukturen werden als gekürzte beziehungsweise
unvollständige Erfassung kenntlich gemacht.

Ein einmaliger, fortsetzbarer Import wird bewusst über das lokale Hilfsskript
gestartet. Es verwendet ausschließlich lesende REST-Aufrufe; die bestehenden
Zugriffslimits und die Serialisierung je Account bleiben wirksam. Bei
Anmeldeproblemen oder einer Zugriffssperre hält der Lauf an. Der private Bericht
enthält IDs und Erfassungsstände, keine Nachrichtentexte. Derselbe Bericht setzt
einen unterbrochenen Lauf fort; für einen neuen Stand einen neuen Bericht wählen:

```bash
.venv/bin/python script/archive_messaging_account.py \
  --account-id privat \
  --report data/messaging/imports/private-account.json
```

Für langfristigen Zugriff unabhängig von API, Safari oder Kleinanzeigen kann
ein eigenständiger Export erstellt werden:

```bash
.venv/bin/python script/export_messaging_archive.py --account-id privat
```

Der ausgegebene neue Ordner unter `data/messaging/exports/` enthält `index.html`,
`archive.json`, einzelne Gesprächsseiten, Bilddateien und ein Manifest mit
SHA-256-Prüfsummen. `index.html` lässt sich ohne Anmeldung und Internetverbindung
in einem Browser öffnen. Der Export enthält keine Cookies, Browserprofile oder
Zugangsdaten. SQLite bleibt das Arbeitsarchiv; HTML und JSON sind transportable,
offene Formate. Der vollständige Exportordner gehört zusätzlich in ein
unabhängiges Backup: Eine weitere Kopie auf demselben Datenträger schützt nicht
vor dessen Ausfall. Vor dem ersten Abruf gelöschte Inhalte können nicht
nachträglich wiederhergestellt werden.

Die REST-Schnittstelle beginnt mit `GET /messaging/accounts`. Alle weiteren
Routen liegen unter `/messaging/accounts/{account_id}`:

| Route | Methode und Inhalt |
|---|---|
| `/connect`, `/disconnect` | `POST`; Anmeldung mit optionalem `label`, beziehungsweise Sitzung trennen. |
| `/conversations` | `GET` mit `source`, `cursor`, `limit` und optional `unread_only`. |
| `/conversations/{conversation_id}` | `GET` mit `source`, `cursor` und `limit`. |
| `/images/{image_id}` | `GET`; archiviertes Bild als Binärantwort. |
| `/attachments` | `POST`; rohe Bildbytes, Bild-`Content-Type` und optional `X-Filename`. |
| `/start-conversation` | `POST` mit `listing_id`, `text`, `attachment_ids`, `idempotency_key`. |
| `/send-message` | `POST` mit `conversation_id`, `text`, `attachment_ids`, `idempotency_key`. |
| `/operations/{operation_id}` | `GET`; aktueller persistierter Auftragszustand. |

Die privaten Routen sind nur lokal erreichbar und weisen fremde Browser-Origins
ab. Das bestehende 64-KiB-Limit für JSON bleibt bestehen; ausschließlich die
Bild-Uploadroute hat ein größeres, begrenztes Binärlimit.

#### Nachrichtentests ausschließlich eigener Accounts

Der normale Offline-Testlauf und `pytest --run-live` versenden keine Nachrichten.
Schreibtests benötigen **zusätzlich zum serverseitigen Testmodus** den separaten
Schalter `--run-live-messaging`. Vorher zwei eigene Accounts manuell anmelden und
eine bestehende eigene Anzeige des zweiten Accounts auswählen. Die tatsächlichen
Kleinanzeigen-Nutzer-IDs aus `list_accounts` übernehmen, keine E-Mail-Adressen oder
Anzeigen anderer Nutzer als Freigaben verwenden.

Folgende Einstellungen werden beim Start des Backends gelesen:

| Einstellung | Bedeutung |
|---|---|
| `KLEINANZEIGEN_MESSAGING_TEST_MODE=true` | Alle Nachrichtenversände serverseitig auf die Testfreigaben begrenzen. |
| `KLEINANZEIGEN_MESSAGING_TEST_ACCOUNT_IDS` | Kommagetrennte lokale IDs der beiden eigenen Accounts. |
| `KLEINANZEIGEN_MESSAGING_TEST_USER_IDS` | Kommagetrennte verifizierte Kleinanzeigen-Nutzer-IDs dieser Accounts. |
| `KLEINANZEIGEN_MESSAGING_TEST_LISTING_IDS` | Explizite Anzeigen-IDs eigener Testanzeigen. |

Im Testmodus erlauben fehlende oder unpassende Freigaben keinen Versand. Die
Backend-Einstellungen gelten auch für Aufträge, die nicht vom Testskript kommen.
Ein bereits laufendes Backend muss mit der passenden Konfiguration neu gestartet
werden; Umgebungsvariablen nur im Testterminal ändern nicht seinen Testmodus.

Der Testprozess verwendet `KLEINANZEIGEN_LIVE_BASE_URL` sowie
`KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_A`,
`KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_B`,
`KLEINANZEIGEN_LIVE_MESSAGING_LISTING_ID` und die verpflichtende
`KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID`. Die Laufkennung muss bewusst gewählt
werden: 1–64 ASCII-Buchstaben, Ziffern, Unterstriche oder Bindestriche, beginnend
mit einem Buchstaben oder einer Ziffer. Für denselben beauftragten Testlauf
einschließlich Wiederholungen bleibt sie unverändert; sie bestimmt die
Idempotenzschlüssel der Versandaufträge. Der Test erzeugt keine zufällige neue
Laufkennung. Er überprüft die Serverfreigaben und den
Eigentümer der Anzeige vor dem ersten Versand. Erst dann versendet er einen
gekennzeichneten Testtext und ein selbst erzeugtes harmloses Bild zwischen den
eigenen Accounts. Es wird keine Anzeige erstellt.

```sh
export KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID='eigener-test-01'
uv run pytest tests/test_messaging_live.py --run-live-messaging -m live_messaging
```

Bei `outcome_unknown` ausschließlich den bestehenden Auftragsstatus und das
eigene Gespräch lesend prüfen. Den Test nicht blind wiederholen und keine neue
Laufkennung vergeben, um einen unklaren Ausgang zu umgehen. Eine neue Kennung
gehört nur zu einem bewusst neu beauftragten Testlauf.

Der Test prüft Erstkontakt, Bildempfang, Antwort, Bildabruf und Archivzugriff.
Wiederanlauf und unklare Versandausgänge werden mit Offline-Tests geprüft; ein
Backendneustart während echter Versände wird nicht vom Live-Harness ausgelöst.
Die tatsächliche Kleinanzeigen-Oberfläche und ihre Bildfunktionen benötigen eine
Abnahme nach der Anmeldung. Ein bestandener Offline-Test ersetzt diese Abnahme
nicht. Weitere Hinweise stehen in `tests/README.md`.

#### Large results and client limits

Prefer cursor pagination over returning every result at once. The tools
`search_listings`, `continue_search`, `merge_search_results`,
`get_listing_details`, and `get_job_results` advertise
`_meta["anthropic/maxResultSizeChars"] = 500000`, allowing Claude Code to handle
text results up to the supported 500,000-character ceiling. Claude Code can also
raise its general MCP output budget when needed:

```sh
MAX_MCP_OUTPUT_TOKENS=50000 claude
```

The metadata limit and `MAX_MCP_OUTPUT_TOKENS` are client output controls; they
do not change the API's 200-item result-page limit or 64 KiB request-body limit.

#### Tests and diagnosis

Run the MCP-focused offline tests, then the complete offline suite:

```sh
./.venv/bin/pytest -q -p no:cacheprovider tests/test_mcp_*.py
./.venv/bin/pytest -q -p no:cacheprovider
```

Useful checks:

```sh
codex mcp list
claude mcp list
jq . data/.mcp-runtime/backend.json
tail -n 100 data/.mcp-runtime/backend.log
```

- Auto-start failure: inspect `data/.mcp-runtime/backend.log`; do not start a
  second Uvicorn process against the same SQLite database.
- Incompatible explicit address: remove `KLEINANZEIGEN_API_BASE_URL` to use a
  dynamically reserved port, or point it at the intended jobs API. The launcher
  never terminates the process currently owning that address.
- HTTP `503`: the browser or durable job manager is not ready; inspect the API
  log. For a manually managed HTTP backend, also inspect
  `GET /outbound-status`.
- `Another scheduler already owns this job database`: stop the unintended
  second Uvicorn process; do not add workers or use `--reload`.
- HTTP `413`: the MCP/REST request exceeded 64 KiB; split explicit detail IDs.
- Missing tools: confirm the exact `/mcp` URL, restart the client, and inspect
  `codex mcp list`, `claude mcp list`, or `/mcp` in the client.
- Tool timeout: verify `tool_timeout_sec = 900` for Codex or
  `"timeout": 900000` on Claude Code's `kleinanzeigen` entry. The Claude
  per-server value overrides the `MCP_TOOL_TIMEOUT` environment fallback.
  Accepted durable jobs continue even if an MCP client disconnects; reconnect
  and read their status/results.

### API Endpoints

#### Canonical durable job workflow

Create a search from structured filters. Search jobs automatically traverse all
required result pages up to 200 unique listings and persist every extracted
search-card field from those pages:

```sh
curl -i -X POST http://localhost:8000/jobs/search \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: agent-a-fahrrad-2026-08-21' \
  -d '{"query":"Fahrrad","location":"Berlin","radius":5}'
```

Alternatively, submit a complete validated Kleinanzeigen search URL:

```json
{"url":"https://www.kleinanzeigen.de/s-fahrraeder/berlin/c217l3331"}
```

Both forms return `202` with `job_id`, `status_url`, and `results_url`. The
optional idempotency key returns the existing job when key and payload match;
reusing it with another payload returns `409`.

Use the status and result endpoints without causing additional browser access:

```http
GET /jobs/{job_id}
GET /jobs/{job_id}/results?limit=100&cursor=...
```

Status values are `queued`, `running`, `waiting_cooldown`,
`awaiting_continuation`, `needs_attention`, `completed`, and `cancelled`.
Results use a stable opaque cursor backed by SQLite.

Search cursors page through immutable result order. Detail results are a
monotonic change feed while work is running: the same `listing_id` can appear
again after changing from `pending` to a final state, and clients should keep
the newest item per ID. After the detail job reaches a terminal state, a fresh
request without a cursor returns exactly one current row per requested ID.

If Kleinanzeigen reports more than 200 results, the first 200 unique results are
released and the job enters `awaiting_continuation`. Each explicit call releases
and schedules at most the next 200. Read `continuation.token` from the current
job status and submit it unchanged:

```http
POST /jobs/{job_id}/continue
Content-Type: application/json

{"continuation_token":"<token-from-job-status>"}
```

The token authorizes exactly one tranche. Retrying the same token after a lost
or cancelled HTTP acknowledgement is idempotent and cannot release another
200 results. A later tranche requires the new token returned by status or the
previous continuation acknowledgement. Durable action receipts make even older
valid tokens replay their original acknowledgement without changing work;
unknown or mismatched revisions return `409`.

Any unavoidable overflow already present on a boundary page remains
stored but invisible until this confirmation. Search results are deduplicated by
canonical `adid`; organic, Top, and Pro cards are included.

Opening listing pages is always a second explicit job. It can freeze the
currently released results of a search or accept an explicit ID list:

```sh
curl -X POST http://localhost:8000/jobs/details \
  -H 'Content-Type: application/json' \
  -d '{"source_search_job_id":"<search-job-uuid>"}'
```

```json
{"listing_ids":["3490152593-217-3401","3461111871"]}
```

Every requested ID remains visible as `pending`, `complete`, `not_found`,
`needs_attention`, or `cancelled`; failures are not silently removed.
Persistent technical failures pause the job. Read `resume.token` from current
job status and retry exactly that attention cycle explicitly:

```http
POST /jobs/{job_id}/resume
Content-Type: application/json

{"resume_token":"<token-from-job-status>"}
```

Resume tokens also have durable replay receipts, so an old retry can never
authorize work from a newer failure cycle. `DELETE /jobs/{job_id}` is the only
request that cancels accepted work; cancellation drains already running browser
work even if the requesting client disconnects after the database commit.

The older scraping endpoints below are compatibility adapters. `/inserate`,
`/inserate-by-url`, `/inserat/{id}`, and `/inserate/batch` now return `202` job
records rather than synchronous scrape results. `/inserate-detailed` returns
`410 Gone`; use a search job followed by a detail job.

#### 1. Fetch Listings (Standard)

**Endpoint:** `GET /inserate`

**Deprecated:** Creates a durable search job. `page_count` is accepted only for
legacy compatibility; the job discovers pages itself and applies the 200-result
continuation boundary.

##### Query Parameters

- **`query`** *(string, optional)*: The search term (e.g., "fahrrad" to search for bicycles).
- **`location`** *(string, optional)*: The location or postal code to narrow the search (e.g., `10178` for Berlin).
- **`radius`** *(integer, optional)*: The search radius in kilometers from the specified location (e.g., `5` for a 5 km radius).
- **`min_price`** *(integer, optional)*: The minimum price in Euros for the listings (e.g., `200` for at least 200 Euros).
- **`max_price`** *(integer, optional)*: The maximum price in Euros for the listings (e.g., `500` for at most 500 Euros).
- **`page_count`** *(integer, deprecated)*: Accepted for compatibility but
  ignored. The durable job discovers pages itself and releases results in
  200-item steps.
- **`min_publish_date`** *(datetime, optional)*: Stop fetching once a page contains listings published before this datetime. Listings older than the threshold are removed from the final results. Format: `YYYY-MM-DDTHH:MM:SS` (e.g., `2026-05-03T08:00:00`). Useful for intraday runs that should only collect new listings.

##### Example Requests

```http
GET /inserate?query=fahrrad&location=10178&radius=5&min_price=200&page_count=5
```

**With `min_publish_date` — stop when listings older than this morning are reached:**
```sh
curl "http://localhost:8000/inserate?query=fahrrad&page_count=10&min_publish_date=2026-05-04T08:00:00"
```

#### 2. Fetch Listing Details

**Endpoint:** `GET /inserat/{id}`

**Deprecated:** Creates a one-ID detail job and returns `202`. Use
`POST /jobs/details` for new integrations.

##### Path Parameters

- **`id`** *(string)*: The listing identifier. Two formats are accepted:

  | Format | Example | Source |
  |--------|---------|--------|
  | Full URL segment *(recommended)* | `3399373623-220-16792` | Last path segment of the listing `url` field from search results |
  | Plain adid | `3399373623` | The `adid` field from search results |

  Kleinanzeigen listing URLs follow the pattern:
  ```
  /s-anzeige/{slug}/{adid}-{category_id}-{location_id}
  ```
  The suffix `-{category_id}-{location_id}` is used by Kleinanzeigen for routing.
  The full segment is the most precise identifier exposed by search results;
  Kleinanzeigen may still redirect to a canonical URL containing the title slug.

##### Query Parameters

- **`batch_id`** *(string, deprecated)*: Accepted and validated for legacy
  compatibility, but ignored by the durable job adapter.

##### curl Examples

**With full URL segment (recommended):**
```sh
curl -s http://localhost:8000/inserat/3399373623-220-16792 | python -m json.tool
```

**With plain adid only (also works):**
```sh
curl -s http://localhost:8000/inserat/3399373623 | python -m json.tool
```

Both calls now return the `202` job-submission shape documented above. Retrieve
the eventual detail data through the returned `results_url`.

**Tip:** When iterating over search results, use the `url` field directly — extract the last path segment to get the full id:
```sh
# Extract the id from a listing url
echo "https://www.kleinanzeigen.de/s-anzeige/fendt-bianco/3399373623-220-16792" | grep -oP '[^/]+$'
# → 3399373623-220-16792
```

##### Historical synchronous response (no longer returned by this endpoint)

```json
{
  "success": true,
  "time_taken": 4.108,
  "data": {
    "id": "3399373623",
    "url_requested": "https://www.kleinanzeigen.de/s-anzeige/3399373623-220-16792",
    "url_redirected": "https://www.kleinanzeigen.de/s-anzeige/fendt-bianco-aktiv-550-kg/3399373623-220-16792",
    "categories": ["Wohnwagen & Reisemobile", "Wohnwagen"],
    "title": "Fendt Bianco Aktiv 550 KG",
    "status": "active",
    "price": {
      "amount": "12500",
      "currency": "€",
      "negotiable": true
    },
    "delivery": "pickup",
    "location": {
      "zip": "88299",
      "city": "Leutkirch im Allgäu"
    },
    "description": "Verkaufe unseren gepflegten Fendt Bianco...",
    "media": {
      "images": {
        "count": 1,
        "urls": [
          "https://img.kleinanzeigen.de/api/v1/prod-ads/images/..."
        ]
      },
      "videos": {"count": 0, "urls": []},
      "audios": {"count": 0, "urls": []}
    },
    "details": {
      "Baujahr": "2015",
      "Zustand": "Gebraucht"
    },
    "features": {},
    "seller": {
      "name": "Max Mustermann",
      "type": "private"
    },
    "extra_info": {
      "created_at": "4. Mai 2026",
      "views": "142"
    }
  },
  "performance_metrics": {
    "success_rate": 100,
    "time_taken": 4.108
  }
}
```

**Fields in a completed detail-job result:**

- **`data.id`** — Kleinanzeigen listing ID (numeric string).
- **`data.url_requested`** — The URL sent to Kleinanzeigen. When called with a plain adid, this is the short form without slug.
- **`data.url_redirected`** — The final URL after redirects. It may contain the
  canonical slug and routing suffix even when the submitted ID already used the
  full segment.
- **`data.status`** — Listing status: `active`, `sold`, `reserved`, or `deleted`.
- **`data.price.negotiable`** — `true` if the price is marked as negotiable (VB).
- **`data.delivery`** — `pickup`, `shipping`, or `null`.
- **`data.media.images`** — Image count and all extracted image URLs.
- **`data.details`** — Key/value pairs of listing attributes (e.g. Baujahr, Zustand).
- **`data.features`** — Additional configuration options if present.
- **`data.extra_info.views`** — View count if Kleinanzeigen exposes it.

#### 3. Fetch Listings with Details (Combined)

**Endpoint:** `GET /inserate-detailed`

**Removed:** Returns `410 Gone`. Create a search job first and then explicitly
create a detail job from its `job_id`. This prevents an apparently harmless
search request from opening hundreds of listing pages.

#### 4. Scrape by URL

**Endpoint:** `POST /inserate-by-url`

**Deprecated:** Creates a durable search job from a full Kleinanzeigen URL and
returns `202`. Prefer `POST /jobs/search` with the same `url` field.

##### Request Body

```json
{
  "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
  "max_pages": 3
}
```

- **`url`** *(string, required)*: HTTPS search or category URL on
  `kleinanzeigen.de` or `www.kleinanzeigen.de` (maximum 4096 UTF-8 bytes).
- **`max_pages`** *(integer, deprecated)*: Accepted but ignored. The durable job
  discovers pages and pauses after every 200 released unique results.
- **`min_publish_date`** *(datetime, optional)*: Stop fetching once a page contains listings published before this datetime. Listings older than the threshold are removed from the final results. Format: `YYYY-MM-DDTHH:MM:SS`. Supports intraday precision — e.g. `"2026-05-04T08:00:00"` keeps only listings from that morning onward.

For SSRF protection, redirects must stay on a canonical Kleinanzeigen host,
subresources are restricted to the `kleinanzeigen.de` DNS namespace, and
WebSocket connections are disabled for scraper pages.

##### Legacy submission examples (`max_pages` is ignored)

**Legacy payload with `max_pages: 1`:**
```sh
curl -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 1
  }'
```

**Legacy payload with `max_pages: 3`:**
```sh
curl -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 3
  }'
```

**With full filters — VW Kombi/SUV, Automatik, CNG/LPG, ab 2008:**
```sh
curl -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.ez_i:2008%2C+autos.fuel_s:(cng%2Clpg)+autos.marke_s:volkswagen+autos.shift_s:automatik+autos.typ_s:(kombi%2Csuv)",
    "max_pages": 3
  }'
```

**Wohnwagen — Fendt, Klima, max 15.000 €, ab 2008:**
```sh
curl -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-wohnwagen-mobile/wohnwagen/preis::15000/klima/k0c220+wohnwagen_mobile.art_s:wohnwagen+wohnwagen_mobile.ez_i:2008%2C+wohnwagen_mobile.marke_s:fendt",
    "max_pages": 2
  }'
```

**With `min_publish_date` — stop once listings older than this morning are found (intraday run):**
```sh
curl -s -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 20,
    "min_publish_date": "2026-05-04T08:00:00"
  }' | python -m json.tool
```
Expected: a `202` job record. Poll its status and results; the persisted search
stops when it reaches the cutoff.

**Verify early-stop: future cutoff → 0 results, only 1 page fetched:**
```sh
curl -s -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 5,
    "min_publish_date": "2099-01-01T00:00:00"
  }' | python -m json.tool
```
Expected: a `202` job record whose result set is empty after completion.

**Verify no false filtering with a past cutoff:**
```sh
curl -s -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 1,
    "min_publish_date": "2000-01-01T00:00:00"
  }' | python -m json.tool
```
Expected: a `202` job record; matching results become available asynchronously.

**Plain-date compatibility:**
```sh
curl -s -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen",
    "max_pages": 1,
    "min_publish_date": "2026-05-04"
  }' | python -m json.tool
```
Expected: a `202` job record. Pydantic normalizes the plain date to midnight;
send a full timestamp when intraday precision matters.

**Pretty-print the response with Python:**
```sh
curl -s -X POST http://localhost:8000/inserate-by-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen", "max_pages": 1}' \
  | python -m json.tool
```

##### Historical synchronous response (no longer returned by this endpoint)

```json
{
  "success": true,
  "results": [
    {
      "adid": "3398033022",
      "url": "https://www.kleinanzeigen.de/s-anzeige/...",
      "title": "VW Golf Variant 2.0 TDI Automatik Klima",
      "price": "12500",
      "description": "Gepflegter Zustand, Erstbesitz...",
      "published_at": "2026-05-03T22:06:00"
    }
  ],
  "unique_results": 75,
  "total_results": 120140,
  "time_taken": 12.358,
  "performance_metrics": {
    "pages_requested": 3,
    "pages_successful": 3,
    "success_rate": 100.0,
    "average_page_time": 4.119
  },
  "browser_metrics": {
    "contexts_created": 3,
    "contexts_reused": 0,
    "contexts_in_pool": 3,
    "contexts_in_use": 0
  }
}
```

**Fields in the historical synchronous response:**

- **`results`**: Array of listing objects. Each listing contains:
  - `adid` — Kleinanzeigen internal listing ID
  - `url` — Direct link to the listing
  - `title` — Listing title
  - `price` — Price as string (numeric digits only, no currency symbol; empty string if not set)
  - `description` — Short teaser text from the listing card
  - `published_at` — ISO 8601 publish datetime (e.g. `"2026-05-03T22:06:00"`). Older listings show only the date at midnight (`"2026-04-26T00:00:00"`). `null` if not shown on the card.
- **`unique_results`**: Total number of listings returned across all fetched pages.
- **`total_results`**: Total hits reported by Kleinanzeigen for this search (e.g. `120140`). Extracted from the page breadcrumb on the first page load — no extra request needed.
- **`time_taken`**: Total elapsed time in seconds.
- **`performance_metrics.pages_requested`** / **`pages_successful`** / **`success_rate`**: Page-level fetch statistics.

#### 5. Convert URL to API Parameters

**Endpoint:** `POST /convert-url`

**Description:** Parses a Kleinanzeigen URL into legacy `inserate_params` and
`unmapped` filters. The emitted `page_count` is retained for compatibility but
is ignored by the durable `/inserate` adapter. New job integrations can submit
the original validated URL directly to `POST /jobs/search`.

##### Request Body

```json
{
  "url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen"
}
```

##### curl Examples

**Basic — category + keyword only:**
```sh
curl -X POST http://localhost:8000/convert-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.marke_s:volkswagen"}' \
  | python -m json.tool
```

**With year filter:**
```sh
curl -X POST http://localhost:8000/convert-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.ez_i:2008%2C+autos.marke_s:volkswagen"}' \
  | python -m json.tool
```

**With full filters — fuel, transmission, body type:**
```sh
curl -X POST http://localhost:8000/convert-url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.kleinanzeigen.de/s-autos/volkswagen/klima/k0c216+autos.ez_i:2008%2C+autos.fuel_s:(cng%2Clpg)+autos.marke_s:volkswagen+autos.shift_s:automatik+autos.typ_s:(kombi%2Csuv)"}' \
  | python -m json.tool
```

##### Example Response

```json
{
  "inserate_params": {
    "query": "klima",
    "page_count": 1
  },
  "unmapped": {
    "category_slug": "s-autos",
    "subcategory": "volkswagen",
    "category_id": 216,
    "year_from": 2008,
    "year_to": null,
    "brands": ["volkswagen"]
  }
}
```

### Documentation

#### API Response Format

##### Durable jobs

Submission:

```json
{
  "job_id": "0d42f27f-7e2f-44c1-b917-cab38cc5a53d",
  "type": "search",
  "state": "queued",
  "idempotent_replay": false,
  "status_url": "/jobs/0d42f27f-7e2f-44c1-b917-cab38cc5a53d",
  "results_url": "/jobs/0d42f27f-7e2f-44c1-b917-cab38cc5a53d/results"
}
```

Status includes persistent progress, result counts, continuation information,
queue age/ETA, cooldown information, and visible errors. Result pages contain
`items`, `next_cursor`, and `has_more`. Search polling and result pagination
never contact Kleinanzeigen.

##### `GET /inserate` and `GET /inserate-detailed`

The response below documents the historical synchronous API. `/inserate` now
returns the job submission shape above and `/inserate-detailed` returns `410`.

```json
{
  "success": true,
  "time_taken": 1.23,
  "unique_results": 25,
  "data": [
    {
      "adid": "123456",
      "url": "https://www.kleinanzeigen.de/s-anzeige/...",
      "title": "Example Item",
      "price": "100",
      "description": "Short teaser text..."
    }
  ],
  "performance_metrics": {
    "pages_requested": 1,
    "pages_successful": 1,
    "success_rate": 100.0,
    "average_page_time": 1.23
  }
}
```

The `/inserate-detailed` response additionally nests a `details` object inside each listing with full description, seller info, and location.

##### `POST /inserate-by-url`

The response below is historical. This adapter now returns a search-job record.

```json
{
  "success": true,
  "results": [
    {
      "adid": "123456",
      "url": "https://www.kleinanzeigen.de/s-anzeige/...",
      "title": "Example Item",
      "price": "100",
      "description": "Short teaser text...",
      "published_at": "2026-05-03T22:06:00"
    }
  ],
  "unique_results": 75,
  "total_results": 120140,
  "time_taken": 12.358,
  "performance_metrics": {
    "pages_requested": 3,
    "pages_successful": 3,
    "success_rate": 100.0,
    "average_page_time": 4.119
  },
  "browser_metrics": { ... }
}
```

`total_results` is the total hit count shown by Kleinanzeigen (e.g. `120140`). It is extracted from the breadcrumb of the first page — no extra request is made. The field is omitted if the breadcrumb is not found.

##### `POST /convert-url`

```json
{
  "inserate_params": {
    "query": "klima",
    "page_count": 1
  },
  "unmapped": {
    "category_slug": "s-autos",
    "subcategory": "volkswagen",
    "category_id": 216,
    "year_from": 2008,
    "year_to": null,
    "brands": ["volkswagen"]
  }
}
```

## Performance Features

## 📊 Performance Benchmarks

> **Historical:** These figures predate durable jobs and the conservative
> process-wide 3-second start interval. They are not latency targets for the
> current job API. Backlog changes ETA, never job correctness or retention.

Performance tests conducted on **Arch Linux** with **Intel i7-1260P** processor:

| Endpoint | Operation | Avg Time | Min Time | Max Time | Success Rate | Results |
|----------|-----------|----------|----------|----------|--------------|---------|
| Root | API status check | 0.004s | 0.001s | 0.009s | 100% | - |
| Listings | 1 page search | 1.209s | 1.081s | 1.596s | 100% | 25 |
| Listings | 5 pages search | 2.436s | 2.328s | 2.532s | 100% | 125 |
| Listings | 10 pages search | 4.989s | 4.790s | 5.229s | 100% | 250 |
| Details | Single listing details | 1.072s | 0.928s | 1.276s | 100% | 1 |
| Combined | 1 page + details | 9.058s | 8.804s | 9.319s | 100% | 25 |
| Combined | 2 pages + details | 17.512s | 17.063s | 18.038s | 100% | 50 |

### Performance Highlights

- **Single page search**: ~1.2s average response time
- **10-page search**: ~5.0s average response time  
- **Individual listing details**: ~1.1s average response time
- **Combined search + details**: ~9.1s average for 1 page with full details

*All tests performed with uvloop optimization and browser context pooling enabled.*

### 🚀 Technical Features

- **uvloop Integration**: High-performance asyncio event loop
- **Context Pooling**: Efficient browser resource reuse
- **Memory Optimization**: Automatic garbage collection and efficient processing
- **Intelligent Concurrency**: Optimal concurrent processing with resource control

### 🔧 Production Ready

- **Clean API Responses**: Minimal overhead with essential data only
- **Resource Management**: Automatic cleanup and efficient resource usage
- **Durable single-process architecture**: Queues concurrent submissions without
  multiplying outbound browser activity
- **Docker packaging**: Installs production dependencies with pip and bundles
  Playwright Chromium

API documentation is available at `http://localhost:8000/docs` when running locally.

## License

Distributed under the MIT License. See `LICENSE` for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## Changelog

> **Historical API note:** The dated entries below describe the synchronous API
> as it behaved at that time. The current scraping endpoints return durable job
> records (or, for `/inserate-detailed`, `410 Gone`); wording such as "now" and
> "after" in these entries is relative to the dated release, not the current API.

### May 2026 — Multi-image fetching, deleted-ad detection, media response restructure ⚠️ Breaking

**Maintainer:** [Darko Palić](https://github.com/dpalic)

#### ⚠️ Breaking changes

##### 1. `GET /inserat/{id}` — response shape changed

The `images` and `views` top-level fields have been replaced. The data is still present
but lives in a different location.

| Field | Before | After |
|---|---|---|
| Image URLs | `data.images` → `["url"]` (one image) | `data.media.images.urls` → `["url1", "url2", …]` (all images) |
| Image count | not provided | `data.media.images.count` |
| Views | `data.views` | `data.extra_info.views` (unchanged location — was already there) |

**How to migrate:**

```python
# Before
image = response["data"]["images"]        # single URL string or list of one
views = response["data"]["views"]

# After
images = response["data"]["media"]["images"]["urls"]   # list of all gallery images
count  = response["data"]["media"]["images"]["count"]
views  = response["data"]["extra_info"]["views"]       # unchanged
```

```javascript
// Before
const image = data.images;
const views = data.views;

// After
const images = data.media.images.urls;   // array of all gallery images
const count  = data.media.images.count;
const views  = data.extra_info.views;    // unchanged
```

##### 2. `GET /inserat/{id}` — deleted/expired ads now return HTTP 404

Previously, fetching a deleted or expired ad returned HTTP 200 with `success: true`
and empty fields (title `"[ERROR] Title not found"`, status `"active"`).

Now, two conditions are detected and return HTTP **404** with `success: false`:
- The URL redirects away from the ad page (ad deleted — Kleinanzeigen redirects to homepage)
- The page contains `#srchrslt-adexpired` (ad expired page)

**How to migrate:**

```python
# Before — callers had to inspect fields to detect a missing ad
resp = requests.get(f"/inserat/{id}", params={"batch_id": "x"})
data = resp.json()["data"]
if data["title"] == "[ERROR] Title not found":
    # ad was gone

# After — standard HTTP semantics
resp = requests.get(f"/inserat/{id}", params={"batch_id": "x"})
if resp.status_code == 404:
    # ad is deleted or expired
    print(resp.json()["detail"]["status"])  # "deleted"
else:
    data = resp.json()["data"]
```

```javascript
// Before
const res = await fetch(`/inserat/${id}?batch_id=x`);
const { data } = await res.json();
if (data.title === "[ERROR] Title not found") { /* gone */ }

// After
const res = await fetch(`/inserat/${id}?batch_id=x`);
if (res.status === 404) {
    const { detail } = await res.json();
    // detail.status === "deleted"
} else {
    const { data } = await res.json();
}
```

#### New fields (non-breaking, additive)

- **`data.scraped_at`** — ISO 8601 UTC timestamp of when the ad was scraped
- **`data.url_requested`** — URL sent to Kleinanzeigen (built from the id)
- **`data.url_redirected`** — final URL after any server-side redirect
- **`data.seller.user_id`** — numeric Kleinanzeigen user ID extracted from the profile link href

#### Improvements (non-breaking)

- **All gallery images returned** — previously only the first image was fetched
  (`#viewad-image`). Now all images from `.galleryimage-element img` are returned,
  including `data-src` lazy-loaded images. Deduplication applied.
- **`POST /inserate-by-url` — smarter pagination** — breadcrumb is now fully parsed
  on page 1 to determine the exact page count. Fetching stops automatically when the
  real page count is reached, avoiding wasted requests for non-existent pages.
- **`POST /inserate-by-url` — URL pagination fix** — page injection now correctly
  handles complex URLs with filters before the category segment
  (e.g. `anzeige:angebote`, `preis::N`).
- **Healthcheck endpoint** — `GET /health` added for load balancer / uptime monitoring.

#### Tests

- 16 live integration tests in `tests/test_inserat_live.py` covering active ads
  (both full-segment and plain adid formats), all response fields, and deleted-ad
  detection (HTTP 404, `status: "deleted"`) for 2 known-expired ad IDs.

---

### May 2026 — `GET /inserat/{id}` URL tracking (`url_requested` / `url_redirected`)

**Maintainer:** [Darko Palić](https://github.com/dpalic)

#### Changed

- **`GET /inserat/{id}` response** now includes two URL fields in `data`:
  - `url_requested` — the URL that was sent to Kleinanzeigen (built from the id passed to the endpoint).
  - `url_redirected` — the final URL as reported by the browser after any server-side redirect. Identical to `url_requested` when no redirect occurred.
- Useful when calling with a plain adid (e.g. `/inserat/3399373623`) to discover the canonical URL including slug and routing suffix.

#### Tests

- 13 live integration tests in `tests/test_inserat_live.py` covering both id formats (full segment and plain adid), `url_requested`, `url_redirected`, content fields, and status validation.

---

### May 2026 — Intraday `min_publish_date` precision (datetime)

**Maintainer:** [Darko Palić](https://github.com/dpalic)

#### Changed

- **`min_publish_date` upgraded from `date` to `datetime`** on both `GET /inserate` and `POST /inserate-by-url`. The parameter now accepts a full ISO 8601 datetime string (`YYYY-MM-DDTHH:MM:SS`) instead of a plain date. This enables intraday filtering — e.g. `"2026-05-04T08:00:00"` keeps only listings published from that exact time onward, not just from that calendar day.
- The internal comparison in `_page_has_old_listings` and `_filter_by_min_publish_date` now operates on full `datetime` objects instead of stripping to `.date()`, preserving hour/minute precision.
- German date strings (`"Heute, 08:46"`, `"Gestern, 14:29"`) already produced full ISO 8601 datetimes — no parsing changes required.

#### Tests

- 3 new live integration tests in `tests/test_inserate_by_url_live.py` covering `min_publish_date`:
  - Future cutoff (`2099-01-01T00:00:00`) returns 0 results and stops after page 1.
  - Past cutoff (`2000-01-01T00:00:00`) does not filter anything — result count unchanged.
  - All returned `published_at` values satisfy the cutoff when a real intraday threshold is used.

---

### May 2026 — URL-passthrough scraping, publish date, rate-limit fixes

**Maintainer:** [Darko Palić](https://github.com/dpalic)

#### New endpoints

- **`POST /inserate-by-url`** — scrape listings using any full Kleinanzeigen search URL. All filters encoded in the URL (category, brand, year, fuel type, transmission, body type, etc.) are preserved as-is. Page numbers are injected automatically for multi-page fetching. Response includes `total_results` (total hit count from the page breadcrumb, no extra request) and `published_at` on every listing.
- **`POST /convert-url`** — parse a Kleinanzeigen URL into `inserate_params` (what `/inserate` understands today) and `unmapped` (filters not yet supported by the structured endpoint).

#### New listing field

- **`published_at`** — ISO 8601 publish datetime added to every listing result (e.g. `"2026-05-04T22:06:00"`). Extracted from the listing card during the existing page load — no extra request. Older listings return a date-only value (`"2026-04-26T00:00:00"`); `null` if not shown on the card.

#### Bug fixes

- **Cookie clearing on context release** — browser contexts now have session cookies cleared before returning to the pool, preventing Kleinanzeigen tracking state from carrying over between unrelated requests and causing silent empty results on pages 2+.
- **Inter-page delay** — a 2-second pause between sequential page fetches within a single multi-page scrape reduces bot-detection false positives.

#### Tests

- 11 unit tests for `/convert-url` (`tests/test_convert_url.py`, no server required)
- 14 live integration tests for `/inserate-by-url` (`tests/test_inserate_by_url_live.py`), covering JSON structure, `published_at`, `total_results`, and exact result counts for 1–4 pages (25 / 50 / 75 / 100 results)
