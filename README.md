# GoldSpread

Phase 1 data logger: measures triangulation divergences across the broker's gold pairs.

**Status:** Phase 1 — measurement only. NO trading, NO execution, NO live decisions.

## What it does

Every 200 ms (configurable) it records, for one tick:
- bid/ask of 6 XAU pairs: `XAUUSD`, `XAUEUR`, `XAUGBP`, `XAUAUD`, `XAUJPY`, `XAUDCHF`
- bid/ask of 5 forex pairs: `EURUSD`, `GBPUSD`, `AUDUSD`, `USDJPY`, `USDCHF`
- For each derived XAU pair: theoretical price (triangulated through the FX pair), divergence in **USD per XAU unit**, the XAU-pair spread in USD-per-XAU, the FX-pair hedge spread in USD-per-XAU, and a boolean `edge_exists` (= `|divergence| > combined spreads`).

Output:
- `data/goldspread.db` — SQLite (WAL mode, autocommit)
- `data/goldspread_YYYY-MM-DD.csv` — daily CSV export at UTC day rollover and on clean shutdown

## Triangulation formulas

| Pair | Theoretical | Mode |
|------|-------------|------|
| XAUEUR | XAUUSD / EURUSD | div |
| XAUGBP | XAUUSD / GBPUSD | div |
| XAUAUD | XAUUSD / AUDUSD | div |
| XAUJPY | XAUUSD × USDJPY | mul |
| XAUDCHF | XAUUSD × USDCHF | mul |

All comparisons happen in USD-per-1-XAU-unit. The FX-pair "hedge spread" is the cost in USD of the implicit FX leg required to hold the synthetic position. Derivation is in `main.py:_compute_derived` with comments per branch.

## Install

```
git clone https://github.com/hhvivaldi/goldspread.git
cd goldspread
python -m venv venv
venv\Scripts\activate           # Windows; or `source venv/bin/activate`
pip install -r requirements.txt
copy .env.example .env          # then edit .env with MT5 credentials
```

## Run

```
python main.py
```

Stop with Ctrl-C. Clean shutdown: CSV export of current day → DB close → MT5 disconnect.

## Configuration

`.env` (copy from `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `MT5_ACCOUNT` | demo account number | required |
| `MT5_PASSWORD` | demo account password | required |
| `MT5_SERVER` | MT5 server name | required |
| `MT5_TERMINAL_PATH` | optional path to `terminal64.exe` | (empty) |
| `GOLDSPREAD_TICK_INTERVAL_MS` | poll period in milliseconds | 200 |
| `GOLDSPREAD_DB_PATH` | SQLite path | `data/goldspread.db` |
| `GOLDSPREAD_LOG_LEVEL` | `DEBUG` / `INFO` / `WARN` | `INFO` |
| `GOLDSPREAD_DAILY_CSV_EXPORT` | `true`/`false` — export CSV at day rollover | `true` |

## Data schema

One row per tick, ~60 columns. See `logger.py:_build_columns()` for the canonical list. Per-derived-pair columns:

```
{pair}_bid                          REAL
{pair}_ask                          REAL
{pair}_spread_pips                  REAL    -- display only
{pair}_theoretical                  REAL    -- triangulated
{pair}_divergence_usd_per_xau       REAL    -- (real_mid − theoretical) × USD-per-quote
{pair}_xau_spread_usd_per_xau       REAL    -- broker XAU-pair spread, USD-equivalent
{pair}_forex_spread_usd_per_xau     REAL    -- FX hedge spread, USD-equivalent
{pair}_edge_exists                  INTEGER -- 1 if abs(divergence) > sum of spreads, 0 otherwise, NULL if uncomputable
```

XAUUSD has only `bid`/`ask`/`spread_pips` (it's the anchor).

Forex pairs have `bid`/`ask`/`spread_pips`.

`missing_symbols` (TEXT) lists any symbols that returned no tick this read; their columns are NULL.

NULL distinguishes "not computable" from "real zero".

### Useful queries

Recent rows where any pair shows an edge:

```sql
SELECT ts_utc,
       xaueur_divergence_usd_per_xau, xaueur_xau_spread_usd_per_xau, xaueur_forex_spread_usd_per_xau,
       xaugbp_divergence_usd_per_xau, xaugbp_xau_spread_usd_per_xau, xaugbp_forex_spread_usd_per_xau,
       xauaud_divergence_usd_per_xau, xauaud_xau_spread_usd_per_xau, xauaud_forex_spread_usd_per_xau,
       xaujpy_divergence_usd_per_xau, xaujpy_xau_spread_usd_per_xau, xaujpy_forex_spread_usd_per_xau,
       xaudchf_divergence_usd_per_xau, xaudchf_xau_spread_usd_per_xau, xaudchf_forex_spread_usd_per_xau
  FROM ticks
 WHERE COALESCE(xaueur_edge_exists, 0)
     + COALESCE(xaugbp_edge_exists, 0)
     + COALESCE(xauaud_edge_exists, 0)
     + COALESCE(xaujpy_edge_exists, 0)
     + COALESCE(xaudchf_edge_exists, 0) > 0
 ORDER BY ts_utc DESC LIMIT 50;
```

Daily edge-counts by pair:

```sql
SELECT substr(ts_utc, 1, 10) AS day,
       SUM(COALESCE(xaueur_edge_exists, 0))  AS xaueur,
       SUM(COALESCE(xaugbp_edge_exists, 0))  AS xaugbp,
       SUM(COALESCE(xauaud_edge_exists, 0))  AS xauaud,
       SUM(COALESCE(xaujpy_edge_exists, 0))  AS xaujpy,
       SUM(COALESCE(xaudchf_edge_exists, 0)) AS xaudchf
  FROM ticks GROUP BY day ORDER BY day;
```

Average spreads per pair (USD-per-XAU):

```sql
SELECT AVG(xaueur_xau_spread_usd_per_xau)   AS xaueur_xau,
       AVG(xaueur_forex_spread_usd_per_xau) AS xaueur_fx,
       AVG(xaugbp_xau_spread_usd_per_xau)   AS xaugbp_xau,
       AVG(xaugbp_forex_spread_usd_per_xau) AS xaugbp_fx
  FROM ticks WHERE xauusd_bid IS NOT NULL;
```

## Operational notes

- 1 pip = 0.01 for XAU pairs (display only); load-bearing math is USD-per-XAU
- Missing symbols (broker doesn't offer one) are logged once at startup and persisted as NULL each tick
- Per-tick failures are logged at DEBUG to avoid 1Hz spam; one INFO heartbeat per minute summarises ticks/writes/edges/missing
- SQLite WAL mode enables concurrent reads while logging
- Single Python thread; no MT5-proxy lock needed (FlokiWatch's `mt5_safe` is for multi-thread safety)

## Phase 1 success criteria

Run 5 full market days. Then analyse:
- How often does `edge_exists = 1` occur per pair?
- Distribution of (|divergence| − combined_spread) — the "edge magnitude"
- Persistence: do edges last >1s, >10s, >1min, or are they all flash spikes?

That analysis informs whether Phase 2 (signal engine) is worth building. Phase 1 ships zero trading code by design.

## What this project is NOT

- No agents (Floki, Rex, Luna, Echo, Simba, Sage)
- No ML
- No FastAPI dashboard
- No prompt system
- No order execution path

Reference patterns from FlokiWatch (`executor.py` for MT5 connection style, `db_writer.py` for SQLite shape, `config.py` for python-dotenv) are studied — not copied.
