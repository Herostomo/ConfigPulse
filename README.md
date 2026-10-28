# Configuration Change Monitoring System

A Python backend that monitors configuration files, computes structured diffs, records change events in PostgreSQL, and delivers events to a webhook asynchronously. Includes retry scheduling, dead-letter handling, replay, a REST API, structured JSON logs, Prometheus metrics, unit tests, and an end-to-end delivery test.

## 1. Architecture

```mermaid
flowchart TD
    C[JSON / YAML / .env / properties files]
    W[Watchdog + periodic reconciliation]
    S[Debounce + stable file reader]
    P[Format parser + validation]
    D[Recursive diff engine]
    TX[Atomic PostgreSQL transaction]
    DB[(PostgreSQL: snapshots, events, delivery_jobs)]
    WK[Delivery worker]
    WH[Webhook destination]
    API[FastAPI management API]
    M[JSON logs + Prometheus metrics]

    C --> W --> S --> P --> D --> TX --> DB
    DB --> WK --> WH
    API <--> DB
    W -.-> M
    P -.-> M
    D -.-> M
    WK -.-> M
```

### Component responsibilities

- **Watcher:** Uses Watchdog for fast filesystem events, debounces event bursts, and periodically reconciles the monitored-file registry to recover missed events.
- **Reader/parser:** Reads a file only after it is stable, validates UTF-8 and parses JSON, YAML, dotenv-style `.env`, or a common `key=value`/`key:value` properties subset.
- **Diff engine:** Finds added, removed, updated, nested and type-changed values. Arrays are currently compared atomically; mapping changes use dot-separated paths.
- **PostgreSQL:** Stores the monitored registry, last valid snapshot, immutable events, and durable delivery jobs.
- **Delivery worker:** Claims due jobs using row locks, posts events to a configured webhook, schedules retries with exponential backoff and jitter, and moves exhausted/permanent failures to `dead_letter`.
- **Management API:** Registers/removes watched files, lists events and delivery outcomes, supports manual replay and exposes health/metrics endpoints.

### Atomic event recording

When a valid file state changes, one database transaction updates the snapshot and inserts the event plus the initial delivery job. This avoids advancing a snapshot without recording its event. The filesystem and database cannot participate in one shared transaction, so periodic reconciliation repairs missed file-system notifications after a restart.

### Delivery guarantee

The worker is designed for **at-least-once delivery**. A crash after a remote webhook has accepted an event but before the worker records success can cause a retry. Each event therefore has a stable `event_id`, sent in both `Idempotency-Key` and `X-Config-Monitor-Event-Id` headers. Consumers should deduplicate by that ID if they need idempotent effects. The system does not claim exactly-once effects across arbitrary HTTP endpoints.

## 2. Requirements

- Python 3.11 or newer (Python 3.12 recommended; Python 3.13 should also work with current dependencies)
- PostgreSQL 14+
- A terminal with access to the project directory

Docker support is included, but local execution is the recommended first run.

## 3. Run locally on Windows (PowerShell)

### Step A — Create PostgreSQL database/user

Create a database and user using `psql` as a PostgreSQL administrator. Run these commands individually (if the role/database already exist, skip the corresponding creation command):

```sql
CREATE USER config_monitor WITH PASSWORD 'config_monitor';
CREATE DATABASE config_monitor OWNER config_monitor;
```

You can run them from `psql -U postgres` or pgAdmin's Query Tool. Ensure PostgreSQL is running and listening on `localhost:5432`.

### Step B — Create and activate the Python environment

Open PowerShell in the project folder:

```powershell
py -3.12 -m venv .venv  # or: py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks environment activation, either adjust the execution policy for your user according to your organization's policy or run the environment's Python directly as `.\.venv\Scripts\python.exe` without activation.

Open `.env` and confirm `DATABASE_URL` matches your local PostgreSQL credentials. The sample uses:

```text
DATABASE_URL=postgresql+psycopg://config_monitor:config_monitor@localhost:5432/config_monitor
CONFIG_ROOT=./config_samples
WEBHOOK_URL=http://127.0.0.1:9001/webhook
```

### Step C — Initialize tables

```powershell
python -m app.cli init-db
```

Expected output:

```text
Database tables created/verified.
```

The API and worker also verify that tables exist at startup, but running the command explicitly is a useful first check.

### Step D — Start four processes

Keep each process in a separate PowerShell terminal, all opened in the project folder with the virtual environment activated.

**Terminal 1 — Mock webhook receiver**

```powershell
python -m app.cli mock-webhook
```

It listens on `http://127.0.0.1:9001/webhook` and prints every accepted event. This lets you test without calling an external service.

