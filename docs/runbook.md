# Operations runbook

This runbook covers the **implemented MVP**. The distributed topology, regional failover, durable queues, automatic retention, and HA database in [design.md](design.md) are proposals. They are not available by changing the current replica count.

Examples assume release `health-check`, namespace `health-check`, and `fullnameOverride=health-check-service` as in the README. The application Deployment/Service is `health-check-service`; the PostgreSQL StatefulSet is `health-check-service-postgres`, pod `health-check-service-postgres-0`, and PVC `postgres-data-health-check-service-postgres-0`. Adjust names if the release was installed differently.

## First response and service checks

```sh
kubectl -n health-check get pods,services,pvc
kubectl -n health-check get events --sort-by=.metadata.creationTimestamp
kubectl -n health-check logs deployment/health-check-service --tail=200
kubectl -n health-check logs statefulset/health-check-service-postgres --tail=200
kubectl -n health-check describe pod health-check-service-postgres-0
kubectl -n health-check port-forward service/health-check-service 8000:8000
```

In another terminal:

```sh
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
curl -sS http://127.0.0.1:8000/metrics
curl -sS http://127.0.0.1:8000/endpoints
```

`/healthz` tests that the HTTP process responds. It deliberately does not fail because PostgreSQL is down, avoiding repeated restarts during an external database outage. `/readyz` checks database access and, in the normal runner, the scheduler's liveness/heartbeat; a 503 removes the pod from ready service endpoints. `/metrics` stays available during database failures. It exposes `health_check_database_up=0`; endpoint gauges may then reflect the last successful refresh and must not be treated as current.

When a service has no ready endpoints, `kubectl -n health-check port-forward deployment/health-check-service 8000:8000` can target the application pod directly for diagnosis. For CrashLoopBackOff, inspect the prior container with `kubectl -n health-check logs deployment/health-check-service --previous`. Do not print Secret contents or full environment dumps into incident logs.

## Metrics and alerting

The application exposes these Prometheus metrics plus the client's standard process/runtime metrics:

| Metric | Meaning / operational interpretation |
| --- | --- |
| `health_checks_total{outcome="success\|failure"}` | HTTP observations by outcome; a failed persistence step may prevent history from matching this counter |
| `health_check_duration_seconds` | Histogram of check request duration; includes failed observations |
| `health_check_endpoints{state="unknown\|healthy\|unhealthy"}` | Last refreshed counts of registered endpoints by state |
| `health_check_database_up` | Whether the metrics endpoint's latest database refresh succeeded |
| `health_check_scheduler_in_flight` | Submitted unfinished checks; bounded by the configured worker count |
| `health_check_scheduler_last_poll_timestamp_seconds` | Last successful scheduler poll timestamp |
| `health_check_scheduler_errors_total{stage="load\|check\|submit"}` | Scheduler loading, check execution/persistence, or submission errors |

Metric names are low-cardinality: endpoint IDs and URLs are not labels. Counters reset when the process restarts; use `rate`/`increase`, not differences across restarts. A scrape is not proof that checks are being scheduled. Compare endpoint ages and scheduler activity. Successful HTTP responses are also not proof that their results were persisted if the database is failing.

The chart does not install Prometheus or Alertmanager. Configure an existing Prometheus to scrape the application on port 8000 at `/metrics`, for example every 15 seconds with a scrape timeout above the database statement timeout. For a local Prometheus on the same host as the port-forward:

```yaml
scrape_configs:
  - job_name: health-check-service
    scrape_interval: 15s
    scrape_timeout: 10s
    static_configs:
      - targets: ['127.0.0.1:8000']
```

Inside Kubernetes, use the Service DNS name `health-check-service.health-check.svc:8000` or the cluster's existing discovery configuration. A containerized Prometheus does not reach the host's loopback through its own `127.0.0.1`; use the appropriate host/service address.

Suggested initial alerts (tune to check intervals and workload; these rules are documentation, not installed resources):

```yaml
groups:
  - name: health-check-service
    rules:
      - alert: HealthCheckServiceUnreachable
        expr: up{job="health-check-service"} == 0
        for: 2m
        labels: {severity: critical}
        annotations:
          summary: Health check service cannot be scraped
      - alert: HealthCheckDatabaseUnavailable
        expr: health_check_database_up{job="health-check-service"} == 0
        for: 1m
        labels: {severity: critical}
        annotations:
          summary: Health check service cannot refresh PostgreSQL data
      - alert: HealthCheckSchedulerStalled
        expr: time() - health_check_scheduler_last_poll_timestamp_seconds{job="health-check-service"} > 30
        for: 1m
        labels: {severity: critical}
        annotations:
          summary: Scheduler has not completed a successful poll recently
      - alert: HealthCheckInternalErrors
        expr: sum by (job, instance) (increase(health_check_scheduler_errors_total[5m])) > 0
        for: 2m
        labels: {severity: warning}
        annotations:
          summary: Scheduler or result persistence errors are occurring
      - alert: HealthCheckFailureRatioHigh
        expr: sum by (job, instance) (rate(health_checks_total{outcome="failure"}[5m])) / clamp_min(sum by (job, instance) (rate(health_checks_total[5m])), 0.001) > 0.2
        for: 5m
        labels: {severity: warning}
        annotations:
          summary: More than 20 percent of observed endpoint checks fail
```

