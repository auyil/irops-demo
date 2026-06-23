"""
api/hooks/workflow_trace.py
────────────────────────────
WorkflowTraceHook: captures Strands agent events and writes structured
trace steps to the WorkflowStore. The SSE endpoint reads from the store's
asyncio.Queue and streams each step to the frontend in real time.

Adapted from mable's TraceCaptureHook (logging.txt) with two key changes:
  1. Steps go to WorkflowStore (in-memory + queue) instead of a logger
  2. Token costs are calculated and accumulated per step using
     BEDROCK_PRICING_USD_PER_1K from governance.py

Registered on:
  AfterToolCallEvent   — one step per tool call (query, rebook, voucher etc.)
  AfterInvocationEvent — one summary step per agent invocation completion
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent, AfterToolCallEvent

from ..config.governance import BEDROCK_PRICING_USD_PER_1K
from ..skills.step_summarizer import summarize_step
from ..store.workflow_store import append_step, workflow_store


def _calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a Bedrock model call."""
    pricing = BEDROCK_PRICING_USD_PER_1K.get(model_id, {})
    input_cost  = (input_tokens  / 1000) * pricing.get("input",  0.0008)
    output_cost = (output_tokens / 1000) * pricing.get("output", 0.0032)
    return round(input_cost + output_cost, 6)


def _get_model_id(agent: Agent) -> str:
    """Extract model ID string from a Strands Agent."""
    model = getattr(agent, "model", None)
    if model and hasattr(model, "config"):
        return model.config.get("model_id", "unknown")
    return getattr(agent, "_model_id", "unknown")


def _get_token_counts(agent: Agent) -> tuple[int, int, int]:
    """Extract accumulated token counts from agent event loop metrics."""
    try:
        metrics = agent.event_loop_metrics
        usage   = metrics.accumulated_usage or {}
        return (
            usage.get("inputTokens",  0),
            usage.get("outputTokens", 0),
            usage.get("totalTokens",  0),
        )
    except Exception:
        return 0, 0, 0


def _get_latency_ms(agent: Agent) -> int:
    """Extract accumulated latency from agent event loop metrics."""
    try:
        metrics = agent.event_loop_metrics
        return metrics.accumulated_metrics.get("latencyMs", 0)
    except Exception:
        return 0


def _extract_tool_inputs(event: AfterToolCallEvent) -> dict:
    """Safely extract the tool call input arguments from the event."""
    try:
        tool_use = event.tool_use  # ToolUse TypedDict: {input, name, toolUseId}
        if tool_use is None:
            return {}
        inputs = tool_use.get("input") if isinstance(tool_use, dict) else getattr(tool_use, "input", None)
        if isinstance(inputs, dict):
            return inputs
        return {}
    except Exception:
        return {}


def _extract_tool_result(event: AfterToolCallEvent) -> str:
    """Safely extract and stringify the tool result from the event."""
    try:
        result = event.result  # ToolResult TypedDict: {content, status, toolUseId}
        if result is None:
            return ""
        # ToolResult is a TypedDict — access via dict key or attribute
        content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
        if content is not None:
            return _extract_tool_result_from_content(content)
        return str(result)[:600]
    except Exception:
        return ""


def _extract_tool_result_from_content(content) -> str:
    """Extract text from a ToolResultContent list."""
    if not isinstance(content, list):
        return str(content)[:600]
    parts = []
    for item in content:
        if isinstance(item, dict):
            parts.append(item.get("text", "") or json.dumps(item, default=str))
        else:
            text = getattr(item, "text", None)
            parts.append(text if text is not None else str(item))
    return " ".join(parts)[:600]


