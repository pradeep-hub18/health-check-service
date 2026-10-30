# Health Check Service

A Temporal Platform Team take-home project: register HTTP(S) endpoints, check their expected status on a configurable interval, and query recent results and health transitions. The implementation uses Flask, PostgreSQL, a concurrent in-process scheduler, Prometheus, Docker, and Helm. It does not require a Temporal cluster.

## Architecture and scope

```text
API client → Flask :8000 → PostgreSQL endpoints / history / transitions
                │                         ↑
        one scheduler thread → bounded check workers → HTTP(S) endpoints
                │
Prometheus ← /metrics
```

`python3 run.py` starts one API process and one scheduler. The scheduler polls enabled endpoints, limits unfinished work to its worker count, and prevents overlapping checks of the same endpoint within that process. Results, current state, and actual transitions are saved atomically. Deleting an endpoint also deletes its history and transitions.

PostgreSQL is used because endpoint configuration, current state, check history, and transitions are related records that benefit from foreign keys, indexed time-range queries, and transactions. The checker commits each result, current state, and any state transition atomically, while cascading foreign keys make endpoint deletion consistent. PostgreSQL is also a mature open-source datastore with an official container image that fits the local Docker and Helm stack. The MVP uses one database instance and creates its schema at startup; migrations, high availability, connection pooling, and automated retention are production concerns covered in the design proposal.


This is an MVP for a trusted local or cluster network. It has no API authentication, tenant isolation, distributed scheduler, database HA, automatic retention, or schema migration framework. Run **one application instance per database**. The Helm chart enforces one replica and uses `Recreate`, so upgrades include brief downtime. The three-region, 50,000-endpoint design is a **proposal**, described in [docs/design.md](docs/design.md); it is not a capacity claim for this implementation.

## Prerequisites

- Python 3.12 with a supported OpenSSL build for local development; the Docker image uses the tested Python 3.12 runtime. Use Docker if the host only provides the older macOS Python 3.9/LibreSSL environment.
- For integration tests, the `openssl` CLI and permission to bind loopback sockets. The HTTPS transport test generates temporary certificates; PostgreSQL integration fixtures also bind loopback HTTP sockets.
- PostgreSQL 16, locally installed or in Docker. `psql`, `pg_dump`, and `pg_restore` are useful for operations.
- Docker Engine/Desktop for container builds and the database examples.
- For Kubernetes: `kubectl`, Helm 3 or 4, and a cluster with a default dynamic StorageClass. Minikube is sufficient for the demo.
- Outbound DNS and HTTP(S) access to registered endpoints; trusted access to the API.

All commands below run from the repository root and use `python3`. Do not commit passwords, database dumps, generated Secret YAML, or local environment files.

## Automated local build and validation

Run one command to build the image, validate the project, create a local Minikube deployment, and verify the live service:

```sh
bash scripts/validate-local.sh
```

The script runs the Python test suite and dependency audit, builds the application image, validates the Helm chart, creates a dedicated Minikube profile and Kubernetes Secret, and installs the application with PostgreSQL. It checks readiness and liveness, registers `https://example.com`, waits for a scheduled result, checks history, transitions, and Prometheus metrics, then uninstalls and reinstalls the Helm release to confirm the PostgreSQL PVC and successful check history persist.

Docker Engine must already be running and accessible. The host also needs Bash, `curl`, `python3`, and internet access for downloads and the live endpoint check. The script supports macOS, Linux on x86_64 or ARM64, and Linux under WSL2; it installs missing `kubectl`, Helm, and Minikube using Homebrew on macOS or `~/.local/bin` on Linux/WSL2. Native PowerShell and Git Bash are not supported.

After a successful run, the reviewer Minikube profile, namespace, pods, PostgreSQL PVC, Secret, endpoint, and history stay available for inspection. The script prints commands to inspect them. On failure it prints available diagnostics and removes resources it created. Pass `--cleanup` when you want the script to tear down the reviewer environment after validation. A normal successful run keeps the reviewer resources and prints Minikube profile and state directory commands for removing them later.

This script automates the Docker, test database, Minikube, and Helm setup described in the manual sections below. Use those steps when you want to configure each component separately.
## Local development

Create the environment and install runtime and test dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

