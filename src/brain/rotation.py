"""
Sell-to-fund capital rotation (docs/capital_rotation.md).

Pure planner. It runs only when a gated buy cannot be paid for, frees only enough cash for
that one buy, and never reviews the book for its own sake. The Autotrader calls it with the
sleeve's own holdings. The Advisor book is not wired: it is never starved, so the trigger
would not fire.

A sale settles the next session. This function only chooses the pair. The executor places
the sell, stores the intent, and funds the buy after the cash has settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src import config
from src.agent_guardrails import business_days_between

# Intent is a priority ladder, not a veto. Lower sorts first (easier to release).
_INTENT_RANK = {"written_off": 0, "do_not_add": 1, "opportunistic": 2, "thesis_hold": 3}


@dataclass(frozen=True)
class RotationPlan:
    sell_ticker: str
    sell_fraction: float          # (0, 1]; 1.0 is a full exit
    buy_ticker: str
    expected_proceeds: float
    challenger_score: float
    incumbent_score: float
    margin: float
    reason: str
    full_exit: bool


def _as_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _watch_floor() -> float:
    if config.brain.conviction_primary:
        return config.brain.cp_watch_threshold
    return config.brain.watch_threshold


def clamp_rotation_preference(ranked: list[dict], preference: list[str] | None) -> list[dict]:
    """Reorder only inside the top 3, and never across the release ladder.

    A preference may swap two names the ranker already surfaced. It may not introduce a
    name, and it may not lift a thesis_hold (or any higher band) above an easier release.
    Dust stays ahead of real positions. Anything past the top 3 is left alone.
    """
    if not preference or len(ranked) < 2:
        return ranked
    top, rest = ranked[:3], ranked[3:]
    pref = {ticker: i for i, ticker in enumerate(preference)}

    def band(inc: dict) -> tuple:
        if inc.get("dust"):
            return (0,)
        return (1, _INTENT_RANK.get(inc.get("intent") or "opportunistic", 2))

    def key(inc: dict) -> tuple:
        return (band(inc), pref.get(inc["ticker"], 99))

    return sorted(top, key=key) + rest


def _rank_incumbents(incumbents: list[dict], cooled: set[str], now: datetime) -> list[dict]:
    """Weakest releasable holding first. Dust ignores intent and sorts ahead of everything."""
    min_hold = config.agent.min_hold_days
    minimum = config.agent.min_position_usd
    ranked: list[dict] = []
    for inc in incumbents:
        ticker = inc.get("ticker")
        score = inc.get("own_score")
        value = inc.get("value_usd")
        if not ticker or score is None or not value or value <= 0:
            continue
        if inc.get("excluded") or ticker in cooled:
            continue
        bought = _as_dt(inc.get("last_buy_at"))
        if bought is not None and business_days_between(bought, now) < min_hold:
            continue
        intent = inc.get("intent") or "opportunistic"
        dust = value < minimum
        row = {**inc, "intent": intent, "dust": dust, "own_score": float(score), "value_usd": float(value)}
        ranked.append(row)

    def sort_key(inc: dict) -> tuple:
        if inc["dust"]:
            return (0, inc["own_score"], inc["ticker"])
        return (1, _INTENT_RANK.get(inc["intent"], 2), inc["own_score"], inc["ticker"])

    ranked.sort(key=sort_key)
    return ranked


def _best_challenger(challengers: list[dict], cooled: set[str]) -> dict | None:
    """The single strongest unfunded buy that the sleeve does not already hold."""
    best: dict | None = None
    for ch in challengers:
        ticker = ch.get("ticker")
        score = ch.get("own_score")
        if not ticker or score is None or ticker in cooled:
            continue
        if ch.get("excluded") or ch.get("held") or ch.get("hold_only") or ch.get("do_not_add"):
            continue
        row = {**ch, "own_score": float(score)}
        if best is None or row["own_score"] > best["own_score"]:
            best = row
    return best


def plan_rotation(
    challengers: list[dict],
    incumbents: list[dict],
    budget: float,
    *,
    weight: float = 1.0,
    cooled: set[str] | None = None,
    preference: list[str] | None = None,
    now: datetime | None = None,
) -> RotationPlan | None:
    """Choose one sell that funds one buy, or nothing.

    `challengers` are gated buys the allocator could not fund. Each needs `ticker`,
    `own_score`, and optionally `desired_usd` (defaults to the sleeve's minimum lot),
    `held`, `hold_only`, `do_not_add`, `excluded`.

    `incumbents` are sleeve holdings. Each needs `ticker`, `own_score`, `value_usd`,
    and optionally `intent`, `last_buy_at`, `excluded`.

    `weight` is the Playbook weight. At 1 the published margins apply. Lower makes the
    required edge larger. Zero or less plans nothing.

    `preference` is an optional reorder of the top three release candidates (the LLM
    hint). It is clamped so it cannot cause a sale the ranker would not have considered.
    """
    if weight <= 0:
        return None
    now = now or datetime.now()
    cooled = cooled or set()
    scale = 1.0 / min(1.0, max(0.25, weight))

    challenger = _best_challenger(challengers, cooled)
    if challenger is None:
        return None
    desired = float(challenger.get("desired_usd") or config.agent.min_position_usd)
    shortfall = desired - float(budget or 0.0)
    if shortfall <= 0 or desired <= 0:
        return None

    ranked = clamp_rotation_preference(_rank_incumbents(incumbents, cooled, now), preference)
    # Never sell the name we are trying to buy.
    ranked = [inc for inc in ranked if inc["ticker"] != challenger["ticker"]]

    target = shortfall * (1.0 + config.brain.swap_overshoot)
    watch = _watch_floor()
    need = max(config.brain.swap_min_lot, desired)
    min_proceeds = config.brain.swap_min_proceeds

    for inc in ranked:
        base = (
            config.brain.swap_margin_thesis_hold
            if inc["intent"] == "thesis_hold"
            else config.brain.swap_margin
        )
        margin = base * scale
        edge = challenger["own_score"] - inc["own_score"]
        if edge < margin:
            continue

        value = inc["value_usd"]
        full_exit = inc["own_score"] < watch
        if full_exit:
            fraction = 1.0
            proceeds = round(value, 2)
        else:
            # A trim, never a full exit of a name that still clears the watch bar.
            fraction = round(target / value, 6)
            if fraction <= 0 or fraction >= 1:
                continue
            proceeds = round(value * fraction, 2)

        if proceeds < min_proceeds or float(budget or 0.0) + proceeds < need:
            continue

        kind = "exit" if full_exit else f"trim {fraction:.0%}"
        reason = (
            f"{challenger['ticker']} ({challenger['own_score']:.2f}) beats "
            f"{inc['ticker']} ({inc['own_score']:.2f}, {inc['intent']}) by {edge:.2f} "
            f"(needs {margin:.2f}); {kind} frees ${proceeds:.0f} toward a ${desired:.0f} lot"
        )
        return RotationPlan(
            sell_ticker=inc["ticker"],
            sell_fraction=fraction,
            buy_ticker=challenger["ticker"],
            expected_proceeds=proceeds,
            challenger_score=challenger["own_score"],
            incumbent_score=inc["own_score"],
            margin=round(margin, 4),
            reason=reason,
            full_exit=full_exit,
        )
    return None
