# bbx — multi-league baseball betting engine

MLB · NPB · KBO · LMB in one model.

## Why this doesn't spit out favourite +1.5 every day

The old engine ranked candidates by **win probability**. Favourite +1.5 has the
highest win probability on almost every baseball board, so it won every time.

This one ranks by **edge against the de-vigged fair price**:

1. Group each market's prices into a complete book (both sides, or all three
   for NPB/KBO moneylines).
2. Strip the bookmaker margin using a **power de-vig**, which solves for `k` in
   `sum(p_i^k) = 1`. Plain normalisation shaves too much off favourites — that
   bias is precisely what manufactures fake edge on chalk.
3. Bet only where `model_prob − fair_prob ≥ MIN_EDGE`.

A correctly priced favourite now lands at roughly zero edge, which is the truth.

Additional guards:

- `MIN_ODDS = 1.60` — nothing shorter is ever considered
- `MAX_ODDS = 8.00` — longshots are model noise, not signal
- Incomplete markets (one side only) are skipped; they can't be de-vigged
- Stakes are **quarter Kelly, hard capped at 2%** of bankroll
- Grades are discounted by a `confidence` score, so thin data cannot produce an A

## Layout

| file | job |
|---|---|
| `leagues.py` | per-league run environment, tie rules, park factors |
| `sources.py` | schedule / odds / results scrapers + Elo ratings |
| `model.py` | run projection, Monte Carlo, market probabilities |
| `markets.py` | de-vig, edge, Kelly, grading |
| `engine.py` | orchestration |
| `run.py` | daily entry point + offline self-test |

## Run it

```bash
pip install -r requirements.txt

python run.py --dry                    # offline self-test, no network
python run.py                          # fair price sheet, all four leagues
python run.py --leagues NPB,KBO
python run.py --date 2026-08-18 --sims 60000
python run.py --odds                   # ALSO pull odds and compute edges
```

Default mode uses **no odds feed at all**. It prints a fair price sheet per game:

```
  selection                            prob   fair   need   stake
  Yomiuri Giants ML                   60.7%   1.65   1.70   1.08%
```

- `prob` — model probability
- `fair` — break-even price, `1/prob`
- `need` — price required to clear a 3% edge, `1.03/prob`
- `stake` — quarter-Kelly stake if you get exactly `need`

**Workflow:** open Superbet, compare their price to `need`. Above it, bet.
Below it, skip. One number to check per selection.

Rows marked `*` need a price under the 1.60 floor — always skip those. That is
where favourite +1.5 lands, permanently.

Quick manual check once you have a real price:

```python
import sheet
sheet.check(0.607, 1.85)     # -> BET  prob 60.7%  price 1.85  EV +12.3%  stake 2.00%
```

Outputs:

- `sheet.csv` — every priced selection, rewritten each run
- `picks.csv` — only with `--odds`, appended forever
- `diagnostics.csv` — one row per game with a `reason` column
- `runs/<date>.json` — full projection snapshot

## Why there is no single "best pick" per game

Because without a price there cannot be one. Edge is model probability versus
market price. Strip out the price and the only ranking left is probability, and
the highest-probability selection on a baseball board is always the favourite's
+1.5 run line. Ranking by probability *is* the chalk generator — it was the
original bug. The sheet gives you every selection and its required price, and
your book decides which one is actually a bet.

## The two-tier data problem — read this

MLB has a free official API (`statsapi.mlb.com`) with probable pitchers and
full stats. **NPB, KBO and LMB have nothing equivalent that is public.**

So the engine runs two tiers:

- **Tier 1 (MLB):** starter ERA, bullpen, park, team form
- **Tier 2 (NPB/KBO/LMB):** team strength inferred from recent results via
  margin-aware Elo plus runs scored/allowed

Tier 2 is genuinely weaker. It cannot see that a team's ace is starting. The
engine handles this by setting `confidence = 0.72` for those leagues, which
shrinks grades and stakes. It does not pretend the two tiers are equivalent.

If you find a real NPB/KBO stats feed, add an adapter in `sources.py` and flip
`has_pitcher_api = True` in `leagues.py`.

## Sofascore blocks datacenter IPs

Odds and non-MLB schedules come from Sofascore's public JSON API. It rejects
most cloud IP ranges. Symptom: every game shows `odds_count = 0` and the run
prints a `BLOCKED` warning.

That is not fixable in code. Run from Google Colab or a home machine.

## Every constant here is a starting point

The run environments (`rpg`), park factors, dispersion values and Elo `K` are
reasonable estimates, **not measured facts**. Until you validate them against
settled results, treat the edge numbers as directional rather than exact — and
remember that staking scales with edge, so a miscalibrated constant becomes a
miscalibrated bet size.

Fill in the `result` and `closing_price` columns in `picks.csv` as games settle.
Closing line value is the only honest short-run measure of whether any of this
works; profit over a few dozen bets tells you almost nothing.

## Reality check

A ~4–5% margin on a two-way baseball market is a real hurdle. Slates where this
engine finds nothing are normal and correct. An engine that finds a bet every
day is broken — that was the last one.
