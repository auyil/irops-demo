"""
api/store/token_ledger.py
──────────────────────────
TokenLedger: per-workflow token and cost accumulator.

Provides a clean API for agents and hooks to record token usage without
directly mutating the workflow_store dict. Also exposes a summary method
used by the SSE complete event and the frontend cost panel.

The WorkflowTraceHook updates the ledger after each tool call.
main.py reads the ledger snapshot for the workflow complete SSE event.
"""

from dataclasses import dataclass, field
from typing import Optional

from ..config.governance import BEDROCK_PRICING_USD_PER_1K


@dataclass
class AgentUsage:
    """Token usage for a single agent within a workflow."""
    agent_name:    str
    model_id:      str
    input_tokens:  int = 0
    output_tokens: int = 0
    total_tokens:  int = 0
    cost_usd:      float = 0.0
    call_count:    int = 0


@dataclass
class TokenLedger:
    """
    Accumulates token usage and cost across all agents in one workflow.

    Usage:
        ledger = TokenLedger(workflow_id)
        ledger.record("fetcher", "amazon.nova-pro-v1:0", 1200, 340)
        summary = ledger.summary()
    """
    workflow_id: str
    _agents: dict[str, AgentUsage] = field(default_factory=dict)

    def record(
        self,
        agent_name: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """
        Record a token usage event and return the cost for this call.

        Args:
            agent_name:    Agent that made the call ("orchestrator", "fetcher" etc.)
            model_id:      Bedrock model ID string
            input_tokens:  Input tokens consumed
            output_tokens: Output tokens generated

        Returns:
            float: USD cost for this specific call
        """
        pricing    = BEDROCK_PRICING_USD_PER_1K.get(model_id, {})
        input_rate = pricing.get("input",  0.0008)
        output_rate= pricing.get("output", 0.0032)
        call_cost  = round(
            (input_tokens / 1000) * input_rate +
            (output_tokens / 1000) * output_rate,
            6,
        )

        if agent_name not in self._agents:
            self._agents[agent_name] = AgentUsage(
                agent_name=agent_name,
                model_id=model_id,
            )

        usage = self._agents[agent_name]
        usage.input_tokens  += input_tokens
        usage.output_tokens += output_tokens
        usage.total_tokens  += input_tokens + output_tokens
        usage.cost_usd      = round(usage.cost_usd + call_cost, 6)
        usage.call_count    += 1

        return call_cost

    @property
    def total_cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self._agents.values()), 6)

    @property
    def total_input_tokens(self) -> int:
        return sum(a.input_tokens for a in self._agents.values())

    @property
    def total_output_tokens(self) -> int:
        return sum(a.output_tokens for a in self._agents.values())

    @property
    def total_tokens(self) -> int:
        return sum(a.total_tokens for a in self._agents.values())

    def summary(self) -> dict:
        """
        Return a JSON-serialisable cost summary for the workflow.
        Emitted as the final SSE event and stored in the workflow record.
        """
        return {
            "workflow_id":       self.workflow_id,
            "total_cost_usd":    self.total_cost_usd,
            "total_input_tokens":  self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens":        self.total_tokens,
            "by_agent": [
                {
                    "agent_name":    a.agent_name,
                    "model_id":      a.model_id,
                    "input_tokens":  a.input_tokens,
                    "output_tokens": a.output_tokens,
                    "total_tokens":  a.total_tokens,
                    "cost_usd":      a.cost_usd,
                    "call_count":    a.call_count,
                }
                for a in sorted(self._agents.values(), key=lambda x: x.cost_usd, reverse=True)
            ],
        }


# ── Global ledger registry ─────────────────────────────────────────────────
# One TokenLedger per active workflow, keyed by workflow_id.
# Created alongside the WorkflowStore record in create_workflow().
_ledger_registry: dict[str, TokenLedger] = {}


def get_or_create_ledger(workflow_id: str) -> TokenLedger:
    """Return existing ledger for workflow_id or create a new one."""
    if workflow_id not in _ledger_registry:
        _ledger_registry[workflow_id] = TokenLedger(workflow_id=workflow_id)
    return _ledger_registry[workflow_id]


def get_ledger(workflow_id: str) -> Optional[TokenLedger]:
    """Return ledger if it exists, None otherwise."""
    return _ledger_registry.get(workflow_id)


def evict_ledger(workflow_id: str) -> None:
    """Remove ledger when workflow is evicted from store."""
    _ledger_registry.pop(workflow_id, None)
