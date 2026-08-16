"""
sources.py — scrapers / adapters.

Three jobs, cleanly separated so a failure in one does not silently corrupt
the others:

  1. schedule  — who plays whom, where, when
  2. odds      — the price board
  3. history   — settled results, used to build team ratings

Sofascore is the only free source that covers MLB + NPB + KBO + LMB in one
shape, so it is the universal adapter. MLB additionally gets the official
StatsAPI, which is far richer and never rate-limits.

IMPORTANT: Sofascore blocks most datacenter IP ranges. If you run this from a
cloud CI runner you will likely get 403s on every request. That is not a bug in
this code — check diagnostics output and move to a residential-IP environment
(Colab, a home machine) if it happens.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Dict, List, Optional, Tuple

import requests

from leagues import LeagueConfig
from markets import Odds

SOFA = "https://api.sofascore.com/api/v1"
MLB_API = "https://statsapi.mlb.com/api/v1"

HDRS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
}


class FetchStats:
    """Tracks what actually came back, so diagnostics can tell you the truth."""

    def __init__(self):
        self.ok = 0
        self.blocked = 0
        self.failed = 0
        self.codes: Dict[int, int] = {}

    def note(self, code: Optional[int]):
        if code is None:
            self.failed += 1
        elif code == 200:
            self.ok += 1
        else:
            self.codes[code] = self.codes.get(code, 0) + 1
            if code in (403, 429, 401):
                self.blocked += 1
            else:
                self.failed += 1

    @property
    def looks_blocked(self) -> bool:
        return self.blocked > 0 and self.ok == 0

    def summary(self) -> str:
        bits = [f"ok={self.ok}", f"blocked={self.blocked}", f"failed={self.failed}"]
        if self.codes:
            bits.append("codes=" + ",".join(f"{k}x{v}" for k, v in self.codes.items()))
        return " ".join(bits)


STATS = FetchStats()


def _get(url: str, params: Optional[dict] = None, tries: int = 3,
         pause: float = 0.7) -> Optional[dict]:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HDRS, params=params, timeout=20)
            last = r.status_code
            if r.status_code == 200:
                STATS.note(200)
                return r.json()
            if r.status_code in (403, 429):
                time.sleep(pause * (attempt + 1) * 2)
                continue
            break
        except Exception:
            last = None
            time.sleep(pause)
    STATS.note(last)
    return None


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _frac_to_dec(choice: dict) -> Optional[float]:
    frac = choice.get("fractionalValue")
    if frac and "/" in str(frac):
        try:
            n, _, d = str(frac).partition("/")
            return round(int(n) / int(d) + 1, 3)
        except Exception:
            pass
    for k in ("decimalValue", "value"):
        try:
            v = float(choice.get(k))
            if v > 1.0:
                return round(v, 3)
        except Exception:
            continue
    return None


def _side(name: str) -> Optional[str]:
    n = (name or "").strip().lower()
    if n in ("1", "home", "w1"):
        return "HOME"
    if n in ("2", "away", "w2"):
        return "AWAY"
    if n in ("x", "draw", "tie"):
        return "DRAW"
    if n.startswith("over") or n == "o":
        return "OVER"
    if n.startswith("under") or n == "u":
        return "UNDER"
    return None


def _to_float(v) -> Optional[float]:
    try:
        return float(str(v).replace("+", "").strip())
    except Exception:
        return None


# ===========================================================================
# SCHEDULE
# ===========================================================================

def sofa_schedule(lg: LeagueConfig, date: str) -> List[dict]:
    """Fixtures for one league on one date, normalised."""
    js = _get(f"{SOFA}/sport/baseball/scheduled-events/{date}")
    if not js:
        return []
    out = []
    for ev in js.get("events", []):
        t = ev.get("tournament") or {}
        blob = _norm(t.get("name", "")) + _norm((t.get("uniqueTournament") or {}).get("name", ""))
        if not any(_norm(tok) in blob for tok in lg.sofa_tokens):
            continue
        out.append({
            "event_id": ev.get("id"),
            "away": (ev.get("awayTeam") or {}).get("name", ""),
            "home": (ev.get("homeTeam") or {}).get("name", ""),
            "venue": ((ev.get("venue") or {}).get("stadium") or {}).get("name", ""),
            "start_ts": ev.get("startTimestamp"),
            "status": ((ev.get("status") or {}).get("type") or ""),
        })
    return out


def mlb_schedule(date: str) -> List[dict]:
    js = _get(f"{MLB_API}/schedule",
              {"sportId": 1, "date": date,
               "hydrate": "probablePitcher,venue,team,linescore"})
    if not js:
        return []
    out = []
    for d in js.get("dates", []):
        for g in d.get("games", []):
            if (g.get("status") or {}).get("abstractGameState") == "Final":
                continue
            out.append({
                "game_pk": g.get("gamePk"),
                "away": g["teams"]["away"]["team"]["name"],
                "home": g["teams"]["home"]["team"]["name"],
                "away_id": g["teams"]["away"]["team"]["id"],
                "home_id": g["teams"]["home"]["team"]["id"],
                "venue": (g.get("venue") or {}).get("name", ""),
                "away_sp": (g["teams"]["away"].get("probablePitcher") or {}).get("id"),
                "home_sp": (g["teams"]["home"].get("probablePitcher") or {}).get("id"),
                "start_ts": g.get("gameDate"),
            })
    return out


# ===========================================================================
# ODDS
# ===========================================================================

def sofa_odds(event_id: int) -> Tuple[List[Odds], Optional[str]]:
    js = _get(f"{SOFA}/event/{event_id}/odds/1/all")
    if not js:
        return [], None
    book = js.get("providerName") or (js.get("provider") or {}).get("name")
    src = f"sofascore/{book}" if book else "sofascore"
    out: List[Odds] = []

    for m in js.get("markets", []):
        raw = (m.get("marketName") or "").strip().lower()
        # skip derivative markets we do not model
        if any(t in raw for t in ("inning", "1st", "first 5", "period",
                                  "player", "strikeout", "hits", "asian")):
            continue
        group = _to_float(m.get("choiceGroup"))

        for ch in m.get("choices", []):
            price = _frac_to_dec(ch)
            side = _side(ch.get("name", ""))
            if not price or not side:
                continue

            if "winner" in raw or raw in ("1x2", "match winner", "full time"):
                if side in ("HOME", "AWAY", "DRAW"):
                    out.append(Odds("ML", side, None, price, src))

            elif "handicap" in raw or "spread" in raw or "run line" in raw:
                h = group if group is not None else _to_float(
                    (ch.get("name") or "").split()[-1])
                if h is not None and side in ("HOME", "AWAY"):
                    # store the handicap from that side's perspective
                    signed = h if side == "HOME" else -h
                    out.append(Odds("RL", side, signed, price, src))

            elif "total" in raw or "over/under" in raw:
                if group is not None and side in ("OVER", "UNDER"):
                    out.append(Odds("TOTAL", side, group, price, src))

    # dedupe, keep best price per selection
    best: Dict[tuple, Odds] = {}
    for o in out:
        cur = best.get(o.key)
        if cur is None or o.price > cur.price:
            best[o.key] = o
    return list(best.values()), book


# ===========================================================================
# HISTORY -> ratings
# ===========================================================================

def sofa_results(lg: LeagueConfig, days_back: int = 45,
                 end: Optional[str] = None) -> List[dict]:
    """Settled games, used to fit Elo and recent run rates."""
    end_d = dt.date.fromisoformat(end) if end else dt.date.today()
    games = []
    for i in range(1, days_back + 1):
        d = (end_d - dt.timedelta(days=i)).isoformat()
        js = _get(f"{SOFA}/sport/baseball/scheduled-events/{d}")
        if not js:
            continue
        for ev in js.get("events", []):
            t = ev.get("tournament") or {}
            blob = _norm(t.get("name", "")) + _norm((t.get("uniqueTournament") or {}).get("name", ""))
            if not any(_norm(tok) in blob for tok in lg.sofa_tokens):
                continue
            st = ((ev.get("status") or {}).get("type") or "")
            if st != "finished":
                continue
            hs = ((ev.get("homeScore") or {}).get("current"))
            as_ = ((ev.get("awayScore") or {}).get("current"))
            if hs is None or as_ is None:
                continue
            games.append({
                "date": d,
                "home": (ev.get("homeTeam") or {}).get("name", ""),
                "away": (ev.get("awayTeam") or {}).get("name", ""),
                "home_runs": int(hs), "away_runs": int(as_),
            })
        time.sleep(0.25)
    return games


def mlb_results(days_back: int = 45, end: Optional[str] = None) -> List[dict]:
    end_d = dt.date.fromisoformat(end) if end else dt.date.today()
    start = (end_d - dt.timedelta(days=days_back)).isoformat()
    js = _get(f"{MLB_API}/schedule",
              {"sportId": 1, "startDate": start,
               "endDate": (end_d - dt.timedelta(days=1)).isoformat(),
               "hydrate": "linescore,team"})
    out = []
    for d in (js or {}).get("dates", []):
        for g in d.get("games", []):
            if (g.get("status") or {}).get("abstractGameState") != "Final":
                continue
            try:
                out.append({
                    "date": d.get("date"),
                    "home": g["teams"]["home"]["team"]["name"],
                    "away": g["teams"]["away"]["team"]["name"],
                    "home_runs": int(g["teams"]["home"]["score"]),
                    "away_runs": int(g["teams"]["away"]["score"]),
                })
            except Exception:
                continue
    return out


def mlb_pitcher(pid: int, season: int) -> Optional[dict]:
    if not pid:
        return None
    js = _get(f"{MLB_API}/people/{pid}",
              {"hydrate": f"stats(group=[pitching],type=[season],season={season})"})
    if not js or not js.get("people"):
        return None
    p = js["people"][0]
    for s in p.get("stats", []):
        for sp in s.get("splits", []):
            st = sp.get("stat", {})
            ip = _to_float(st.get("inningsPitched")) or 0.0
            er = _to_float(st.get("earnedRuns")) or 0.0
            if ip >= 10:
                return {
                    "name": p.get("fullName"),
                    "era": round(er * 9 / ip, 3),
                    "ip": ip,
                    "gs": st.get("gamesStarted") or 0,
                }
    return {"name": p.get("fullName"), "era": None, "ip": 0, "gs": 0}


# ===========================================================================
# ELO
# ===========================================================================

def build_ratings(results: List[dict], lg: LeagueConfig,
                  k: float = 6.0, base: float = 1500.0) -> Dict[str, dict]:
    """
    Margin-aware Elo plus recent run rates. Baseball has low signal per game,
    so K is deliberately small — a big K here just fits noise.
    """
    R: Dict[str, float] = {}
    rs: Dict[str, List[int]] = {}
    ra: Dict[str, List[int]] = {}

    for g in sorted(results, key=lambda x: x.get("date") or ""):
        h, a = g["home"], g["away"]
        R.setdefault(h, base)
        R.setdefault(a, base)
        hr, ar = g["home_runs"], g["away_runs"]

        exp_h = 1.0 / (1.0 + 10 ** (-((R[h] + 25) - R[a]) / 400.0))
        if hr > ar:
            act_h = 1.0
        elif hr < ar:
            act_h = 0.0
        else:
            act_h = 0.5

        margin = abs(hr - ar)
        mult = 1.0 + 0.30 * min(margin, 6) / 6.0
        delta = k * mult * (act_h - exp_h)
        R[h] += delta
        R[a] -= delta

        rs.setdefault(h, []).append(hr); ra.setdefault(h, []).append(ar)
        rs.setdefault(a, []).append(ar); ra.setdefault(a, []).append(hr)

    out = {}
    for team in R:
        s, d = rs.get(team, []), ra.get(team, [])
        recent_s, recent_d = s[-25:], d[-25:]
        out[team] = {
            "rating": round(R[team], 1),
            "rs_pg": round(sum(recent_s) / len(recent_s), 3) if recent_s else None,
            "ra_pg": round(sum(recent_d) / len(recent_d), 3) if recent_d else None,
            "games": len(s),
        }
    return out


def match_team(name: str, table: Dict[str, dict]) -> Optional[dict]:
    """Fuzzy-ish team lookup across naming differences between feeds."""
    if name in table:
        return table[name]
    n = _norm(name)
    for k, v in table.items():
        if _norm(k) == n:
            return v
    for k, v in table.items():
        kn = _norm(k)
        if n and (n in kn or kn in n):
            return v
    tail = _norm(name.split()[-1]) if name.split() else ""
    if tail:
        for k, v in table.items():
            if tail and tail in _norm(k):
                return v
    return None
