"""
model.py — run projection + Monte Carlo simulation.

Two-tier by design:

  TIER 1 (MLB): starter quality, bullpen, park, weather feed the projection.
  TIER 2 (NPB/KBO/LMB): no public pitcher API exists, so team strength is
         inferred from recent results via Elo + runs scored/allowed. Weaker,
         and the engine says so by lowering `confidence`, which shrinks stakes.

Never pretend tier 2 is tier 1. The confidence value is what keeps the staking
honest when the inputs are thin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from leagues import LeagueConfig


@dataclass
class TeamInput:
    name: str
    rating: float            # Elo-style, 1500 = league average
    rs_pg: Optional[float] = None    # runs scored per game, recent
    ra_pg: Optional[float] = None    # runs allowed per game, recent
    starter_ra9: Optional[float] = None   # tier 1 only
    starter_ip: float = 5.4
    bullpen_ra9: Optional[float] = None
    games_sampled: int = 0
    rest_days: int = 1


def _blend(*pairs: Tuple[Optional[float], float]) -> Optional[float]:
    num = den = 0.0
    for val, w in pairs:
        if val is not None and w > 0:
            num += val * w
            den += w
    return num / den if den else None


def project_runs(off: TeamInput, deff: TeamInput, lg: LeagueConfig,
                 park: float, is_home: bool, weather: float = 1.0) -> float:
    """Expected runs for `off` against `deff`. Returns a mean, not a sample."""
    lg_rpg = lg.rpg

    # offence: blend recent scoring with Elo-implied scoring
    elo_off = lg_rpg * (1.0 + (off.rating - 1500) / 1200.0)
    off_rate = _blend((off.rs_pg, min(off.games_sampled, 25)),
                      (elo_off, 12.0)) or lg_rpg

    # defence: starter carries most of the weight where we have it
    if deff.starter_ra9 is not None:
        sp_share = max(0.30, min(0.72, deff.starter_ip / 9.0))
        pen = deff.bullpen_ra9 if deff.bullpen_ra9 is not None else lg_rpg
        def_rate = deff.starter_ra9 * sp_share + pen * (1 - sp_share)
    else:
        elo_def = lg_rpg * (1.0 - (deff.rating - 1500) / 1200.0)
        def_rate = _blend((deff.ra_pg, min(deff.games_sampled, 25)),
                          (elo_def, 12.0)) or lg_rpg

    # log5-style combination against league average
    mu = (off_rate * def_rate) / lg_rpg
    mu *= park * weather

    if is_home:
        mu += lg.hfa_runs / 2.0
    else:
        mu -= lg.hfa_runs / 2.0

    # short rest on a starter leaks runs
    if deff.rest_days is not None and deff.rest_days <= 3 and deff.starter_ra9:
        mu *= 1.03

    return float(max(1.2, min(mu, lg_rpg * 2.6)))


def _nb_sample(mu: float, dispersion: float, n: int, rng) -> np.ndarray:
    """Negative binomial run totals. Variance = mu * (1 + dispersion)."""
    if mu <= 0:
        return np.zeros(n, dtype=int)
    var = mu * (1.0 + dispersion)
    if var <= mu:
        return rng.poisson(mu, n)
    p = mu / var
    r = mu * p / (1 - p)
    return rng.negative_binomial(max(r, 0.05), p, n)


def simulate(mu_away: float, mu_home: float, lg: LeagueConfig,
             sims: int = 40000, seed: Optional[int] = None) -> Dict[str, np.ndarray]:
    """
    Simulate a full game including extra innings and league tie rules.

    Regulation is simulated directly. If tied, extra innings are simulated as
    half-inning pairs until someone leads at the end of an inning, or until the
    league's innings cap forces a draw.
    """
    rng = np.random.default_rng(seed)
    a = _nb_sample(mu_away, lg.dispersion, sims, rng)
    h = _nb_sample(mu_home, lg.dispersion, sims, rng)

    tied = a == h
    if tied.any():
        # per-inning scoring rate; ghost runner inflates it materially
        per_inn_a = mu_away / 9.0 * (2.2 if lg.ghost_runner else 1.0)
        per_inn_h = mu_home / 9.0 * (2.2 if lg.ghost_runner else 1.0)
        cap = lg.extra_innings_limit
        max_extra = (cap - 9) if cap else 12

        idx = np.where(tied)[0]
        for _ in range(max_extra):
            if idx.size == 0:
                break
            ea = rng.poisson(per_inn_a, idx.size)
            eh = rng.poisson(per_inn_h, idx.size)
            a[idx] += ea
            h[idx] += eh
            still = a[idx] == h[idx]
            idx = idx[still]
        # whatever is still level: draw if allowed, else coin-flip the outcome
        if idx.size and not lg.allows_ties:
            flip = rng.random(idx.size) < 0.5
            a[idx[flip]] += 1
            h[idx[~flip]] += 1

    return {"away": a, "home": h, "total": a + h, "diff": h - a}


def market_probs(sim: Dict[str, np.ndarray], lg: LeagueConfig,
                 total_lines: List[float],
                 tt_lines: Dict[str, List[float]]) -> Dict[str, float]:
    """Every probability the engine can price, keyed 'MARKET|SIDE|HANDICAP'."""
    a, h, tot, diff = sim["away"], sim["home"], sim["total"], sim["diff"]
    n = len(a)
    p: Dict[str, float] = {}

    home_w = float((diff > 0).mean())
    away_w = float((diff < 0).mean())
    draw = float((diff == 0).mean())

    p["ML|HOME|None"] = home_w
    p["ML|AWAY|None"] = away_w
    if lg.allows_ties and draw > 0:
        p["ML|DRAW|None"] = draw

    # run lines
    p["RL|HOME|-1.5"] = float((diff >= 2).mean())
    p["RL|AWAY|1.5"] = float((diff <= 1).mean())
    p["RL|AWAY|-1.5"] = float((diff <= -2).mean())
    p["RL|HOME|1.5"] = float((diff >= -1).mean())

    # totals — push-aware (exclude exact ties from the denominator)
    for line in sorted(set(total_lines)):
        over = float((tot > line).sum())
        under = float((tot < line).sum())
        push = n - over - under
        denom = n - push
        if denom <= 0:
            continue
        p[f"TOTAL|OVER|{line}"] = over / denom
        p[f"TOTAL|UNDER|{line}"] = under / denom

    # team totals
    for market, arr in (("TT_HOME", h), ("TT_AWAY", a)):
        for line in sorted(set(tt_lines.get(market, []))):
            over = float((arr > line).sum())
            under = float((arr < line).sum())
            push = n - over - under
            denom = n - push
            if denom <= 0:
                continue
            p[f"{market}|OVER|{line}"] = over / denom
            p[f"{market}|UNDER|{line}"] = under / denom

    return p


def confidence(lg: LeagueConfig, away: TeamInput, home: TeamInput) -> float:
    """
    0..1 measure of how much the inputs deserve to be trusted.
    Directly multiplies into grading, so thin data cannot produce an A.
    """
    c = 1.0
    if not lg.has_pitcher_api:
        c *= 0.72                      # team-strength tier, not pitcher tier
    if away.starter_ra9 is None or home.starter_ra9 is None:
        c *= 0.90
    sample = min(away.games_sampled, home.games_sampled)
    if sample < 10:
        c *= 0.70
    elif sample < 20:
        c *= 0.88
    return round(max(0.35, c), 3)
