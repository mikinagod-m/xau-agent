-- Counter-bias verification over the live journal (run in Railway Postgres).
-- Classifies each closed trade's direction against the CTX Bias/Flow/Momentum
-- captured at entry, then aggregates win rate and net realised R per class.
-- Mirrors app/alignment.py: vote per field (+1 bull / -1 bear / 0), net dotted
-- against direction; ALIGNED dot >= 2, COUNTER dot <= -2, else MIXED.

WITH closed AS (
    SELECT side, setup, grade, outcome, entry, sl, tp, opened_at,
           ctx->>'Bias'     AS bias,
           ctx->>'Flow'     AS flow,
           ctx->>'Momentum' AS momentum
    FROM trades
    WHERE outcome IN ('TP HIT', 'SL HIT')
      AND entry IS NOT NULL AND sl IS NOT NULL AND tp IS NOT NULL
),
voted AS (
    SELECT *,
        (CASE WHEN upper(coalesce(bias,''))     LIKE '%BULL%' OR upper(coalesce(bias,''))     LIKE 'LONG%'  THEN 1
              WHEN upper(coalesce(bias,''))     LIKE '%BEAR%' OR upper(coalesce(bias,''))     LIKE 'SHORT%' THEN -1 ELSE 0 END
       + CASE WHEN upper(coalesce(flow,''))     LIKE '%BULL%' THEN 1
              WHEN upper(coalesce(flow,''))     LIKE '%BEAR%' THEN -1 ELSE 0 END
       + CASE WHEN upper(coalesce(momentum,'')) LIKE '%BULL%' THEN 1
              WHEN upper(coalesce(momentum,'')) LIKE '%BEAR%' THEN -1 ELSE 0 END)
       * (CASE WHEN side = 'LONG' THEN 1 ELSE -1 END) AS dot,
        CASE WHEN outcome = 'TP HIT'
             THEN abs(tp - entry) / NULLIF(abs(entry - sl), 0)
             ELSE -1.0 END AS r
    FROM closed
),
classed AS (
    SELECT *,
        CASE WHEN bias IS NULL AND flow IS NULL AND momentum IS NULL THEN 'UNKNOWN'
             WHEN dot >= 2  THEN 'ALIGNED'
             WHEN dot <= -2 THEN 'COUNTER'
             ELSE 'MIXED' END AS alignment
    FROM voted
)
SELECT alignment,
       count(*)                                   AS trades,
       count(*) FILTER (WHERE outcome = 'TP HIT') AS wins,
       round(100.0 * count(*) FILTER (WHERE outcome = 'TP HIT') / count(*), 1) AS wr_pct,
       round(sum(r)::numeric, 2)                  AS net_r
FROM classed
GROUP BY alignment
ORDER BY alignment;

-- Drill-down: the individual COUNTER trades, to eyeball against the charts.
-- SELECT opened_at, side, grade, setup, bias, flow, momentum, outcome
-- FROM classed WHERE alignment = 'COUNTER' ORDER BY opened_at;
