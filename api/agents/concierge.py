"""
api/agents/concierge.py
────────────────────────
ConciergeAgent: customer-facing conversational agent for the IROPS demo.

Standalone — not called by the Orchestrator. Has its own endpoint:
POST /api/chat

Two-layer guardrail architecture:
  Layer 1 (Bedrock Guardrails): Applied to raw user input BEFORE the
           agent is invoked. Blocks off-topic queries (new bookings,
           seat upgrades, loyalty points) and prompt injection.
           Returns a guardrail_block SSE event immediately if triggered.

  Layer 2 (Application guardrails via GuardrailHook): Fires before
           get_pnr_status and flag_human_review tool calls within
           the agent loop. Handles domain-specific logic.

Skills used for document checks:
  - lookup_policy: answers compensation questions from policy chunks
  - check_ssr_codes: explains SSR needs to the passenger

Tools used for data access:
  - get_pnr_status: retrieves current booking + resolution status
  - flag_human_review: escalates complex cases to human agents
"""

import json
import logging
import os
from typing import AsyncGenerator

import boto3
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from ..config.governance import AGENT_MODEL_CONFIG, GOVERNANCE_LIMITS
from ..agents.orchestrator import run_internal_workflow
from ..db.database import seed_custom_scenario
from ..hooks.workflow_trace import WorkflowTraceHook
from ..skills.check_ssr_codes import check_ssr_codes
from ..skills.lookup_iata_code import lookup_iata_code
from ..skills.lookup_policy import lookup_policy
from ..store.workflow_store import create_workflow
from ..tools.flag_human_review import flag_human_review
from ..tools.get_pnr_status import get_pnr_status

logger = logging.getLogger(__name__)

CONCIERGE_SYSTEM_PROMPT = """You are the Qantas IROPS Demo Assistant. You handle three types of requests \
and route to the right tools automatically. Never assume the user's personal details.

## INTENT 1 — POLICY QUESTIONS
Trigger: user asks what compensation they are entitled to, what the policy says, \
what happens for delays/cancellations/overbooking, SSR handling, etc.
Action: call lookup_policy(query) and answer based only on what it returns. \
Never invent policy details. Cite the policy chunk you used.

## INTENT 2 — BOOKING STATUS
Trigger: user provides a PNR locator (6-char code like AABBCC) and asks \
about their rebooking or voucher status.
Action: call get_pnr_status(pnr_locator) and report exactly what the data shows. \
Never infer or guess fields not returned by the tool.

## INTENT 3 — PIPELINE SIMULATION
Trigger: user wants to run a demo, test the workflow, see the system in action, \
or asks what scenarios are available.
Action: collect the minimum data needed, then call trigger_custom_workflow \
ONLY after the user explicitly confirms. If the user has no preference, \
suggest a concrete example scenario and ask if they want to run it.

Data to collect for a simulation (ask 1-2 things per turn, conversationally):
  - Flight number (any format: QF9, EK415)
  - Origin and destination (IATA codes: SYD, MEL, LHR, DXB)
  - Disruption type: cancellation (CX) or delay (DL)
  - Cause → infer the IATA code, call lookup_iata_code to confirm
  - At least one passenger: first name, last name, loyalty tier \
    (Platinum/Gold/Silver/Bronze/None), cabin class (F/J/W/Y), SSR codes if any

IATA code inference reference:
  mechanical/AOG → 41 | carried-over defect → 42 | maintenance overrun → 43
  weather/storm/fog → 71 or 72 | de-icing → 75
  ATC/airspace → 81-82 | runway closed → 83 | strike → 91
  IT outage → 93 | security → 96 | government/customs → 97

Interesting demo combinations to suggest naturally:
  - WCHR/UMNR/MEDA SSR codes → triggers SSR safety halt (TC-02)
  - Uncontrollable codes 71-97 → hotel voucher blocked by policy guardrail (TC-03)

## OUT OF SCOPE
Anything unrelated to flight disruptions (new bookings, seat upgrades, \
loyalty points, baggage) is handled by infrastructure guardrails. \
If a topic reaches you that is clearly out of scope, decline politely in one sentence.

## TONE
- Conversational and concise — 2-4 sentences per turn
- Never invent data, never assume the user's identity or booking details
- Only generate demo/simulated data when the user asks for it

## FORMAT
Plain conversational text. No markdown headers. No bullet lists unless the user \
asks for a breakdown. One clear message per turn.
"""


