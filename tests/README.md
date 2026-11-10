# Tests

## Pytest-Suite

Der normale Testlauf ist vollständig offline. Tests, die einen laufenden API-Server
benötigen oder Kleinanzeigen aufrufen können, tragen den Marker `live` und werden
standardmäßig übersprungen:

```bash
pytest
```

Live-Tests müssen ausdrücklich aktiviert werden. Dazu zuerst den API-Server starten
und anschließend den Opt-in-Schalter verwenden:

```bash
uvicorn main:app
pytest --run-live -m live
```

Die kanonische Live-Suite liegt in `test_persistent_jobs_live.py`. Sie reicht pro
UTC-Tag dank stabiler `Idempotency-Key`-Werte höchstens einen schmalen Suchjob und
einen Detailjob ein. Alle Tests teilen diese beiden Jobs, fragen ihren Status ab
und lesen Ergebnisse anschließend aus SQLite. Es gibt absichtlich keine
parallelen Live-Anfragen und keinen Live-Lasttest. Die testseitige Polling-Grenze
beträgt standardmäßig 900 Sekunden; sie kann mit
`KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS` angepasst werden und verwirft den
serverseitig angenommenen Job nicht. Eine abweichende lokale API-Adresse kann
mit `KLEINANZEIGEN_LIVE_BASE_URL` gesetzt werden.

Die früheren synchronen Live-Suiten bleiben nur als historische Referenz erhalten
und sind auch mit `--run-live` explizit übersprungen. Ihre Annahmen zu synchronen
`200`-Antworten, Seitenzahlen und paralleler Last gelten für die persistente
Job-API nicht mehr.

Mit `pytest --markers` lässt sich die registrierte Marker-Beschreibung anzeigen.

## Nachrichten: eigener Live-Schalter und eigene Accounts

Nachrichtentests tragen den Marker `live_messaging`. Der Schalter `--run-live`
aktiviert sie **nicht**, auch wenn ein Test zusätzlich als `live` markiert wäre.
Ohne `--run-live-messaging` wird kein Testversand ausgeführt. Zwei Offline-Tests
in `test_messaging_live.py` prüfen diese Trennung der Schalter.

Vor einem Nachrichtentest müssen im lokalen Backend zwei eigene Accounts über
`connect_account` manuell angemeldet sein. Der zweite Account muss die bestehende
Testanzeige besitzen. Der Test erstellt keine Anzeige und öffnet nur die für die
eigenen Testaccounts zugeordneten Gespräche.

Das Backend muss bereits mit folgenden Einstellungen gestartet sein:

- `KLEINANZEIGEN_MESSAGING_TEST_MODE=true`
- `KLEINANZEIGEN_MESSAGING_TEST_ACCOUNT_IDS`: beide lokale Account-IDs als CSV
- `KLEINANZEIGEN_MESSAGING_TEST_USER_IDS`: die verifizierten Kleinanzeigen-Nutzer-IDs
  beider eigener Accounts als CSV
- `KLEINANZEIGEN_MESSAGING_TEST_LISTING_IDS`: ausdrücklich ausgewählte eigene
  Anzeigen-IDs als CSV

Der Testmodus setzt die Grenzen serverseitig bei jedem Versand durch. Fehlende
oder fremde IDs erlauben keinen Versand. Im Testterminal zusätzlich setzen:

- `KLEINANZEIGEN_LIVE_BASE_URL`: lokale HTTP-Adresse des bereits laufenden Backends,
  standardmäßig `http://127.0.0.1:8000`
- `KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_A`: lokaler Name des ersten eigenen Accounts
- `KLEINANZEIGEN_LIVE_MESSAGING_ACCOUNT_B`: lokaler Name des zweiten eigenen Accounts
- `KLEINANZEIGEN_LIVE_MESSAGING_LISTING_ID`: bestehende eigene Anzeige von Account B
- `KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID`: verpflichtende, bewusst gewählte Kennung
  des beauftragten Testlaufs; 1–64 ASCII-Buchstaben, Ziffern, Unterstriche oder
  Bindestriche, beginnend mit Buchstabe oder Ziffer. Bei Wiederholungen desselben
  Laufs unverändert beibehalten, damit dieselben Idempotenzschlüssel gelten.