The failure-ratio alert is an aggregate diagnostic and can miss a single critical endpoint. Current-state counts are snapshots, not availability percentages. Use endpoint history and per-service requirements for targeted alerting. A missing Prometheus target may disappear from `up` rather than become zero; add an `absent(up{job="health-check-service"})` alert if this job must always exist. Also monitor restart counts, OOM kills, CPU throttling, memory, PVC usage, database connections, long transactions, and backups via Kubernetes/database exporters; the application does not export those database internals.

Useful queries:

```promql
sum(rate(health_checks_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(health_check_duration_seconds_bucket[5m])))
sum(health_check_endpoints{state="unhealthy"})
health_check_scheduler_in_flight
```

Do not mistake a high healthy count for fresh coverage. To inspect staleness in PostgreSQL, run this read-only query with the database client:

```sql
SELECT e.id, e.interval_seconds, e.current_state, max(r.checked_at) AS last_result
FROM endpoints e
LEFT JOIN check_results r ON r.endpoint_id = e.id
WHERE e.enabled
GROUP BY e.id, e.interval_seconds, e.current_state
HAVING max(r.checked_at) IS NULL
    OR max(r.checked_at) < now() - make_interval(secs => 2 * e.interval_seconds::double precision)
ORDER BY last_result NULLS FIRST;
```

For a large history table this diagnostic can be expensive; use it cautiously and move to maintained freshness metrics in the proposed architecture.

## Troubleshooting

| Symptom | Checks and corrective action |
| --- | --- |
| Pod stuck in `Pending` | Inspect events for unavailable CPU/memory, missing/default StorageClass, PVC binding, or volume zone conflicts. Confirm requested storage is provisionable. |
| App init container waiting | Inspect PostgreSQL pod/logs, Service selectors/port, Secret existence, DNS, PVC permissions, and PostgreSQL readiness. `pg_isready` reports server acceptance; it does not prove application authentication. |
| `ImagePullBackOff` | Check image repository/tag/pull policy and registry credentials. For Minikube, rebuild and `minikube image load health-check-service:local`; a host image is not automatically in the cluster runtime. |
| `/healthz` works, `/readyz` is 503 | Check database connectivity/credentials first, then scheduler heartbeat and thread errors. Restart only after addressing the cause; a database outage is not fixed by repeatedly restarting Flask. |
| Registration returns 400 | Send a JSON object, an HTTP(S) URL with host, a positive integer interval, and status 100–599. Booleans are not integers for this API. Private targets require explicit trusted-network opt-in. |
| Registration returns 409 | URL already exists. Use its ID from `GET /endpoints`; deletion removes all prior history. |
| History returns 400 | Use timezone-aware ISO 8601 `from`/`to`, URL-encode `+` offsets, and keep `from <= to`. Database errors instead return 503. |
| Checks fail but API is healthy | Inspect history's error category/status, cluster egress/DNS/TLS, and expected status. A redirect is observed as 3xx; it is not followed. Check target ownership before probing manually. |
| Private/internal endpoint fails | Default destination policy blocks non-public addresses. Enable `ALLOW_PRIVATE_ENDPOINTS=true` (Helm `scheduler.allowPrivateEndpoints=true`) only for trusted callers and authorized destinations; keep API/egress restricted. |
| History stops growing | Check scheduler heartbeat/errors and database availability. In-flight saturation suggests slow endpoints; queueing is bounded, so due endpoints wait for capacity rather than allocating an unbounded backlog. |
| Repeated/duplicate observations | Verify there is only one application process for the database. Check for a local runner, duplicate Helm release, extra replicas, multiple WSGI workers, or manual checker calls. |
| State remains healthy during an outage | State is the last persisted result; inspect its age. Missing checks do not automatically create an unhealthy transition. |
| Database disk fills | Check table/index sizes, WAL/long transactions, PVC capacity, and retention. Take a backup, remove obsolete history in bounded batches, or expand the volume where supported. Never delete `PGDATA` files manually. |
| OOM or CPU throttling | Inspect resource usage and request duration. Reduce worker concurrency or increase measured resource limits; verify database connection capacity before increasing concurrency. |
| Termination exceeds grace | Inspect blocked DNS/network/database operations. HTTP inactivity timeouts are not total deadlines. The runner force-exits unsuccessfully when its own drain grace expires; investigate instead of silently increasing it indefinitely. |

