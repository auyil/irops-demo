"""
api/agents/workflow_builder.py
───────────────────────────────
WorkflowBuilderAgent: conversational custom scenario configurator.

Collects disruption details from the user, validates them using the
lookup_iata_code skill, then seeds the database and fires a live
IROPS workflow. Emits a custom_workflow_triggered SSE event that the
frontend uses to connect the trace panel.

Invoked by POST /api/chat when custom_mode=True in the request body.
"""

import asyncio
import json as _json
import logging
from typing import AsyncGenerator

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from ..agents.orchestrator import run_internal_workflow
from ..config.governance import AGENT_MODEL_CONFIG, GOVERNANCE_LIMITS
from ..db.database import seed_custom_scenario
from ..skills.lookup_iata_code import lookup_iata_code
from ..store.workflow_store import create_workflow

logger = logging.getLogger(__name__)

BUILDER_SYSTEM_PROMPT = """You are the IROPS Custom Test Builder — an internal tool for configuring \
custom flight disruption simulations for the Qantas IROPS demo.

## YOUR JOB
Collect the minimum required data to construct a disruption scenario, confirm it with the user, \
then call trigger_custom_workflow to seed the database and launch the live workflow.

## DATA TO COLLECT (in a natural conversation — ask 1-2 things per turn)
1. Flight number  (e.g. QF9, EK415 — any format)
2. Origin airport  (IATA code: SYD, MEL, LHR, DXB, etc.)
3. Destination airport  (IATA code)
4. Disruption type: cancellation → "CX", delay → "DL"
5. Cause of disruption → infer IATA delay code, then call lookup_iata_code to confirm and label it
6. At least one passenger with: first name, last name, loyalty tier \
   (Platinum/Gold/Silver/Bronze/None), cabin class (F/J/W/Y), and any SSR codes

## IATA CODE INFERENCE (always call lookup_iata_code to verify before presenting to user)
"mechanical / engine / AOG"            → 41
"carried-over defect from prev flight" → 42
"maintenance overrun / late release"   → 43
"weather / storm / fog / cloud"        → 71 (departure) or 72 (destination)
"de-icing"                             → 75
"ATC restriction / airspace closure"   → 81 or 82
"runway closed"                        → 83
"strike / industrial action"           → 91
"IT outage / systems failure"          → 93
"security threat"                      → 96
"government / customs restriction"     → 97

## SCENARIO TIPS (share these naturally when relevant)
- WCHR, WCHS, WCHC, UMNR, MEDA, OXYG, BLND SSR codes → triggers SSR safety guardrail halt
- Uncontrollable codes (71, 72, 75, 81-83, 91, 96, 97) → hotel voucher blocked by policy guardrail
- 4+ passengers → may hit the $0.05 per-run budget cap mid-workflow

## CALLING trigger_custom_workflow
- ONLY after the user explicitly confirms ("yes", "go", "trigger it", "run it", etc.)
- For std (scheduled time): use a near-future datetime in "YYYY-MM-DDTHH:MM:SS" format. \
  If the user doesn't specify, pick a reasonable one (e.g. "2025-07-15T10:00:00")
- passengers_json must be a valid JSON array string, e.g.:
  '[{"first_name":"John","last_name":"Smith","tier_status":"Gold","class_of_service":"J","ssr_codes":""}]'
- ssr_codes: comma-separated codes like "WCHR" or empty string "" if none

## TONE
Concise and technical — this is an internal tool, not a customer-facing chat.
Present confirmations as brief summaries. No markdown headers.
"""


