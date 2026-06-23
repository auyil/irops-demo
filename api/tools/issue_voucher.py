"""
api/tools/issue_voucher.py
───────────────────────────
RebookingAgent tool: issues a compensation voucher for a passenger and
updates their RESOLUTION record.

The GuardrailHook fires BEFORE this tool (TC-03 policy alignment check)
— so by the time this runs, the voucher type has already been validated
against the IATA delay code and control classification. This tool only
handles the write — it does not re-validate policy.
"""

import json
from datetime import datetime, timezone

from strands import tool
from ..db.database import get_connection


VALID_VOUCHER_TYPES = {
    "MEAL_30_AUD":    "Digital meal/refreshment voucher — $30 AUD",
    "HOTEL":          "Hotel accommodation + $60 AUD meal allowance (overnight)",
    "TRAVEL_VOUCHER": "Qantas travel voucher — overbooking (international flight, next available >4h)",
    "NONE":           "No voucher issued — policy does not apply",
}


@tool
def issue_voucher(
    resolution_id: str,
    pax_id: str,
    pnr_locator: str,
    voucher_type: str,
    workflow_id: str,
    agent_justification: str,
) -> dict:
    """Issue a compensation voucher and update the passenger's resolution record.

    Appends the voucher type to the existing RESOLUTION row created by
    rebook_flight. If no RESOLUTION row exists yet (edge case), creates one.

    Args:
        resolution_id:        RESOLUTION record to update (from rebook_flight output)
        pax_id:               Passenger ID
        pnr_locator:          PNR booking reference
        voucher_type:         One of: "MEAL_30_AUD", "HOTEL", "NONE"
        workflow_id:          Parent workflow ID for tracing
        agent_justification:  Policy reference supporting this voucher decision

    Returns:
        dict with keys:
            success             bool
            resolution_id       str
            pax_id              str
            pnr_locator         str
            voucher_type        str
            voucher_label       str  — human-readable voucher description
            issued_at           str  — ISO-8601 timestamp
            agent_justification str
            error               str  — populated only on failure
    """
    if voucher_type.upper() not in VALID_VOUCHER_TYPES:
        return {
            "success":     False,
            "pax_id":      pax_id,
            "pnr_locator": pnr_locator,
            "error": (
                f"Invalid voucher_type '{voucher_type}'. "
                f"Must be one of: {list(VALID_VOUCHER_TYPES.keys())}"
            ),
        }

    voucher_type  = voucher_type.upper()
    voucher_label = VALID_VOUCHER_TYPES[voucher_type]
    issued_at     = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_connection()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT resolution_id FROM RESOLUTION WHERE resolution_id = ?",
                    (resolution_id,),
                ).fetchone()

                if existing:
                    conn.execute(
                        """
                        UPDATE RESOLUTION
                        SET voucher_type        = ?,
                            agent_justification = ?,
                            resolved_at         = ?
                        WHERE resolution_id = ?
                        """,
                        (voucher_type, agent_justification, issued_at, resolution_id),
                    )
                else:
                    # Fallback: create resolution row if rebook_flight wasn't called first
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
                            "UNKNOWN", None, "VOUCHER_ONLY",
                            voucher_type, 0, "[]",
                            agent_justification, issued_at,
                        ),
                    )
        finally:
            conn.close()

        return {
            "success":              True,
            "resolution_id":        resolution_id,
            "pax_id":               pax_id,
            "pnr_locator":          pnr_locator,
            "voucher_type":         voucher_type,
            "voucher_label":        voucher_label,
            "issued_at":            issued_at,
            "agent_justification":  agent_justification,
            "error":                "",
        }

    except Exception as e:
        return {
            "success":     False,
            "pax_id":      pax_id,
            "pnr_locator": pnr_locator,
            "error":       f"issue_voucher failed: {str(e)}",
        }
