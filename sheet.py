"""
sheet.py — fair price sheet. No odds feed, no scraper, no excuses.

For every game it prints what the model thinks, expressed as a PRICE:

    fair    = 1 / probability          (break-even, zero margin)
    need    = 1.03 / probability       (price that clears a 3% edge)

Then you look at Superbet. If their price is above `need`, it is a bet, and the
stake column tells you how much. If it is below, it is not. That is the whole
workflow, and it is honest in a way that "best pick" cannot be without a price.

There is deliberately NO single "best pick" per game. Ranking selections by
probability alone always crowns the favourite's +1.5 run line, because that is
mathematically the most likely thing on the board. That ranking is the bug, not
the feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import markets as mk
from leagues import LeagueConfig

# Only surface selections that could realistically be a bet.
MIN_PROB = 0.18          # below this, the model is guessing
MAX_PROB = 0.80          # above this the required price is unreachable anyway


@dataclass
class SheetRow:
    league: str
    game: str
    market: str
    side: str
    handicap: Optional[float]
    label: str
    prob: float
    fair: float          # break-even decimal price
    need: float          # minimum decimal price for MIN_EDGE
    max_stake_pct: float  # stake IF you get exactly `need`
    confidence: float
    note: str = ""


def _stake_at(prob: float, price: float) -> float:
    return round(mk.kelly(prob, price) * 100, 2)


def build_sheet(league: str, game: str, probs: Dict[str, float],
                labels: Dict[str, str], confidence: float) -> List[SheetRow]:
    rows: List[SheetRow] = []

    for key, p in probs.items():
        if p <= 0 or not (MIN_PROB <= p <= MAX_PROB):
            continue
        market, side, hcap = key.split("|")
        h = None if hcap == "None" else float(hcap)

        fair = 1.0 / p
        need = (1.0 + mk.MIN_EDGE) / p

        note = ""
        if need < mk.MIN_ODDS:
            # the required price is shorter than we would ever bet
            note = f"below {mk.MIN_ODDS} floor"
            need = max(need, mk.MIN_ODDS)
        if confidence < 0.75:
            note = (note + "; " if note else "") + "thin data"

        rows.append(SheetRow(
            league=league, game=game, market=market, side=side, handicap=h,
            label=labels.get(key, key),
            prob=round(p, 4),
            fair=round(fair, 2),
            need=round(need, 2),
            max_stake_pct=_stake_at(p, need),
            confidence=confidence,
            note=note,
        ))

    order = {"ML": 0, "RL": 1, "TOTAL": 2, "TT_HOME": 3, "TT_AWAY": 4}
    rows.sort(key=lambda r: (order.get(r.market, 9), -r.prob))
    return rows


def render(rows: List[SheetRow], game: str, header: str = "",
           per_market: int = 3) -> str:
    """`per_market` caps how many selections print per market group."""
    if not rows:
        return f"\n{game}\n  nothing in the printable probability band"

    out = [f"\n{game}"]
    if header:
        out.append(f"  {header}")
    width = 34
    out.append(f"  {'selection':<{width}}{'prob':>7}{'fair':>7}{'need':>7}{'stake':>8}")
    rule = "  " + "-" * (width + 29)
    out.append(rule)

    seen: Dict[str, int] = {}
    last = None
    for r in rows:
        n = seen.get(r.market, 0)
        if n >= per_market:
            continue
        seen[r.market] = n + 1
        if last and r.market != last:
            out.append(rule)
        last = r.market
        star = " *" if r.note and "floor" in r.note else ""
        out.append(f"  {r.label[:width-1]:<{width}}{100*r.prob:6.1f}%"
                   f"{r.fair:>7.2f}{r.need:>7.2f}{r.max_stake_pct:>7.2f}%{star}")

    out.append(rule)
    out.append("  Bet only where your book pays MORE than 'need'.")
    if any("floor" in (r.note or "") for r in rows[:sum(seen.values())]):
        out.append(f"  * required price sits under the {mk.MIN_ODDS} floor — skip")
    if rows and rows[0].confidence < 0.75:
        out.append("  thin data for this league — stakes already discounted")
    return "\n".join(out)


def check(prob: float, price: float) -> str:
    """
    Quick manual check for one selection once you have the real price.

        sheet.check(0.564, 1.95)
    """
    ev = prob * price - 1.0
    stake = _stake_at(prob, price)
    verdict = "BET" if ev >= mk.MIN_EDGE and price >= mk.MIN_ODDS else "PASS"
    return (f"{verdict}  prob {100*prob:.1f}%  price {price:.2f}  "
            f"EV {100*ev:+.1f}%  stake {stake:.2f}%")
