"""
api/agents/orchestrator.py
───────────────────────────
Orchestrator: the workflow coordinator for internal IROPS processing.

Model: Claude Haiku 4.5 — chosen for proven tool-use reliability
as a sequential coordinator, not for reasoning depth.

Responsibility:
  1. Receive the disruption trigger (flight_num, iata_delay_code, scenario)
  2. Call FetcherAgent to retrieve passenger manifest + inventory
  3. Pass FetcherAgent output to RebookingAgent for processing
  4. Handle GuardrailException from either subagent gracefully
  5. Call complete_workflow() to signal the SSE stream to close

The Orchestrator does NOT call SQLite tools directly. It coordinates
subagents and manages workflow lifecycle only.

FetcherAgent and RebookingAgent are invoked as plain Python async calls
(not as Strands @tool subagents) to keep the control flow explicit and
the GuardrailException propagation clean.
"""

import asyncio
import json
import logging

from ..agents.fetcher import create_fetcher_agent
from ..agents.rebooking import create_rebooking_agent
from ..config.governance import AGENT_MODEL_CONFIG, GOVERNANCE_LIMITS, SCENARIO_CONFIG
from ..hooks.guardrail import GuardrailException
from ..store.workflow_store import complete_workflow, workflow_store

logger = logging.getLogger(__name__)


async def run_internal_workflow(
    workflow_id: str,
    scenario: str,
    flight_num: str,
    iata_delay_code: int,
) -> None:
    """
    Run the full internal IROPS workflow for a disruption event.

    Called as a background asyncio task from POST /api/irops/trigger.
    Updates workflow_store throughout so the SSE stream stays live.

    Sequence:
      FetcherAgent → RebookingAgent → complete_workflow()

    GuardrailException from either agent is caught here and translated
    to the appropriate workflow terminal status.

    Args:
        workflow_id:      Workflow ID (already created in workflow_store)
        scenario:         "A" | "B" | "C" | "D"
        flight_num:       Disrupted flight number e.g. "QF400"
        iata_delay_code:  IATA delay code e.g. 41
    """
    logger.info(
        "internal_workflow_start",
        extra={
            "workflow_id":      workflow_id,
            "scenario":         scenario,
            "flight_num":       flight_num,
            "iata_delay_code":  iata_delay_code,
        },
    )

    try:
        # ── Phase 1: FetcherAgent ──────────────────────────────────────
        fetcher = create_fetcher_agent(workflow_id=workflow_id)

        # Inject workflow context into agent state for hooks
        fetcher.state.set("workflow_id",     workflow_id)
        fetcher.state.set("iata_delay_code", iata_delay_code)
        fetcher.state.set("cumulative_cost_usd", 0.0)

        fetcher_prompt = (
            f"IROPS disruption triggered. Execute these tool calls now:\n\n"
            f"STEP 1: Call query_flight_status with flight_num=\"{flight_num}\"\n"
            f"STEP 2: Call query_passengers with flight_num=\"{flight_num}\"\n"
            f"STEP 3: From the passenger manifest, get origin and destination airports, "
            f"then call query_inventory with those airports and the disruption time as after_datetime.\n\n"
            f"Context:\n"
            f"  flight_num={flight_num}\n"
            f"  iata_delay_code={iata_delay_code}\n"
            f"  workflow_id={workflow_id}\n\n"
            f"Return a structured summary of all results for RebookingAgent."
        )

        # Run FetcherAgent — may raise GuardrailException (budget cap)
        fetcher_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: fetcher(fetcher_prompt),
        )

        logger.info("fetcher_complete", extra={"workflow_id": workflow_id})

        # ── Phase 2: Build structured manifest from DB ─────────────────
        # Inject exact IDs directly from DB so RebookingAgent never needs
        # to infer pnr_locator / segment_id / pax_id from LLM text output.
        db_manifest = _load_manifest(flight_num)

        # ── Phase 3: RebookingAgent ────────────────────────────────────
        rebooking = create_rebooking_agent(workflow_id=workflow_id)

        # Pass accumulated cost from FetcherAgent into RebookingAgent state
        fetcher_cost = fetcher.state.get("cumulative_cost_usd") or 0.0
        rebooking.state.set("workflow_id",        workflow_id)
        rebooking.state.set("iata_delay_code",    iata_delay_code)
        rebooking.state.set("cumulative_cost_usd", fetcher_cost)

        rebooking_prompt = (
            f"IROPS disruption — workflow_id={workflow_id}, "
            f"original_flight={flight_num}, iata_delay_code={iata_delay_code}, "
            f"flight_status={db_manifest.get('flight_status', 'unknown')} "
            f"(CX=cancelled, DL=delayed)\n\n"
            f"## PASSENGERS (use these EXACT IDs in every tool call)\n"
            f"{json.dumps(db_manifest['passengers'], indent=2)}\n\n"
            f"## ALTERNATE FLIGHTS AVAILABLE\n"
            f"{json.dumps(db_manifest['alternate_flights'], indent=2)}\n\n"
            f"irops_id = \"{db_manifest.get('irops_id', '')}\"\n\n"
            f"Follow your system prompt sequence for each passenger.\n"
            f"For issue_voucher use the resolution_id returned by rebook_flight.\n"
            f"Last step: update_irops_log(irops_id, workflow_id, resolution_status=\"resolved\").\n"
        )

        # Run RebookingAgent — may raise GuardrailException (SSR halt, budget cap)
        rebooking_result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: rebooking(rebooking_prompt),
        )

        logger.info("rebooking_complete", extra={"workflow_id": workflow_id})

        # ── Count resolved passengers from DB ─────────────────────────
        passengers_resolved = _count_resolved(workflow_id)

        # ── Determine final status ─────────────────────────────────────
        record = workflow_store.get(workflow_id, {})
        if record.get("requires_human_review"):
            final_status = "human_review"
        else:
            final_status = "complete"

        complete_workflow(
            workflow_id=workflow_id,
            status=final_status,
            passengers_resolved=passengers_resolved,
        )
        logger.info(
            "workflow_complete",
            extra={
                "workflow_id":         workflow_id,
                "status":              final_status,
                "passengers_resolved": passengers_resolved,
            },
        )

    except GuardrailException as e:
        # Guardrail killed the workflow (budget cap or unrecoverable halt)
        logger.warning(
            "workflow_halted_by_guardrail",
            extra={
                "workflow_id":    workflow_id,
                "guardrail_code": e.code,
                "reason":         e.message,
            },
        )
        terminal_status = (
            "budget_exceeded" if e.code == "BUDGET_EXCEEDED" else "guardrail_halt"
        )
        passengers_resolved = _count_resolved(workflow_id)
        complete_workflow(
            workflow_id=workflow_id,
            status=terminal_status,
            passengers_resolved=passengers_resolved,
        )

    except Exception as e:
        logger.error(
            "workflow_error",
            extra={"workflow_id": workflow_id, "error": str(e)},
            exc_info=True,
        )
        complete_workflow(
            workflow_id=workflow_id,
            status="error",
            passengers_resolved=0,
        )


