"""
api/tools/update_irops_log.py
──────────────────────────────
RebookingAgent tool: marks the IROPS_LOG entry as resolved (or
human_review / budget_exceeded) once the workflow completes.

Called as the final step of the internal workflow after all passengers
have been processed. Provides the audit trail for the disruption event.
"""

from datetime import datetime, timezone

from strands import tool
from ..db.database import get_connection


VALID_STATUSES = {"resolved", "human_review", "budget_exceeded", "pending"}


@tool
def update_irops_log(
    irops_id: str,
    workflow_id: str,
    resolution_status: str,
    action_summary: str = "",
) -> dict:
    """Update the IROPS_LOG entry with workflow outcome and resolution status.

    Links the IROPS event to the workflow that processed it and records
    the final resolution status. This is the audit record for the disruption.

    Args:
        irops_id:           IROPS_LOG primary key (e.g. "IROPS-A-001")
        workflow_id:        Workflow ID that processed this disruption
        resolution_status:  One of: "resolved", "human_review", "budget_exceeded"
        action_summary:     Brief summary of actions taken (ignored — for LLM context only)

    Returns:
        dict with keys:
            success             bool
            irops_id            str
            workflow_id         str
            resolution_status   str
            updated_at          str  — ISO-8601 timestamp
            error               str  — populated only on failure
    """
    if resolution_status not in VALID_STATUSES:
        return {
            "success":  False,
            "irops_id": irops_id,
            "error": (
                f"Invalid resolution_status '{resolution_status}'. "
                f"Must be one of: {sorted(VALID_STATUSES)}"
            ),
        }

    updated_at = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE IROPS_LOG
                    SET workflow_id       = ?,
                        resolution_status = ?
                    WHERE irops_id = ?
                    """,
                    (workflow_id, resolution_status, irops_id),
                )
        finally:
            conn.close()

        return {
            "success":           True,
            "irops_id":          irops_id,
            "workflow_id":       workflow_id,
            "resolution_status": resolution_status,
            "updated_at":        updated_at,
            "error":             "",
        }

    except Exception as e:
        return {
            "success":  False,
            "irops_id": irops_id,
            "error":    f"update_irops_log failed: {str(e)}",
        }
