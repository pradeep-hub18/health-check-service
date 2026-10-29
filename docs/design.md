# Design: MVP and a three-region service

## Scope and status

The repository implements a **single-instance MVP**: a Flask API, a concurrent in-process scheduler, PostgreSQL history/state, Prometheus instrumentation, a container image, and a Helm deployment. It does not use the Temporal SDK or a Temporal cluster; “Temporal Platform Team” identifies the take-home context.

Everything under **Proposed production architecture** below is a design proposal, not functionality delivered by this repository. In particular, there is no distributed scheduler, durable queue, regional replication, automatic retention, authentication, migration framework, autoscaling, or PostgreSQL HA in the MVP.

## Implemented MVP

```text
Operator / API client ──HTTP──> Flask API ───────────> PostgreSQL
                                  │                  endpoints
                                  │                  check_results
                         one scheduler thread        state_transitions
                                  │                       ▲
                         bounded thread executor          │
                                  │                       │
                         HTTP(S) status checks ──atomic result/state transaction

Prometheus ──scrape /metrics──> the same application process
```

Endpoint registration stores a URL, a positive interval, an expected HTTP status, and an initial `unknown` state. The scheduler polls enabled endpoint configuration and dispatches due checks without concurrent checks of the same endpoint. The number of submitted, unfinished checks is bounded by the configured worker count. Scheduling state lives in process memory. Restarting loses that state; enabled endpoints become eligible again and missed intervals are not replayed.

A check compares the first response's status with the configured status. It does not follow redirects or download response bodies. Network/request failures produce an unsuccessful observation. Persistence locks the endpoint row, inserts the observation, updates current state, and inserts a transition only if state changed, in one transaction. The initial `unknown → healthy/unhealthy` observation is a transition. Deletion cascades to history and transitions; a check whose endpoint disappears before persistence cannot recreate it. A database failure cannot partially commit a state change.

The API exposes recent history and transitions (100 rows per response); history supports inclusive time bounds. PostgreSQL indexes support endpoint/time lookups. Tables are created at startup with `CREATE TABLE IF NOT EXISTS`; this is initialization, **not schema migration**. The API and scheduler open short-lived database connections. There is no connection pool, history partitioning, retention job, or durable work backlog.

One application process must own each database's schedule. The chart enforces one application replica and a `Recreate` update strategy. Multiple standalone processes or separate releases pointed at the same database remain unsafe: the in-memory exclusion set does not coordinate them. The bundled single PostgreSQL StatefulSet and PVC offer persistence across ordinary pod replacement, not high availability or backup.

The API has no authentication. Its deployment is intended for a trusted local or cluster network. Public HTTP(S) destinations are the default; internal destinations require an explicit operator opt-in. The checker resolves and validates all returned addresses, then pins one address while preserving the original HTTP Host header and HTTPS hostname verification. This protects the check execution boundary but does not substitute for tenant authorization, an egress firewall, or admission rate limits.

The normal runner uses Werkzeug's threaded HTTP server and handles SIGINT/SIGTERM by stopping dispatch and draining active checks. Its default 25-second drain grace ends with an unsuccessful forced exit if workers remain blocked. Requests uses separate connect/read inactivity timeouts; these and OS DNS are not a strict total request deadline. This is a documented MVP limit, not the proposed 5-second end-to-end production deadline below.

## Proposed production architecture — not implemented

### Requirements and assumptions

Assume **50,000 distinct endpoints are checked from each of three regions every 30 seconds**, so each endpoint receives three regional observations per interval. This measures regional reachability rather than merely distributing workload. If the intended requirement instead assigns each endpoint to only one region, global load and storage are one third of the estimates below, with weaker regional coverage.

Proposed service objectives, to validate with load tests:

- 99.9% of enabled endpoint/region slots start within 5 seconds of their scheduled time under the supported load.
- 99.9% of completed observations become queryable within 10 seconds; the API always reports observation age and region.
- Missing observations are `unknown/stale`, never proof that an endpoint is healthy or unhealthy.
- Network timeout is an explicit 5-second **total deadline**, including DNS, connect, TLS, and headers. No body collection by default. This is stronger than a library's separate connect/read timeouts.
- Survive one worker or availability-zone loss without manual resharding. A whole-region outage reduces coverage and raises a monitoring-service alert; it is not an endpoint failure.
- Provisional recovery targets: regional data-store RPO ≤ 5 minutes and RTO ≤ 30 minutes, backed by WAL archiving and exercised restore/failover tests. These are design targets, not current guarantees.

### Capacity and cost model