Dann ausschließlich den gezielten Ablauf starten:

```sh
export KLEINANZEIGEN_LIVE_MESSAGING_RUN_ID='eigener-test-01'
uv run pytest tests/test_messaging_live.py --run-live-messaging -m live_messaging
```

Der Harness prüft zunächst Testmodus, Accountbindung und erlaubte IDs. Ein
öffentlicher Detailjob verifiziert zusätzlich, dass die Anzeige Account B gehört.
Er erzeugt sein Testbild selbst im Speicher; private lokale Bilder werden nicht
verwendet. Danach folgen Erstkontakt A → B, Lesen von Text und Bild in B, Antwort
B → A sowie Lesen und Archivabruf in A. Eine Wiederholung desselben Erstauftrags
muss dieselbe `operation_id` liefern. Unklare Ergebnisse, Teilversand oder erneute
Anmeldeanforderung beenden den Test; es wird kein Ersatzauftrag erzeugt.

Die Polling-Grenze kann wie bei den übrigen Live-Tests über
`KLEINANZEIGEN_LIVE_POLL_TIMEOUT_SECONDS` gesetzt werden. Ein Testabbruch storniert
keinen angenommenen Versandauftrag. Bei einem unklaren Ergebnis zuerst dessen
Status und das eigene Gespräch prüfen; den Live-Test nicht blind neu starten.
Bei `outcome_unknown` zunächst ausschließlich lesen. Keine neue Laufkennung
vergeben, um einen unklaren Ausgang zu umgehen; eine neue Kennung ist einem
bewusst neu beauftragten Testlauf vorbehalten. Der Harness generiert keine
zufälligen Ersatzkennungen.

Die Offline-Suite prüft zusätzlich Trennung der Accounts, Archivpersistenz,
Idempotenz, Wiederanlauf nach Fehlern und Schutz der privaten Routen. Der Live-Test
führt keinen Backendneustart aus und stellt keine Plattformfähigkeit allein durch
Mocks als geprüft dar. Besonders Bildfunktionen müssen nach der manuellen
Anmeldung an der tatsächlich angebotenen Kleinanzeigen-Oberfläche bestätigt
werden.

### Safari offline prüfen

Die Safari-Anbindung verwendet denselben Nachrichtenservice und Testmodus. Ihre
gezielten Prüfungen benötigen weder einen angemeldeten Browser noch Netzwerkzugriff
auf Kleinanzeigen:

```sh
.venv/bin/pytest tests/test_messaging_safari.py tests/test_safari_integration.py
node --test safari-extension/web/tests/*.test.cjs
./safari-extension/native/test.sh
```

Geprüft werden unter anderem native Profilbindung, fremde Accounts, private
Kopplungsdateien, unveränderte HTTP-Grenzen, begrenzte Bildabschnitte und verlorene
Versandantworten. Die nativen Transporttests verwenden nur einen lokalen Testserver.
Live-Prüfungen erfordern zusätzlich die signierte und aktivierte Erweiterung;
Build, Installation und Freigabe sind in
[Safari-Anbindung](../safari-extension/README.md) beschrieben.

Die Offline-Prüfungen decken die Wiederherstellung gebundener Kontotabs,
die Zuordnung von Bildanhängen und verlorene Versandbestätigungen ab. Die
IPv4-/IPv6-Transporttests benötigen die Berechtigung für lokale Listen-Sockets.

### Abnahme von Versand und Archiv

Der Live-Test mit zwei eigenen Accounts bestätigte Erstkontakt, Antwort, Text-
und Bildempfang sowie den Archivzugriff nach einem Backendneustart. Mit beiden
MCP-Transporten wurden der Werkzeugkatalog, gespeicherte Gesprächsverläufe und
JPEG-Inhalte geprüft. Die Prüfsumme des ausgelieferten Bildes stimmte mit dem
Archiv überein. Ein unterbrochener oder unklar bestätigter Sendeschritt bleibt
als `outcome_unknown` einsehbar.

