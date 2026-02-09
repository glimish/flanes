"""
Cost Budgets

Per-lane cost budget configuration, checking, and reporting.
Budgets are stored in the existing lanes.metadata JSON column
under the "budget" key — no schema change required.
"""

import json
import logging
from dataclasses import dataclass, field

from .serializable import Serializable

logger = logging.getLogger(__name__)


@dataclass
class BudgetConfig(Serializable):
    """Budget limits for a lane."""

    max_tokens_in: int | None = None
    max_tokens_out: int | None = None
    max_api_calls: int | None = None
    max_wall_time_ms: float | None = None
    alert_threshold_pct: float = 80.0


@dataclass
class BudgetStatus(Serializable):
    """Current budget usage and status."""

    config: BudgetConfig
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_api_calls: int = 0
    total_wall_time_ms: float = 0.0
    warnings: list = field(default_factory=list)
    exceeded: list = field(default_factory=list)


class BudgetError(Exception):
    """Raised when a budget limit is exceeded."""

    pass


def get_lane_budget(wsm, lane: str) -> BudgetConfig | None:
    """Get the budget config for a lane, if any."""
    row = wsm.conn.execute("SELECT metadata FROM lanes WHERE name = ?", (lane,)).fetchone()
    if row is None:
        return None
    metadata = json.loads(row[0]) if row[0] else {}
    budget_data = metadata.get("budget")
    if budget_data is None:
        return None
    return BudgetConfig.from_dict(budget_data)


def set_lane_budget(wsm, lane: str, config: BudgetConfig) -> None:
    """Store budget config in lane metadata."""
    row = wsm.conn.execute("SELECT metadata FROM lanes WHERE name = ?", (lane,)).fetchone()
    if row is None:
        raise ValueError(f"Lane not found: {lane}")
    metadata = json.loads(row[0]) if row[0] else {}
    metadata["budget"] = config.to_dict()
    wsm.conn.execute(
        "UPDATE lanes SET metadata = ? WHERE name = ?",
        (json.dumps(metadata), lane),
    )
    wsm.conn.commit()


def compute_budget_status(wsm, lane: str) -> BudgetStatus | None:
    """Compute current budget usage for a lane."""
    config = get_lane_budget(wsm, lane)
    if config is None:
        return None

    rows = wsm.conn.execute("SELECT cost_json FROM transitions WHERE lane = ?", (lane,)).fetchall()

    total_in = 0
    total_out = 0
    total_calls = 0
    total_wall = 0.0

    for (cost_json,) in rows:
        cost = json.loads(cost_json) if cost_json else {}
        total_in += cost.get("tokens_in", 0)
        total_out += cost.get("tokens_out", 0)
        total_calls += cost.get("api_calls", 0)
        total_wall += cost.get("wall_time_ms", 0.0)

    status = BudgetStatus(
        config=config,
        total_tokens_in=total_in,
        total_tokens_out=total_out,
        total_api_calls=total_calls,
        total_wall_time_ms=total_wall,
    )

    threshold = config.alert_threshold_pct / 100.0

    def _check(name, current, limit):
        if limit is None:
            return
        if current >= limit:
            status.exceeded.append(name)
        elif current >= limit * threshold:
            status.warnings.append(name)

    _check("tokens_in", total_in, config.max_tokens_in)
    _check("tokens_out", total_out, config.max_tokens_out)
    _check("api_calls", total_calls, config.max_api_calls)
    _check("wall_time_ms", total_wall, config.max_wall_time_ms)

    return status


def check_budget(wsm, lane: str, additional_cost: dict | None = None) -> BudgetStatus | None:
    """Check budget and raise BudgetError if any limit is exceeded.

    If additional_cost is provided, it is added to the totals before checking.
    """
    status = compute_budget_status(wsm, lane)
    if status is None:
        return None

    if additional_cost:
        status.total_tokens_in += additional_cost.get("tokens_in", 0)
        status.total_tokens_out += additional_cost.get("tokens_out", 0)
        status.total_api_calls += additional_cost.get("api_calls", 0)
        status.total_wall_time_ms += additional_cost.get("wall_time_ms", 0.0)

        # Recompute warnings/exceeded with additional cost
        status.warnings = []
        status.exceeded = []
        config = status.config
        threshold = config.alert_threshold_pct / 100.0

        def _check(name, current, limit):
            if limit is None:
                return
            if current >= limit:
                status.exceeded.append(name)
            elif current >= limit * threshold:
                status.warnings.append(name)

        _check("tokens_in", status.total_tokens_in, config.max_tokens_in)
        _check("tokens_out", status.total_tokens_out, config.max_tokens_out)
        _check("api_calls", status.total_api_calls, config.max_api_calls)
        _check("wall_time_ms", status.total_wall_time_ms, config.max_wall_time_ms)

    if status.exceeded:
        raise BudgetError(f"Budget exceeded for lane '{lane}': {', '.join(status.exceeded)}")

    return status