At ten workers, all checks taking five seconds permit approximately two completions/second before database/DNS overhead. A 30-second interval therefore supports roughly 60 such endpoints at full utilization, with no safety margin. Higher capacity requires measurement and headroom; changing one environment variable does not turn this MVP into the proposed distributed system.

## Routine deployment and shutdown

Before an update, run unit/integration tests, Helm lint/render validation, build the image, inspect rendered changes, and take a database backup. Prefer a unique image tag per build; a reused `local` tag with `IfNotPresent` can keep an old image on nodes. The single-instance `Recreate` strategy trades brief API/checking downtime for avoiding overlapping schedulers.

```sh
helm upgrade health-check helm/health-check-service -n health-check \
  --set fullnameOverride=health-check-service \
  --set database.existingSecret=health-check-db \
  --set image.repository=YOUR_REGISTRY/health-check-service \
  --set image.tag=YOUR_IMMUTABLE_BUILD_TAG \
  --wait --timeout 5m
kubectl -n health-check rollout status deployment/health-check-service --timeout=180s
```

Replace registry/tag placeholders before execution and include your protected deployment values file when applicable; do not discard existing custom values accidentally. Check `/readyz`, metrics, a fresh history entry, and expected transitions after rollout. API readiness alone does not verify endpoint traffic.

SIGINT/SIGTERM stops new scheduling, stops accepting normal API traffic, and drains active workers. Kubernetes termination grace must exceed `SHUTDOWN_GRACE_SECONDS` plus server shutdown overhead; the chart uses 60 seconds. A forced kill may lose the current in-memory check; PostgreSQL rolls back incomplete transactions. Restart makes all enabled endpoints eligible again and does not replay missed historical checks.

To pause the entire MVP for maintenance, scale the application to zero (this pauses both API and checks):

```sh
kubectl -n health-check scale deployment/health-check-service --replicas=0
```

After maintenance, restore exactly one replica and verify readiness/history:

```sh
kubectl -n health-check scale deployment/health-check-service --replicas=1
```

There is no endpoint-enable/disable HTTP route. Direct database changes are an administrative operation and should be recorded. Do not delete endpoints simply to pause them, because deletion also removes their evidence/history.

## Backups and restore drills

The bundled PostgreSQL deployment has **no scheduled backups or point-in-time recovery**. A PVC is not a backup. Choose a private durable backup directory outside the repository, restrict access, encrypt/copy backups to independent storage, and regularly restore them. Record the snapshot time, database/image/schema versions, checksum, and retention date.

For a logical custom-format dump (substitute a real directory):

```sh
export BACKUP_DIR=/path/to/private/durable/backups
umask 077
mkdir -p "$BACKUP_DIR"
backup_file="$BACKUP_DIR/healthcheck-$(date -u +%Y%m%dT%H%M%SZ).dump"
kubectl -n health-check exec health-check-service-postgres-0 -- sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_file"
test -s "$backup_file"
shasum -a 256 "$backup_file"
```

Check the exit status of `pg_dump` and the checksum; a nonempty file alone does not prove a valid backup. Do not save these dumps in Git. Use a compatible PostgreSQL client version. `pg_dump` captures a consistent database snapshot but does not include cluster roles; preserve role names/privileges and restore credentials through the Secret system separately.

Practice recovery into a **separate namespace/release and database**, preserving the original PVC. Install an isolated recovery chart with a fresh Secret/PVC, then scale its application to zero. The following assumes namespace `health-check-recovery` and the same `fullnameOverride`; it creates a new `healthcheck_restore` database and never uses `--clean` against the live database:

```sh
kubectl -n health-check-recovery scale deployment/health-check-service --replicas=0
kubectl -n health-check-recovery exec health-check-service-postgres-0 -- sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; exec createdb -U "$POSTGRES_USER" healthcheck_restore'
kubectl -n health-check-recovery exec -i health-check-service-postgres-0 -- sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d healthcheck_restore' \
  < "$backup_file"
kubectl -n health-check-recovery exec health-check-service-postgres-0 -- sh -c \
  'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d healthcheck_restore -c "SELECT count(*) FROM endpoints; SELECT count(*) FROM check_results; SELECT count(*) FROM state_transitions;"'
```

