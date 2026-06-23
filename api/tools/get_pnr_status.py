"""
api/tools/get_pnr_status.py
────────────────────────────
ConciergeAgent tool: retrieves a passenger's full booking status
including their current itinerary, resolution record if one exists,
and SSR codes — everything needed to answer a passenger's query about
their disrupted booking.
"""

from strands import tool
from ..db.database import get_connection


@tool
def get_pnr_status(pnr_locator: str) -> dict:
    """Retrieve full booking status for a PNR including disruption resolution.

    Joins PNR_HEADER, PASSENGER_SEGMENT, PASSENGER_PROFILE, FLIGHT_LEG,
    and RESOLUTION to return a complete picture of the passenger's current
    situation. Used by ConciergeAgent to answer queries like:
      - "What flight am I rebooked onto?"
      - "Did I get a meal voucher?"
      - "Why was my rebooking flagged for human review?"

    Args:
        pnr_locator: PNR alphanumeric booking reference (e.g. "AABBCC")

    Returns:
        dict with keys:
            found              bool  — False if PNR not in database
            pnr_locator        str
            booking_status     str   — "HK" (confirmed) or "XX" (cancelled)
            passenger          dict  — name, tier_status, qff_number, ssr_codes
            current_segment    dict  — flight_num, std, eta, class_of_service,
                                       flight_status, origin, destination
            resolution         dict  — action_taken, new_flight_num, voucher_type,
                                       requires_human_review, agent_justification
                                       (null if no resolution record yet)
            has_resolution     bool  — True if a resolution record exists
            error              str   — populated only on failure
    """
    try:
        conn = get_connection()
        try:
            # Core booking + passenger + current segment
            seg_row = conn.execute(
                """
                SELECT
                    ph.pnr_locator,
                    ph.booking_status,
                    pp.pax_id,
                    pp.first_name,
                    pp.last_name,
                    pp.tier_status,
                    pp.qff_number,
                    pp.ssr_codes,
                    ps.segment_id,
                    ps.class_of_service,
                    ps.ticket_num,
                    fl.flight_num,
                    fl.origin_apt       AS origin,
                    fl.dest_apt         AS destination,
                    fl.std,
                    fl.eta,
                    fl.flight_status
                FROM PNR_HEADER ph
                JOIN PASSENGER_SEGMENT ps ON ph.pnr_locator = ps.pnr_locator
                JOIN PASSENGER_PROFILE pp ON ps.pax_id      = pp.pax_id
                JOIN FLIGHT_LEG        fl ON ps.flight_num   = fl.flight_num
                WHERE ph.pnr_locator = ?
                LIMIT 1
                """,
                (pnr_locator.upper(),),
            ).fetchone()

            if not seg_row:
                return {
                    "found":       False,
                    "pnr_locator": pnr_locator,
                    "error":       f"PNR '{pnr_locator}' not found.",
                }

            seg = dict(seg_row)

            # Resolution record (may not exist yet if workflow hasn't run)
            res_row = conn.execute(
                """
                SELECT
                    resolution_id,
                    action_taken,
                    new_flight_num,
                    voucher_type,
                    requires_human_review,
                    guardrail_events,
                    agent_justification,
                    resolved_at
                FROM RESOLUTION
                WHERE pnr_locator = ?
                ORDER BY resolved_at DESC
                LIMIT 1
                """,
                (pnr_locator.upper(),),
            ).fetchone()

        finally:
            conn.close()

        resolution = dict(res_row) if res_row else None
        if resolution:
            resolution["requires_human_review"] = bool(
                resolution.get("requires_human_review")
            )

        return {
            "found":          True,
            "pnr_locator":    seg["pnr_locator"],
            "booking_status": seg["booking_status"],
            "passenger": {
                "pax_id":      seg["pax_id"],
                "first_name":  seg["first_name"],
                "last_name":   seg["last_name"],
                "tier_status": seg["tier_status"],
                "qff_number":  seg["qff_number"],
                "ssr_codes":   seg["ssr_codes"],
            },
            "current_segment": {
                "flight_num":        seg["flight_num"],
                "origin":            seg["origin"],
                "destination":       seg["destination"],
                "std":               seg["std"],
                "eta":               seg["eta"],
                "flight_status":     seg["flight_status"],
                "class_of_service":  seg["class_of_service"],
            },
            "resolution":     resolution,
            "has_resolution": resolution is not None,
            "error":          "",
        }

    except Exception as e:
        return {
            "found":       False,
            "pnr_locator": pnr_locator,
            "error":       f"get_pnr_status failed: {str(e)}",
        }