def _make_trigger_tool(pending: dict):
    """
    Return a Strands @tool that seeds the DB and creates the workflow.
    Uses a closure to pass the workflow_id back to run_workflow_builder
    without requiring agent state access.
    """
    def trigger_custom_workflow(
        flight_num: str,
        origin: str,
        destination: str,
        std: str,
        flight_status: str,
        iata_delay_code: int,
        passengers_json: str,
    ) -> dict:
        """Seed the database and create a custom IROPS workflow. Call ONLY after user confirms.

        Args:
            flight_num: Flight number e.g. "QF9"
            origin: IATA origin airport code e.g. "SYD"
            destination: IATA destination airport code e.g. "LHR"
            std: Scheduled departure in ISO format e.g. "2025-07-15T10:00:00"
            flight_status: "CX" for cancellation, "DL" for delay
            iata_delay_code: Validated IATA delay code integer e.g. 41
            passengers_json: JSON array string. Each object: {"first_name": str,
                "last_name": str, "tier_status": "Platinum"|"Gold"|"Silver"|"Bronze"|"None",
                "class_of_service": "F"|"J"|"W"|"Y", "ssr_codes": str}
                Example: '[{"first_name":"Jane","last_name":"Lee","tier_status":"Platinum",
                "class_of_service":"J","ssr_codes":"WCHR"}]'

        Returns:
            dict with workflow_id and stream_url confirming the scenario was seeded.
        """
        try:
            pax_list = _json.loads(passengers_json)
        except _json.JSONDecodeError as exc:
            return {"status": "error", "message": f"passengers_json is not valid JSON: {exc}"}
        payload = {
            "flight_num":       flight_num.upper().strip(),
            "origin":           origin.upper().strip(),
            "destination":      destination.upper().strip(),
            "std":              std,
            "flight_status":    flight_status.upper().strip(),
            "iata_delay_code":  int(iata_delay_code),
            "passengers":       pax_list,
        }

        seed_custom_scenario(payload)
        workflow_id = create_workflow("CUSTOM", payload["flight_num"], payload["iata_delay_code"])

        pending["workflow_id"]      = workflow_id
        pending["flight_num"]       = payload["flight_num"]
        pending["iata_delay_code"]  = payload["iata_delay_code"]
        pending["payload_summary"]  = {
            "flight":       f"{payload['flight_num']} {payload['origin']}→{payload['destination']}",
            "disruption":   "Cancelled" if payload["flight_status"] == "CX" else "Delayed",
            "iata_code":    payload["iata_delay_code"],
            "passengers":   len(pax_list),
        }

        return {
            "status":       "ok",
            "workflow_id":  workflow_id,
            "stream_url":   f"/api/workflow/{workflow_id}/stream",
            "message":      (
                f"Custom scenario seeded. Workflow {workflow_id} created. "
                "The trace panel will activate momentarily."
            ),
        }

    return tool(trigger_custom_workflow)


async def run_workflow_builder(
    message: str,
    session_id: str,
    conversation_history: list[dict],
) -> AsyncGenerator[dict, None]:
    """
    Run the WorkflowBuilderAgent for one chat turn.
    Yields SSE event dicts consumed by POST /api/chat.

    If the agent calls trigger_custom_workflow during this turn,
    a background asyncio task is spawned and a custom_workflow_triggered
    event is yielded so the frontend can connect the SSE trace stream.

    Yields:
        {"type": "token", "text": str}
        {"type": "custom_workflow_triggered", "workflow_id": str, "stream_url": str, ...}
        {"type": "complete", "cost_usd": float}
        {"type": "error", "detail": str}
    """
    pending: dict = {}
    trigger_tool = _make_trigger_tool(pending)

    try:
        model = BedrockModel(
            model_id=AGENT_MODEL_CONFIG["concierge"],
            temperature=GOVERNANCE_LIMITS["chat_temperature"],
            max_tokens=1024,
        )

        agent = Agent(
            model=model,
            system_prompt=BUILDER_SYSTEM_PROMPT,
            tools=[lookup_iata_code, trigger_tool],
        )

        agent.state.set("cumulative_cost_usd", 0.0)

        # Build prompt with conversation history
        full_prompt = message
        if conversation_history:
            history_text = "\n".join(
                f"{t['role'].upper()}: {t['content']}"
                for t in conversation_history[-8:]
            )
            full_prompt = f"Conversation so far:\n{history_text}\n\nUser: {message}"

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: agent(full_prompt),
        )

        # Stream response as word tokens
        response_text = str(response)
        words = response_text.split(" ")
        for i, word in enumerate(words):
            yield {"type": "token", "text": word + (" " if i < len(words) - 1 else "")}

        # If the trigger tool fired, spawn background workflow and emit event
        if pending.get("workflow_id"):
            asyncio.create_task(
                run_internal_workflow(
                    workflow_id=pending["workflow_id"],
                    scenario="CUSTOM",
                    flight_num=pending["flight_num"],
                    iata_delay_code=pending["iata_delay_code"],
                )
            )
            yield {
                "type":            "custom_workflow_triggered",
                "workflow_id":     pending["workflow_id"],
                "stream_url":      f"/api/workflow/{pending['workflow_id']}/stream",
                "payload_summary": pending.get("payload_summary", {}),
            }

        cost = agent.state.get("cumulative_cost_usd") or 0.0
        yield {"type": "complete", "cost_usd": round(cost, 6)}

    except Exception as e:
        logger.error("workflow_builder_error", extra={"error": str(e), "session_id": session_id})
        yield {"type": "error", "detail": "An error occurred in the workflow builder. Please retry."}
