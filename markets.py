"""
markets.py — the part that stops you betting chalk.

Core idea: a raw price contains the bookmaker's margin. Comparing a model
probability against a RAW implied probability systematically overstates edge on
short prices and understates it on long ones. That is exactly how an engine ends
up recommending favourite -1.5/+1.5 every single day.

So: de-vig each market to a fair price first, then measure the model against
the fair line. An efficiently priced favourite lands at ~0 edge, where it
belongs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --- selection thresholds --------------------------------------------------
MIN_EDGE = 0.030         # model must beat the FAIR price by this much
MIN_ODDS = 1.60          # never bet shorter than this
MAX_ODDS = 8.00          # longshots: model noise dominates, skip
MAX_STAKE_PCT = 0.02     # hard cap, fraction of bankroll
KELLY_FRACTION = 0.25    # quarter Kelly


@dataclass
class Odds:
    market: str              # ML / RL / TOTAL / TT_HOME / TT_AWAY
    side: str                # HOME / AWAY / DRAW / OVER / UNDER
    handicap: Optional[float]
    price: float
    source: str = ""

    @property
    def key(self) -> Tuple[str, str, Optional[float]]:
        return (self.market, self.side, self.handicap)


@dataclass
class Pick:
    league: str
    game: str
    market: str
    side: str
    handicap: Optional[float]
    label: str
    model_prob: float
    price: float
    fair_prob: float         # de-vigged market probability
    fair_price: float
    edge: float              # model_prob * price - 1
    edge_vs_fair: float      # model_prob - fair_prob
    kelly_pct: float
    grade: str
    source: str = ""
    notes: str = ""


def implied(price: float) -> float:
    return 1.0 / price if price and price > 1.0 else 0.0


def devig(prices: List[float], method: str = "power") -> List[float]:
    """
    Strip the bookmaker margin from a complete market.

    'multiplicative' just normalises implied probabilities. It is simple but
    biased: it shaves too much off favourites and not enough off longshots,
    which is the same failure mode we are trying to kill.

    'power' solves for k in sum(p_i**k) = 1. It handles favourite-longshot
    bias far better and is the default.
    """
    imps = [implied(p) for p in prices]
    if not imps or min(imps) <= 0:
        return []
    total = sum(imps)
    if total <= 1.0:                       # no margin (or arb) -> leave alone
        return imps

    if method == "multiplicative":
        return [i / total for i in imps]

    lo, hi = 0.5, 2.0
    for _ in range(80):                    # bisection on k
        k = (lo + hi) / 2
        s = sum(i ** k for i in imps)
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    out = [i ** k for i in imps]
    s = sum(out)
    return [o / s for o in out]


def build_fair_book(odds: List[Odds]) -> Dict[Tuple[str, str, Optional[float]], float]:
    """
    Group odds into complete markets, de-vig each group, return fair
    probability per selection. Incomplete markets (one side only) are skipped —
    they cannot be de-vigged and betting into them blind is how you get hurt.
    """
    groups: Dict[Tuple[str, Optional[float]], List[Odds]] = {}
    for o in odds:
        if not o.price or o.price <= 1.0:
            continue
        # Run lines are stored signed per side (HOME +1.5 / AWAY -1.5). Those
        # are two sides of ONE market, so group them on the absolute handicap,
        # otherwise each side looks like an incomplete book and gets dropped.
        gk = abs(o.handicap) if (o.market == "RL" and o.handicap is not None) \
            else o.handicap
        groups.setdefault((o.market, gk), []).append(o)

    fair: Dict[Tuple[str, str, Optional[float]], float] = {}
    for (market, hcap), grp in groups.items():
        sides = {o.side: o for o in grp}
        # a market is only complete if we have both/all sides
        if market == "ML":
            need = [{"HOME", "AWAY"}, {"HOME", "AWAY", "DRAW"}]
        elif market == "RL":
            need = [{"HOME", "AWAY"}]
        else:
            need = [{"OVER", "UNDER"}]
        if not any(set(sides) == n for n in need):
            continue
        keys = list(sides)
        probs = devig([sides[k].price for k in keys])
        for k, p in zip(keys, probs):
            fair[(market, k, hcap)] = p
    return fair


def kelly(prob: float, price: float) -> float:
    """Fractional Kelly stake as a fraction of bankroll, capped."""
    b = price - 1.0
    if b <= 0:
        return 0.0
    f = (prob * price - 1.0) / b
    if f <= 0:
        return 0.0
    return min(f * KELLY_FRACTION, MAX_STAKE_PCT)


def grade(edge_vs_fair: float, confidence: float) -> str:
    """Grade on edge against the fair line, discounted by data confidence."""
    e = edge_vs_fair * confidence
    if e >= 0.070:
        return "A"
    if e >= 0.050:
        return "A-"
    if e >= 0.038:
        return "B"
    if e >= MIN_EDGE:
        return "C+"
    return "NO BET"


def evaluate(league: str, game: str, model_probs: Dict[str, float],
             odds: List[Odds], confidence: float,
             labels: Dict[str, str]) -> List[Pick]:
    """
    Turn model probabilities + a price board into ranked, staked picks.

    model_probs keys must match the odds keys as f"{market}|{side}|{handicap}".
    Anything unpriced, too short, too long, or in an incomplete market is
    silently dropped — it cannot be evaluated honestly.
    """
    fair = build_fair_book(odds)
    picks: List[Pick] = []

    for o in odds:
        key = (o.market, o.side, o.handicap)
        mk = f"{o.market}|{o.side}|{o.handicap}"
        p = model_probs.get(mk)
        if p is None:
            continue
        if not (MIN_ODDS <= o.price <= MAX_ODDS):
            continue
        fp = fair.get(key)
        if fp is None:                       # incomplete market, cannot de-vig
            continue

        edge = p * o.price - 1.0
        evf = p - fp
        if evf < MIN_EDGE:
            continue

        st = kelly(p, o.price)
        if st <= 0:
            continue

        picks.append(Pick(
            league=league, game=game, market=o.market, side=o.side,
            handicap=o.handicap,
            label=labels.get(mk, mk),
            model_prob=round(p, 4), price=o.price,
            fair_prob=round(fp, 4), fair_price=round(1 / fp, 3) if fp else 0.0,
            edge=round(edge, 4), edge_vs_fair=round(evf, 4),
            kelly_pct=round(st * 100, 2),
            grade=grade(evf, confidence), source=o.source,
        ))

    picks = [p for p in picks if p.grade != "NO BET"]
    picks.sort(key=lambda x: x.edge_vs_fair, reverse=True)
    return picks
