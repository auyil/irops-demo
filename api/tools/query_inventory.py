"""
api/tools/query_inventory.py
─────────────────────────────
FetcherAgent tool: finds available alternate flights and seat inventory
for a given route, filtered by minimum departure time to exclude the
disrupted flight itself.

Used by FetcherAgent to identify rebooking options before handing
the manifest to RebookingAgent.
"""

from strands import tool
from ..db.database import get_connection


@tool
def query_inventory(
    origin: str,
    destination: str,
    after_datetime: str,
    required_class: str = "Y",
) -> dict:
    """Find available alternate flights with seat inventory for a given route.

    Queries FLIGHT_LEG joined with FLIGHT_INVENTORY to return scheduled
    flights with available seats, ordered by departure time (earliest first).
    Only returns flights with at least 1 seat available in the requested
    class or higher.

    Class hierarchy for upgrade fallback: Y < W < J < F.
    If no seats in required_class, returns options in next available class up.

    Args:
        origin:           IATA airport code (e.g. "SYD")
        destination:      IATA airport code (e.g. "MEL")
        after_datetime:   ISO-8601 datetime — only return flights departing after this
                          (e.g. "2025-07-14T10:15:00")
        required_class:   Preferred class of service ("F", "J", "W", "Y")
                          Defaults to "Y" (Economy)

    Returns:
        dict with keys:
            origin            str  — queried origin
            destination       str  — queried destination
            after_datetime    str  — filter applied
            options_found     int  — number of alternate flights available
            options           list — list of dicts, each with:
                                     flight_num, std, eta, aircraft_type,
                                     class_of_service, seats_available
            error             str  — populated only on failure
    """
    # Class hierarchy — for fallback to higher cabin if required class full
    class_hierarchy = ["Y", "W", "J", "F"]
    try:
        required_index = class_hierarchy.index(required_class.upper())
    except ValueError:
        required_index = 0
    eligible_classes = class_hierarchy[required_index:]

    placeholders = ",".join("?" * len(eligible_classes))

    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                f"""
                SELECT
                    fl.flight_num,
                    fl.std,
                    fl.eta,
                    fl.aircraft_type,
                    fi.class_of_service,
                    fi.seats_available
                FROM FLIGHT_LEG fl
                JOIN FLIGHT_INVENTORY fi ON fl.flight_num = fi.flight_num
                WHERE fl.origin_apt    = ?
                  AND fl.dest_apt      = ?
                  AND fl.std           > ?
                  AND fl.flight_status = 'SC'
                  AND fi.class_of_service IN ({placeholders})
                  AND fi.seats_available  > 0
                ORDER BY fl.std ASC, fi.class_of_service DESC
                """,
                (origin, destination, after_datetime, *eligible_classes),
            ).fetchall()
        finally:
            conn.close()

        options = [dict(row) for row in rows]

        return {
            "origin":         origin,
            "destination":    destination,
            "after_datetime": after_datetime,
            "options_found":  len(options),
            "options":        options,
            "error":          "",
        }

    except Exception as e:
        return {
            "origin":         origin,
            "destination":    destination,
            "after_datetime": after_datetime,
            "options_found":  0,
            "options":        [],
            "error":          f"query_inventory failed: {str(e)}",
        }
