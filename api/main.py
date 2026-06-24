"""
api/main.py
────────────
FastAPI application for the IROPS Autonomous Disruption Management demo.

Endpoints:
  POST /api/irops/trigger          Fire internal workflow (scenario button)
  GET  /api/workflow/{id}/stream   SSE stream of workflow trace steps
  POST /api/chat                   ConciergeAgent chat (SSE streaming)
  POST /api/reset/{scenario}       Reset scenario for re-demo
  GET  /api/status/{workflow_id}   Current workflow record (non-streaming)
  GET  /api/health                 Container health check

Architecture:
  - POST /api/irops/trigger returns immediately with workflow_id
  - Workflow runs as background asyncio task
  - GET /api/workflow/{id}/stream yields SSE as hooks fire
  - POST /api/chat applies Bedrock Guardrail then streams ConciergeAgent
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .agents.concierge import run_concierge
from .agents.orchestrator import run_internal_workflow
from .config.governance import SCENARIO_CONFIG
from .db.database import init_db, reset_scenario, get_workflow_history, seed_custom_scenario
from .store.workflow_store import (
    create_workflow,
    get_workflow,
    stream_workflow,
    workflow_store,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB on startup."""
    logger.info("irops_api_startup")
    init_db()
    logger.info("irops_db_ready")
    yield
    logger.info("irops_api_shutdown")


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="IROPS Autonomous Disruption Management API",
    description="Qantas IROPS demo — multi-agent orchestration with live SSE observability",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Cache-Control", "Accept"],
)


# ── Request / response models ──────────────────────────────────────────────

class TriggerRequest(BaseModel):
    scenario: str                   # "A" | "B" | "C" | "D"
    flight_num: Optional[str] = None        # override seed flight
    iata_delay_code: Optional[int] = None   # override seed code


class TriggerResponse(BaseModel):
    workflow_id: str
    scenario: str
    flight_num: str
    iata_delay_code: int
    stream_url: str
    message: str


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = ""
    conversation_history: Optional[list[dict]] = []


class CustomTriggerRequest(BaseModel):
    flight_num: str
    origin: str
    destination: str
    std: str
    flight_status: str          # "CX" | "DL"
    iata_delay_code: int
    passengers: list[dict]


# ── SSE helpers ────────────────────────────────────────────────────────────

async def _sse_generator(
    generator: AsyncGenerator[dict, None],
) -> AsyncGenerator[str, None]:
    """
    Wrap an async dict generator into SSE-formatted text chunks.
    Each event: "data: {json}\n\n"
    Final event: "data: [DONE]\n\n"

    Sends SSE comment keepalives every 25s so CloudFront does not close
    the connection on its origin response timeout (default 60s).
    """
    _KEEPALIVE_INTERVAL = 25.0
    buf: asyncio.Queue = asyncio.Queue()

    async def _drain():
        try:
            async for event in generator:
                await buf.put(event)
        finally:
            await buf.put(None)  # sentinel

    task = asyncio.create_task(_drain())
    try:
        while True:
            try:
                event = await asyncio.wait_for(buf.get(), timeout=_KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
    except asyncio.CancelledError:
        task.cancel()
    finally:
        task.cancel()
        yield "data: [DONE]\n\n"


async def _workflow_step_generator(
    workflow_id: str,
) -> AsyncGenerator[dict, None]:
    """Yield workflow trace steps from the store queue."""
    async for step in stream_workflow(workflow_id):
        yield step

    # Yield the final workflow summary as the last event
    record = get_workflow(workflow_id)
    if record:
        yield {
            "type":    "workflow_complete",
            "summary": {
                "workflow_id":           record["workflow_id"],
                "scenario":              record["scenario"],
                "status":                record["status"],
                "passengers_resolved":   record["passengers_resolved"],
                "total_cost_usd":        record["total_cost_usd"],
                "total_tokens":          record["total_tokens"],
                "requires_human_review": record["requires_human_review"],
                "guardrail_event":       record["guardrail_event"],
                "completed_at":          record["completed_at"],
            },
        }


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.post("/api/irops/trigger", response_model=TriggerResponse)
async def trigger_irops(req: TriggerRequest):
    """
    Fire the internal IROPS workflow for a disruption scenario.

    Returns immediately with workflow_id. Connect to
    GET /api/workflow/{workflow_id}/stream for live trace.
    """
    scenario = req.scenario.upper()
    if scenario not in SCENARIO_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scenario '{scenario}'. Must be A, B, C, or D.",
        )

    config          = SCENARIO_CONFIG[scenario]
    flight_num      = req.flight_num      or config["flight_num"]
    iata_delay_code = req.iata_delay_code or config["iata_delay_code"]

    # Reset scenario so it can be re-run cleanly
    reset_scenario(scenario)

    # Create workflow record in store
    workflow_id = create_workflow(
        scenario=scenario,
        flight_num=flight_num,
        iata_delay_code=iata_delay_code,
    )

    # Fire workflow as background task — returns immediately
    asyncio.create_task(
        run_internal_workflow(
            workflow_id=workflow_id,
            scenario=scenario,
            flight_num=flight_num,
            iata_delay_code=iata_delay_code,
        )
    )

    logger.info(
        "irops_triggered",
        extra={
            "workflow_id":     workflow_id,
            "scenario":        scenario,
            "flight_num":      flight_num,
            "iata_delay_code": iata_delay_code,
        },
    )

    return TriggerResponse(
        workflow_id=workflow_id,
        scenario=scenario,
        flight_num=flight_num,
        iata_delay_code=iata_delay_code,
        stream_url=f"/api/workflow/{workflow_id}/stream",
        message=(
            f"Workflow {workflow_id} started for {config['label']}. "
            f"Connect to stream_url for live trace."
        ),
    )