Beim historischen Import wurden Gesprächs- und Nachrichtenseiten bis zur letzten
Seite abgerufen. Der Abgleich prüfte die Gesprächs- und Nachrichten-IDs,
Zeitpunkte, Anbieterquellen und die Reihenfolge der Bildanhänge. Unterbrochene
Lesevorgänge lassen sich mit dem privaten Fortschrittsbericht fortsetzen.
Fehlende Anbieterangaben und nur teilweise unterstützte Plattformereignisse
bleiben im Ergebnis ausdrücklich gekennzeichnet.

Die Exportprüfung umfasste Dateigrößen und SHA-256-Prüfsummen, die Zuordnung
der HTML-Seiten und Bilddateien sowie die privaten Dateirechte. Eine separate
WKWebView öffnete eine Gesprächsseite mit mehreren lokalen Bildern über
`file://` und bestätigte, dass die Bilder vollständig geladen wurden.

## Historisches Load Testing Framework

> **Nicht gegen Kleinanzeigen ausführen.** Die folgenden Skripte stammen aus der
> früheren synchronen Architektur und sind kein Abnahmetest der persistenten
> Job-API. Parallelität und Fairness werden ausschließlich offline mit Fake-Browsern
> getestet.

This directory contains a comprehensive load testing framework for validating the Kleinanzeigen API performance optimizations. The framework simulates multiple concurrent users making API requests and provides detailed performance analysis.

## Features

- **Multiple Test Scenarios**: Single user, multiple users, stress testing, and mixed workloads
- **Concurrent User Simulation**: Realistic simulation of multiple users making simultaneous requests
- **Comprehensive Metrics**: Response times, success rates, throughput, and error analysis
- **Performance Reporting**: Console reports, JSON data, CSV summaries, and detailed request logs
- **Recommendations**: Actionable performance recommendations based on test results
- **Flexible Configuration**: Customizable test parameters and scenarios

## Quick Start

### Prerequisites

Make sure the API server is running:
```bash
# Start the API server
python main.py
# or
uvicorn main:app --reload
```

### Run Quick Performance Test

```bash
# Run a quick test with default settings
python tests/run_load_tests.py --quick

# Test against a different URL
python tests/run_load_tests.py --quick http://localhost:8000
```

### Run Comprehensive Test Suite

```bash
# Run all test scenarios and save results
python tests/run_load_tests.py --comprehensive
```

This will run:
1. Single user with multiple page counts (1, 5, 10, 15, 20 pages)
2. Multiple users with standard load (5 users, 5 requests each)
3. Stress test (10 users, 10 requests each)
4. Mixed workload test (8 users, 6 requests each)

Results are saved to the `load_test_results/` directory.

## Test Scenarios

### 1. Single User Multiple Pages
Tests performance with varying page counts to validate the 20-page < 3-second requirement.

```python
async with LoadTester() as tester:
    report = await tester.run_single_user_multiple_pages_test(
        page_counts=[1, 5, 10, 20],
        iterations=3
    )
```

### 2. Multiple Users Standard Load
Simulates realistic concurrent usage patterns.

```python
async with LoadTester() as tester:
    report = await tester.run_multiple_users_standard_load_test(
        user_count=5,
        requests_per_user=5
    )
```

### 3. Stress Test
High-load testing with many concurrent users and large page counts.

```python
async with LoadTester() as tester:
    report = await tester.run_stress_test(
        user_count=10,
        requests_per_user=10
    )
```

### 4. Mixed Workload
Realistic mix of different endpoints and usage patterns.

```python
async with LoadTester() as tester:
    report = await tester.run_mixed_workload_test(
        user_count=8,
        requests_per_user=6
    )
```

## Custom Testing

### Basic Usage

```python
import asyncio
from load_test import LoadTester, TestScenario

async def custom_test():
    async with LoadTester("http://localhost:8000") as tester:
        # Make individual requests
        result = await tester.make_request("/inserate", {
            "query": "laptop",
            "page_count": 10
        })
        
        # Simulate concurrent users
        user_metrics = await tester.simulate_concurrent_users(
            user_count=3,
            requests_per_user=5,
            scenario=TestScenario.MIXED_WORKLOAD
        )

asyncio.run(custom_test())
```

### Performance Validation

