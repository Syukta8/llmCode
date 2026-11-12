# -*- coding: utf-8 -*-
"""
LiteLLM proxy hooks for normalizing client parameters to local engine templates.

Why: Claude Code defaults to `high` effort (or large thinking budgets that LiteLLM maps to `high`),
while local models like Qwen3.8 only support `xhigh`, `medium`, and `low` in their Jinja templates.
"""
from typing import Any, Dict

from litellm.integrations.custom_logger import CustomLogger

_HIGH_EFFORTS = frozenset({'high', 'max'})
_HIGH_THINKING_BUDGET_THRESHOLD = 8192


def normalize_reasoning_effort_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Maps `high`/`max` effort (or large thinking budgets without explicit effort) to `xhigh`."""
    if not isinstance(data, dict):
        return data

    effort = data.get('reasoning_effort')
    if isinstance(effort, str) and effort.lower() in _HIGH_EFFORTS:
        data['reasoning_effort'] = 'xhigh'

    output_cfg = data.get('output_config')
    has_output_effort = False
    if isinstance(output_cfg, dict):
        out_effort = output_cfg.get('effort')
        if isinstance(out_effort, str) and out_effort.strip():
            has_output_effort = True
            if out_effort.lower() in _HIGH_EFFORTS:
                output_cfg['effort'] = 'xhigh'

    thinking = data.get('thinking')
    if isinstance(thinking, dict) and not data.get('reasoning_effort') and not has_output_effort:
        t_type = thinking.get('type')
        try:
            budget = int(thinking.get('budget_tokens') or 0)
        except (TypeError, ValueError):
            budget = 0
        if t_type == 'adaptive' or budget >= _HIGH_THINKING_BUDGET_THRESHOLD:
            data['reasoning_effort'] = 'xhigh'

    return data


class ReasoningEffortNormalizer(CustomLogger):
    """Normalizes incoming Anthropic/OpenAI effort levels before LiteLLM forwards to llama-server."""

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: Dict[str, Any], call_type: Any):
        return normalize_reasoning_effort_payload(data)


proxy_effort_hook = ReasoningEffortNormalizer()
