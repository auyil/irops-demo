"""
api/agents/fetcher.py
──────────────────────
FetcherAgent: owns all database reads for the internal workflow.

Silo responsibility:
  Given a flight disruption event, return the full affected passenger
  manifest ranked by tier priority, plus available alternate flight
  inventory. No writes. No decisions. Pure data retrieval.

Called by the Orchestrator as a subagent tool.
Registers WorkflowTraceHook + GuardrailHook for observability.

The system prompt contains the full semantic layer (DDL + glossary +
gold queries) — the "Amazon Q-style semantic layer" referenced in the
functional spec. This teaches the agent to reason about the schema
without hallucinating column names.
"""

from strands import Agent
from strands.models.bedrock import BedrockModel

from ..config.governance import AGENT_MODEL_CONFIG, GOVERNANCE_LIMITS
from ..hooks.guardrail import GuardrailHook
from ..hooks.workflow_trace import WorkflowTraceHook
from ..tools.query_flight_status import query_flight_status
from ..tools.query_inventory import query_inventory
from ..tools.query_passengers import query_passengers

# ── Semantic layer system prompt ───────────────────────────────────────────
# Injected verbatim into FetcherAgent — constitutes the Amazon Q-style
# semantic layer. Teaches the agent the schema, glossary, and query patterns.

_SEMANTIC_LAYER = """
## DATABASE SCHEMA (DDL)

CREATE TABLE FLIGHT_LEG (
    flight_num       TEXT PRIMARY KEY,
    origin_apt       TEXT NOT NULL,        -- IATA airport code e.g. 'SYD'
    dest_apt         TEXT NOT NULL,
    std              TEXT NOT NULL,        -- scheduled departure ISO-8601
    eta              TEXT,                 -- estimated arrival ISO-8601
    flight_status    TEXT NOT NULL,        -- 'SC'=scheduled 'DL'=delayed 'CX'=cancelled
    aircraft_type    TEXT
);

CREATE TABLE PNR_HEADER (
    pnr_locator      TEXT PRIMARY KEY,     -- 6-char alphanumeric e.g. 'AABBCC'
    booking_status   TEXT NOT NULL,        -- 'HK'=confirmed 'XX'=cancelled
    created_at       TEXT NOT NULL
);

CREATE TABLE PASSENGER_SEGMENT (
    segment_id       TEXT PRIMARY KEY,
    pnr_locator      TEXT REFERENCES PNR_HEADER,
    pax_id           TEXT NOT NULL,
    flight_num       TEXT REFERENCES FLIGHT_LEG,
    ticket_num       TEXT NOT NULL,
    class_of_service TEXT NOT NULL         -- 'F'=First 'J'=Business 'W'=PremEco 'Y'=Economy
);

CREATE TABLE PASSENGER_PROFILE (
    pax_id           TEXT PRIMARY KEY,
    first_name       TEXT NOT NULL,
    last_name        TEXT NOT NULL,
    qff_number       TEXT,
    tier_status      TEXT NOT NULL,        -- 'Platinum' > 'Gold' > 'Silver' > 'Bronze' > 'None'
    ssr_codes        TEXT                  -- comma-separated e.g. 'WCHR,DBML'
);

CREATE TABLE FLIGHT_INVENTORY (
    flight_num       TEXT REFERENCES FLIGHT_LEG,
    class_of_service TEXT NOT NULL,
    seats_available  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flight_num, class_of_service)
);

CREATE TABLE IROPS_LOG (
    irops_id         TEXT PRIMARY KEY,
    flight_num       TEXT REFERENCES FLIGHT_LEG,
    iata_delay_code  INTEGER NOT NULL,
    triggered_at     TEXT NOT NULL,
    workflow_id      TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'pending'
);

## BUSINESS GLOSSARY

| Term                  | Data mapping                                        |
|-----------------------|-----------------------------------------------------|
| cancelled flight      | flight_status = 'CX'                                |
| delayed flight        | flight_status = 'DL'                                |
| weather delay         | iata_delay_code = 71 or 72                          |
| AOG / mechanical      | iata_delay_code = 41, 42, or 43                     |
| controllable event    | iata_delay_code IN (14,21,31,41,42,43,61,93)        |
| uncontrollable event  | iata_delay_code IN (71,72,75,81,82,83,91,96,97)     |
| premium passenger     | tier_status IN ('Platinum', 'Gold')                 |
| needs assistance      | ssr_codes IS NOT NULL AND ssr_codes != ''           |
| confirmed booking     | booking_status = 'HK'                               |
| next available flight | flight_status = 'SC' AND std > <disruption time>    |

## GOLD STANDARD QUERIES

-- 1. All confirmed passengers on a disrupted flight, tier-ranked
SELECT pp.*, ps.class_of_service, ps.segment_id, ps.pnr_locator
FROM PASSENGER_SEGMENT ps
JOIN PASSENGER_PROFILE pp ON ps.pax_id = pp.pax_id
JOIN PNR_HEADER ph ON ps.pnr_locator = ph.pnr_locator
WHERE ps.flight_num = 'QF400' AND ph.booking_status = 'HK'
ORDER BY
  CASE pp.tier_status
    WHEN 'Platinum' THEN 1 WHEN 'Gold' THEN 2
    WHEN 'Silver' THEN 3 WHEN 'Bronze' THEN 4 ELSE 5 END,
  CASE ps.class_of_service
    WHEN 'F' THEN 1 WHEN 'J' THEN 2 WHEN 'W' THEN 3 ELSE 4 END;

-- 2. Available alternate flights with inventory for a route
SELECT fl.*, fi.class_of_service, fi.seats_available
FROM FLIGHT_LEG fl
JOIN FLIGHT_INVENTORY fi ON fl.flight_num = fi.flight_num
WHERE fl.origin_apt = 'SYD' AND fl.dest_apt = 'MEL'
  AND fl.std > '2025-07-14T10:15:00'
  AND fl.flight_status = 'SC'
  AND fi.seats_available > 0
ORDER BY fl.std ASC;

-- 3. Passengers with safety-sensitive SSR codes on a cancelled flight
SELECT pp.pax_id, pp.first_name, pp.last_name, pp.ssr_codes, pp.tier_status
FROM PASSENGER_SEGMENT ps
JOIN PASSENGER_PROFILE pp ON ps.pax_id = pp.pax_id
JOIN PNR_HEADER ph ON ps.pnr_locator = ph.pnr_locator
WHERE ps.flight_num = 'QF8'
  AND ph.booking_status = 'HK'
  AND pp.ssr_codes IS NOT NULL AND pp.ssr_codes != '';
"""

