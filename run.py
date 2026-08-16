"""
run.py — daily entry point.

    python run.py                      # all four leagues, today
    python run.py --leagues MLB,NPB    # subset
    python run.py --date 2026-08-18
    python run.py --dry                # synthetic data, no network, proves logic

Writes:
    picks.csv        every qualifying selection, appended forever
    diagnostics.csv  one row per game INCLUDING rejections, so you can see why
    runs/<date>.json full projections snapshot
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
from dataclasses import asdict

PICKS = pathlib.Path("picks.csv")
SHEET = pathlib.Path("sheet.csv")
DIAG = pathlib.Path("diagnostics.csv")
RUNS = pathlib.Path("runs")

PICK_FIELDS = ["run_ts", "date", "league", "game", "grade", "label", "market",
               "side", "handicap", "price", "model_prob", "fair_prob",
               "edge_vs_fair", "kelly_pct", "source", "result", "closing_price",
               "pnl"]

DIAG_FIELDS = ["run_ts", "date", "league", "game", "odds_count", "confidence",
               "proj_total", "ratings_found", "sample_games", "picks_found",
               "best_label", "best_price", "best_edge", "reason"]

SHEET_FIELDS = ["run_ts", "date", "league", "game", "market", "label", "prob",
                "fair", "need", "max_stake_pct", "confidence", "note"]


def _append(path: pathlib.Path, fields, rows):
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def _reason(g: dict) -> str:
    if g["odds_count"] == 0:
        return "NO ODDS — feed failure or market not open"
    if not g["ratings_found"]:
        return "NO RATINGS — team not matched in history"
    if g["sample_games"] < 8:
        return f"THIN SAMPLE — {g['sample_games']} games"
    if not g["picks"]:
        return "NO EDGE — priced efficiently"
    return "OK"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="MLB,NPB,KBO,LMB")
    ap.add_argument("--date", default=None)
    ap.add_argument("--sims", type=int, default=40000)
    ap.add_argument("--history", type=int, default=45)
    ap.add_argument("--max-games", type=int, default=None)
    ap.add_argument("--dry", action="store_true",
                    help="offline self-test with synthetic data")
    ap.add_argument("--odds", action="store_true",
                    help="also pull the odds feed and compute edges "
                         "(off by default; the feed is unreliable)")
    ap.add_argument("--per-market", type=int, default=3,
                    help="how many selections to print per market group")
    a = ap.parse_args(argv)

    if a.dry:
        return dry_run()

    import engine

    date = a.date or dt.date.today().isoformat()
    codes = [c.strip().upper() for c in a.leagues.split(",") if c.strip()]
    run_ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    pick_rows, diag_rows, sheet_rows, snapshot = [], [], [], {}
    blocked_leagues = []

    for code in codes:
        try:
            res = engine.run_league(code, date, sims=a.sims,
                                    history_days=a.history,
                                    max_games=a.max_games,
                                    use_odds=a.odds)
        except Exception as e:
            print(f"[{code}] FAILED :: {type(e).__name__}: {e}")
            continue

        if res.get("blocked"):
            blocked_leagues.append(code)

        snapshot[code] = {
            "fetch": res.get("fetch"),
            "games": [{k: v for k, v in g.items() if k != "picks"}
                      for g in res["games"]],
        }

        for g in res["games"]:
            best = g["picks"][0] if g["picks"] else None
            diag_rows.append({
                "run_ts": run_ts, "date": date, "league": code,
                "game": g["game"], "odds_count": g["odds_count"],
                "confidence": g["confidence"], "proj_total": g["proj_total"],
                "ratings_found": g["ratings_found"],
                "sample_games": g["sample_games"],
                "picks_found": len(g["picks"]),
                "best_label": best.label if best else "",
                "best_price": best.price if best else "",
                "best_edge": round(100 * best.edge_vs_fair, 2) if best else "",
                "reason": _reason(g),
            })
            for p in g["picks"]:
                d = asdict(p)
                d.update({"run_ts": run_ts, "date": date,
                          "result": "", "closing_price": "", "pnl": ""})
                pick_rows.append(d)

            for r in g.get("sheet", []):
                d = asdict(r)
                d.update({"run_ts": run_ts, "date": date})
                sheet_rows.append(d)

    if pick_rows:
        _append(PICKS, PICK_FIELDS, pick_rows)
    _append(DIAG, DIAG_FIELDS, diag_rows)
    if sheet_rows:
        with SHEET.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=SHEET_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in sheet_rows:
                w.writerow(r)

    RUNS.mkdir(exist_ok=True)
    (RUNS / f"{date}.json").write_text(
        json.dumps(snapshot, indent=1, default=str), encoding="utf-8")

    print(f"\n{'='*72}")
    print(f"{len(sheet_rows)} priced selection(s) -> {SHEET}")
    if a.odds:
        print(f"{len(pick_rows)} pick(s) across {len(codes)} league(s) -> {PICKS}")
    print(f"{len(diag_rows)} game(s) logged -> {DIAG}")
    if a.odds:
        no_odds = sum(1 for d in diag_rows if d["odds_count"] == 0)
        if no_odds:
            print(f"WARNING: {no_odds}/{len(diag_rows)} games had ZERO prices.")
    if a.odds and blocked_leagues:
        print(f"WARNING: requests look BLOCKED for {', '.join(blocked_leagues)}."
              "\n  Datacenter IPs are usually the cause. Run from Colab or a"
              " home machine instead.")
    return 0


# ---------------------------------------------------------------------------
# offline self-test — proves the maths without touching the network
# ---------------------------------------------------------------------------

def dry_run() -> int:
    import leagues
    import markets as mk
    import model as md
    from markets import Odds

    print("DRY RUN — synthetic data, no network\n" + "=" * 72)

    for code in ("MLB", "NPB", "KBO", "LMB"):
        lg = leagues.get(code)
        away = md.TeamInput("Away", 1480, rs_pg=lg.rpg * 0.95,
                            ra_pg=lg.rpg * 1.05, games_sampled=30)
        home = md.TeamInput("Home", 1545, rs_pg=lg.rpg * 1.10,
                            ra_pg=lg.rpg * 0.92, games_sampled=30)
        mu_a = md.project_runs(away, home, lg, 1.0, False)
        mu_h = md.project_runs(home, away, lg, 1.0, True)
        sim = md.simulate(mu_a, mu_h, lg, sims=40000, seed=7)
        line = round((mu_a + mu_h) * 2) / 2
        probs = md.market_probs(sim, lg, [line], {})

        draw = probs.get("ML|DRAW|None", 0.0)
        print(f"\n{code}: proj {mu_a:.2f}-{mu_h:.2f}  "
              f"home ML {100*probs['ML|HOME|None']:.1f}%  "
              f"draw {100*draw:.1f}%  "
              f"home -1.5 {100*probs['RL|HOME|-1.5']:.1f}%  "
              f"home +1.5 {100*probs['RL|HOME|1.5']:.1f}%")
        assert lg.allows_ties or draw == 0.0, f"{code} produced draws illegally"

        # price the board efficiently (5% margin) -> engine must find NO edge
        fair_h = probs["ML|HOME|None"]
        fair_a = probs["ML|AWAY|None"]
        tot = fair_h + fair_a + draw
        marg = 1.05
        odds = [
            Odds("ML", "HOME", None, round(tot / (fair_h * marg), 3), "test"),
            Odds("ML", "AWAY", None, round(tot / (fair_a * marg), 3), "test"),
            Odds("RL", "HOME", -1.5,
                 round(1 / (probs["RL|HOME|-1.5"] * marg), 3), "test"),
            Odds("RL", "AWAY", 1.5,
                 round(1 / (probs["RL|AWAY|1.5"] * marg), 3), "test"),
        ]
        if lg.allows_ties and draw > 0:
            odds.append(Odds("ML", "DRAW", None,
                             round(tot / (draw * marg), 3), "test"))

        picks = mk.evaluate(code, "Away @ Home", probs, odds, 1.0, {})
        chalk = [p for p in picks if p.market == "RL" and p.handicap == 1.5]
        print(f"   efficiently priced -> {len(picks)} pick(s)"
              f" | chalk +1.5 selections: {len(chalk)}")
        assert not chalk, f"{code}: chalk +1.5 leaked through on a fair book"

        # now mispriced: give the away side a real overlay
        odds2 = list(odds)
        odds2[1] = Odds("ML", "AWAY", None,
                        round(1 / max(fair_a - 0.09, 0.02), 3), "test")
        picks2 = mk.evaluate(code, "Away @ Home", probs, odds2, 1.0,
                             {"ML|AWAY|None": "Away ML"})
        if picks2:
            p = picks2[0]
            print(f"   overlay injected -> {p.grade} {p.label} @ {p.price}"
                  f"  edge {100*p.edge_vs_fair:+.1f}%  stake {p.kelly_pct:.2f}%")
        else:
            print("   overlay injected -> nothing (unexpected)")

    print("\n" + "=" * 72)
    print("all assertions passed: no illegal draws, no chalk on a fair book")
    return 0


if __name__ == "__main__":
    sys.exit(main())
