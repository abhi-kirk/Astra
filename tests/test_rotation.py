"""Capital rotation: sell one weaker sleeve holding to fund one buy the budget cannot cover."""

from datetime import datetime, timedelta

from src.brain.rotation import clamp_rotation_preference, plan_rotation

NOW = datetime(2026, 10, 30, 13, 0)


def _buy(ticker="NBIS", score=0.90, desired=200.0, **kw):
    return {"ticker": ticker, "own_score": score, "desired_usd": desired, **kw}


def _held(ticker="ASTS", score=0.40, value=400.0, intent="opportunistic", **kw):
    return {"ticker": ticker, "own_score": score, "value_usd": value, "intent": intent, **kw}


def test_no_rotation_when_the_buy_is_already_funded():
    assert plan_rotation([_buy()], [_held()], budget=250.0, now=NOW) is None


def test_no_rotation_without_a_challenger():
    assert plan_rotation([], [_held()], budget=50.0, now=NOW) is None


def test_skips_a_challenger_the_sleeve_already_holds():
    """Rotation funds a new name. It does not sell something to add to a name already held."""
    plan = plan_rotation(
        [_buy(ticker="NVDA", held=True), _buy(ticker="NBIS", score=0.80)],
        [_held()],
        budget=100.0,
        now=NOW,
    )
    assert plan is not None and plan.buy_ticker == "NBIS"


def test_hold_only_and_excluded_names_are_never_funded():
    plan = plan_rotation(
        [_buy(ticker="NIO", hold_only=True), _buy(ticker="TSLA", excluded=True, score=0.99)],
        [_held()],
        budget=100.0,
        now=NOW,
    )
    assert plan is None


def test_opportunistic_margin_is_lower_than_thesis_hold():
    # Edge of 0.20 clears 0.15 and fails 0.30.
    incumbents = [
        _held(ticker="DUST", score=0.70, value=30.0, intent="opportunistic"),  # too small to fund
        _held(ticker="OPP", score=0.70, value=400.0, intent="opportunistic"),
        _held(ticker="CORE", score=0.70, value=400.0, intent="thesis_hold"),
    ]
    plan = plan_rotation([_buy(score=0.90)], incumbents, budget=100.0, now=NOW)
    assert plan is not None
    assert plan.sell_ticker == "OPP"
    assert plan.full_exit is False
    assert plan.margin == 0.15


def test_thesis_hold_requires_the_wider_margin():
    plan = plan_rotation(
        [_buy(score=0.90)],
        [_held(score=0.50, intent="thesis_hold", value=400.0)],
        budget=100.0,
        now=NOW,
    )
    # 0.40 edge clears the 0.30 thesis-hold bar. Score 0.50 is still above the watch floor, so trim.
    assert plan is not None and plan.sell_ticker == "ASTS" and plan.full_exit is False
    assert abs(plan.sell_fraction - round(110 / 400, 6)) < 1e-9


def test_thesis_hold_below_the_wider_margin_is_kept():
    plan = plan_rotation(
        [_buy(score=0.70)],
        [_held(score=0.50, intent="thesis_hold", value=400.0)],
        budget=100.0,
        now=NOW,
    )
    assert plan is None  # edge 0.20 < 0.30


def test_dust_sorts_first_but_a_sale_that_cannot_fund_the_lot_is_skipped():
    """A $20 stub is the weakest holding and still must not be sold: the proceeds cannot
    pay for a $200 lot. The planner walks to the next name that can."""
    plan = plan_rotation(
        [_buy(score=0.90)],
        [
            _held(ticker="TSM", score=0.10, value=20.0, intent="thesis_hold"),
            _held(ticker="PL", score=0.55, value=400.0, intent="opportunistic"),
        ],
        budget=100.0,
        now=NOW,
    )
    assert plan is not None and plan.sell_ticker == "PL"


def test_full_exit_only_when_the_incumbent_is_below_the_watch_bar():
    plan = plan_rotation(
        [_buy(score=0.90)],
        [_held(score=0.20, value=400.0, intent="opportunistic")],
        budget=100.0,
        now=NOW,
    )
    assert plan is not None and plan.full_exit and plan.sell_fraction == 1.0
    assert plan.expected_proceeds == 400.0


def test_a_name_bought_inside_the_minimum_hold_is_not_sold():
    bought = NOW - timedelta(days=1)
    plan = plan_rotation(
        [_buy(score=0.90)],
        [_held(last_buy_at=bought, value=400.0, score=0.20)],
        budget=100.0,
        now=NOW,
    )
    assert plan is None


def test_cooldown_blocks_both_sides():
    plan = plan_rotation(
        [_buy(ticker="NBIS", score=0.90)],
        [_held(ticker="ASTS", score=0.20, value=400.0)],
        budget=100.0,
        cooled={"ASTS"},
        now=NOW,
    )
    assert plan is None
    plan = plan_rotation(
        [_buy(ticker="NBIS", score=0.90)],
        [_held(ticker="ASTS", score=0.20, value=400.0)],
        budget=100.0,
        cooled={"NBIS"},
        now=NOW,
    )
    assert plan is None


def test_excluded_incumbent_is_never_sold():
    plan = plan_rotation(
        [_buy(score=0.99)],
        [_held(ticker="TSLA", score=0.0, value=500.0, excluded=True)],
        budget=50.0,
        now=NOW,
    )
    assert plan is None


def test_lower_play_weight_demands_a_wider_edge():
    incumbents = [_held(score=0.70, value=400.0, intent="opportunistic")]
    # Edge 0.20 clears the 0.15 bar at full weight, and fails when weight 0.5 doubles it to 0.30.
    assert plan_rotation([_buy(score=0.90)], incumbents, budget=100.0, weight=1.0, now=NOW) is not None
    assert plan_rotation([_buy(score=0.90)], incumbents, budget=100.0, weight=0.5, now=NOW) is None
    assert plan_rotation([_buy(score=0.90)], incumbents, budget=100.0, weight=0.0, now=NOW) is None


def test_preference_reorders_within_a_band_and_cannot_promote_a_thesis_hold():
    ranked = [
        _held(ticker="A", score=0.40, intent="opportunistic"),
        _held(ticker="B", score=0.45, intent="opportunistic"),
        _held(ticker="C", score=0.10, intent="thesis_hold"),
    ]
    for row in ranked:
        row["dust"] = False
    out = clamp_rotation_preference(ranked, ["C", "B", "A", "NOT_A_CANDIDATE"])
    assert [r["ticker"] for r in out] == ["B", "A", "C"]


def test_intent_ladder_releases_written_off_before_a_core_holding():
    plan = plan_rotation(
        [_buy(score=0.90)],
        [
            _held(ticker="CORE", score=0.40, value=400.0, intent="thesis_hold"),
            _held(ticker="DEAD", score=0.40, value=400.0, intent="written_off"),
        ],
        budget=100.0,
        now=NOW,
    )
    # Same score. written_off is easier to release, and 0.50 edge clears its 0.15 bar.
    # CORE would also clear 0.30, but it sorts later.
    assert plan is not None and plan.sell_ticker == "DEAD"
