"""
api/db/database.py
──────────────────
SQLite helpers.  DB lives at /data/irops.db (bind-mounted from ./data/).

Public API
    get_connection()         → sqlite3.Connection with Row factory (caller closes)
    init_db()                → idempotent: create schema + seed (IF NOT EXISTS / OR IGNORE)
    reset_scenario(scenario) → clear RESOLUTION rows, restore segments/inventory, reset IROPS_LOG
    save_workflow_history()  → upsert completed workflow into WORKFLOW_HISTORY
    get_workflow_history()   → list of history records newest-first
    seed_custom_scenario()   → seed DB for WorkflowBuilderAgent custom scenarios
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = os.environ.get("IROPS_DB_PATH", "/data/irops.db")
_SCHEMA = Path(__file__).parent / "schema.sql"
_SEED   = Path(__file__).parent / "seed.sql"

_SCENARIO_IROPS_ID = {
    "A": "IROPS-A-001",
    "B": "IROPS-B-001",
    "C": "IROPS-C-001",
    "D": "IROPS-D-001",
}
_SCENARIO_FLIGHT = {
    "A": "QF400",
    "B": "QF731",
    "C": "QF8",
    "D": "QF500",
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create schema and seed demo data. Safe to call multiple times."""
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA.read_text())
        conn.executescript(_SEED.read_text())
    finally:
        conn.close()


def reset_scenario(scenario: str) -> None:
    """
    Reset a scenario so it can be re-triggered cleanly.

    For each AUTO_REBOOK RESOLUTION tied to the scenario's flight:
      - Re-increment seats on the alternate flight using the passenger's current class
      - Restore PASSENGER_SEGMENT.flight_num to the original disrupted flight
    Then delete all RESOLUTION rows for the scenario and reset the IROPS_LOG entry.
    """
    scenario   = scenario.upper()
    irops_id   = _SCENARIO_IROPS_ID.get(scenario)
    flight_num = _SCENARIO_FLIGHT.get(scenario)
    if not irops_id or not flight_num:
        return

    conn = get_connection()
    try:
        with conn:
            resolutions = conn.execute(
                "SELECT pax_id, new_flight_num FROM RESOLUTION WHERE original_flight = ?",
                (flight_num,),
            ).fetchall()

            for res in resolutions:
                if not res["new_flight_num"]:
                    continue
                seg = conn.execute(
                    "SELECT class_of_service FROM PASSENGER_SEGMENT WHERE pax_id = ?",
                    (res["pax_id"],),
                ).fetchone()
                if seg:
                    conn.execute(
                        """
                        UPDATE FLIGHT_INVENTORY
                        SET seats_available = seats_available + 1
                        WHERE flight_num = ? AND class_of_service = ?
                        """,
                        (res["new_flight_num"], seg["class_of_service"]),
                    )
                conn.execute(
                    "UPDATE PASSENGER_SEGMENT SET flight_num = ? WHERE pax_id = ?",
                    (flight_num, res["pax_id"]),
                )

            conn.execute(
                "DELETE FROM RESOLUTION WHERE original_flight = ?",
                (flight_num,),
            )
            conn.execute(
                """
                UPDATE IROPS_LOG
                SET resolution_status = 'pending', workflow_id = NULL
                WHERE irops_id = ?
                """,
                (irops_id,),
            )
    finally:
        conn.close()


def save_workflow_history(record: dict) -> None:
    """Upsert a completed workflow record into WORKFLOW_HISTORY."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO WORKFLOW_HISTORY (
                    workflow_id, scenario, flight_num, iata_delay_code,
                    status, passengers_resolved, total_cost_usd, total_tokens,
                    requires_human_review, guardrail_event, steps_json,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    status                = excluded.status,
                    passengers_resolved   = excluded.passengers_resolved,
                    total_cost_usd        = excluded.total_cost_usd,
                    total_tokens          = excluded.total_tokens,
                    requires_human_review = excluded.requires_human_review,
                    guardrail_event       = excluded.guardrail_event,
                    steps_json            = excluded.steps_json,
                    completed_at          = excluded.completed_at
                """,
                (
                    record.get("workflow_id"),
                    record.get("scenario"),
                    record.get("flight_num"),
                    record.get("iata_delay_code"),
                    record.get("status"),
                    record.get("passengers_resolved", 0),
                    record.get("total_cost_usd", 0.0),
                    record.get("total_tokens", 0),
                    1 if record.get("requires_human_review") else 0,
                    record.get("guardrail_event"),
                    json.dumps(record.get("steps", [])),
                    record.get("created_at"),
                    record.get("completed_at"),
                ),
            )
    finally:
        conn.close()