def _get_bedrock_guardrail_client() -> boto3.client:
    """Return a Bedrock runtime client for guardrail application."""
    return boto3.client("bedrock-runtime", region_name="us-east-1")


import re as _re

# Patterns that clearly signal disruption-domain intent — Bedrock's topic classifier
# can false-positive on words like "Gold" (loyalty tier) or "weather" (travel advice).
# These patterns pre-approve the input so the classifier is not invoked for them.
_DISRUPTION_INTENT_PATTERNS = [
    r"\b(simulat|scenario|test|run a|trigger|demo)\b",     # simulation intent
    r"\b(delay|cancell|disruption|irops|rebook|rebooked)\b",  # operational terms
    r"\b(pnr|booking ref|locator)\b",                      # booking status lookup
    r"\b(compensation|voucher|entitl|policy)\b",           # policy questions
    r"\b(ssr|wchr|umnr|meda|passenger)\b",                 # passenger handling
    r"\b(platinum|gold|silver|bronze)\s+(passenger|tier|class|member)\b",  # tier as attribute
]
_DISRUPTION_RE = _re.compile(
    "|".join(_DISRUPTION_INTENT_PATTERNS), _re.IGNORECASE
)


def _is_clearly_disruption_domain(text: str) -> bool:
    """Return True if input clearly concerns flight disruptions — skip Bedrock classifier."""
    return bool(_DISRUPTION_RE.search(text))


def _apply_bedrock_guardrail(user_input: str) -> dict:
    """
    Apply the Bedrock Guardrail to raw user input.

    Returns:
        dict with keys:
            blocked:  bool   — True if guardrail blocked the input
            action:   str    — "GUARDRAIL_INTERVENED" or "NONE"
            topic:    str    — matched denied topic name (if blocked)
            message:  str    — blocked_input_messaging from guardrail config
    """
    # Short-circuit for inputs that are unambiguously in-scope — avoids false positives
    # from Bedrock's topic classifier on terms like "Gold passenger" or "weather scenario".
    if _is_clearly_disruption_domain(user_input):
        return {"blocked": False, "action": "NONE", "topic": "", "message": ""}

    guardrail_id      = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
    guardrail_version = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "1")

    if not guardrail_id:
        # No guardrail configured — pass through (dev mode)
        logger.warning("BEDROCK_GUARDRAIL_ID not set — skipping guardrail check")
        return {"blocked": False, "action": "NONE", "topic": "", "message": ""}

    try:
        client = _get_bedrock_guardrail_client()
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
            source="INPUT",
            content=[{"text": {"text": user_input}}],
        )

        action = response.get("action", "NONE")
        if action == "GUARDRAIL_INTERVENED":
            # Extract which topic was matched from assessments
            topic = ""
            for assessment in response.get("assessments", []):
                topic_policy = assessment.get("topicPolicy", {})
                for topic_entry in topic_policy.get("topics", []):
                    if topic_entry.get("action") == "BLOCKED":
                        topic = topic_entry.get("name", "")
                        break
                if topic:
                    break

            blocked_message = response.get("outputs", [{}])[0].get("text", (
                "This assistant is scoped to active flight disruption assistance only."
            ))

            return {
                "blocked": True,
                "action":  "GUARDRAIL_INTERVENED",
                "topic":   topic,
                "message": blocked_message,
            }

        return {"blocked": False, "action": "NONE", "topic": "", "message": ""}

    except Exception as e:
        logger.error("bedrock_guardrail_error", extra={"error": str(e)})
        # Fail open — let the agent handle it (GuardrailHook is backup)
        return {"blocked": False, "action": "ERROR", "topic": "", "message": ""}


