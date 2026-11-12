# -*- coding: utf-8 -*-
"""
LiteLLM proxy hooks for normalizing client parameters to local engine templates.

Why: Claude Code defaults to `high` effort (or large thinking budgets that LiteLLM maps to `high`),
while local models like Qwen3.8 only support `xhigh`, `medium`, and `low` in their Jinja templates.
"""
from typing import Any, Dict

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.reasoning_effort_utils import reasoning_effort_from_thinking_budget

_HIGH_EFFORTS = frozenset({'high', 'max'})


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
    if isinstance(thinking, dict):
        thinking_type = thinking.get('type')
        budget = thinking.get('budget_tokens')
        if thinking_type == 'enabled' and isinstance(budget, (int, float)) and not isinstance(budget, bool):
            # LiteLLM translates legacy budgets after this hook. Supply the mapped
            # effort as adaptive input so its adapter cannot recreate "high".
            mapped = reasoning_effort_from_thinking_budget(budget)
            mapped = {'high': 'xhigh', 'minimal': 'low'}.get(mapped, mapped)
            data['thinking'] = {**thinking, 'type': 'adaptive'}
            data['thinking'].pop('budget_tokens', None)
            data['output_config'] = {**(output_cfg if isinstance(output_cfg, dict) else {}), 'effort': mapped}
        elif thinking_type == 'adaptive' and not has_output_effort:
            data['output_config'] = {**(output_cfg if isinstance(output_cfg, dict) else {}), 'effort': 'xhigh'}

    return data


class ReasoningEffortNormalizer(CustomLogger):
    """Normalizes incoming Anthropic/OpenAI effort levels before LiteLLM forwards to llama-server."""

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: Dict[str, Any], call_type: Any):
        return normalize_reasoning_effort_payload(data)


proxy_effort_hook = ReasoningEffortNormalizer()
