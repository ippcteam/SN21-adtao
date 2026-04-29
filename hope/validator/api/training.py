"""Training data endpoint — miners fetch historical episodes with known outcomes.

Public endpoint (no auth) — this is training data, not live epoch data.
Miners use this to build and improve their prediction models.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/training/episodes")
async def get_training_episodes(request: Request):
    """Get historical episodes with known outcomes for model training.

    Returns episodes that have measured t7/t14 outcomes — the ground truth
    that miners train their models against. No authentication required.

    Each training example contains:
    - input: the full episode payload (same format as live epochs)
    - outcome: the actual t7/t14 deltas (what really happened)
    - scoring_metadata: goal type, resolution, coverage status
    """
    state = request.app.state.validator
    episodes = state.get("episodes", [])
    outcomes = state.get("outcomes", [])

    if not episodes or not outcomes:
        return {
            "training_episodes": [],
            "count": 0,
            "message": "No training data available. Start the validator with a release first.",
        }

    training = []
    for ep, outcome in zip(episodes, outcomes):
        if not hasattr(outcome, "t7") or (not outcome.t7 and not outcome.t14):
            continue

        training.append({
            "episode_id": ep.episode_metadata.episode_id,
            "input": ep.model_dump(mode="json"),
            "outcome": {
                "t7": outcome.t7.model_dump(mode="json") if outcome.t7 else None,
                "t14": outcome.t14.model_dump(mode="json") if outcome.t14 else None,
            },
            "scoring_metadata": outcome.scoring_metadata.model_dump(mode="json"),
        })

    return {
        "training_episodes": training,
        "count": len(training),
        "total_episodes": len(episodes),
        "schema_version": "v1.9",
        "description": (
            "Each example has 'input' (episode payload) and 'outcome' (actual deltas). "
            "Train your model to predict outcome from input. "
            "See docs/miner_quickstart.md for scoring details."
        ),
    }


@router.get("/training/summary")
async def get_training_summary(request: Request):
    """Get summary stats about available training data."""
    state = request.app.state.validator
    episodes = state.get("episodes", [])
    outcomes = state.get("outcomes", [])

    with_t7 = 0
    with_t14 = 0
    action_types = {}

    for ep, outcome in zip(episodes, outcomes):
        if hasattr(outcome, "t7") and outcome.t7:
            with_t7 += 1
        if hasattr(outcome, "t14") and outcome.t14:
            with_t14 += 1

        actions = ep.action_bundle.actions
        if actions:
            at = actions[0].type
            action_types[at] = action_types.get(at, 0) + 1

    return {
        "total_episodes": len(episodes),
        "with_t7_outcomes": with_t7,
        "with_t14_outcomes": with_t14,
        "action_type_distribution": action_types,
        "schema_version": "v1.9",
    }
