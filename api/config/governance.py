"""
api/config/governance.py
─────────────────────────
Single source of truth for all governance configuration.

Demonstrates three JD competencies in one file:
  - Model selection governance (right model for the right task)
  - Platform cost governance (budget cap, iteration limits)
  - Responsible AI configuration (temperature constraints, SSR safety codes)

Imported by: agents, hooks, store, main.py
"""

# ── Model selection ────────────────────────────────────────────────────────
# Haiku 4.5: orchestrator + rebooking (reliable multi-step tool-use chains).
# Nova Lite: fetcher + concierge (simple reads and chat — cost-optimised).
AGENT_MODEL_CONFIG: dict[str, str] = {
    "orchestrator": "amazon.nova-lite-v1:0",
    "fetcher":      "amazon.nova-lite-v1:0",
    "rebooking":    "amazon.nova-lite-v1:0",
    "concierge":    "amazon.nova-lite-v1:0",
}

# ── Governance limits ──────────────────────────────────────────────────────
GOVERNANCE_LIMITS: dict[str, float | int] = {
    # Cost cap per workflow — TC-04 trigger threshold
    # Chosen to allow ~5 passengers before firing in Scenario D
    "max_token_budget_per_workflow_usd": 0.10,

    # Max tool-call iterations per agent invocation
    # Prevents runaway loops if an agent gets stuck
    "max_agent_iterations": 4,

    # Temperature for SQL-generating agents (FetcherAgent)
    # 0.0 = fully deterministic — no hallucinated column names
    "deterministic_temperature": 0.0,

    # Temperature for customer-facing chat (ConciergeAgent)
    "chat_temperature": 0.7,

    # Temperature for rebooking logic (RebookingAgent)
    # Low but not zero — needs some flexibility for edge cases
    "rebooking_temperature": 0.2,
}

# ── Bedrock pricing (us-east-1, June 2025) ────────────────────────────────
# Used by WorkflowTraceHook and TokenLedger for live cost calculation.
# Units: USD per 1,000 tokens.
BEDROCK_PRICING_USD_PER_1K: dict[str, dict[str, float]] = {
    "amazon.nova-lite-v1:0": {
        "input":  0.00006,
        "output": 0.00024,
    },
}

# ── Safety-sensitive SSR codes ─────────────────────────────────────────────
# Passengers with any of these codes must NOT be automatically rebooked.
# GuardrailHook TC-02 checks against this set before every rebook_flight call.
# Source: IATA SSR code definitions + Qantas ground handling policy.
SAFETY_SENSITIVE_SSR: set[str] = {
    "WCHR",  # Wheelchair — ramp
    "WCHS",  # Wheelchair — steps
    "WCHC",  # Wheelchair — cabin (fully immobile)
    "WCHP",  # Wheelchair — power/electric
    "UMNR",  # Unaccompanied minor
    "MEDA",  # Medical case — requires clearance
    "OXYG",  # Supplemental oxygen required
    "BLND",  # Blind passenger (may have guide dog)
}

# ── Scenario metadata ──────────────────────────────────────────────────────
# Used by main.py to look up seed data for each demo scenario button.
SCENARIO_CONFIG: dict[str, dict] = {
    "A": {
        "label":          "Scenario A — Happy Path (AOG, Platinum)",
        "flight_num":     "QF400",
        "irops_id":       "IROPS-A-001",
        "iata_delay_code": 41,
        "description":    "QF400 SYD→MEL cancelled due to AOG (controllable). "
                          "Platinum passenger auto-rebooked + $30 meal voucher.",
    },
    "B": {
        "label":          "Scenario B — Weather Delay (Policy Guardrail TC-03)",
        "flight_num":     "QF731",
        "irops_id":       "IROPS-B-001",
        "iata_delay_code": 71,
        "description":    "QF731 SYD→BNE delayed by weather (uncontrollable). "
                          "Bronze passenger rebooked — hotel voucher BLOCKED by policy.",
    },
    "C": {
        "label":          "Scenario C — WCHR Passenger (SSR Hard Stop TC-02)",
        "flight_num":     "QF8",
        "irops_id":       "IROPS-C-001",
        "iata_delay_code": 41,
        "description":    "QF8 SYD→LHR cancelled. Gold passenger with WCHR SSR — "
                          "automated rebooking halted, case routed to human agent.",
    },
    "D": {
        "label":          "Scenario D — Budget Cap (TC-04, 12 passengers)",
        "flight_num":     "QF500",
        "irops_id":       "IROPS-D-001",
        "iata_delay_code": 41,
        "description":    "QF500 MEL→SYD cancelled. 12 passengers across all tiers — "
                          "workflow cost cap fires mid-processing.",
    },
}
