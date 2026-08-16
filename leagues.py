"""
leagues.py — everything that differs between baseball leagues.

The whole point of this file: one model, four leagues, and the league-specific
assumptions live in ONE place instead of being hardcoded across the engine.

Every number here is a TUNABLE STARTING POINT, not a fact. Run calibrate.py
against settled results and update them. The defaults are order-of-magnitude
correct but should not be trusted for staking until you have verified them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class LeagueConfig:
    code: str
    name: str

    # --- run environment ---------------------------------------------------
    rpg: float               # runs per team per game, league average
    hfa_runs: float          # home field advantage expressed in runs

    # --- rules that change the market structure ----------------------------
    allows_ties: bool        # NPB/KBO can end in a draw -> 3-way moneyline
    extra_innings_limit: Optional[int]   # innings cap before a draw is called
    ghost_runner: bool       # runner on 2nd in extras inflates extra-inning runs

    # --- simulation shape --------------------------------------------------
    # Negative binomial dispersion. Higher = more variance in team run totals.
    # Low-scoring leagues are relatively MORE dispersed per run scored.
    dispersion: float

    # --- data availability -------------------------------------------------
    has_pitcher_api: bool    # True only where a real stats API exists
    sofa_tokens: tuple       # substrings used to spot the league on Sofascore

    # --- park factors, 1.00 = neutral. Keyed by venue substring. -----------
    parks: Dict[str, float] = field(default_factory=dict)

    def park(self, venue: str) -> float:
        v = (venue or "").lower()
        for key, mult in self.parks.items():
            if key.lower() in v:
                return mult
        return 1.00


# ---------------------------------------------------------------------------
# MLB — the only league with a real free API (statsapi.mlb.com)
# ---------------------------------------------------------------------------
MLB = LeagueConfig(
    code="MLB",
    name="Major League Baseball",
    rpg=4.40,
    hfa_runs=0.18,
    allows_ties=False,
    extra_innings_limit=None,
    ghost_runner=True,
    dispersion=1.10,
    has_pitcher_api=True,
    sofa_tokens=("mlb", "major league baseball"),
    parks={
        "coors": 1.28, "great american": 1.10, "fenway": 1.07,
        "globe life": 1.06, "chase field": 1.04, "yankee": 1.03,
        "wrigley": 1.01, "citi field": 0.96, "petco": 0.95,
        "oracle": 0.93, "t-mobile": 0.92, "loandepot": 0.91,
    },
)

# ---------------------------------------------------------------------------
# NPB — lower run environment, draws after 12 innings, no public stats API
# ---------------------------------------------------------------------------
NPB = LeagueConfig(
    code="NPB",
    name="Nippon Professional Baseball",
    rpg=3.90,
    hfa_runs=0.20,
    allows_ties=True,
    extra_innings_limit=12,
    ghost_runner=False,
    dispersion=1.18,
    has_pitcher_api=False,
    sofa_tokens=("npb", "nippon", "japan baseball"),
    parks={
        "koshien": 0.92, "tokyo dome": 1.08, "meiji jingu": 1.10,
        "yokohama": 1.04, "mazda": 0.96, "kyocera": 0.97,
        "sapporo": 0.95, "escon": 1.00, "zozo": 0.94, "rakuten": 1.02,
    },
)

# ---------------------------------------------------------------------------
# KBO — high offence, draws after 12 innings
# ---------------------------------------------------------------------------
KBO = LeagueConfig(
    code="KBO",
    name="Korea Baseball Organization",
    rpg=5.00,
    hfa_runs=0.22,
    allows_ties=True,
    extra_innings_limit=12,
    ghost_runner=False,
    dispersion=1.05,
    has_pitcher_api=False,
    sofa_tokens=("kbo", "korea baseball", "korean baseball"),
    parks={
        "jamsil": 0.93, "gocheok": 0.95, "incheon": 1.08,
        "daejeon": 1.06, "changwon": 1.05, "suwon": 1.04,
        "daegu": 1.03, "gwangju": 1.01, "sajik": 0.99,
    },
)

# ---------------------------------------------------------------------------
# LMB — extreme offence, altitude, no ties
# ---------------------------------------------------------------------------
LMB = LeagueConfig(
    code="LMB",
    name="Liga Mexicana de Beisbol",
    rpg=5.80,
    hfa_runs=0.25,
    allows_ties=False,
    extra_innings_limit=None,
    ghost_runner=True,
    dispersion=0.95,
    has_pitcher_api=False,
    sofa_tokens=("lmb", "liga mexicana", "mexican league"),
    parks={
        "harp helu": 1.12, "monterrey": 1.02, "puebla": 1.15,
        "saltillo": 1.18, "aguascalientes": 1.16, "leon": 1.14,
        "yucatan": 0.97, "kukulcan": 0.97, "tijuana": 1.03,
        "oaxaca": 1.13, "laguna": 1.08, "quintana roo": 0.98,
    },
)

ALL: Dict[str, LeagueConfig] = {c.code: c for c in (MLB, NPB, KBO, LMB)}


def get(code: str) -> LeagueConfig:
    code = code.upper()
    if code not in ALL:
        raise KeyError(f"unknown league {code!r}; have {list(ALL)}")
    return ALL[code]