```python
async def validate_20_page_requirement():
    async with LoadTester() as tester:
        # Test 20-page requests multiple times
        results = []
        for i in range(5):
            result = await tester.make_request("/inserate", {
                "query": "smartphone",
                "page_count": 20
            })
            results.append(result)
        
        # Check if all requests completed in under 3 seconds
        avg_time = sum(r.response_time for r in results if r.success) / len([r for r in results if r.success])
        print(f"Average 20-page response time: {avg_time:.2f}s")
        print(f"Requirement met: {avg_time < 3.0}")
```

## Report Generation

The framework generates multiple types of reports:

### Console Report
Human-readable summary displayed in the terminal:
```
================================================================================
LOAD TEST REPORT - MULTIPLE_USERS_STANDARD_LOAD
================================================================================

TEST OVERVIEW
----------------------------------------
Scenario: multiple_users_standard_load
Start Time: 2024-01-15 14:30:25
End Time: 2024-01-15 14:31:45
Total Duration: 80.23 seconds
Concurrent Users: 5

REQUEST STATISTICS
----------------------------------------
Total Requests: 25
Successful Requests: 24
Failed Requests: 1
Success Rate: 96.00%
Error Rate: 4.00%
Requests per Second: 0.31
```

### JSON Report
Complete data in JSON format for programmatic analysis:
```json
{
  "scenario": "multiple_users_standard_load",
  "total_requests": 25,
  "successful_requests": 24,
  "average_response_time": 2.45,
  "recommendations": [
    "Performance looks good! All metrics are within acceptable ranges."
  ]
}
```

### CSV Summary
Metrics summary for spreadsheet analysis:
```csv
Metric,Value,Unit
Scenario,multiple_users_standard_load,
Total Duration,80.23,seconds
Success Rate,96.00,%
Average Response Time,2.450,seconds
```

## Performance Targets

The framework validates against these performance targets:

- **20-page requests**: Must complete in under 3 seconds
- **Success rate**: Should be above 95% for standard load
- **Concurrent requests**: System should handle 5+ simultaneous users
- **Error rate**: Should be below 5% under normal conditions

## Command Line Options

```bash
# Available options
python tests/run_load_tests.py --help

# Run specific test types
python tests/run_load_tests.py --quick           # Quick performance test
python tests/run_load_tests.py --individual     # Individual scenario examples
python tests/run_load_tests.py --comprehensive  # Full test suite
python tests/run_load_tests.py --validation     # Performance validation
python tests/run_load_tests.py --custom         # Custom test patterns
python tests/run_load_tests.py --all           # All test types

# Specify different API URL
python tests/run_load_tests.py --comprehensive https://api.example.com
```

## Output Files

When running comprehensive tests, results are saved to `load_test_results/`:

- `{scenario}_{timestamp}_report.txt` - Human-readable report
- `{scenario}_{timestamp}_data.json` - Complete test data
- `{scenario}_{timestamp}_summary.csv` - Metrics summary
- `{scenario}_{timestamp}_requests.csv` - Individual request details

## Troubleshooting

### Common Issues

1. **Connection Errors**
   - Ensure the API server is running
   - Check the base URL is correct
   - Verify network connectivity

2. **High Error Rates**
   - Check server logs for errors
   - Reduce concurrent user count
   - Increase request timeouts

3. **Slow Performance**
   - Monitor server resource usage
   - Check browser context pool settings
   - Verify database performance

### Debug Mode

For detailed debugging, modify the test script to include more logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Run tests with debug output
```

## Integration with CI/CD

The framework can be integrated into CI/CD pipelines:

```bash
# Example CI script
#!/bin/bash
set -e

# Start API server in background
python main.py &
API_PID=$!

# Wait for server to start
sleep 5

# Run performance validation
python tests/run_load_tests.py --validation

# Check if performance targets were met
if [ $? -eq 0 ]; then
    echo "Performance tests passed"
else
    echo "Performance tests failed"
    exit 1
fi

# Clean up
kill $API_PID
```

## Contributing

When adding new test scenarios:

1. Add the scenario to the `TestScenario` enum
2. Implement the test logic in the `LoadTester` class
3. Add appropriate recommendations in `_generate_recommendations`
4. Update this README with usage examples

## Requirements

The load testing framework requires:
- Python 3.7+
- aiohttp (for HTTP requests)
- asyncio (for concurrent operations)
- Standard library modules: statistics, json, csv, datetime