def get_workflow_history(workflow_id: str | None = None) -> list[dict]:
    """
    Return workflow history records ordered newest-first.
    If workflow_id is given, return that single record with steps parsed from JSON.
    List responses omit steps_json for compactness.
    """
    conn = get_connection()
    try:
        if workflow_id:
            row = conn.execute(
                "SELECT * FROM WORKFLOW_HISTORY WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if not row:
                return []
            record = dict(row)
            try:
                record["steps"] = json.loads(record.pop("steps_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                record["steps"] = []
            record["requires_human_review"] = bool(record.get("requires_human_review"))
            return [record]
        else:
            rows = conn.execute(
                """
                SELECT workflow_id, scenario, flight_num, iata_delay_code,
                       status, passengers_resolved, total_cost_usd, total_tokens,
                       requires_human_review, guardrail_event, created_at, completed_at
                FROM WORKFLOW_HISTORY
                ORDER BY created_at DESC
                """,
            ).fetchall()
            records = []
            for row in rows:
                r = dict(row)
                r["requires_human_review"] = bool(r.get("requires_human_review"))
                records.append(r)
            return records
    finally:
        conn.close()


def seed_custom_scenario(payload: dict) -> None:
    """
    Seed the database with a custom scenario from WorkflowBuilderAgent.

    payload keys: flight_num, origin, destination, std, flight_status,
                  iata_delay_code, passengers (list of dicts with
                  first_name, last_name, tier_status, class_of_service, ssr_codes)
    """
    flight_num      = payload["flight_num"].upper()
    origin          = payload["origin"].upper()
    destination     = payload["destination"].upper()
    std             = payload["std"]
    flight_status   = payload["flight_status"].upper()
    iata_delay_code = int(payload["iata_delay_code"])
    passengers      = payload["passengers"]
    triggered_at    = datetime.now(timezone.utc).isoformat()
    irops_id        = f"IROPS-CUSTOM-{uuid.uuid4().hex[:6].upper()}"

    # Alternate flight: same route, 4 hours later
    try:
        alt_std = (datetime.fromisoformat(std) + timedelta(hours=4)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    except ValueError:
        alt_std = std
    alt_flight = f"{flight_num}X"

    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO FLIGHT_LEG
                    (flight_num, origin_apt, dest_apt, std, eta, flight_status, aircraft_type)
                VALUES (?, ?, ?, ?, ?, ?, 'B737')
                """,
                (flight_num, origin, destination, std, std, flight_status),
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO FLIGHT_LEG
                    (flight_num, origin_apt, dest_apt, std, eta, flight_status, aircraft_type)
                VALUES (?, ?, ?, ?, ?, 'SC', 'B737')
                """,
                (alt_flight, origin, destination, alt_std, alt_std),
            )
            for cls, seats in [("F", 4), ("J", 8), ("W", 16), ("Y", 40)]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO FLIGHT_INVENTORY
                        (flight_num, class_of_service, seats_available)
                    VALUES (?, ?, ?)
                    """,
                    (alt_flight, cls, seats),
                )

            for pax in passengers:
                pax_id     = f"PAX-C-{uuid.uuid4().hex[:8].upper()}"
                pnr        = f"C{uuid.uuid4().hex[:5].upper()}"
                segment_id = f"SEG-C-{uuid.uuid4().hex[:8].upper()}"
                ticket_num = f"TKT-C-{uuid.uuid4().hex[:8].upper()}"
                now_str    = datetime.now(timezone.utc).isoformat()

                conn.execute(
                    """
                    INSERT OR IGNORE INTO PASSENGER_PROFILE
                        (pax_id, first_name, last_name, qff_number, tier_status, ssr_codes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pax_id,
                        pax.get("first_name", "Unknown"),
                        pax.get("last_name", "Passenger"),
                        f"QFF{uuid.uuid4().hex[:7].upper()}",
                        pax.get("tier_status", "None"),
                        pax.get("ssr_codes", "") or None,
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO PNR_HEADER (pnr_locator, booking_status, created_at) VALUES (?, 'HK', ?)",
                    (pnr, now_str),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO PASSENGER_SEGMENT
                        (segment_id, pnr_locator, pax_id, flight_num, ticket_num, class_of_service)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment_id, pnr, pax_id, flight_num,
                        ticket_num, pax.get("class_of_service", "Y"),
                    ),
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO IROPS_LOG
                    (irops_id, flight_num, iata_delay_code, triggered_at, resolution_status, is_custom)
                VALUES (?, ?, ?, ?, 'pending', 1)
                """,
                (irops_id, flight_num, iata_delay_code, triggered_at),
            )
    finally:
        conn.close()