class WorkflowTraceHook(HookProvider):
    """
    Strands HookProvider that emits structured trace steps to WorkflowStore.

    Each tool call and agent invocation produces one step dict that is:
      1. Appended to workflow_store[workflow_id]["steps"]
      2. Put on workflow_store[workflow_id]["queue"] (asyncio.Queue)
      3. Picked up by GET /api/workflow/{id}/stream and yielded as SSE

    Also accumulates running cost in agent.state["cumulative_cost_usd"]
    so GuardrailHook can check the budget cap before each tool call.
    """

    def __init__(self, workflow_id: str, agent_name: str):
        """
        Args:
            workflow_id:  Workflow ID this hook instance is tracking
            agent_name:   Human label for this agent ("orchestrator",
                          "fetcher", "rebooking", "concierge")
        """
        self.workflow_id = workflow_id
        self.agent_name  = agent_name
        self._step_num   = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent,   self.after_tool_call)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    # ── AfterToolCallEvent ─────────────────────────────────────────────────

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Emit one trace step per tool call completion."""
        try:
            agent = event.agent

            # Tool name
            tool_name = ""
            if event.selected_tool:
                tool_name = getattr(event.selected_tool, "tool_name", str(event.selected_tool))

            # Token counts and cost
            input_tokens, output_tokens, total_tokens = _get_token_counts(agent)
            model_id = _get_model_id(agent)
            step_cost = _calculate_cost(model_id, input_tokens, output_tokens)

            # Update cumulative cost in agent state for GuardrailHook
            current_cost = agent.state.get("cumulative_cost_usd") or 0.0
            new_cost = current_cost + step_cost
            agent.state.set("cumulative_cost_usd", new_cost)

            # Also update workflow-level total
            if self.workflow_id in workflow_store:
                workflow_store[self.workflow_id]["total_cost_usd"] = round(
                    workflow_store[self.workflow_id].get("total_cost_usd", 0.0) + step_cost,
                    6,
                )
                workflow_store[self.workflow_id]["total_tokens"] = (
                    workflow_store[self.workflow_id].get("total_tokens", 0) + total_tokens
                )

            # Extract inputs and result for Nova Lite summarization
            tool_inputs = _extract_tool_inputs(event)
            tool_result = _extract_tool_result(event)
            # If result extraction failed, give the summarizer the inputs as context
            # so it can produce "called X with Y" rather than fabricating an outcome
            if not tool_result:
                tool_result = f"(result not captured; tool was called with inputs: {json.dumps(tool_inputs, default=str)[:300]})"
            summary = summarize_step(self.agent_name, tool_name, tool_inputs, tool_result)

            self._step_num += 1
            step = {
                "step_num":      self._step_num,
                "type":          "tool_call",
                "agent":         self.agent_name,
                "action":        tool_name,
                "status":        "done",
                "detail":        f"{self.agent_name} → {tool_name}",
                "summary":       summary,
                "model_id":      model_id,
                "input_tokens":  input_tokens,
                "output_tokens": output_tokens,
                "total_tokens":  total_tokens,
                "step_cost_usd": step_cost,
                "cumulative_cost_usd": new_cost,
                "latency_ms":    _get_latency_ms(agent),
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }

            append_step(self.workflow_id, step)

        except Exception as e:
            # Never let hook errors break the agent loop
            _emit_error_step(self.workflow_id, self.agent_name, "after_tool_call", str(e))

    # ── AfterInvocationEvent ───────────────────────────────────────────────

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        """Emit a summary step when the agent's full invocation completes."""
        try:
            agent = event.agent
            input_tokens, output_tokens, total_tokens = _get_token_counts(agent)
            model_id  = _get_model_id(agent)
            step_cost = _calculate_cost(model_id, input_tokens, output_tokens)

            self._step_num += 1
            step = {
                "step_num":      self._step_num,
                "type":          "invocation_complete",
                "agent":         self.agent_name,
                "action":        "invocation_complete",
                "status":        "done",
                "detail":        f"{self.agent_name} invocation complete",
                "model_id":      model_id,
                "input_tokens":  input_tokens,
                "output_tokens": output_tokens,
                "total_tokens":  total_tokens,
                "step_cost_usd": step_cost,
                "cumulative_cost_usd": agent.state.get("cumulative_cost_usd") or 0.0,
                "latency_ms":    _get_latency_ms(agent),
                "timestamp":     datetime.now(timezone.utc).isoformat(),
            }

            append_step(self.workflow_id, step)

        except Exception as e:
            _emit_error_step(self.workflow_id, self.agent_name, "after_invocation", str(e))


def _emit_error_step(
    workflow_id: str,
    agent_name: str,
    hook_name: str,
    error: str,
) -> None:
    """Emit a non-fatal error step so the frontend knows something went wrong."""
    try:
        append_step(workflow_id, {
            "step_num":      -1,
            "type":          "hook_error",
            "agent":         agent_name,
            "action":        hook_name,
            "status":        "error",
            "detail":        f"Hook error in {hook_name}: {error}",
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        pass  # Last resort — if the store itself is broken, swallow silently
