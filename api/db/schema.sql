CREATE TABLE IF NOT EXISTS FLIGHT_LEG (
    flight_num      TEXT PRIMARY KEY,
    origin_apt      TEXT NOT NULL,
    dest_apt        TEXT NOT NULL,
    std             TEXT NOT NULL,  -- scheduled departure (ISO-8601 local)
    eta             TEXT,           -- estimated arrival (ISO-8601 local)
    flight_status   TEXT NOT NULL   -- 'SC'=scheduled, 'DL'=delayed, 'CX'=cancelled
                    CHECK (flight_status IN ('SC', 'DL', 'CX')),
    aircraft_type   TEXT
);

CREATE TABLE IF NOT EXISTS FLIGHT_INVENTORY (
    flight_num      TEXT NOT NULL REFERENCES FLIGHT_LEG(flight_num),
    class_of_service TEXT NOT NULL
                    CHECK (class_of_service IN ('F', 'J', 'W', 'Y')),
    seats_available INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (flight_num, class_of_service)
);

CREATE TABLE IF NOT EXISTS PASSENGER_PROFILE (
    pax_id          TEXT PRIMARY KEY,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    qff_number      TEXT,
    tier_status     TEXT NOT NULL   -- Platinum > Gold > Silver > Bronze > None
                    CHECK (tier_status IN ('Platinum', 'Gold', 'Silver', 'Bronze', 'None')),
    ssr_codes       TEXT            -- comma-separated IATA SSR codes e.g. 'WCHR,BLND'
);

CREATE TABLE IF NOT EXISTS PNR_HEADER (
    pnr_locator     TEXT PRIMARY KEY,
    booking_status  TEXT NOT NULL   -- 'HK'=confirmed, 'XX'=cancelled
                    CHECK (booking_status IN ('HK', 'XX')),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS PASSENGER_SEGMENT (
    segment_id      TEXT PRIMARY KEY,
    pnr_locator     TEXT NOT NULL REFERENCES PNR_HEADER(pnr_locator),
    pax_id          TEXT NOT NULL,
    flight_num      TEXT NOT NULL REFERENCES FLIGHT_LEG(flight_num),
    ticket_num      TEXT NOT NULL,
    class_of_service TEXT NOT NULL  -- 'F'=First, 'J'=Business, 'W'=PremEco, 'Y'=Economy
                    CHECK (class_of_service IN ('F', 'J', 'W', 'Y'))
);

CREATE TABLE IF NOT EXISTS IROPS_LOG (
    irops_id            TEXT PRIMARY KEY,
    flight_num          TEXT NOT NULL REFERENCES FLIGHT_LEG(flight_num),
    iata_delay_code     INTEGER NOT NULL,
    triggered_at        TEXT NOT NULL,
    workflow_id         TEXT,
    resolution_status   TEXT NOT NULL DEFAULT 'pending'
                        CHECK (resolution_status IN ('pending', 'resolved', 'human_review', 'budget_exceeded')),
    is_custom           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS RESOLUTION (
    resolution_id       TEXT PRIMARY KEY,
    workflow_id         TEXT NOT NULL,
    pnr_locator         TEXT NOT NULL REFERENCES PNR_HEADER(pnr_locator),
    pax_id              TEXT NOT NULL,
    original_flight     TEXT NOT NULL,
    new_flight_num      TEXT,
    action_taken        TEXT,
    voucher_type        TEXT,
    requires_human_review INTEGER NOT NULL DEFAULT 0,
    guardrail_events    TEXT,
    agent_justification TEXT,
    resolved_at         TEXT
);

CREATE TABLE IF NOT EXISTS WORKFLOW_HISTORY (
    workflow_id             TEXT PRIMARY KEY,
    scenario                TEXT NOT NULL,
    flight_num              TEXT NOT NULL,
    iata_delay_code         INTEGER NOT NULL,
    status                  TEXT NOT NULL,
    passengers_resolved     INTEGER NOT NULL DEFAULT 0,
    total_cost_usd          REAL NOT NULL DEFAULT 0.0,
    total_tokens            INTEGER NOT NULL DEFAULT 0,
    requires_human_review   INTEGER NOT NULL DEFAULT 0,
    guardrail_event         TEXT,
    steps_json              TEXT NOT NULL DEFAULT '[]',
    created_at              TEXT NOT NULL,
    completed_at            TEXT
);