def _make_trigger_tool(pending: dict):
    """Return a trigger_custom_workflow tool that passes workflow_id back via closure."""
    def trigger_custom_workflow(
        flight_num: str,
        origin: str,
        destination: str,
        std: str,
        flight_status: str,
        iata_delay_code: int,
        passengers_json: str,
    ) -> dict:
        """Seed the database and launch a custom IROPS workflow. Call ONLY after user confirms.

        Args:
            flight_num: Flight number e.g. "QF9"
            origin: IATA origin airport code e.g. "SYD"
            destination: IATA destination airport code e.g. "LHR"
            std: Scheduled departure ISO datetime e.g. "2025-07-15T10:00:00"
            flight_status: "CX" for cancellation, "DL" for delay
            iata_delay_code: Validated IATA delay code integer e.g. 41
            passengers_json: JSON array string. Each object needs first_name, last_name,
                tier_status (Platinum/Gold/Silver/Bronze/None),
                class_of_service (F/J/W/Y), ssr_codes (comma-separated or empty string).

        Returns:
            dict with workflow_id and stream_url.
        """
        try:
            pax_list = json.loads(passengers_json)
        except json.JSONDecodeError as exc:
            return {"status": "error", "message": f"passengers_json is not valid JSON: {exc}"}

        payload = {
            "flight_num":      flight_num.upper().strip(),
            "origin":          origin.upper().strip(),
            "destination":     destination.upper().strip(),
            "std":             std,
            "flight_status":   flight_status.upper().strip(),
            "iata_delay_code": int(iata_delay_code),
            "passengers":      pax_list,
        }

        seed_custom_scenario(payload)
        workflow_id = create_workflow("CUSTOM", payload["flight_num"], payload["iata_delay_code"])

        pending["workflow_id"]     = workflow_id
        pending["flight_num"]      = payload["flight_num"]
        pending["iata_delay_code"] = payload["iata_delay_code"]
        pending["payload_summary"] = {
            "flight":     f"{payload['flight_num']} {payload['origin']}→{payload['destination']}",
            "disruption": "Cancelled" if payload["flight_status"] == "CX" else "Delayed",
            "iata_code":  payload["iata_delay_code"],
            "passengers": len(pax_list),
        }

        return {
            "status":      "ok",
            "workflow_id": workflow_id,
            "stream_url":  f"/api/workflow/{workflow_id}/stream",
            "message":     f"Scenario seeded. Workflow {workflow_id} created. Trace panel will activate.",
        }

    return tool(trigger_custom_workflow)


async def run_concierge(
    message: str,
    session_id: str,
    conversation_history: list[dict],
) -> AsyncGenerator[dict, None]:
    """
    Unified concierge: routes policy questions, booking status, and
    pipeline simulations from a single chat interface.

    Applies Bedrock Guardrail first, then invokes the agent.

    Yields SSE event dicts:
      {"type": "guardrail_block", "layer": "bedrock", "topic": str, "message": str}
      {"type": "token", "text": str}
      {"type": "custom_workflow_triggered", "workflow_id": str, "stream_url": str, ...}
      {"type": "complete", "cost_usd": float}
      {"type": "error", "detail": str}
    """
    import asyncio

    # ── Bedrock Guardrail ─────────────────────────────────────────────
    guardrail_result = _apply_bedrock_guardrail(message)
    if guardrail_result["blocked"]:
        yield {
            "type":    "guardrail_block",
            "layer":   "bedrock",
            "topic":   guardrail_result["topic"],
            "message": guardrail_result["message"],
        }
        return

    # ── Agent invocation ──────────────────────────────────────────────
    pending: dict = {}
    trigger_tool  = _make_trigger_tool(pending)

    try:
        model = BedrockModel(
            model_id=AGENT_MODEL_CONFIG["concierge"],
            temperature=GOVERNANCE_LIMITS["chat_temperature"],
            max_tokens=1024,
        )

        hooks = [WorkflowTraceHook(workflow_id=session_id, agent_name="concierge")]

        agent = Agent(
            model=model,
            system_prompt=CONCIERGE_SYSTEM_PROMPT,
            tools=[
                lookup_policy,
                lookup_iata_code,
                check_ssr_codes,
                get_pnr_status,
                flag_human_review,
                trigger_tool,
            ],
            hooks=hooks,
        )

        agent.state.set("session_id",          session_id)
        agent.state.set("workflow_id",         session_id)
        agent.state.set("cumulative_cost_usd", 0.0)

        full_prompt = message
        if conversation_history:
            history_text = "\n".join(
                f"{t['role'].upper()}: {t['content']}"
                for t in conversation_history[-8:]
            )
            full_prompt = f"Conversation so far:\n{history_text}\n\nUser: {message}"

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: agent(full_prompt))

        import re as _re
        response_text = _re.sub(r"<thinking>.*?</thinking>", "", str(response), flags=_re.DOTALL).strip()
        words = response_text.split(" ")
        for i, word in enumerate(words):
            yield {"type": "token", "text": word + (" " if i < len(words) - 1 else "")}

        # If trigger fired, spawn background workflow and notify frontend
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
        logger.error("concierge_error", extra={"error": str(e), "session_id": session_id})
        yield {"type": "error", "detail": "An error occurred. Please try again."}
