"""
api/tools/rebook_flight.py
───────────────────────────
RebookingAgent tool: updates a passenger's segment to a new flight and
decrements inventory on the alternate flight.

This is a write tool — it modifies PASSENGER_SEGMENT and FLIGHT_INVENTORY
in a single transaction. The GuardrailHook fires BEFORE this tool executes
(SSR check, TC-02) so by the time this runs, the passenger is confirmed
safe for automated rebooking.
"""

import uuid
from datetime import datetime, timezone

from strands import tool
from ..db.database import get_connection


@tool
def rebook_flight(
    segment_id: str,
    pax_id: str,
    pnr_locator: str,
    original_flight: str,
    new_flight_num: str,
    new_class_of_service: str,
    workflow_id: str,
    agent_justification: str,
) -> dict:
    """Rebook a passenger onto an alternate flight.

    Updates the passenger's PASSENGER_SEGMENT record to the new flight,
    decrements available seats in FLIGHT_INVENTORY, and writes an initial
    RESOLUTION record (without voucher — issue_voucher handles that).

    Wrapped in a transaction — rolls back cleanly if inventory is
    exhausted between FetcherAgent's query and this write.

    Args:
        segment_id:            Original segment ID to update
        pax_id:                Passenger ID
        pnr_locator:           PNR booking reference
        original_flight:       Original disrupted flight number
        new_flight_num:        Alternate flight to book onto
        new_class_of_service:  Class of service on new flight ("F","J","W","Y")
        workflow_id:           Parent workflow ID for tracing
        agent_justification:   RebookingAgent's reasoning (policy reference etc.)

    Returns:
        dict with keys:
            success             bool — True if rebooked successfully
            resolution_id       str  — new RESOLUTION record ID
            pax_id              str
            pnr_locator         str
            original_flight     str
            new_flight_num      str
            new_class_of_service str
            action_taken        str  — "AUTO_REBOOK"
            resolved_at         str  — ISO-8601 timestamp
            agent_justification str
            error               str  — populated only on failure
    """
    resolution_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
    resolved_at   = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_connection()
        try:
            with conn:
                # Check inventory hasn't been exhausted since FetcherAgent ran
                inv = conn.execute(
                    """
                    SELECT seats_available FROM FLIGHT_INVENTORY
                    WHERE flight_num = ? AND class_of_service = ?
                    """,
                    (new_flight_num, new_class_of_service),
                ).fetchone()

                if not inv or inv["seats_available"] < 1:
                    return {
                        "success":       False,
                        "pax_id":        pax_id,
                        "pnr_locator":   pnr_locator,
                        "new_flight_num": new_flight_num,
                        "error": (
                            f"No seats available in class {new_class_of_service} "
                            f"on {new_flight_num}. Inventory exhausted."
                        ),
                    }

                # Update the passenger's segment to the new flight
                conn.execute(
                    """
                    UPDATE PASSENGER_SEGMENT
                    SET flight_num = ?, class_of_service = ?
                    WHERE segment_id = ?
                    """,
                    (new_flight_num, new_class_of_service, segment_id),
                )

                # Decrement inventory
                conn.execute(
                    """
                    UPDATE FLIGHT_INVENTORY
                    SET seats_available = seats_available - 1
                    WHERE flight_num = ? AND class_of_service = ?
                    """,
                    (new_flight_num, new_class_of_service),
                )

                # Write initial resolution record (voucher added later by issue_voucher)
                conn.execute(
                    """
                    INSERT INTO RESOLUTION (
                        resolution_id, workflow_id, pnr_locator, pax_id,
                        original_flight, new_flight_num, action_taken,
                        voucher_type, requires_human_review, guardrail_events,
                        agent_justification, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolution_id, workflow_id, pnr_locator, pax_id,
                        original_flight, new_flight_num, "AUTO_REBOOK",
                        None, 0, "[]",
                        agent_justification, resolved_at,
                    ),
                )
        finally:
            conn.close()

        return {
            "success":              True,
            "resolution_id":        resolution_id,
            "pax_id":               pax_id,
            "pnr_locator":          pnr_locator,
            "original_flight":      original_flight,
            "new_flight_num":       new_flight_num,
            "new_class_of_service": new_class_of_service,
            "action_taken":         "AUTO_REBOOK",
            "resolved_at":          resolved_at,
            "agent_justification":  agent_justification,
            "error":                "",
        }

    except Exception as e:
        return {
            "success":     False,
            "pax_id":      pax_id,
            "pnr_locator": pnr_locator,
            "error":       f"rebook_flight failed: {str(e)}",
        }