@app.get("/api/workflow/{workflow_id}/stream")
async def stream_workflow_trace(workflow_id: str):
    """
    SSE stream of workflow trace steps.

    Connect immediately after POST /api/irops/trigger.
    Streams until the workflow completes or is halted by a guardrail.

    Events:
      Tool call steps:      {"type": "tool_call", "agent": ..., "action": ..., ...}
      Guardrail events:     {"type": "guardrail", "action": "SSR_SAFETY_HALT", ...}
      Workflow complete:    {"type": "workflow_complete", "summary": {...}}
    """
    if workflow_id not in workflow_store:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_id}' not found.",
        )

    return StreamingResponse(
        _sse_generator(_workflow_step_generator(workflow_id)),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",   # Disable nginx buffering for SSE
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    ConciergeAgent chat endpoint with Bedrock Guardrail pre-check.

    Applies Bedrock Guardrail to raw input first. If blocked, returns
    a guardrail_block SSE event immediately without invoking the agent.

    SSE events:
      {"type": "guardrail_block", "layer": "bedrock", "topic": str, "message": str}
      {"type": "token", "text": str}
      {"type": "complete", "cost_usd": float}
      {"type": "error", "detail": str}
    """
    session_id = req.session_id or f"sess-{uuid.uuid4().hex[:8]}"

    async def _chat_generator() -> AsyncGenerator[dict, None]:
        async for event in run_concierge(
            message=req.message,
            session_id=session_id,
            conversation_history=req.conversation_history or [],
        ):
            yield event

    return StreamingResponse(
        _sse_generator(_chat_generator()),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/api/reset/{scenario}")
async def reset(scenario: str):
    """
    Reset a scenario so it can be re-run without restarting the container.
    Clears RESOLUTION rows and resets IROPS_LOG status to 'pending'.
    """
    scenario = scenario.upper()
    if scenario not in SCENARIO_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scenario '{scenario}'. Must be A, B, C, or D.",
        )
    try:
        reset_scenario(scenario)
        return {
            "reset":    True,
            "scenario": scenario,
            "message":  f"Scenario {scenario} reset. Ready to trigger again.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status/{workflow_id}")
async def get_status(workflow_id: str):
    """
    Return current workflow record without SSE streaming.
    Useful for polling or page reload recovery.
    """
    record = get_workflow(workflow_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_id}' not found.",
        )
    return record


@app.post("/api/custom/trigger")
async def custom_trigger(req: CustomTriggerRequest):
    """
    Seed the database with a custom scenario and fire the IROPS workflow.

    This endpoint is the direct REST equivalent of the WorkflowBuilderAgent's
    trigger_custom_workflow tool call. Can be used independently of the chat UI.
    """
    payload = req.model_dump()
    seed_custom_scenario(payload)

    workflow_id = create_workflow("CUSTOM", req.flight_num, req.iata_delay_code)

    asyncio.create_task(
        run_internal_workflow(
            workflow_id=workflow_id,
            scenario="CUSTOM",
            flight_num=req.flight_num,
            iata_delay_code=req.iata_delay_code,
        )
    )

    logger.info("custom_workflow_triggered", extra={
        "workflow_id": workflow_id,
        "flight_num":  req.flight_num,
    })

    return {
        "workflow_id": workflow_id,
        "stream_url":  f"/api/workflow/{workflow_id}/stream",
        "message":     f"Custom workflow {workflow_id} started for {req.flight_num}.",
    }


@app.get("/api/history")
async def list_history():
    """
    Return all workflow history records ordered newest-first.
    Excludes steps_json for compact list responses.
    """
    return get_workflow_history()


@app.get("/api/history/{workflow_id}")
async def get_history_item(workflow_id: str):
    """
    Return the full history record for a single workflow, including the
    steps list (parsed from steps_json).
    """
    records = get_workflow_history(workflow_id)
    if not records:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_id}' not found in history.",
        )
    return records[0]


@app.get("/api/health")
async def health():
    """Container health check — used by nginx and docker-compose."""
    return {"status": "ok", "service": "irops-api"}


@app.get("/api/instance-status")
async def instance_status():
    """
    Demo availability check — if this endpoint responds, the instance is running.
    The frontend uses reachability (200 OK) to determine online state.
    """
    import os
    instance_id = os.environ.get("EC2_INSTANCE_ID")
    return {"online": True, "instance_id": instance_id}
