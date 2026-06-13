# XAU-U10 Trading Agent — Project Context

## What this is
A 3m XAUUSD (OANDA) Pine Script engine (`XAU-U10`, indicator ID `b5wwFe` on the
connected chart) feeding a FastAPI agent on Railway. The agent journals every
signal to Postgres, asks Claude for a TAKE/REDUCE/SKIP verdict with live-stats
context, and pushes the verdict to Telegram. **Currently in observation mode —
no live trading on verdicts yet.**

## Live ground truth (read before touching Pine logic)
The deployed agent exposes JSON/CSV endpoints with the real trade journal.
The base URL and secret token are in `.env` (gitignored) as `AGENT_BASE_URL`
and `AGENT_TOKEN` — read them from there, never hardcode or print them.

- `GET {AGENT_BASE_URL}/stats?secret={AGENT_TOKEN}` — live aggregates: win
  rate / net R by side, setup, grade, regime+side bucket, HTF alignment, and
  Claude's own past verdict performance.
- `GET {AGENT_BASE_URL}/trades.csv?secret={AGENT_TOKEN}` — full row-level
  journal export.

**Before proposing any Pine change, pull `/stats` fresh.** The numbers below
are a snapshot from 12 Jun 2026 (53 closed trades, merged history from both
engine generations) — treat them as a starting hypothesis, not current truth.

## Established findings (as of 12 Jun 2026, 53 trades)
- **REVERSAL setups: 0/6 lifetime, -6.0R.** The single strongest, most
  consistent finding. Every gate-passing REVERSAL has lost.
- Raw engine is roughly breakeven before costs (+0.23R / 53 trades over the
  backtest month, PF ~0.81-0.85) — any edge has to come from filtering, not
  the signal generation itself.
- LONG -6.5R vs SHORT +6.73R over the period — but this tracks a real
  market regime (a multi-day decline then a sharp rally) more than a
  structural long/short bias. Don't treat this as a standing rule; check
  whether it persists as more trades accumulate.
- Plain-A grade is the *worst* live grade bucket (-2.97R/35); A+ and B are
  mildly positive on small samples. The engine's grading isn't separating
  signal quality well live.
- NEUTRAL-regime trades are the worst regime bucket (-2.0R/7) — losses
  cluster on ambiguous regime reads, not on counter-trend entries.

## Already tested and decided — don't re-litigate without new evidence
Two Pine inputs were added and A/B tested in Strategy Tester over a pinned
month (2026-05-12 to 2026-06-12, Fixed R TP mode, all 4 regimes enabled,
Backtest Min Grade = A):

1. **`Reversals: Allow Wide SL (RR-gated)`** (RISK group) — REVERSAL setups
   at quality >= `Reversals: Min Quality % for Wide SL` (default 88) use
   `Reversals: Max SL ATR Override` (default 3.2) instead of the standard
   Max SL ATR cap, still gated on Minimum RR. **Result: 107->108 trades,
   net -3.01->-2.38 USD, PF 0.81->0.85, drawdown unchanged. Net positive,
   currently ENABLED on the live script.**
2. **HTF Alignment `Counter-Bias Filter`** (Hard block vs Off) — **Result:
   zero trades changed, byte-identical output to baseline.** The engine
   never fires counter to its own Bias/Flow/Momentum context, so this
   filter is provably inert. **Currently set to Off — leave it Off** unless
   new live data (the `alignment` block in `/stats`) shows otherwise.

## Validation rule for any new Pine change
Curve-fitting one month of data is the failure mode we're actively avoiding
(we already burned one afternoon on a counter-bias theory that the data
rejected). For any proposed change:

1. State the hypothesis and which live `/stats` bucket motivates it.
2. Use `pine_compile` / `pine_get_errors` to confirm it compiles clean.
3. Run it through the Strategy Tester / `replay_*` tools over the **same
   pinned date range** (2026-05-12 to 2026-06-12) as a baseline comparison.
   Use `batch_run` for parameter sweeps rather than one-at-a-time manual runs.
4. Judge on **net profit and profit factor**, not win rate (gated reversals
   are low-frequency, high-R trades — win rate will look worse even when the
   change is good).
5. A change earns a recommendation only if it beats baseline on both PF and
   net profit without materially increasing max drawdown.
6. Never propose changing the live alert directly — propose the input change
   and its test results; the human decides whether/when to update the live
   script and recreate the TradingView alert.

## Repo layout
- `app/` — FastAPI agent (main, db, stats, analyst, alignment, parser, notify)
- `seed/backtest_stats.json` — static backtest priors + notes injected into
  every Claude verdict prompt
- `analysis/` — one-off SQL/scripts (alignment check, old-journal import)
- Pine source lives in TradingView itself (indicator `XAU-U10`, id `b5wwFe`
  on the connected chart) — not currently mirrored in this repo. If you pull
  it via `pine_get_source` to work on it, consider saving a copy here under
  `pine/XAU-U10.pine` so changes are diffable.