For a new disposable development database, generate a password in the shell and start PostgreSQL. The named volume preserves its data when the container is recreated; the port is bound to loopback only.

```sh
export DB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export POSTGRES_PASSWORD="$DB_PASSWORD"
docker network inspect healthcheck-net >/dev/null 2>&1 || docker network create healthcheck-net
docker run -d --name healthcheck-db --network healthcheck-net \
  -p 127.0.0.1:5432:5432 \
  -e POSTGRES_DB=healthcheck -e POSTGRES_USER=healthapp -e POSTGRES_PASSWORD \
  -v healthcheck-pgdata:/var/lib/postgresql/data postgres:16
docker exec healthcheck-db pg_isready -U healthapp -d healthcheck
```

Wait until `pg_isready` reports accepting connections. For an existing PostgreSQL instance, create/use a dedicated database and role, and export that role's password instead. Reusing an initialized PostgreSQL volume requires its existing credentials; changing `POSTGRES_PASSWORD` does not change the stored database password.

```sh
export DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=healthcheck DB_USER=healthapp
python3 run.py
```

The API listens on port 8000. In another terminal:

```sh
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Stop with Ctrl-C or SIGTERM. Shutdown stops new scheduling and drains active checks; the process exits unsuccessfully if its shutdown grace expires. The runner uses Werkzeug's threaded server for the take-home MVP. Do not use multiple WSGI workers or Flask's auto-reloader: they do not provide safe distributed scheduling. Importing the Flask app factory by itself does not start the scheduler.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `healthcheck` | Database name |
| `DB_USER` | `healthapp` | Database role |
| `DB_PASSWORD` | None | Supply securely; there is no embedded password |
| `PORT` | `8000` | Local runner HTTP port; the chart uses 8000 |
| `SCHEDULER_MAX_WORKERS` | `10` | Maximum unfinished checks in this process |
| `CHECK_TIMEOUT_SECONDS` | `5` | HTTP connect/read inactivity timeout |
| `ALLOW_PRIVATE_ENDPOINTS` | `false` | Explicitly permit private/internal monitoring targets |
| `SHUTDOWN_GRACE_SECONDS` | `25` | Maximum scheduler drain time before forced unsuccessful exit |

Database operations use bounded connection, statement, and lock timeouts. The HTTP timeout is not a strict end-to-end deadline: OS DNS and slow incremental response headers can take longer. Shutdown grace provides a final process-level bound; see the [runbook](docs/runbook.md).

The service accepts only valid HTTP(S) URLs without embedded credentials. By default, resolved addresses must be public, and execution pins the validated address while preserving HTTPS hostname verification. Redirects and environment proxy inheritance are disabled, and response bodies are not downloaded. Use `ALLOW_PRIVATE_ENDPOINTS=true` only when trusted callers are authorized to probe that network. The flag broadens access; it is not an endpoint allowlist. Do not put credentials in URL query strings or expose this unauthenticated API publicly.

## Docker

Build the application image:

```sh
docker build -t health-check-service:local .
```

With `healthcheck-db` running on `healthcheck-net` as above and `DB_PASSWORD` exported:

```sh
docker run --rm --name healthcheck-app --network healthcheck-net \
  -p 127.0.0.1:8000:8000 \
  -e DB_HOST=healthcheck-db -e DB_PORT=5432 \
  -e DB_NAME=healthcheck -e DB_USER=healthapp -e DB_PASSWORD \
  health-check-service:local
```

The application image runs as a non-root user. Stop it from another terminal with `docker stop --time 60 healthcheck-app`, allowing the application's drain period to finish. Keep credentials outside the image and build context.

## Minikube and Helm

For the automated image build, Helm validation, Minikube deployment, and live smoke test, use [the validation script](scripts/validate-local.sh). The manual steps below are for configuring the cluster yourself.

These commands install a **new release**. If an older version of this chart is already installed, read [the migration procedure](docs/runbook.md#upgrading-an-original-chart-installation) first: resource names/selectors have changed, and the old PostgreSQL PVC must be preserved.

```sh
minikube start --driver=docker
docker build -t health-check-service:local .
minikube image load health-check-service:local
kubectl create namespace health-check --dry-run=client -o yaml | kubectl apply -f -
```

Create an externally managed database Secret with all three required keys. This avoids storing the password in Helm values or a rendered release manifest. For a new installation, generate `DB_PASSWORD` as in the local example. For an existing database, use its actual password. The temporary directory is outside the repository; remove it immediately after Secret creation.

```sh
: "${DB_PASSWORD:?Export the database password first}"
secret_dir="$(mktemp -d)"
chmod 700 "$secret_dir"
umask 077
printf '%s' healthcheck > "$secret_dir/POSTGRES_DB"
printf '%s' healthapp > "$secret_dir/POSTGRES_USER"
printf '%s' "$DB_PASSWORD" > "$secret_dir/POSTGRES_PASSWORD"
kubectl -n health-check create secret generic health-check-db \
  --from-file="$secret_dir" --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$secret_dir"

