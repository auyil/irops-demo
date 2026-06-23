"""
api/tools/query_flight_status.py
─────────────────────────────────
FetcherAgent tool: retrieves full flight leg details including current
status and associated IROPS log entry if one exists.

Used by FetcherAgent at the start of the internal workflow to confirm
the disruption details before querying the passenger manifest.
"""

from strands import tool
from ..db.database import get_connection


STATUS_LABELS = {
    "SC": "Scheduled",
    "DL": "Delayed",
    "CX": "Cancelled",
}


@tool
def query_flight_status(flight_num: str) -> dict:
    """Retrieve current status and IROPS log for a flight.

    Returns flight leg details joined with the most recent IROPS_LOG
    entry for that flight (if any). This gives the orchestrator the
    IATA delay code and disruption context needed to route correctly.

    Args:
        flight_num: Flight number to query (e.g. "QF400")

    Returns:
        dict with keys:
            flight_num          str  — queried flight
            origin              str  — IATA origin airport code
            destination         str  — IATA destination airport code
            std                 str  — scheduled departure (ISO-8601)
            eta                 str  — estimated arrival (ISO-8601) or null
            flight_status       str  — "SC", "DL", or "CX"
            flight_status_label str  — human-readable status
            aircraft_type       str  — e.g. "B737", "A380"
            irops_id            str  — IROPS log ID or null if no disruption logged
            iata_delay_code     int  — IATA delay code or null
            triggered_at        str  — disruption trigger time or null
            resolution_status   str  — "pending", "resolved", etc. or null
            workflow_id         str  — linked workflow ID or null
            error               str  — populated only on failure
    """
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT
                    fl.flight_num,
                    fl.origin_apt       AS origin,
                    fl.dest_apt         AS destination,
                    fl.std,
                    fl.eta,
                    fl.flight_status,
                    fl.aircraft_type,
                    il.irops_id,
                    il.iata_delay_code,
                    il.triggered_at,
                    il.resolution_status,
                    il.workflow_id
                FROM FLIGHT_LEG fl
                LEFT JOIN IROPS_LOG il ON fl.flight_num = il.flight_num
                WHERE fl.flight_num = ?
                ORDER BY il.triggered_at DESC
                LIMIT 1
                """,
                (flight_num,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return {
                "flight_num": flight_num,
                "error":      f"Flight {flight_num} not found in database.",
            }

        result = dict(row)
        result["flight_status_label"] = STATUS_LABELS.get(
            result.get("flight_status", ""), "Unknown"
        )
        result["error"] = ""
        return result

    except Exception as e:
        return {
            "flight_num": flight_num,
            "error":      f"query_flight_status failed: {str(e)}",
        }
