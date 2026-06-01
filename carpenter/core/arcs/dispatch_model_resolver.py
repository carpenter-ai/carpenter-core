"""Dispatch-time model resolution.

At arc dispatch time, resolve the model to actually invoke:
- If the arc has a model_policy_id with policy_json, run the scoring
  selector against available models; return the top choice plus the
  ranked list of fallbacks for failover.
- Otherwise return the hard-pinned model row (if any).
"""

import logging

logger = logging.getLogger(__name__)


def resolve_dispatch_model(arc_id: int, arc_info: dict) -> tuple[dict | None, list, str | None, bool]:
    """Resolve model config for an arc about to invoke an agent.

    Args:
        arc_id: The arc being dispatched (for logging).
        arc_info: The arc row dict.

    Returns:
        Tuple of (model_config, fallback_models, selected_model_id, connectivity_degraded):
            model_config: dict with model / agent_role / temperature / max_tokens
                keys, or None if no policy matched.
            fallback_models: list of SelectionResult alternatives (score-descending).
                Empty if the arc uses a hard-pinned model.
            selected_model_id: model_id chosen by the policy selector, or None if
                no selection was performed.
            connectivity_degraded: True if policy-based selection returned an empty
                ranked list (caller should abort dispatch and fire a connectivity event).
    """
    from . import manager as arc_manager

    policy_id = arc_info.get("model_policy_id")
    model_config = None
    fallback_models: list = []
    selected_model_id: str | None = None

    if policy_id is not None:
        policy_row = arc_manager.get_model_policy(policy_id)
        if policy_row:
            policy_json = policy_row.get("policy_json")
            if policy_json and not policy_row.get("model"):
                # Policy-based selection — get ranked list for fallback
                try:
                    from ..models.selector import ModelPolicy, select_models
                    policy = ModelPolicy.from_db_row(policy_row)
                    ranked = select_models(policy)
                    if not ranked:
                        return None, [], None, True
                    # Use top-ranked model, keep rest as fallbacks
                    top = ranked[0]
                    fallback_models = ranked[1:]
                    selected_model_id = top.model_id
                    # Build model_config dict from selection
                    model_config = {
                        "model": top.model_id,
                        "agent_role": policy_row.get("agent_role"),
                        "temperature": policy_row.get("temperature"),
                        "max_tokens": policy_row.get("max_tokens"),
                    }
                    logger.info(
                        "Arc %d: model selector chose %s (%s), %d fallback(s)",
                        arc_id, top.model_key, top.reason,
                        len(fallback_models),
                    )
                except (ImportError, KeyError, ValueError, TypeError) as _exc:
                    logger.exception("Arc %d: model selector failed, falling back", arc_id)
                    model_config = dict(policy_row)
            else:
                # Hard-pinned model in policy row
                model_config = dict(policy_row)

    return model_config, fallback_models, selected_model_id, False
