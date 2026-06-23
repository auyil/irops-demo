"""
api/skills/lookup_policy.py
────────────────────────────
Stateless skill: keyword search over policy_chunks.json to retrieve
relevant compensation and disruption handling policy sections.

Policy chunks are built offline by scripts/build_policy_chunks.py
from the three official source documents:
  - Qantas Compensation and Refunds Policy
  - Qantas Conditions of Carriage
  - ACCC Travel Rights guidelines
  - Qantas internal mock disruption handling guidelines

Used by:
  - RebookingAgent  — validate voucher eligibility before issue_voucher
  - ConciergeAgent  — answer passenger policy questions

No model call. Keyword intersection search over pre-built JSON chunks.
Falls back to hardcoded mock guidelines if policy_chunks.json not found.
"""

import json
import pathlib
import re
from strands import tool


_CHUNKS_PATH = pathlib.Path(__file__).parent.parent / "data" / "skills" / "policy_chunks.json"

# ── Fallback: mock guidelines (always available, no file needed) ───────────
# This is the mock guideline document from the functional spec.
# Used when policy_chunks.json hasn't been built yet, or as a guaranteed
# baseline that agents can always fall back to.
_MOCK_GUIDELINES: list[dict] = [
    {
        "id": "mock-001",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Definitions of Control",
        "keywords": ["controllable", "uncontrollable", "within control", "outside control",
                     "engineering", "maintenance", "aog", "weather", "atc"],
        "text": (
            "Events Within Our Control: Engineering issues, aircraft maintenance (AOG), "
            "IT system outages, delayed baggage delivery, crew shortages. "
            "Events Outside Our Control: Severe weather events, natural disasters, "
            "air traffic control (ATC) mandates, third-party industrial action, "
            "geopolitical security issues."
        ),
    },
    {
        "id": "mock-002",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Compensation — Events Within Our Control",
        "keywords": ["controllable", "within control", "delay", "2 hours", "meal",
                     "voucher", "refreshment", "hotel", "accommodation", "overnight",
                     "rebooking", "rebook", "compensation"],
        "text": (
            "Delay of 2+ Hours: Provide a $30 AUD digital Refreshment Voucher. "
            "Delay forcing an Overnight Stay (Away from Home Port): Provide accommodation "
            "(Hotel Voucher), transport to/from the hotel, and a $60 AUD meal allowance. "
            "Rebooking: Passengers must be rebooked on the next available flight without charge."
        ),
    },
    {
        "id": "mock-003",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Compensation — Events Outside Our Control",
        "keywords": ["uncontrollable", "outside control", "weather", "atc", "hotel",
                     "meal", "voucher", "not obligated", "no obligation", "rebooking"],
        "text": (
            "Rebooking: Rebook the passenger on the next available flight without charge. "
            "Meals & Accommodation: Qantas is NOT legally obligated to provide hotel "
            "accommodation or meal vouchers for events outside our control."
        ),
    },
    {
        "id": "mock-004",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Australian Consumer Law — Refund Policy",
        "keywords": ["refund", "cancel", "cancelled", "72 hours", "acl",
                     "australian consumer law", "cash refund", "full refund"],
        "text": (
            "If a flight is cancelled and an acceptable alternative flight cannot be "
            "offered within 72 hours, the passenger has a statutory right to a full "
            "cash refund of the ticket price under Australian Consumer Law (ACL)."
        ),
    },
    {
        "id": "mock-005",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Tier Priority — Rebooking Order",
        "keywords": ["platinum", "gold", "silver", "bronze", "tier", "priority",
                     "frequent flyer", "qff", "loyalty", "order", "ranking"],
        "text": (
            "Rebooking priority follows Qantas Frequent Flyer tier status: "
            "Platinum first, then Gold, Silver, Bronze, and non-members last. "
            "Within the same tier, passengers with premium cabin bookings (First, Business) "
            "are prioritised over Economy passengers."
        ),
    },
    {
        "id": "mock-006",
        "source": "Qantas Disruption Handling Guidelines (Internal Mock)",
        "section": "Special Service Requests — Safety Protocol",
        "keywords": ["ssr", "wchr", "wheelchair", "umnr", "unaccompanied", "meda",
                     "medical", "special", "assistance", "human", "review"],
        "text": (
            "Passengers with safety-sensitive Special Service Requests (SSR codes: "
            "WCHR, WCHS, WCHC, WCHP, UMNR, MEDA, OXYG, BLND) must NOT be "
            "automatically rebooked. Aircraft and ground handling compatibility must "
            "be verified by a human agent before any rebooking action is taken."
        ),
    },
]


