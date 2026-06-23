"""
api/tools/flag_human_review.py
───────────────────────────────
Shared tool: flags a passenger or workflow for human agent review.

Used by:
  - RebookingAgent  — when GuardrailHook fires SSR hard stop (TC-02)
  - ConciergeAgent  — when a passenger query requires escalation

Writes to the RESOLUTION table and signals the WorkflowStore so the
SSE stream can emit a HUMAN_REVIEW event to the frontend immediately.
"""

import json
from datetime import datetime, timezone

from strands import tool
from ..db.database import get_connection
from ..store.workflow_store import workflow_store


@tool
def flag_human_review(
    pax_id: str,
    pnr_locator: str,
    workflow_id: str,
    reason: str,
    guardrail_event: str,
    original_flight: str = "",
    resolution_id: str = "",
) -> dict:
    """Flag a passenger case for human agent review and halt automated processing.

    Updates or creates a RESOLUTION record with requires_human_review=True,
    appends the guardrail event to the audit trail, and notifies the
    WorkflowStore so the SSE stream emits a HUMAN_REVIEW event.

    Args:
        pax_id:           Passenger ID
        pnr_locator:      PNR booking reference
        workflow_id:      Parent workflow ID
        reason:           Human-readable explanation for the escalation
        guardrail_event:  Guardrail code that triggered this (e.g. "SSR_SAFETY_HALT")
        original_flight:  Original disrupted flight number (optional)
        resolution_id:    Existing RESOLUTION ID to update (optional —
                          if empty, a new record is created)

    Returns:
        dict with keys:
            success              bool
            pax_id               str
            pnr_locator          str
            requires_human_review bool  — always True on success
            guardrail_event      str
            reason               str
            flagged_at           str   — ISO-8601 timestamp
            error                str   — populated only on failure
    """
    flagged_at = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_connection()
        try:
            with conn:
                if resolution_id:
                    # Update existing resolution record
                    existing = conn.execute(
                        "SELECT guardrail_events FROM RESOLUTION WHERE resolution_id = ?",
                        (resolution_id,),
                    ).fetchone()

                    if existing:
                        try:
                            events = json.loads(existing["guardrail_events"] or "[]")
                        except (json.JSONDecodeError, TypeError):
                            events = []
                        events.append(guardrail_event)

                        conn.execute(
                            """
                            UPDATE RESOLUTION
                            SET requires_human_review = 1,
                                guardrail_events      = ?,
                                agent_justification   = ?,
                                resolved_at           = ?
                            WHERE resolution_id = ?
                            """,
                            (json.dumps(events), reason, flagged_at, resolution_id),
                        )
                else:
                    # Create new resolution record for this escalation
                    import uuid
                    new_res_id = f"RES-{uuid.uuid4().hex[:8].upper()}"
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
                            new_res_id, workflow_id, pnr_locator, pax_id,
                            original_flight, None, "HUMAN_REVIEW",
                            None, 1, json.dumps([guardrail_event]),
                            reason, flagged_at,
                        ),
                    )
                    resolution_id = new_res_id
        finally:
            conn.close()

        # Notify WorkflowStore — SSE stream picks this up immediately
        if workflow_id and workflow_id in workflow_store:
            workflow_store[workflow_id]["requires_human_review"] = True
            workflow_store[workflow_id]["guardrail_event"] = guardrail_event

        return {
            "success":               True,
            "pax_id":                pax_id,
            "pnr_locator":           pnr_locator,
            "resolution_id":         resolution_id,
            "requires_human_review": True,
            "guardrail_event":       guardrail_event,
            "reason":                reason,
            "flagged_at":            flagged_at,
            "error":                 "",
        }

    except Exception as e:
        return {
            "success":     False,
            "pax_id":      pax_id,
            "pnr_locator": pnr_locator,
            "error":       f"flag_human_review failed: {str(e)}",
        }