**Terminal 2 — API**

```powershell
uvicorn app.api:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 3 — File watcher**

```powershell
python -m app.cli watcher
```

On its first run, the watcher registers the supported files already in `config_samples` and creates baseline snapshots without generating events for every existing key. Later newly discovered files under that root generate creation events.

**Terminal 4 — Delivery worker**

```powershell
python -m app.cli worker
```

The worker will deliver pending jobs to the mock receiver. Press `Ctrl+C` in each terminal to stop it.

### Step E — Verify the API

Open `http://127.0.0.1:8000/docs` for interactive API documentation. Check health and files:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/files
Invoke-RestMethod http://127.0.0.1:8000/events
Invoke-RestMethod http://127.0.0.1:8000/deliveries
```

### Step F — Trigger a change

Edit `config_samples\database.yaml`, for example change:

```yaml
logging:
  level: info
```

to:

```yaml
logging:
  level: debug
```

Save the file. After the debounce interval, the watcher should record a `configuration.changed` event. The worker should send it to the mock webhook. Verify the event with:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/events
Invoke-RestMethod http://127.0.0.1:8000/deliveries
```

You can also create a fresh supported file within `config_samples`; it will be detected and registered. If you delete a monitored file, the service records `configuration.deleted`. Avoid putting real credentials in the sample directory.

### Add a future file through the API

A file can be registered before it exists. It will wait for creation:

```powershell
$body = @{ path = "future.yaml" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/watch -ContentType "application/json" -Body $body
```

Then create `config_samples\future.yaml`; its first appearance will generate a `configuration.created` event. The file path must remain inside `CONFIG_ROOT` and use a supported extension/name.

### Replay a previously recorded event

Copy an `event_id` from `GET /events` and submit:

```powershell
$eventId = "PUT-EVENT-UUID-HERE"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/events/$eventId/replay"
```

This creates a new delivery job for the existing event. It does not modify or delete the event's audit history.

## 4. Run tests

From the project root, install the requirements and run:

```powershell
pytest
```

Unit tests cover supported parsers and diff behavior. The integration test creates a temporary SQLite database, changes a sample file, records the event and sends it to a local HTTP test server. It does not require your PostgreSQL service or a real webhook. Production runtime defaults still use PostgreSQL.

## 5. API reference

| Method | Route | Description |
|---|---|---|
| `GET` | `/health` | DB connectivity check / readiness result |
| `GET` | `/files` | Monitored paths and processing statuses |
| `POST` | `/watch` | Register a path relative to `CONFIG_ROOT` |
| `DELETE` | `/watch/{file_id}` | Disable monitoring while retaining history |
| `GET` | `/events?limit=50&offset=0` | Paginated event history; optional `file_id` and `event_type` filters |
| `GET` | `/deliveries?status=dead_letter` | Inspect delivery attempts/status |
| `POST` | `/events/{event_id}/replay` | Enqueue another delivery for an existing event |
| `GET` | `/metrics` | Prometheus-format counters, gauges and histograms |

`DELETE /watch/{file_id}` uses the registry's numeric ID, not an arbitrary path in a URL. Disablement does not delete events or cancel already-created delivery jobs.

## 6. Event contract

Example event payload:

```json
{
  "event_id": "a-unique-uuid",
  "schema_version": 1,
  "event_type": "configuration.changed",
  "file": "database.yaml",
  "timestamp": "2026-10-13T05:30:00Z",
  "changes": {
    "added": {"database.pool_size": 20},
    "updated": {"database.host": {"old": "localhost", "new": "production-db"}},
    "removed": {},
    "type_changes": {}
  }
}
```

All four change groups are present. `configuration.created`, `configuration.changed`, and `configuration.deleted` represent file lifecycle events. The timestamp is UTC. For a deleted file, `changes.removed` describes the leaves from the last valid snapshot.