def _load_chunks() -> list[dict]:
    """Load policy chunks from JSON file, fall back to mock guidelines."""
    if _CHUNKS_PATH.exists():
        try:
            with open(_CHUNKS_PATH) as f:
                chunks = json.load(f)
            # Always include mock guidelines as baseline
            existing_ids = {c["id"] for c in chunks}
            for chunk in _MOCK_GUIDELINES:
                if chunk["id"] not in existing_ids:
                    chunks.append(chunk)
            return chunks
        except Exception:
            pass
    return _MOCK_GUIDELINES.copy()


def _tokenise(text: str) -> set[str]:
    """Lowercase, strip punctuation, return word set."""
    return set(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _score_chunk(chunk: dict, query_tokens: set[str]) -> int:
    """Return number of keyword matches between query and chunk."""
    keyword_tokens = set()
    for kw in chunk.get("keywords", []):
        keyword_tokens.update(_tokenise(kw))
    # Also match against section name and source
    keyword_tokens.update(_tokenise(chunk.get("section", "")))
    return len(query_tokens & keyword_tokens)


@tool
def lookup_policy(query: str, top_k: int = 3) -> dict:
    """Search Qantas disruption policy chunks for content relevant to a query.

    Performs keyword intersection search over pre-built policy chunks derived
    from official Qantas and ACCC policy documents. Returns the most relevant
    sections to inform agent decisions on voucher eligibility, rebooking rules,
    and passenger rights.

    Args:
        query:  Natural language query describing what policy guidance is needed.
                e.g. "Is a hotel voucher required for a weather delay?"
                e.g. "What compensation applies for a 3 hour AOG delay?"
        top_k:  Maximum number of policy chunks to return (default 3).

    Returns:
        dict with keys:
            query         str   — the original query
            chunks_found  int   — number of relevant chunks returned
            results       list  — list of dicts, each with:
                            id, source, section, text, relevance_score
            guidance      str   — concatenated text of all results for
                                  direct injection into agent reasoning
    """
    chunks = _load_chunks()
    query_tokens = _tokenise(query)

    if not query_tokens:
        return {
            "query": query,
            "chunks_found": 0,
            "results": [],
            "guidance": "No query terms provided.",
        }

    # Score and rank all chunks
    scored = [
        (chunk, _score_chunk(chunk, query_tokens))
        for chunk in chunks
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Return top_k with score > 0
    top_chunks = [
        {
            "id":               chunk["id"],
            "source":           chunk["source"],
            "section":          chunk["section"],
            "text":             chunk["text"],
            "relevance_score":  score,
        }
        for chunk, score in scored[:top_k]
        if score > 0
    ]

    # If nothing matched, return the core compensation chunk as a safe default
    if not top_chunks:
        fallback = next(
            (c for c in _MOCK_GUIDELINES if c["id"] == "mock-002"), None
        )
        if fallback:
            top_chunks = [{
                "id":              fallback["id"],
                "source":          fallback["source"],
                "section":         fallback["section"],
                "text":            fallback["text"],
                "relevance_score": 0,
            }]

    guidance = "\n\n".join(
        f"[{r['source']} — {r['section']}]\n{r['text']}"
        for r in top_chunks
    )

    return {
        "query":        query,
        "chunks_found": len(top_chunks),
        "results":      top_chunks,
        "guidance":     guidance,
    }
