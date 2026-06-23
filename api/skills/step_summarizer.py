"""
api/skills/step_summarizer.py
──────────────────────────────
Generates a plain-English summary of a workflow trace step using Amazon Nova Lite.

Called synchronously from WorkflowTraceHook.after_tool_call. Nova Lite is fast
(~200–400 ms) and cheap enough to run per step without materially slowing the
agent loop. Fails silently — returns empty string on any error so the trace
step is still emitted without a summary rather than crashing the hook.
"""

import json
import logging

import boto3

logger = logging.getLogger(__name__)

_SUMMARIZER_MODEL = "amazon.nova-lite-v1:0"

_SYSTEM = (
    "You are a plain-English translator for an airline AI operations system. "
    "Summarise ONLY what is explicitly stated in the Inputs and Result provided. "
    "NEVER invent, infer, or guess details that are not present in the data. "
    "If the result is empty or says no data, say so — do not fabricate outcomes. "
    "Be specific: mention passenger name, flight number, or action taken "
    "only if they appear in the inputs or result. "
    "Use past tense. Maximum 50 words. No jargon, no JSON."
)

# Module-level boto3 client — reused across calls
_client: "boto3.client | None" = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name="us-east-1")
    return _client


def summarize_step(
    agent_name: str,
    tool_name: str,
    tool_inputs: dict,
    tool_result: str,
) -> str:
    """
    Generate a ≤50-word plain-English summary of a tool call using Nova Lite.

    Args:
        agent_name:  Which agent ran the tool ("fetcher", "rebooking", etc.)
        tool_name:   Name of the tool that was called
        tool_inputs: Dict of arguments passed to the tool (may be empty)
        tool_result: String representation of the tool's return value

    Returns:
        Plain-English summary string, or "" on any failure.
    """
    try:
        inputs_str = json.dumps(tool_inputs, default=str)[:500]
        result_str = str(tool_result)[:600]

        user_text = (
            f"Agent: {agent_name}\n"
            f"Tool called: {tool_name}\n"
            f"Inputs: {inputs_str}\n"
            f"Result: {result_str}"
        )

        resp = _get_client().converse(
            modelId=_SUMMARIZER_MODEL,
            system=[{"text": _SYSTEM}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": 90, "temperature": 0.0},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()

        # Hard-cap at 55 words in case the model overshoots
        words = text.split()
        if len(words) > 55:
            text = " ".join(words[:55]) + "…"
        return text

    except Exception as exc:
        logger.warning("step_summary_failed", extra={"tool": tool_name, "error": str(exc)})
        return ""