helm lint --strict helm/health-check-service \
  --set database.existingSecret=health-check-db
helm template health-check helm/health-check-service \
  --namespace health-check \
  --set fullnameOverride=health-check-service \
  --set database.existingSecret=health-check-db
helm upgrade --install health-check helm/health-check-service \
  --namespace health-check \
  --set fullnameOverride=health-check-service \
  --set database.existingSecret=health-check-db \
  --wait --timeout 5m
kubectl -n health-check get pods,services,pvc
kubectl -n health-check port-forward service/health-check-service 8000:8000
```

Use the same API examples below through the port-forward. `ClusterIP` is the default; no public ingress is created. Optional NodePort access can be enabled with `--set service.type=NodePort --set service.nodePort=30080`, then inspected with `minikube service health-check-service -n health-check --url`. Cluster firewall/access controls still matter because the API has no authentication.

`fullnameOverride=health-check-service` gives consistent names in these examples. Without it, names include the release name. The database pod is `health-check-service-postgres-0`; its retained claim is `postgres-data-health-check-service-postgres-0`. Choose `database.storageClass` and `database.storage` to suit the cluster. The default 1 GiB is for a demo, and there is no automatic history cleanup. Supply `database.existingSecret` for normal deployments; `database.password` is an alternative for a protected local values file, with the tradeoff that Helm stores it in release data.

Application and database resources, probes, security contexts, and timing settings are defined in [values.yaml](helm/health-check-service/values.yaml). The chart has no Prometheus server, ServiceMonitor, alert rules, backup operator, or HA PostgreSQL dependency. Helm uninstall leaves the StatefulSet's PVC intact; do not delete the namespace or PVC unless you intend to delete the data.

## API examples

Set the base URL and register an endpoint that you are authorized to check:

```sh
export BASE_URL=http://127.0.0.1:8000
curl -sS -X POST "$BASE_URL/endpoints" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","interval_seconds":30,"expected_status_code":200}'
```

A successful registration returns HTTP 201 and the endpoint, including its assigned `id` and initial `current_state: "unknown"`. Duplicate URLs return 409. Replace `123` below with that returned ID; wait for scheduling before expecting history.

Representative JSON responses (IDs and timestamps are illustrative):

```http
HTTP/1.1 201 Created
Content-Type: application/json

{
  "id": 123,
  "url": "https://example.com",
  "interval_seconds": 30,
  "expected_status_code": 200,
  "enabled": true,
  "current_state": "unknown",
  "created_at": "Fri, 30 Oct 2026 06:00:00 GMT"
}
```

After a check has run, `GET /endpoints/123/history` returns the newest observations:

```json
{
  "endpoint_id": 123,
  "count": 1,
  "history": [
    {
      "id": 456,
      "endpoint_id": 123,
      "checked_at": "Fri, 30 Oct 2026 06:00:30 GMT",
      "success": true,
      "actual_status_code": 200,
      "response_time_ms": 42.7,
      "error_message": null
    }
  ]
}
```

`GET /endpoints/123/transitions` records only state changes:

```json
{
  "endpoint_id": 123,
  "count": 1,
  "transitions": [
    {
      "id": 789,
      "endpoint_id": 123,
      "from_state": "unknown",
      "to_state": "healthy",
      "transitioned_at": "Fri, 30 Oct 2026 06:00:30 GMT"
    }
  ]
}
```

Errors also return JSON, for example `HTTP 409` for an already registered URL:

```json
{"error": "This URL is already registered"}
```


```sh
curl -sS "$BASE_URL/"
curl -sS "$BASE_URL/endpoints"
curl -sS "$BASE_URL/endpoints/123/history"
curl -sS --get "$BASE_URL/endpoints/123/history" \
  --data-urlencode 'from=2026-10-29T00:00:00Z' \
  --data-urlencode 'to=2026-10-30T00:00:00Z'
