"""
api/hooks/guardrail.py
───────────────────────
GuardrailHook: application-level guardrails that fire BEFORE each tool
call in the internal workflow. Enforces three domain-specific constraints
that cannot be handled by Bedrock Guardrails (which only sees raw text,
not structured agent state).

TC-02  SSR Safety Hard Stop
       Fires before rebook_flight if the passenger has any safety-sensitive
       SSR code (WCHR, WCHS, WCHC, WCHP, UMNR, MEDA, OXYG, BLND).
       Halts the rebooking, calls flag_human_review, emits HUMAN_REVIEW step.

TC-03  Policy Alignment Check
       Fires before issue_voucher. Looks up the IATA delay code control
       classification.
       - Blocks HOTEL vouchers for uncontrollable events.
       - Blocks TRAVEL_VOUCHER for non-overbooking events (only valid for code 92).
       Allows MEAL_30_AUD for controllable delays >= 2h.

TC-04  Token Budget Cap
       Fires before every tool call. Checks cumulative_cost_usd in agent
       state against GOVERNANCE_LIMITS["max_token_budget_per_workflow_usd"].
       If exceeded, halts the entire workflow immediately.

Registered on:
  BeforeToolCallEvent — fires synchronously before Strands executes the tool

Note on halt mechanism: Strands does not yet expose a first-class
"cancel tool call" API on BeforeToolCallEvent. The approach here is to
raise a GuardrailException which propagates up through the agent loop,
is caught in the agent runner (orchestrator.py / rebooking.py), and
triggers graceful workflow termination with the correct status.
"""

from datetime import datetime, timezone
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

from ..config.governance import GOVERNANCE_LIMITS, SAFETY_SENSITIVE_SSR
from ..store.workflow_store import append_step, workflow_store
from ..skills.lookup_iata_code import lookup_iata_code