FETCHER_SYSTEM_PROMPT = f"""You are FetcherAgent, the data retrieval specialist in the Qantas IROPS
autonomous disruption management system.

## YOUR SOLE RESPONSIBILITY
Retrieve accurate passenger and inventory data for a disrupted flight.
You do NOT make rebooking decisions. You do NOT issue vouchers.
You return structured data for RebookingAgent to act on.

## TOOLS AVAILABLE
- query_flight_status: Get flight details and IROPS log entry
- query_passengers: Get tier-ranked passenger manifest for a flight
- query_inventory: Get available alternate flights with seat counts

## EXECUTION SEQUENCE
Always follow this exact sequence:
1. Call query_flight_status(flight_num) — confirm disruption details and IATA code
2. Call query_passengers(flight_num) — get full passenger manifest, tier-ranked
3. For each unique route on the manifest, call query_inventory(origin, destination,
   after_datetime, required_class) — find alternate flights with seats

## OUTPUT FORMAT
Return a structured summary containing:
- Flight status confirmation (flight_num, status, IATA code, control classification)
- Passenger manifest (count, tier breakdown, any SSR flags)
- Inventory options (alternate flights with available seats per class)

## SEMANTIC LAYER
{_SEMANTIC_LAYER}

## CONSTRAINTS
- Never hallucinate flight numbers, passenger names, or seat counts
- If a query returns no results, report that clearly — do not invent data
- Temperature is set to 0.0 — your output must be deterministic and accurate
"""


def create_fetcher_agent(workflow_id: str) -> Agent:
    """
    Create a FetcherAgent instance for a specific workflow.

    Args:
        workflow_id: Workflow ID for hook registration and trace correlation

    Returns:
        Strands Agent configured for database reads
    """
    model = BedrockModel(
        model_id=AGENT_MODEL_CONFIG["fetcher"],
        temperature=GOVERNANCE_LIMITS["deterministic_temperature"],
        max_tokens=4096,
    )

    hooks = [
        WorkflowTraceHook(workflow_id=workflow_id, agent_name="fetcher"),
        GuardrailHook(workflow_id=workflow_id,     agent_name="fetcher"),
    ]

    agent = Agent(
        model=model,
        system_prompt=FETCHER_SYSTEM_PROMPT,
        tools=[
            query_flight_status,
            query_passengers,
            query_inventory,
        ],
        hooks=hooks,
    )

    return agent