Sensitive key paths (passwords, secrets, tokens, API keys, credentials, private keys and authorization fields) are redacted in the event payload. The raw parsed snapshot is still stored in PostgreSQL so later comparisons work; restrict database access and use encrypted disks/managed database encryption for sensitive deployments. The API never returns the raw snapshot.

## 7. Failure handling

- **Partial/invalid writes:** stable reads are retried; invalid parses retain the last valid snapshot and set the file status to `error`.
- **Missed file events:** Watchdog events are backed by periodic reconciliation. Reconciliation uses modification time and size as a fast path, then forces a content hash check periodically.
- **Database outage:** event creation cannot be acknowledged as durable if the database transaction fails. The watcher logs the failure; reconciliation retries the file later.
- **Webhook outage:** jobs remain persistent. Network failures, HTTP 408/425/429 and HTTP 5xx are retryable. Other HTTP 4xx responses are treated as permanent and sent to dead-letter status.
- **Backoff:** exponential delays with jitter and a configurable cap. For HTTP 429, `Retry-After` is used when available.
- **Worker crash:** jobs with expired `in_progress` leases can be claimed again.
- **Duplicate webhook requests:** stable event IDs and idempotency headers support receiver-side deduplication; exactly-once external effects cannot be guaranteed generically.
- **Replay:** creates a new delivery job linked to the previous job while preserving the original event.

## 8. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | Local PostgreSQL URL | SQLAlchemy database connection |
| `CONFIG_ROOT` | `./config_samples` | Approved root for monitored files |
| `WEBHOOK_URL` | `http://127.0.0.1:9001/webhook` | Destination for delivery jobs |
| `WEBHOOK_BEARER_TOKEN` | unset | Optional bearer authentication for webhook |
| `POLL_INTERVAL_SECONDS` | `5` | Reconciliation interval |
| `DEBOUNCE_SECONDS` | `0.4` | Quiet time before processing a watcher event |
| `FULL_HASH_INTERVAL_SECONDS` | `60` | Periodic forced hash check interval |
| `WORKER_POLL_SECONDS` | `1` | Idle worker polling interval |
| `WEBHOOK_TIMEOUT_SECONDS` | `5` | HTTP request timeout |
| `MAX_DELIVERY_ATTEMPTS` | `5` | Max tries before dead-letter status |
| `RETRY_BASE_SECONDS` | `1` | Initial exponential backoff delay |
| `RETRY_MAX_SECONDS` | `60` | Maximum retry delay |
| `JOB_LEASE_SECONDS` | `30` | Recovery lease for claimed jobs |
| `LOG_LEVEL` | `INFO` | Root logging level |

## 9. Optional Docker Compose run

Docker assets are provided even though local execution is the recommended first run. Copy `.env.example` to `.env`, then from the project folder run:

```powershell
docker compose up --build
```

The Compose stack starts PostgreSQL, the API, watcher, worker and mock webhook receiver. Visit `http://localhost:8000/docs`. Configuration samples are mounted from the local `config_samples` directory, so edits on the host are visible in the watcher container. Stop with `Ctrl+C`; use `docker compose down -v` only when you intentionally want to delete the persisted PostgreSQL volume.

## 10. Design trade-offs and current limits

- **One watcher owner is the intended deployment.** PostgreSQL row locks serialize processing for a file, but multi-instance watcher leadership/partitioning is not implemented.
- **Database-backed jobs avoid a separate broker.** This simplifies local setup and supports durability. At higher throughput, the outbox dispatcher can be adapted to RabbitMQ or Kafka.
- **Arrays are atomic diff values.** Array-index-aware diffs are deliberately omitted to keep the event schema stable.
- **Properties parsing implements a common subset.** Complex Java-properties escape and continuation rules are not fully implemented.
- **Dot-separated key paths can be ambiguous** if a literal mapping key contains a dot. For production, consider JSON Pointer or escaped path segments.
- **Circuit breaker and distributed tracing are not included.** Retry, dead-letter handling, metrics and JSON logs are implemented first; circuit breaker/OpenTelemetry can be added later.
- **Authentication is not implemented on the management API.** Bind it to loopback for local work or put authentication/authorization in front of it before any shared or public deployment.
- **Database schema changes:** `create_all` supports initial bootstrap, not incremental schema migrations. Add Alembic before evolving a production schema.
- **File monitoring is not a security sandbox.** Restrict `CONFIG_ROOT` permissions and do not monitor directories containing files the process should not read.