class GuardrailException(Exception):
    """Raised by GuardrailHook to halt the agent loop gracefully."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code    = code
        self.message = message


class GuardrailHook(HookProvider):
    """
    Strands HookProvider that enforces domain-specific guardrails before
    each tool call in the internal workflow.

    Instantiated per-workflow with the workflow_id and agent_name so
    guardrail events can be emitted as SSE trace steps.
    """

    def __init__(self, workflow_id: str, agent_name: str):
        self.workflow_id = workflow_id
        self.agent_name  = agent_name

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    # ── BeforeToolCallEvent ────────────────────────────────────────────────

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Run all guardrail checks before the tool executes."""
        tool_name   = getattr(event.selected_tool, "tool_name", "") or ""
        agent_state = event.agent.state

        # Order matters: budget cap first (cheapest check), then domain rules
        self._check_budget_cap(agent_state, tool_name)
        self._check_ssr_hard_stop(agent_state, tool_name)
        self._check_policy_alignment(event, agent_state, tool_name)

    # ── TC-04: Token Budget Cap ────────────────────────────────────────────

    def _check_budget_cap(self, agent_state: Any, tool_name: str) -> None:
        """Halt if cumulative workflow cost exceeds the configured cap."""
        cost = agent_state.get("cumulative_cost_usd") or 0.0
        cap  = GOVERNANCE_LIMITS["max_token_budget_per_workflow_usd"]

        if cost >= cap:
            reason = (
                f"Workflow cost ${cost:.4f} USD has reached the governance cap "
                f"of ${cap:.2f} USD. Agent loop terminated to prevent runaway spend. "
                f"Partial resolutions have been saved. Remaining passengers require "
                f"manual processing."
            )
            self._emit_guardrail_step("BUDGET_EXCEEDED", reason, tool_name)
            self._update_workflow_status("budget_exceeded")
            raise GuardrailException("BUDGET_EXCEEDED", reason)

    # ── TC-02: SSR Safety Hard Stop ────────────────────────────────────────

    def _check_ssr_hard_stop(self, agent_state: Any, tool_name: str) -> None:
        """Halt rebooking if passenger has a safety-sensitive SSR code."""
        if tool_name != "rebook_flight":
            return

        # "passenger_ssr_codes" is set by rebook_flight args via the agent prompt.
        # Primary enforcement: RebookingAgent calls check_ssr_codes() and skips
        # rebook_flight if safety_sensitive=True. This check is a belt-and-suspenders
        # guard for cases where the agent ignores the skill result.
        ssr_codes_str = agent_state.get("passenger_ssr_codes") or ""
        if not ssr_codes_str:
            return

        ssr_codes = [c.strip().upper() for c in ssr_codes_str.split(",") if c.strip()]
        triggered = [c for c in ssr_codes if c in SAFETY_SENSITIVE_SSR]

        if not triggered:
            return

        pax_id      = agent_state.get("current_pax_id", "unknown")
        pnr_locator = agent_state.get("current_pnr_locator", "unknown")
        codes_str   = ", ".join(triggered)

        reason = (
            f"Automated rebooking halted for passenger {pax_id} (PNR: {pnr_locator}). "
            f"Safety-sensitive SSR code(s) detected: {codes_str}. "
            f"Aircraft compatibility and ground handling arrangements must be "
            f"verified by a human agent before rebooking. "
            f"Case escalated to human review queue."
        )

        self._emit_guardrail_step("SSR_SAFETY_HALT", reason, tool_name)
        self._update_workflow_status("human_review", pax_id=pax_id)
        raise GuardrailException("SSR_SAFETY_HALT", reason)

    # ── TC-03: Policy Alignment Check ─────────────────────────────────────

    def _check_policy_alignment(self, event: BeforeToolCallEvent, agent_state: Any, tool_name: str) -> None:
        """Block voucher types that violate policy for the current event type."""
        if tool_name != "issue_voucher":
            return

        # Read voucher_type from the actual tool call arguments (reliable),
        # with agent state as fallback for backward compatibility.
        tool_use = getattr(event, "tool_use", None)
        tool_input = (getattr(tool_use, "input", None) or {}) if tool_use else {}
        pending_voucher = (
            tool_input.get("voucher_type")
            or agent_state.get("pending_voucher_type")
            or ""
        ).upper()

        if pending_voucher not in ("HOTEL", "TRAVEL_VOUCHER"):
            return  # MEAL_30_AUD and NONE never need policy blocking

        iata_code = agent_state.get("iata_delay_code")
        if iata_code is None:
            return  # No code available — fail open

        try:
            code_info = lookup_iata_code.func(iata_delay_code=int(iata_code))
        except Exception:
            return  # If lookup fails, fail open

        control = code_info.get("control", "uncontrollable")

        # Block HOTEL for uncontrollable events (weather, ATC, security, etc.)
        if pending_voucher == "HOTEL" and control == "uncontrollable":
            reason = (
                f"Hotel voucher BLOCKED by policy alignment guardrail. "
                f"IATA delay code {iata_code} ({code_info.get('label', 'unknown')}) "
                f"is classified as an UNCONTROLLABLE event. "
                f"Under Qantas Disruption Handling Guidelines section 3, "
                f"Qantas is not obligated to provide hotel accommodation for "
                f"events outside its control. Only rebooking is required."
            )
            self._emit_guardrail_step("POLICY_VIOLATION", reason, tool_name)
            # Do NOT halt the entire workflow — just block this specific voucher.
            # The agent will receive the exception and re-issue with MEAL_30_AUD or NONE.
            raise GuardrailException("POLICY_VIOLATION", reason)

        # Block TRAVEL_VOUCHER for non-overbooking events (only valid for code 92)
        if pending_voucher == "TRAVEL_VOUCHER" and int(iata_code) != 92:
            reason = (
                f"Travel voucher BLOCKED by policy alignment guardrail. "
                f"IATA delay code {iata_code} ({code_info.get('label', 'unknown')}) "
                f"is not a commercial overbooking event. "
                f"TRAVEL_VOUCHER is only issued for denied boarding due to overbooking "
                f"(IATA code 92) on international flights where the next available flight "
                f"departs more than 4 hours after scheduled departure. "
                f"Issue MEAL_30_AUD or HOTEL per the controllable disruption table instead."
            )
            self._emit_guardrail_step("POLICY_VIOLATION", reason, tool_name)
            raise GuardrailException("POLICY_VIOLATION", reason)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _emit_guardrail_step(
        self,
        event_code: str,
        reason: str,
        tool_name: str,
    ) -> None:
        """Append a GUARDRAIL trace step to the workflow store."""
        step = {
            "step_num":  -1,   # Will be assigned by append_step
            "type":      "guardrail",
            "agent":     self.agent_name,
            "action":    event_code,
            "status":    "halted" if event_code != "POLICY_VIOLATION" else "blocked",
            "detail":    reason,
            "tool_name": tool_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        append_step(self.workflow_id, step)

    def _update_workflow_status(
        self,
        status: str,
        pax_id: str = "",
    ) -> None:
        """Update the workflow store status field."""
        if self.workflow_id not in workflow_store:
            return
        workflow_store[self.workflow_id]["status"] = status
        if pax_id:
            workflow_store[self.workflow_id]["requires_human_review"] = True
            workflow_store[self.workflow_id]["guardrail_event"] = (
                f"{status.upper()} — pax_id: {pax_id}"
            )
