"""Run exactly one API and one in-process scheduler; no import-time I/O."""
import logging
import math
import os
import signal
import threading

from werkzeug.serving import make_server

from app import create_app
from app.checker import request_timeout
from app.scheduler import HealthCheckScheduler

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    grace_seconds = float(os.getenv("SHUTDOWN_GRACE_SECONDS", "25"))
    if not math.isfinite(grace_seconds) or grace_seconds <= 0:
        raise ValueError("SHUTDOWN_GRACE_SECONDS must be finite and positive")
    request_timeout()  # Validate outbound timeout configuration before starting threads.
    app = create_app()
    scheduler = HealthCheckScheduler()
    app.extensions["health_check_scheduler"] = scheduler
    server = make_server("0.0.0.0", int(os.getenv("PORT", "8000")), app, threaded=True)
    stopped = threading.Event()

    def request_shutdown(signum, frame):
        # Signal handlers only signal; shutdown/join occurs outside the handler.
        stopped.set()
        scheduler.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_shutdown)
    scheduler_thread = threading.Thread(target=scheduler.run, name="health-check-scheduler")
    server_thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
    scheduler_thread.start()
    server_thread.start()
    try:
        while not stopped.wait(0.5):
            if not scheduler_thread.is_alive() or not server_thread.is_alive():
                logger.error("A service thread stopped unexpectedly")
                break
    finally:
        scheduler.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
        scheduler_thread.join(timeout=grace_seconds)
        if scheduler_thread.is_alive():
            # DNS and socket inactivity timeouts cannot guarantee a wall-clock bound.
            # A process deadline prevents Kubernetes shutdown from hanging forever.
            logger.error("Shutdown deadline exceeded; unfinished checks will be lost")
            logging.shutdown()
            os._exit(1)
        logger.info("Application shutdown complete")
    return 0 if stopped.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(main())