Verify representative history and transitions against expected backup contents. To test the restored application, update the recovery Secret's `POSTGRES_DB` to `healthcheck_restore` while retaining its actual user/password, restart the recovery PostgreSQL pod so its probe environment matches, and scale the recovery application to one. Keep the recovery API isolated and avoid enabling duplicate outbound monitoring of production endpoints during a drill. For an actual cutover, stop the old application, take/restore a final backup if possible, validate, and move the client/service routing. Retain the old volume until the recovery acceptance period has passed.

Recovery-point loss is bounded by the last verified backup for this MVP; there is no automatic WAL archive. Recovery time depends on provisioning and restore duration. Do not promise the proposed production RPO/RTO for this chart.

## Upgrading an original chart installation

The initial chart used fixed resource names such as `health-check-postgres` and PVC `postgres-data-health-check-postgres-0`. The revised chart uses release-scoped names, updated selectors, and a headless database Service. Kubernetes selectors and StatefulSet identity are not safely transformed by an ordinary `helm upgrade`; a new fullname alone does not migrate stored data.

1. Inventory the existing release with `helm list -A`, `helm get values`, and `kubectl get statefulsets,pvc`. Treat rendered values/manifests as sensitive because an older release may contain a password.
2. Record the original PVC name and back up its database. Test the dump in an isolated recovery database.
3. Install the revised chart in a separate namespace with a new externally managed Secret/PVC. Follow the restore procedure above; do not let two applications check the same endpoint set during cutover.
4. Pause the original app, capture the final consistent snapshot, restore it, validate counts/history/transitions, and switch access to the new Service.
5. Keep the original PVC and backup until rollback is no longer required. Never delete the original namespace as a cleanup shortcut while it contains the only copy of the data.

For updates between compatible revisions of the revised chart, use normal Helm upgrades. Application schema initialization does not perform migrations; future schema changes require a planned migration and backup. `helm rollback` rolls back Kubernetes configuration, not database data or incompatible schemas.

## Retention, volume capacity, and database maintenance

History and transitions grow without bound in the MVP. Set an operational retention policy before running it continuously. Inspect storage before choosing a cutoff:

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
```

After a verified backup and agreement on the retention period, a controlled maintenance session can delete old observations in bounded transactions. This example permanently deletes at most 10,000 observations older than seven days per execution; repeat only as needed and monitor latency/WAL/disk:

```sql
WITH expired AS (
  SELECT id FROM check_results
  WHERE checked_at < now() - interval '7 days'
  ORDER BY checked_at, id
  LIMIT 10000
)
DELETE FROM check_results r USING expired e WHERE r.id = e.id;
```

Choose a separate policy for `state_transitions`; preserving transitions longer can retain useful incident boundaries. Deleting old history does not change `endpoints.current_state`. Run ordinary `VACUUM (ANALYZE)` as appropriate after maintenance; it makes space reusable, not necessarily returned to the filesystem. Avoid `VACUUM FULL` during normal operation because it needs extra space and an exclusive lock. Time partitioning and automated drop/archive are proposed for production, not implemented here.

For expansion, inspect the StorageClass's `allowVolumeExpansion` and CSI support, take a backup, then expand the existing PVC using your cluster's supported procedure. Increasing `database.storage` in Helm values alone may fail against immutable StatefulSet volume-claim templates and does not reliably resize an existing claim. Never replace the claim with an empty volume to solve a full disk.

## Secret rotation and access

The recommended `database.existingSecret` contains `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`. Kubernetes Secrets are not encrypted merely because their representation is base64; use RBAC and encryption at rest. Keep the application Service private and apply environment-appropriate ingress/egress controls. The chart does not install an authentication proxy or NetworkPolicy.

The demo shares PostgreSQL's bootstrap role with the application; the official image creates that role with superuser privileges. Before production use, separate the administrative/migration role from a restricted runtime role and grant only the required schema/table privileges. The current startup schema initialization and bundled Secret wiring are an MVP convenience, not a least-privilege production database setup.

For password rotation, schedule a short maintenance window: pause the app, change the actual PostgreSQL role password using a secure interactive/admin mechanism, update the external Secret through your secret-management workflow, restart PostgreSQL/application pods to refresh environment variables, resume one app replica, and verify `/readyz` plus fresh checks. Changing `POSTGRES_PASSWORD` on an initialized PostgreSQL volume **does not** change that role's stored password. Preserve the prior credential securely until successful validation, then revoke it. Do not pass real passwords through committed values files, shell history, screenshots, or support logs.

## Recovery checklist and completion evidence

Before closing an incident or submitting the take-home, record the observed cause and timestamps, exact software/image revisions, commands actually executed, test results/skips, backup/restore evidence where relevant, and remaining limitations. Confirm one scheduler instance, healthy readiness, fresh results, correct transitions, successful metrics scraping, bounded worker/resource use, and adequate PVC space. A clean Helm render and a successful image build are useful checks; neither establishes operational readiness by itself.
