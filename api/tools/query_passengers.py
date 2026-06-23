"""
api/tools/query_passengers.py
──────────────────────────────
FetcherAgent tool: retrieves all affected passengers for a disrupted
flight, ranked by tier priority (Platinum > Gold > Silver > Bronze > None)
then by cabin class (F > J > W > Y).

This is the primary data-fetch step in the internal workflow.
The result drives RebookingAgent's processing order.
"""

from strands import tool
from ..db.database import get_connection


TIER_ORDER = {"Platinum": 1, "Gold": 2, "Silver": 3, "Bronze": 4, "None": 5}
CLASS_ORDER = {"F": 1, "J": 2, "W": 3, "Y": 4}


@tool
def query_passengers(flight_num: str) -> dict:
    """Retrieve all confirmed passengers on a disrupted flight, ranked by tier priority.

    Joins PASSENGER_SEGMENT, PASSENGER_PROFILE, and PNR_HEADER to return
    a complete manifest. Passengers are sorted Platinum first, then Gold,
    Silver, Bronze, non-members — matching Qantas rebooking priority policy.

    Args:
        flight_num: Flight number to query (e.g. "QF400")

    Returns:
        dict with keys:
            flight_num        str  — queried flight
            passenger_count   int  — total affected passengers
            passengers        list — list of passenger dicts, tier-ranked, each with:
                                     pax_id, first_name, last_name, pnr_locator,
                                     tier_status, qff_number, class_of_service,
                                     ticket_num, segment_id, ssr_codes,
                                     has_ssr, tier_rank, class_rank
            error             str  — populated only on failure
    """
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    pp.pax_id,
                    pp.first_name,
                    pp.last_name,
                    pp.qff_number,
                    pp.tier_status,
                    pp.ssr_codes,
                    ps.pnr_locator,
                    ps.segment_id,
                    ps.ticket_num,
                    ps.class_of_service,
                    ph.booking_status
                FROM PASSENGER_SEGMENT ps
                JOIN PASSENGER_PROFILE pp ON ps.pax_id = pp.pax_id
                JOIN PNR_HEADER ph        ON ps.pnr_locator = ph.pnr_locator
                WHERE ps.flight_num = ?
                  AND ph.booking_status = 'HK'
                ORDER BY pp.tier_status, ps.class_of_service
                """,
                (flight_num,),
            ).fetchall()
        finally:
            conn.close()

        passengers = []
        for row in rows:
            d = dict(row)
            d["tier_rank"]  = TIER_ORDER.get(d["tier_status"], 5)
            d["class_rank"] = CLASS_ORDER.get(d["class_of_service"], 4)
            d["has_ssr"]    = bool(d.get("ssr_codes"))
            passengers.append(d)

        # Sort by tier then cabin class
        passengers.sort(key=lambda p: (p["tier_rank"], p["class_rank"]))

        return {
            "flight_num":       flight_num,
            "passenger_count":  len(passengers),
            "passengers":       passengers,
            "error":            "",
        }

    except Exception as e:
        return {
            "flight_num":       flight_num,
            "passenger_count":  0,
            "passengers":       [],
            "error":            f"query_passengers failed: {str(e)}",
        }
