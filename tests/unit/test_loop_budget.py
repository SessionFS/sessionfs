"""Unit tests for review-loop budget status (Review Loop R3, Brief D2).

The cost-budget hit phrase must not leak a dollar amount to non-admin
watchers; token and round figures are fine.
"""

from __future__ import annotations

from decimal import Decimal

from sessionfs.server.services.loop_budget import BudgetConfig, BudgetStatus


def _status(**kwargs) -> BudgetStatus:
    config = BudgetConfig(
        token_budget=kwargs.pop("token_budget", None),
        cost_budget_usd=kwargs.pop("cost_budget_usd", None),
        max_review_rounds=kwargs.pop("max_review_rounds", None),
    )
    return BudgetStatus(config=config, **kwargs)


def test_cost_limit_summary_has_no_dollar_figure():
    status = _status(cost_budget_usd=12.50, cost_usd_used=Decimal("13"))
    assert status.cost_exceeded is True
    assert status.limit_summary() == "a cost budget"
    assert "$" not in status.limit_summary()
    assert "12.50" not in status.limit_summary()


def test_token_limit_summary_keeps_figure():
    status = _status(token_budget=1000, tokens_used=1000)
    assert status.token_exceeded is True
    assert status.limit_summary() == "token budget of 1,000"


def test_round_limit_summary_keeps_figure():
    status = _status(max_review_rounds=3, rounds_used=3)
    assert status.rounds_exceeded is True
    assert status.limit_summary() == "review-round cap of 3"


def test_token_limit_reported_before_cost():
    status = _status(
        token_budget=1000,
        tokens_used=1000,
        cost_budget_usd=12.50,
        cost_usd_used=Decimal("13"),
    )
    assert status.limit_summary() == "token budget of 1,000"
