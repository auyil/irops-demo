"""
api/skills/check_ssr_codes.py
──────────────────────────────
Stateless skill: translates IATA Special Service Request (SSR) codes
into human-readable meanings and flags safety-sensitive codes that
require human agent handling rather than automated rebooking.

Used by:
  - ConciergeAgent  — to explain SSR needs to the passenger
  - GuardrailHook   — TC-02 SSR hard stop check before rebook_flight

No model call. Pure reference lookup against the embedded table.
"""

from strands import tool


# IATA standard SSR codes relevant to passenger handling.
# safety_sensitive=True means automated rebooking must be halted —
# a human agent must verify aircraft compatibility and ground arrangements.
SSR_CODES: dict[str, dict] = {
    # ── Mobility / physical assistance ────────────────────────────────
    "WCHR": {
        "label": "Wheelchair — ramp (passenger can walk short distances)",
        "category": "Mobility Assistance",
        "safety_sensitive": True,
        "handling_note": (
            "Aircraft and gate compatibility must be verified. "
            "Ground transport and boarding lift availability required. "
            "Must be handled by human agent."
        ),
    },
    "WCHS": {
        "label": "Wheelchair — steps (passenger cannot climb stairs)",
        "category": "Mobility Assistance",
        "safety_sensitive": True,
        "handling_note": (
            "Requires jet bridge or ambulift. Not all aircraft/gates compatible. "
            "Must be handled by human agent."
        ),
    },
    "WCHC": {
        "label": "Wheelchair — cabin seat (passenger completely immobile)",
        "category": "Mobility Assistance",
        "safety_sensitive": True,
        "handling_note": (
            "Requires specific seating position and in-flight assistance plan. "
            "Aircraft type compatibility is critical. "
            "Must be handled by human agent."
        ),
    },
    "WCHP": {
        "label": "Wheelchair — with power (electric wheelchair)",
        "category": "Mobility Assistance",
        "safety_sensitive": True,
        "handling_note": (
            "Battery type (lithium/gel) determines carriage rules. "
            "Cargo hold dimensions must be verified. "
            "Must be handled by human agent."
        ),
    },
    # ── Sensory / communication needs ─────────────────────────────────
    "BLND": {
        "label": "Blind passenger — may travel with guide dog",
        "category": "Sensory Assistance",
        "safety_sensitive": True,
        "handling_note": (
            "Safety briefing must be provided verbally. "
            "Guide dog carriage rules apply if applicable. "
            "Seat assignment must allow aisle access."
        ),
    },
    "DEAF": {
        "label": "Deaf or hard of hearing passenger",
        "category": "Sensory Assistance",
        "safety_sensitive": False,
        "handling_note": (
            "Safety card and written briefing must be provided. "
            "Automated rebooking is permitted but note must be transferred."
        ),
    },
    # ── Unaccompanied and vulnerable passengers ────────────────────────
    "UMNR": {
        "label": "Unaccompanied minor",
        "category": "Vulnerable Passenger",
        "safety_sensitive": True,
        "handling_note": (
            "Must be escorted throughout airport and handed to authorised guardian. "
            "Rebooking requires guardian notification and consent. "
            "Must be handled by human agent."
        ),
    },
    "MEDA": {
        "label": "Medical case — requires medical clearance",
        "category": "Medical",
        "safety_sensitive": True,
        "handling_note": (
            "Medical clearance must be re-confirmed for alternate flight. "
            "In-flight medical equipment (oxygen etc.) must be arranged. "
            "Must be handled by human agent."
        ),
    },
    "OXYG": {
        "label": "Passenger requires supplemental oxygen in-flight",
        "category": "Medical",
        "safety_sensitive": True,
        "handling_note": (
            "Oxygen availability must be confirmed on alternate aircraft. "
            "Must be handled by human agent."
        ),
    },
    # ── Dietary (low sensitivity — automated handling permitted) ───────
    "VGML": {
        "label": "Vegan meal request",
        "category": "Dietary",
        "safety_sensitive": False,
        "handling_note": "Transfer meal preference to alternate flight booking.",
    },
    "KSML": {
        "label": "Kosher meal request",
        "category": "Dietary",
        "safety_sensitive": False,
        "handling_note": "Transfer meal preference to alternate flight booking.",
    },
    "DBML": {
        "label": "Diabetic meal request",
        "category": "Dietary",
        "safety_sensitive": False,
        "handling_note": "Transfer meal preference to alternate flight booking.",
    },
    "GFML": {
        "label": "Gluten-free meal request",
        "category": "Dietary",
        "safety_sensitive": False,
        "handling_note": "Transfer meal preference to alternate flight booking.",
    },
}

_UNKNOWN_SSR = {
    "label": "Unknown SSR code",
    "category": "Unknown",
    "safety_sensitive": True,  # conservative default — halt if unrecognised
    "handling_note": (
        "Unrecognised SSR code. Treat as safety-sensitive — "
        "route to human agent for manual assessment."
    ),
}


@tool
def check_ssr_codes(ssr_codes_str: str) -> dict:
    """Check a passenger's SSR codes and return meanings and safety flags.

    Parses a comma-separated string of SSR codes (as stored in
    PASSENGER_PROFILE.ssr_codes) and returns the full details for each,
    including whether any code requires halting automated rebooking.

    Args:
        ssr_codes_str: Comma-separated SSR codes from PASSENGER_PROFILE
                       e.g. "WCHR,DBML" or "UMNR" or "" (empty = no SSRs)

    Returns:
        dict with keys:
            has_ssr              bool  — True if any SSR codes present
            safety_sensitive     bool  — True if ANY code requires human handling
            codes                list  — list of dicts, one per code, with full details
            human_review_reason  str   — populated if safety_sensitive is True
    """
    # Normalize None/null/none/n/a strings that LLMs pass when no SSR exists
    _NO_SSR_SENTINELS = {"", "null", "none", "n/a", "nil", "no ssr", "no ssrs"}
    if not ssr_codes_str or ssr_codes_str.strip().lower() in _NO_SSR_SENTINELS:
        return {
            "has_ssr": False,
            "safety_sensitive": False,
            "codes": [],
            "human_review_reason": "",
        }

    # Only treat values that look like valid IATA SSR codes (3-4 uppercase letters)
    import re
    all_tokens = [c.strip().upper() for c in ssr_codes_str.split(",") if c.strip()]
    parsed_codes = [c for c in all_tokens if re.match(r'^[A-Z]{3,4}$', c)]

    if not parsed_codes:
        # The input had content but no valid IATA code patterns — treat as no SSR
        return {
            "has_ssr": False,
            "safety_sensitive": False,
            "codes": [],
            "human_review_reason": "",
        }
    results = []
    any_safety_sensitive = False
    safety_reasons = []

    for code in parsed_codes:
        details = SSR_CODES.get(code, _UNKNOWN_SSR).copy()
        details["code"] = code
        results.append(details)

        if details["safety_sensitive"]:
            any_safety_sensitive = True
            safety_reasons.append(f"{code}: {details['handling_note']}")

    human_review_reason = ""
    if any_safety_sensitive:
        human_review_reason = (
            "Passenger has safety-sensitive SSR code(s) requiring human agent handling. "
            + " | ".join(safety_reasons)
        )

    return {
        "has_ssr": True,
        "safety_sensitive": any_safety_sensitive,
        "codes": results,
        "human_review_reason": human_review_reason,
    }