def _load_manifest(flight_num: str) -> dict:
    """Load passengers, alternate inventory, and irops_id from DB as structured data."""
    try:
        from ..db.database import get_connection
        conn = get_connection()
        try:
            # Disrupted flight info
            flight_row = conn.execute(
                "SELECT origin_apt, dest_apt, std, flight_status FROM FLIGHT_LEG WHERE flight_num = ?",
                (flight_num,),
            ).fetchone()
            origin   = flight_row["origin_apt"] if flight_row else ""
            dest     = flight_row["dest_apt"]   if flight_row else ""
            std      = flight_row["std"]         if flight_row else ""

            # Passengers in tier order
            pax_rows = conn.execute(
                """
                SELECT pp.pax_id, pp.first_name, pp.last_name, pp.tier_status,
                       pp.ssr_codes, ps.pnr_locator, ps.segment_id,
                       ps.ticket_num, ps.class_of_service
                FROM PASSENGER_SEGMENT ps
                JOIN PASSENGER_PROFILE pp ON ps.pax_id = pp.pax_id
                JOIN PNR_HEADER ph        ON ps.pnr_locator = ph.pnr_locator
                WHERE ps.flight_num = ? AND ph.booking_status = 'HK'
                ORDER BY
                  CASE pp.tier_status
                    WHEN 'Platinum' THEN 1 WHEN 'Gold' THEN 2
                    WHEN 'Silver' THEN 3 WHEN 'Bronze' THEN 4 ELSE 5 END,
                  CASE ps.class_of_service
                    WHEN 'F' THEN 1 WHEN 'J' THEN 2 WHEN 'W' THEN 3 ELSE 4 END
                """,
                (flight_num,),
            ).fetchall()

            # Alternate flights with available seats on the same route
            inv_rows = conn.execute(
                """
                SELECT fl.flight_num, fl.std, fl.eta, fl.aircraft_type,
                       fi.class_of_service, fi.seats_available
                FROM FLIGHT_LEG fl
                JOIN FLIGHT_INVENTORY fi ON fl.flight_num = fi.flight_num
                WHERE fl.origin_apt = ? AND fl.dest_apt = ?
                  AND fl.std > ? AND fl.flight_status = 'SC'
                  AND fi.seats_available > 0
                ORDER BY fl.std ASC
                """,
                (origin, dest, std),
            ).fetchall()

            irops_row = conn.execute(
                "SELECT irops_id FROM IROPS_LOG WHERE flight_num = ? LIMIT 1",
                (flight_num,),
            ).fetchone()

            return {
                "passengers":       [dict(r) for r in pax_rows],
                "alternate_flights": [dict(r) for r in inv_rows],
                "irops_id":          irops_row["irops_id"] if irops_row else "",
                "flight_status":     flight_row["flight_status"] if flight_row else "unknown",
            }
        finally:
            conn.close()
    except Exception as e:
        logger.error("manifest_load_failed", extra={"error": str(e)})
        return {"passengers": [], "alternate_flights": [], "irops_id": ""}


def _count_resolved(workflow_id: str) -> int:
    """Count RESOLUTION rows linked to this workflow."""
    try:
        from ..db.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM RESOLUTION WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()
    except Exception:
        return 0
