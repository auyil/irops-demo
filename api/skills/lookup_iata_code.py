"""
api/skills/lookup_iata_code.py
───────────────────────────────
Stateless skill: translates an IATA delay code into a human-readable
label and control classification (controllable / uncontrollable).

Used by:
  - RebookingAgent  — to determine voucher eligibility before issue_voucher
  - GuardrailHook   — TC-03 policy alignment check

No model call. Pure reference lookup against the embedded table.
"""

from strands import tool


# IATA standard delay codes relevant to passenger disruption.
# Source: IATA Standard Schedules Information Manual (SSIM).
# Subset covering the codes most relevant to IROPS passenger handling.
IATA_CODES: dict[int, dict] = {
    # ── Controllable (airline-caused) ─────────────────────────────────
    14: {
        "label": "Late check-in — airline error",
        "category": "Ground Operations",
        "control": "controllable",
    },
    21: {
        "label": "Aircraft documentation late or irregular",
        "category": "Aircraft & Ramp Handling",
        "control": "controllable",
    },
    31: {
        "label": "Late cabin cleaning or catering delivery",
        "category": "Cabin Service",
        "control": "controllable",
    },
    41: {
        "label": "Aircraft on Ground — technical/mechanical fault (AOG)",
        "category": "Technical / Engineering",
        "control": "controllable",
    },
    42: {
        "label": "Aircraft defect carried over from previous flight",
        "category": "Technical / Engineering",
        "control": "controllable",
    },
    43: {
        "label": "Scheduled maintenance — late release",
        "category": "Technical / Engineering",
        "control": "controllable",
    },
    61: {
        "label": "Flight plan late filing — airline operations error",
        "category": "Flight Operations",
        "control": "controllable",
    },
    92: {
        "label": "Commercial overbooking — denied boarding",
        "category": "Commercial / Capacity",
        "control": "controllable",
    },
    93: {
        "label": "IT / systems outage — airline-caused",
        "category": "IT Systems",
        "control": "controllable",
    },
    # ── Uncontrollable (external / outside airline control) ───────────
    71: {
        "label": "Weather — meteorological conditions at departure",
        "category": "Weather",
        "control": "uncontrollable",
    },
    72: {
        "label": "Weather — meteorological conditions at destination",
        "category": "Weather",
        "control": "uncontrollable",
    },
    75: {
        "label": "De-icing — adverse weather conditions",
        "category": "Weather",
        "control": "uncontrollable",
    },
    81: {
        "label": "Air Traffic Control restriction — en-route",
        "category": "ATC",
        "control": "uncontrollable",
    },
    82: {
        "label": "Air Traffic Control restriction — airport / terminal",
        "category": "ATC",
        "control": "uncontrollable",
    },
    83: {
        "label": "Airport / runway closure",
        "category": "ATC",
        "control": "uncontrollable",
    },
    91: {
        "label": "Industrial action — third party (airport, ATC, ground handler)",
        "category": "Industrial Action",
        "control": "uncontrollable",
    },
    96: {
        "label": "Security — unspecified threat or breach",
        "category": "Security",
        "control": "uncontrollable",
    },
    97: {
        "label": "Government or customs restriction",
        "category": "Regulatory",
        "control": "uncontrollable",
    },
}

_UNKNOWN = {
    "label": "Unknown delay code",
    "category": "Unknown",
    "control": "uncontrollable",  # conservative default — deny vouchers if unknown
}


@tool
def lookup_iata_code(iata_delay_code: int) -> dict:
    """Look up an IATA delay code and return its label and control classification.

    Used to determine whether a disruption event is controllable (airline-caused)
    or uncontrollable (external), which drives voucher eligibility under Qantas
    compensation policy.

    Args:
        iata_delay_code: Integer IATA delay code (e.g. 41 for AOG, 71 for weather)

    Returns:
        dict with keys:
            code          int   — the input code
            label         str   — human-readable description
            category      str   — high-level category
            control       str   — "controllable" or "uncontrollable"
            voucher_eligible bool — True if meal/hotel vouchers may apply
            policy_note   str   — one-line policy guidance
    """
    result = IATA_CODES.get(iata_delay_code, _UNKNOWN).copy()
    result["code"] = iata_delay_code

    if result["control"] == "controllable":
        result["voucher_eligible"] = True
        if iata_delay_code == 92:
            result["policy_note"] = (
                "Commercial overbooking: Qantas compensation policy section 10 applies. "
                "Rebook on next available Qantas flight — mandatory. "
                "For international flights where next available departs >4h after scheduled: "
                "issue TRAVEL_VOUCHER. Otherwise apply standard controllable disruption table "
                "(MEAL_30_AUD for delay >=2h, HOTEL for overnight away from home)."
            )
        else:
            result["policy_note"] = (
                "Controllable disruption: policy section 2 applies. "
                "Check flight_status (CX=cancelled, DL=delayed) to describe accurately. "
                "Compensation: meal voucher $30 AUD if wait >=2h away from home airport; "
                "hotel + $60 AUD meal allowance if overnight away from home."
            )
    else:
        result["voucher_eligible"] = False
        result["policy_note"] = (
            "Uncontrollable event: Qantas compensation policy section 3 applies. "
            "Rebooking required. Meal and hotel vouchers NOT automatically issued."
        )

    return result
