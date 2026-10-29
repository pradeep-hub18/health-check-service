
from prometheus_client import Counter, Gauge, Histogram

# Total number of health checks, grouped by outcome.
HEALTH_CHECKS_TOTAL = Counter(
    "health_checks_total",
    "Total number of endpoint health checks",
    ["outcome"],
)

# Duration of health checks in seconds.
HEALTH_CHECK_DURATION_SECONDS = Histogram(
    "health_check_duration_seconds",
    "Duration of endpoint health checks in seconds",
)

# Current endpoint state counts.
ENDPOINTS_TOTAL = Gauge(
    "health_check_endpoints",
    "Number of registered endpoints by current state",
    ["state"],
)

DATABASE_UP = Gauge("health_check_database_up", "Database reachable at last metrics scrape")
SCHEDULER_IN_FLIGHT = Gauge("health_check_scheduler_in_flight", "Checks submitted but not yet completed")
SCHEDULER_LAST_POLL = Gauge("health_check_scheduler_last_poll_timestamp_seconds", "Unix timestamp of last successful scheduler poll")
SCHEDULER_ERRORS = Counter("health_check_scheduler_errors_total", "Scheduler errors by stage", ["stage"])
