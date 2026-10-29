
from datetime import datetime

from flask import Response, jsonify, request
import psycopg
from psycopg.errors import UniqueViolation
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.database import get_connection
from app.metrics import DATABASE_UP, ENDPOINTS_TOTAL
from app.url_policy import validate_endpoint_url


def register_routes(app):

    @app.get("/metrics")
    def metrics():
        try:
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT current_state, COUNT(*) AS total FROM endpoints GROUP BY current_state"
                ).fetchall()
            counts = {row["current_state"]: row["total"] for row in rows}
            for state in ("unknown", "healthy", "unhealthy"):
                ENDPOINTS_TOTAL.labels(state=state).set(counts.get(state, 0))
            DATABASE_UP.set(1)
        except psycopg.Error:
            DATABASE_UP.set(0)
            app.logger.error("Metrics database query failed")
        return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)

    @app.get("/")
    def home():
        return jsonify({
            "service": "health-check-service",
            "status": "running",
            "framework": "Flask",
        })

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        conn = None

        try:
            conn = get_connection()
            conn.execute("SELECT 1")

            scheduler = app.extensions.get("health_check_scheduler")
            if scheduler is not None and not scheduler.healthy():
                return jsonify({"status": "not_ready", "reason": "scheduler unavailable"}), 503
            return jsonify({"status": "ready"}), 200

        except psycopg.Error:
            app.logger.exception("Readiness check failed")
            return jsonify({
                "status": "not_ready",
                "reason": "database unavailable",
            }), 503

        finally:
            if conn is not None:
                conn.close()

    @app.post("/endpoints")
    def create_endpoint():
        data = request.get_json(silent=True)

        if not isinstance(data, dict):
            return jsonify({
                "error": "Request body must be a JSON object"
            }), 400

        url = data.get("url")
        interval = data.get("interval_seconds")
        expected_status = data.get("expected_status_code", 200)

        try:
            validate_endpoint_url(url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if (
            not isinstance(interval, int)
            or isinstance(interval, bool)
            or not 1 <= interval <= 2147483647
        ):
            return jsonify({
                "error": "interval_seconds must be a positive integer at most 2147483647"
            }), 400

        if (
            not isinstance(expected_status, int)
            or isinstance(expected_status, bool)
            or not 100 <= expected_status <= 599
        ):
            return jsonify({
                "error": (
                    "expected_status_code must be an integer "
                    "from 100 to 599"
                )
            }), 400

        conn = None

        try:
            conn = get_connection()

            with conn.transaction():
                endpoint = conn.execute(
                    """
                    INSERT INTO endpoints (
                        url,
                        interval_seconds,
                        expected_status_code
                    )
                    VALUES (%s, %s, %s)
                    RETURNING
                        id,
                        url,
                        interval_seconds,
                        expected_status_code,
                        enabled,
                        current_state,
                        created_at
                    """,
                    (url, interval, expected_status),
                ).fetchone()

            return jsonify(dict(endpoint)), 201

        except UniqueViolation:
            return jsonify({
                "error": "This URL is already registered"
            }), 409

        except psycopg.Error:
            app.logger.exception("Failed to register endpoint")
            return jsonify({
                "error": "Database operation failed"
            }), 503

        finally:
            if conn is not None:
                conn.close()

    @app.get("/endpoints")
    def list_endpoints():
        conn = None

        try:
            conn = get_connection()

            endpoints = conn.execute(
                """
                SELECT
                    id,
                    url,
                    interval_seconds,
                    expected_status_code,
                    enabled,
                    current_state,
                    created_at
                FROM endpoints
                ORDER BY id
                """
            ).fetchall()

            return jsonify([
                dict(endpoint) for endpoint in endpoints
            ]), 200

        except psycopg.Error:
            app.logger.exception("Failed to list endpoints")
            return jsonify({
                "error": "Database operation failed"
            }), 503

        finally:
            if conn is not None:
                conn.close()

    @app.delete("/endpoints/<int:endpoint_id>")
    def delete_endpoint(endpoint_id):
        conn = None

        try:
            conn = get_connection()

            with conn.transaction():
                deleted = conn.execute(
                    """
                    DELETE FROM endpoints
                    WHERE id = %s
                    RETURNING id
                    """,
                    (endpoint_id,),
                ).fetchone()

            if deleted is None:
                return jsonify({
                    "error": "Endpoint not found"
                }), 404

            return jsonify({
                "message": "Endpoint deleted successfully",
                "id": deleted["id"],
            }), 200

        except psycopg.Error:
            app.logger.exception("Failed to delete endpoint")
            return jsonify({
                "error": "Database operation failed"
            }), 503

        finally:
            if conn is not None:
                conn.close()

    @app.get("/endpoints/<int:endpoint_id>/history")
    def endpoint_history(endpoint_id):
        # Optional ISO 8601 timestamps, for example:
        # ?from=2026-10-28T00:00:00Z&to=2026-10-29T00:00:00Z
        def parse_timestamp(value):
            if value is None:
                return None
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Timezone required")
            return parsed

        try:
            from_time = parse_timestamp(request.args.get("from"))
            to_time = parse_timestamp(request.args.get("to"))
            if from_time is not None and to_time is not None and from_time > to_time:
                raise ValueError("Reversed range")
        except ValueError:
            return jsonify({"error": "Use ISO 8601 timestamps with timezone and from <= to"}), 400

        conn = None

        try:
            conn = get_connection()

            endpoint = conn.execute(
                "SELECT id FROM endpoints WHERE id = %s",
                (endpoint_id,),
            ).fetchone()

            if endpoint is None:
                return jsonify({
                    "error": "Endpoint not found"
                }), 404

            query = """
                SELECT
                    id,
                    endpoint_id,
                    checked_at,
                    success,
                    actual_status_code,
                    response_time_ms,
                    error_message
                FROM check_results
                WHERE endpoint_id = %s
            """
            params = [endpoint_id]

            if from_time:
                query += " AND checked_at >= %s"
                params.append(from_time)

            if to_time:
                query += " AND checked_at <= %s"
                params.append(to_time)

            query += " ORDER BY checked_at DESC, id DESC LIMIT 100"

            results = conn.execute(query, params).fetchall()

            return jsonify({
                "endpoint_id": endpoint_id,
                "count": len(results),
                "history": [
                    dict(result) for result in results
                ],
            }), 200

        except psycopg.Error:
            app.logger.error("Failed to retrieve endpoint history")
            return jsonify({"error": "Database operation failed"}), 503

        finally:
            if conn is not None:
                conn.close()

    @app.get("/endpoints/<int:endpoint_id>/transitions")
    def endpoint_transitions(endpoint_id):
        conn = None

        try:
            conn = get_connection()

            endpoint = conn.execute(
                "SELECT id FROM endpoints WHERE id = %s",
                (endpoint_id,),
            ).fetchone()

            if endpoint is None:
                return jsonify({
                    "error": "Endpoint not found"
                }), 404

            transitions = conn.execute(
                """
                SELECT
                    id,
                    endpoint_id,
                    from_state,
                    to_state,
                    transitioned_at
                FROM state_transitions
                WHERE endpoint_id = %s
                ORDER BY transitioned_at DESC, id DESC
                LIMIT 100
                """,
                (endpoint_id,),
            ).fetchall()

            return jsonify({
                "endpoint_id": endpoint_id,
                "count": len(transitions),
                "transitions": [
                    dict(transition) for transition in transitions
                ],
            }), 200

        except psycopg.Error:
            app.logger.exception("Failed to retrieve state transitions")
            return jsonify({
                "error": "Database operation failed"
            }), 503

        finally:
            if conn is not None:
                conn.close()