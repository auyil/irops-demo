"""
api/store/workflow_store.py
────────────────────────────
In-memory workflow store with per-workflow asyncio.Queue for SSE streaming.

Structure:
    workflow_store[workflow_id] = WorkflowRecord

WorkflowRecord is a plain dict (not a dataclass) so it serialises to JSON
without ceremony and can be mutated freely by hooks running in the agent loop.

The asyncio.Queue per workflow is the bridge between:
  - WorkflowTraceHook / GuardrailHook  (producers, run in agent threads)
  - GET /api/workflow/{id}/stream       (consumer, async SSE endpoint)

Lifecycle:
  1. create_workflow()    — called by POST /api/irops/trigger
  2. append_step()        — called by WorkflowTraceHook + GuardrailHook
  3. complete_workflow()  — called by orchestrator after all agents finish
  4. get_workflow()       — called by SSE endpoint and GET status checks
  5. Entries expire after WORKFLOW_TTL_SECONDS (not critical for demo)
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from ..db.database import save_workflow_history


# Global store — keyed by workflow_id string
workflow_store: dict[str, dict] = {}

# SSE sentinel — put on queue to signal the stream is finished
_STREAM_DONE = object()

# How many recent workflows to keep in memory (demo: small number is fine)
MAX_WORKFLOWS = 20

# TTL in seconds — entries older than this are eligible for cleanup
WORKFLOW_TTL_SECONDS = 3600  # 1 hour


def create_workflow(scenario: str, flight_num: str, iata_delay_code: int) -> str:
    """
    Create a new workflow record and return its ID.
    Called at the start of POST /api/irops/trigger.

    Args:
        scenario:         "A" | "B" | "C" | "D"
        flight_num:       Disrupted flight (e.g. "QF400")
        iata_delay_code:  IATA delay code (e.g. 41)

    Returns:
        workflow_id: str — UUID string to use for all subsequent calls
    """
    workflow_id = f"wf-{uuid.uuid4().hex[:12]}"

    workflow_store[workflow_id] = {
        "workflow_id":           workflow_id,
        "scenario":              scenario,
        "flight_num":            flight_num,
        "iata_delay_code":       iata_delay_code,
        "status":                "running",
        "steps":                 [],
        "total_cost_usd":        0.0,
        "total_tokens":          0,
        "passengers_resolved":   0,
        "requires_human_review": False,
        "guardrail_event":       None,
        "created_at":            datetime.now(timezone.utc).isoformat(),
        "completed_at":          None,
        # asyncio.Queue — SSE consumer reads from this
        "_queue":                asyncio.Queue(),
    }

    _evict_old_workflows()
    return workflow_id


def append_step(workflow_id: str, step: dict[str, Any]) -> None:
    """
    Append a trace step to the workflow and put it on the SSE queue.

    Assigns a monotonic step_num if the step has step_num == -1
    (used by guardrail steps which don't know their position in advance).

    Args:
        workflow_id: Target workflow
        step:        Step dict from WorkflowTraceHook or GuardrailHook
    """
    if workflow_id not in workflow_store:
        return

    record = workflow_store[workflow_id]

    # Assign real step number if placeholder
    if step.get("step_num", -1) == -1:
        step["step_num"] = len(record["steps"]) + 1

    record["steps"].append(step)

    # Put on queue for SSE consumer — non-blocking
    try:
        record["_queue"].put_nowait(step)
    except asyncio.QueueFull:
        pass  # Drop if consumer is too slow — demo tolerance


def complete_workflow(
    workflow_id: str,
    status: str = "complete",
    passengers_resolved: int = 0,
) -> None:
    """
    Mark the workflow as complete and signal the SSE stream to close.

    Args:
        workflow_id:          Target workflow
        status:               Final status string
        passengers_resolved:  Count of successfully processed passengers
    """
    if workflow_id not in workflow_store:
        return

    record = workflow_store[workflow_id]

    # Only update status if not already halted by a guardrail
    if record["status"] == "running":
        record["status"] = status

    record["passengers_resolved"] = passengers_resolved
    record["completed_at"]        = datetime.now(timezone.utc).isoformat()

    # Persist to SQLite history
    save_workflow_history({k: v for k, v in record.items() if not k.startswith("_")})

    # Signal SSE consumer to close the stream
    try:
        record["_queue"].put_nowait(_STREAM_DONE)
    except asyncio.QueueFull:
        pass


def get_workflow(workflow_id: str) -> dict | None:
    """
    Return the workflow record without the internal queue object.
    Safe to serialise to JSON.
    """
    record = workflow_store.get(workflow_id)
    if not record:
        return None
    return {k: v for k, v in record.items() if not k.startswith("_")}


async def stream_workflow(workflow_id: str):
    """
    Async generator that yields step dicts from the workflow queue.
    Used by GET /api/workflow/{id}/stream.

    Yields:
        dict  — each step dict as it's produced by the agent hooks
        None  — when the stream is complete (triggers SSE close)
    """
    if workflow_id not in workflow_store:
        return

    queue: asyncio.Queue = workflow_store[workflow_id]["_queue"]

    # Drain any steps that were added before the SSE consumer connected
    # (race condition: trigger fires, hook runs before browser connects)
    existing_steps = workflow_store[workflow_id]["steps"].copy()
    for step in existing_steps:
        yield step

    # Stream new steps as they arrive
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=60.0)
        except asyncio.TimeoutError:
            # Workflow silent for 60s — close stream
            break

        if item is _STREAM_DONE:
            break

        # Skip steps already yielded from existing_steps
        if item in existing_steps:
            continue

        yield item


def _evict_old_workflows() -> None:
    """Remove oldest workflows when store exceeds MAX_WORKFLOWS."""
    if len(workflow_store) <= MAX_WORKFLOWS:
        return

    # Sort by created_at, remove oldest
    sorted_ids = sorted(
        workflow_store.keys(),
        key=lambda wid: workflow_store[wid].get("created_at", ""),
    )
    for wid in sorted_ids[: len(workflow_store) - MAX_WORKFLOWS]:
        workflow_store.pop(wid, None)
