"""
engine.py — orchestration. One league, one date, one slate of picks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Dict, List, Optional

import leagues
import markets as mk
import model as md
import sheet
import sources as src
from markets import Odds, Pick

SEASON = dt.date.today().year


def _labels(away: str, home: str, probs: Dict[str, float]) -> Dict[str, str]:
    out = {}
    for key in probs:
        market, side, hcap = key.split("|")
        if market == "ML":
            out[key] = {"HOME": f"{home} ML", "AWAY": f"{away} ML",
                        "DRAW": "Draw"}.get(side, key)
        elif market == "RL":
            team = home if side == "HOME" else away
            h = float(hcap)
            out[key] = f"{team} {h:+.1f}"
        elif market == "TOTAL":
            out[key] = f"{side.title()} {hcap}"
        elif market == "TT_HOME":
            out[key] = f"{home} TT {side.title()} {hcap}"
        elif market == "TT_AWAY":
            out[key] = f"{away} TT {side.title()} {hcap}"
        else:
            out[key] = key
    return out


def _lines_from_odds(odds: List[Odds]):
    totals = sorted({o.handicap for o in odds
                     if o.market == "TOTAL" and o.handicap is not None})
    tts = {
        "TT_HOME": sorted({o.handicap for o in odds if o.market == "TT_HOME"}),
        "TT_AWAY": sorted({o.handicap for o in odds if o.market == "TT_AWAY"}),
    }
    return totals, tts


def _synthetic_lines(mu_a: float, mu_h: float):
    """
    With no odds feed we still need lines to price. Generate the band a book
    would actually hang: the projected total rounded to the nearest half run,
    plus one line either side, and team totals around each side's projection.
    """
    def half(x):
        return round(x * 2) / 2

    base = half(mu_a + mu_h)
    totals = sorted({base - 1.0, base - 0.5, base, base + 0.5, base + 1.0})
    tts = {
        "TT_HOME": sorted({half(mu_h) - 0.5, half(mu_h), half(mu_h) + 0.5}),
        "TT_AWAY": sorted({half(mu_a) - 0.5, half(mu_a), half(mu_a) + 0.5}),
    }
    return [t for t in totals if t > 4], {
        k: [x for x in v if x > 1.5] for k, v in tts.items()}


def analyse_game(lg, game: dict, odds: List[Odds], ratings: Dict[str, dict],
                 sims: int, pitchers: Optional[dict] = None) -> dict:
    away, home = game["away"], game["home"]
    ra = src.match_team(away, ratings) or {}
    rh = src.match_team(home, ratings) or {}

    pitchers = pitchers or {}
    a_in = md.TeamInput(
        name=away, rating=ra.get("rating", 1500.0),
        rs_pg=ra.get("rs_pg"), ra_pg=ra.get("ra_pg"),
        games_sampled=ra.get("games", 0),
        starter_ra9=(pitchers.get("away") or {}).get("era"),
    )
    h_in = md.TeamInput(
        name=home, rating=rh.get("rating", 1500.0),
        rs_pg=rh.get("rs_pg"), ra_pg=rh.get("ra_pg"),
        games_sampled=rh.get("games", 0),
        starter_ra9=(pitchers.get("home") or {}).get("era"),
    )

    park = lg.park(game.get("venue", ""))
    mu_a = md.project_runs(a_in, h_in, lg, park, is_home=False)
    mu_h = md.project_runs(h_in, a_in, lg, park, is_home=True)

    sim = md.simulate(mu_a, mu_h, lg, sims=sims)
    if odds:
        totals, tts = _lines_from_odds(odds)
    else:
        totals, tts = _synthetic_lines(mu_a, mu_h)
    probs = md.market_probs(sim, lg, totals, tts)
    conf = md.confidence(lg, a_in, h_in)

    gname = f"{away} @ {home}"
    labels = _labels(away, home, probs)
    picks = mk.evaluate(lg.code, gname, probs, odds, conf, labels)
    price_sheet = sheet.build_sheet(lg.code, gname, probs, labels, conf)

    return {
        "league": lg.code, "game": gname, "away": away, "home": home,
        "venue": game.get("venue", ""), "park": park,
        "mu_away": round(mu_a, 3), "mu_home": round(mu_h, 3),
        "proj_total": round(mu_a + mu_h, 3),
        "confidence": conf, "odds_count": len(odds),
        "ratings_found": bool(ra) and bool(rh),
        "sample_games": min(a_in.games_sampled, h_in.games_sampled),
        "picks": picks,
        "sheet": price_sheet,
        "probs": probs,
    }


def run_league(code: str, date: Optional[str] = None, sims: int = 40000,
               history_days: int = 45, max_games: Optional[int] = None,
               verbose: bool = True, use_odds: bool = False) -> dict:
    lg = leagues.get(code)
    date = date or dt.date.today().isoformat()

    if verbose:
        print(f"\n{'='*72}\n{lg.name} — {date}\n{'='*72}")

    # ---- schedule ---------------------------------------------------------
    if lg.code == "MLB":
        games = src.mlb_schedule(date)
        if not games:
            games = src.sofa_schedule(lg, date)
    else:
        games = src.sofa_schedule(lg, date)

    if verbose:
        print(f"fixtures: {len(games)}")
    if not games:
        return {"league": lg.code, "date": date, "games": [],
                "fetch": src.STATS.summary(), "blocked": src.STATS.looks_blocked}

    # ---- ratings from history --------------------------------------------
    hist = (src.mlb_results(history_days, date) if lg.code == "MLB"
            else src.sofa_results(lg, history_days, date))
    ratings = src.build_ratings(hist, lg)
    if verbose:
        print(f"history: {len(hist)} settled games -> {len(ratings)} teams rated")

    # ---- per game ---------------------------------------------------------
    out = []
    for g in games[:max_games]:
        ev_id = g.get("event_id")
        odds: List[Odds] = []
        if not use_odds:
            pass
        elif ev_id:
            odds, _ = src.sofa_odds(ev_id)
        elif lg.code == "MLB":
            sofa_games = src.sofa_schedule(lg, date)
            m = next((s for s in sofa_games
                      if src._norm(s["home"])[:6] in src._norm(g["home"])
                      or src._norm(g["home"])[:6] in src._norm(s["home"])), None)
            if m:
                odds, _ = src.sofa_odds(m["event_id"])

        pitchers = None
        if lg.has_pitcher_api and g.get("away_sp"):
            pitchers = {
                "away": src.mlb_pitcher(g.get("away_sp"), SEASON),
                "home": src.mlb_pitcher(g.get("home_sp"), SEASON),
            }

        r = analyse_game(lg, g, odds, ratings, sims, pitchers)
        out.append(r)
        if verbose:
            if use_odds:
                print(render_game(r))
            else:
                hdr = (f"proj {r['mu_away']:.2f}-{r['mu_home']:.2f}"
                       f" (tot {r['proj_total']:.2f}) | park {r['park']:.2f}"
                       f" | conf {r['confidence']:.2f}")
                print(sheet.render(r["sheet"], r["game"], hdr))

    return {"league": lg.code, "date": date, "games": out,
            "fetch": src.STATS.summary(), "blocked": src.STATS.looks_blocked}


def render_game(r: dict) -> str:
    head = (f"\n{r['game']}"
            f"\n  proj {r['mu_away']:.2f} - {r['mu_home']:.2f}"
            f" (tot {r['proj_total']:.2f}) | park {r['park']:.2f}"
            f" | conf {r['confidence']:.2f} | {r['odds_count']} prices")
    if not r["odds_count"]:
        return head + "\n  NO ODDS — nothing to evaluate"
    if not r["picks"]:
        return head + "\n  no selection cleared the edge floor"
    lines = [head]
    for p in r["picks"][:3]:
        lines.append(
            f"  {p.grade:<3} {p.label:<26} {p.price:>5.2f}"
            f"  model {100*p.model_prob:5.1f}%  fair {100*p.fair_prob:5.1f}%"
            f"  edge {100*p.edge_vs_fair:+5.1f}%  stake {p.kelly_pct:.2f}%")
    return "\n".join(lines)


def run_all(codes: List[str], date: Optional[str] = None, **kw) -> List[dict]:
    return [run_league(c, date, **kw) for c in codes]