curl -sS "$BASE_URL/endpoints/123/transitions"
curl -sS "$BASE_URL/metrics"
curl -sS -X DELETE "$BASE_URL/endpoints/123"
```

| Route | Behavior |
| --- | --- |
| `GET /` | Service information |
| `GET /healthz` | Process/API liveness; does not require the database |
| `GET /readyz` | Database readiness and scheduler health when started by the runner; 503 if unready |
| `POST /endpoints` | Register `url` (up to 2,048 characters), integer `interval_seconds` (1–2,147,483,647), optional `expected_status_code` (default 200, range 100–599) |
| `GET /endpoints` | All registered endpoints, ordered by ID |
| `DELETE /endpoints/{id}` | Delete endpoint and its results/transitions; 404 if missing |
| `GET /endpoints/{id}/history` | Up to 100 newest results, with optional inclusive `from`/`to` ISO 8601 timestamps containing a timezone |
| `GET /endpoints/{id}/transitions` | Up to 100 newest actual state changes |
| `GET /metrics` | Prometheus exposition, including database/scheduler signals |

Invalid JSON, URLs, intervals, status codes, timestamps, or reversed time ranges return 400; bodies over 16 KiB return 413. Database failures return 503 on data APIs; metrics remain available with `health_check_database_up=0` and may retain stale endpoint counts. A healthy endpoint is one whose observed status equals its configured status; for example, a 301 is healthy only when 301 is expected. Redirect targets are never followed. No manual-check HTTP route exists.

## Tests and validation

The [validation script](scripts/validate-local.sh) runs the suite, dependency audit, Docker build, Helm checks, and retained Minikube smoke test in one command.

Unit tests mock HTTP/database boundaries and require neither Internet access nor a running database:

```sh
python3 -m pytest -q -m 'not integration'
```

Integration tests use real PostgreSQL in an isolated temporary schema and deterministic local HTTP fixtures. A separate integration test verifies actual HTTPS certificate/hostname checking with a local TLS server; it requires `openssl` and skips if that CLI is unavailable. These tests need permission to bind loopback sockets, but no Internet access. Set `TEST_DATABASE_DSN` explicitly to a **disposable test database**; never point it at production. Without it, the PostgreSQL tests skip (the TLS test can still run). For example, start a separate test database using the exported `POSTGRES_PASSWORD` from above:

```sh
docker run -d --name healthcheck-test-db \
  -p 127.0.0.1:55432:5432 \
  -e POSTGRES_DB=healthcheck_test -e POSTGRES_USER=healthapp -e POSTGRES_PASSWORD \
  postgres:16
docker exec healthcheck-test-db pg_isready -U healthapp -d healthcheck_test
export TEST_DATABASE_DSN="host=127.0.0.1 port=55432 dbname=healthcheck_test user=healthapp password=$DB_PASSWORD"
python3 -m pytest -q -m integration
python3 -m pytest -q
docker rm -f healthcheck-test-db
```

Wait for database readiness before running tests. The test fixture removes its temporary schema after execution. The final command removes only the named disposable test container.

Validate the chart and image independently:

```sh
helm lint --strict helm/health-check-service --set database.existingSecret=health-check-db
helm template health-check helm/health-check-service \
  --set database.existingSecret=health-check-db | kubectl apply --dry-run=client -f -
docker build -t health-check-service:local .
```

`helm template` does not prove that Kubernetes accepts or runs the resources. `kubectl` validation may require a reachable cluster for API discovery; server-side dry-run and an actual install/rollout provide stronger checks. A Docker build does not demonstrate runtime database connectivity or shutdown behavior. Record exactly which commands ran and any skips/failures when submitting; do not treat an unexecuted command as a passing check.

See [docs/runbook.md](docs/runbook.md) for troubleshooting, alerts, backups, recovery, retention, and upgrade procedures.

Executed checks and remaining scope limits are recorded in [docs/validation.md](docs/validation.md).