| Quantity | Per region | Three regions |
| --- | ---: | ---: |
| Endpoint checks per 30-second interval | 50,000 | 150,000 |
| Average starts/results per second | 1,666.7 | 5,000 |
| Observations per day | 144,000,000 | 432,000,000 |
| Concurrent requests at 200 ms mean service time | 334 | 1,000 |
| Concurrent requests if every request reaches 5 seconds | 8,334 | 25,000 |
| Raw+index budget at 300 bytes/observation/day | 43.2 GB | 129.6 GB |

Concurrency follows `in_flight = arrival_rate × mean_time_in_system` (Little's law). The 200 ms value is an assumption, not a benchmark; a 1-second mean needs about 1,667 slots per region. The all-timeout case matters because endpoint incidents must not stop the monitor. Add headroom for DNS/TLS, result publication, scheduler jitter, and zone failures. CPU, network, file-descriptor, and NAT limits must be measured separately from slot count.

An initial timeout-resilient allocation is **18 async worker pods per region, 1,000 request slots each**, spread six per availability zone. Normal full-timeout occupancy is about 46%; losing one zone leaves 12,000 slots for 8,334 requests, about 69% occupancy. This is a test starting point, not an assertion that one pod can sustain 1,000 TLS connections at a specific CPU/memory allocation. Benchmark DNS fan-out, TLS handshakes, response headers, per-request memory, open FDs, conntrack/NAT ports, and downstream limits before selecting pod requests/limits.

At an assumed 5 KB of total traffic per check including headers and amortized TLS, traffic is roughly 8.3 MB/s per region (25 MB/s globally; 2.16 TB/day). Cold TLS handshakes and large headers can increase this substantially. Enforce header-size limits and avoid response bodies. A 500-byte result event is about 2.5 MB/s globally before broker replication and protocol overhead.

The 300-byte database estimate includes an illustrative row/index allowance, but excludes WAL, bloat, backups, and replicas. Seven days of raw observations is **907.2 GB globally** before those overheads. A 1.5× operational allowance makes about 1.36 TB of primary capacity; a primary plus two replicas makes about 4.08 TB, plus archive/backup storage. Measure actual `pg_total_relation_size` growth at production schemas and revise these numbers. One GiB in the demo chart is deliberately unsuitable for this workload.

### Regional topology

```text
Authenticated global API / routing layer
            │
            ├── Configuration primary + transactional change outbox
            │                  └── versioned regional configuration streams
            │
            ├── Region A: three availability zones
            │     schedulers + shard leases → durable check-job broker
            │                                 ↓
            │                          async probe workers
            │                                 ↓
            │                          durable result broker
            │                                 ↓
            │                         batch ingest + state reducers
            │                                 ↓
            │                      regional PostgreSQL shards / replicas
            ├── Region B: same independent data plane
            └── Region C: same independent data plane

Regional query APIs → bounded fan-out/aggregation → global API
Raw archive / rollups → object storage; regional Prometheus → global dashboards
```

Keep execution and result persistence inside each region, avoiding a cross-region database transaction on every check. Use managed multi-AZ PostgreSQL or an operated HA system with automated promotion, backups, and fencing. Broker replication spans the three local zones. Separate API, scheduler, probe, and ingester deployments so their load and failures can be managed independently. Use topology spread constraints, disruption budgets, capacity reservations, and rolling deployments after distributed ownership exists.

The configuration control plane has a single writer per tenant or shard, replicates versioned updates through an outbox, and supports idempotent registration/deletion. Each region keeps a last-known-good configuration snapshot and continues checking during control-plane outages. A deletion creates a versioned tombstone; workers and ingesters reject stale configuration generations. Publish configuration lag and indicate when API writes have not yet reached all regions.

### Distributed scheduling, sharding, and ownership

Use 1,024 stable virtual scheduling shards **per region**, mapped by `hash(endpoint_id) mod 1024`. Each owns about 49 endpoints on average, but allocation should consider observed execution cost and tenant/host concentration. Virtual shards permit moving work without changing endpoint identity. Rendezvous hashing maps shards to scheduler instances; durable leases determine actual authority.

Lease records contain `(region, shard_id, owner_id, lease_until, epoch)`. Acquisition/renewal uses a database transaction and database time, with compare-and-swap on the current owner/epoch. A new owner increments a monotonically increasing fencing epoch. Use a 15-second lease renewed every 5 seconds as an initial setting; stop publishing before local lease uncertainty expires. Every job carries its epoch. Consumers reject jobs from obsolete owners, and the scheduling outbox records uniqueness by `(region, endpoint_id, scheduled_slot, config_version)`.

A bounded `FOR UPDATE SKIP LOCKED` claim of due shards or outbox rows can coordinate schedulers without serializing them on one row. It is suitable for a queue-style claim operation, not a general consistent read of business state. PostgreSQL documents this distinction in its [SELECT locking reference](https://www.postgresql.org/docs/current/sql-select.html).

Within an owned shard, keep a timing wheel or min-heap of next due slots. Assign a stable jitter offset `hash(endpoint_id, region) mod 30 seconds`, distributing 50,000 checks across the interval. Align subsequent due times to the slot schedule rather than completion time to prevent drift. Do not scan all endpoints every second or issue one database timer update per endpoint on every poll. Persist configuration, shard progress, and the durable dispatch outbox; publish jobs in batches with broker acknowledgements.

Check jobs are delivered **at least once**. The job ID identifies one endpoint/region/scheduled slot; a worker may claim a short execution lease, but a crash after sending the external request can still cause a duplicate. Exactly-once outbound HTTP is not achievable with this protocol. Use idempotent result insertion and a reducer keyed by the observation slot to prevent duplicate history/state effects. Fence stale epochs, but do not drop valid already-completed results solely because shard ownership changed: validate the slot's accepted claim/generation. Keep a bounded deduplication horizon longer than maximum queue retention and retry time.

On failover, recover the shard's due schedule and re-drive the outbox. If a slot is more than one interval late, record a missed/expired slot rather than sending a burst of every missed historical check. Re-enter the current schedule with jitter. A fresh network request cannot reconstruct an observation for an earlier time.

### Worker concurrency, fairness, and backpressure

Use async HTTP/DNS clients with a process-wide semaphore, bounded broker prefetch, and explicit total deadlines. Do not allocate one thread per endpoint. Maintain one active check per endpoint/region where practical; expire queued jobs whose slot deadline has passed. Cap concurrency and request rate per tenant and destination host/IP to avoid a single tenant or common upstream exhausting the fleet or causing a denial of service.

Scale workers on scheduling lag, queue age, active-slot utilization, timeout rate, and CPU/memory, not CPU alone. Reserve enough capacity for widespread timeouts and zone loss before relying on autoscaler reaction. A burst of slow endpoints fills slots even when CPU is low. Apply bounded, jittered retries only for internal infrastructure operations (broker/database); an HTTP timeout is an observation, not grounds for unlimited immediate endpoint retries. Never bypass configured check frequency while recovering backlog.

If persistence slows, the durable result broker buffers observations with an explicit retention/size budget. Start with six hours of backlog (108 million events globally; about 54 GB at 500 bytes before replication), alert well before exhaustion, and define the action at the limit: stop accepting new configuration and drop expired jobs with explicit missed-slot counters. Do not let memory grow unbounded or silently label absent observations as successful.

### PostgreSQL write path and query scale

Keep the small configuration/current-state workload separate from append-heavy observations. Start with four regional observation database shards per region, mapping endpoint IDs consistently. That is about 417 result rows/s per shard on average; the count is a conservative operational partitioning choice and must be validated against real row size, WAL, indexes, replicas, and retention jobs. A capable single primary may handle more, but storage, maintenance windows, and failure blast radius can motivate sharding before raw insert throughput does.

Batch 100–1,000 result rows or flush within 100 ms, whichever comes first. Use a bounded PgBouncer/application pool (for example, 10 ingester connections per primary initially), multi-row inserts or `COPY` into staging, then idempotent upsert into the destination. Avoid opening two database connections and multiple transactions per probe as the MVP does. Broker offsets are committed only after the database transaction succeeds; duplicate replay remains safe.

Store `endpoint_id`, `region`, `scheduled_at`, `started_at`, `finished_at`, `config_version`, `job_id`, outcome/status, duration, and a bounded error category. Deduplication must include the time-partition key if implemented as a unique constraint on a partitioned PostgreSQL table. Route each job to a single deterministic shard. State reducers process `(endpoint_id, region)` in slot order, update current state only for a newer accepted slot, and atomically create actual transitions. Late results remain history without rewinding current state. Optionally use consecutive-failure/recovery thresholds; document their detection-delay tradeoff.

Partition raw history by UTC time (hourly or daily according to measured partition size) within each database shard. Keep an index on `(endpoint_id, scheduled_at DESC)` and limited supporting indexes; avoid indexing every error field. Build upcoming partitions ahead of time, monitor the default partition, tune autovacuum on mutable state tables, and track WAL throughput, replication lag, checkpoints, bloat, and slow queries. PostgreSQL supports partition pruning and partition detach/drop for retention; the [partitioning documentation](https://www.postgresql.org/docs/current/ddl-partitioning.html) explains the operational benefits and limitations.

Serve bounded recent-history queries from regional read replicas with keyset pagination and required time ranges. Use primaries where read-after-write is required. Keep raw history off the configuration transaction path. A global API fans out to the relevant three regional shards with timeouts and returns partial results plus explicit missing-region metadata. Never conceal stale replicas behind a “healthy” aggregate.

### Retention and aggregation

Proposed defaults require product/cost agreement: seven days of raw history in PostgreSQL, 30 days of one-minute rollups, 13 months of hourly rollups and state transitions, and optional compressed raw object-storage archives. Rollups preserve success/failure/timeout/missing counts and latency distributions; averages alone cannot answer percentile questions. Three regions × 50,000 endpoints × 1,440 minute buckets is 216 million minute rows/day, so per-endpoint minute rollups only halve the raw row count. Prefer hourly or tenant-level rollups where that resolution meets the use case; minute retention is a substantial explicit cost.

Archive/checksum a closed partition, verify ingestion completeness and rollup watermarks, then detach/drop it after its retention cutoff. Retention is based on scheduled event time with a bounded late-arrival window. Monitor cleanup lag and storage forecasting. Do not use a giant daily `DELETE` over hundreds of millions of rows. Backups and object archives have independent retention/lifecycle rules, including deletion-policy compliance.

### Failure handling and recovery

| Failure | Proposed behavior |
| --- | --- |
| Worker crash | Lease/ack timeout redelivers job; result dedup prevents duplicate state effects. Duplicate HTTP remains possible. |
| Scheduler crash or partition | Lease expires, epoch fences former owner, another scheduler resumes current slots. |
| PostgreSQL failover | Ingester retries with jitter; result broker absorbs backlog; idempotent replay resumes after promotion. |
| Broker outage | Schedulers retain dispatch outbox; workers stop prefetching; expire stale slots when service returns. |
| Region outage | Other regions continue their own vantage checks; mark failed region's observations stale and alert coverage loss. |
| Control-plane outage | Existing versioned regional configuration continues; configuration writes fail or queue with explicit status. |
| Corrupt/deleted data | Restore isolated backup plus WAL, validate consistency, then controlled cutover; reconcile broker replay window. |
| Clock skew | NTP monitoring; database time for leases; monotonic timers locally; UTC slot IDs and skew rejection at ingestion. |

Do not move a region's checks elsewhere and label them as observations from the failed region. Extra checks from healthy regions may support diagnosis, but must retain their actual vantage point. Disaster recovery of a region uses a tested configuration snapshot, fresh leases/epochs, empty current-slot queues, and restored data with known RPO. Control-plane promotion must fence the old writer to avoid two configuration authorities.

### Security, observability, and rollout

Authenticate and authorize API clients; enforce tenant quotas, audit configuration changes, encrypt database/broker links, and use a secret manager with short-lived credentials where available. Isolate probe workers from control-plane credentials. Validate destinations before each connection, pin the validated resolved address while preserving TLS hostname validation, disable automatic redirects and proxy inheritance, block metadata/private/reserved networks unless an explicit tenant policy permits them, and enforce those restrictions again at the egress layer. Internal monitoring requires a separate, authorized worker pool rather than granting arbitrary callers internal access.

Export low-cardinality metrics for region/shard group/outcome: schedule lateness, expired slots, lease contention/renewal, active work, DNS/connect/TLS/header latency, result age, queue backlog, duplicate/stale results, ingestion failures, database connections/WAL/replication lag, and retention progress. Put individual endpoint IDs/URLs in access-controlled history or sampled structured logs, not unbounded Prometheus labels; see [Prometheus instrumentation guidance](https://prometheus.io/docs/practices/instrumentation/). Keep monitoring infrastructure failure alerts distinct from customer endpoint failure alerts.

Roll out in stages: establish deterministic unit/database tests and load fixtures; measure the MVP; separate API and scheduler; introduce durable slot identity/leases/outbox; add regional workers and result ingestion; add partitions/retention; then exercise multi-AZ and region failure drills. Replay synthetic workloads with slow DNS, timeouts, oversized headers, duplicate jobs, stale owners, database loss, and deletion races. Require a 24-hour 2× steady-load soak and an all-timeout/zone-loss scenario before claiming the 50,000-endpoint SLO. The repository's tests and chart validation do not establish that production capacity.
