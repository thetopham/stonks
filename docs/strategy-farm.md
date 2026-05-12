# Strategy Farm

The strategy farm is a local-only split-test harness for Stonks Paper Bot. It lets many variants run side-by-side with different timeframes, scan cadences, entry gates, risk settings, and screener caps while the main bot can remain the only process allowed to submit Alpaca paper orders.

For historical TradingView MCP strategy-family research, use the separate read-only [Research Farm](research-farm.md). The strategy farm tests the live bot loop; the research farm tests historical `backtest_strategy` matrices for RSI, Bollinger, MACD, EMA cross, Supertrend, and Donchian.

The short version:

- Main paper bot: `config.paper.toml`, ledger `data/paper-ledger.sqlite3`, may submit Alpaca paper orders if `[broker].submit_orders=true`.
- Strategy farm: `configs/farm/*.toml`, ledgers `data/farm/*.sqlite3`, always forced to local SQLite shadow mode.
- Each farm variant uses the same scanner, scoring, ranked selection, BUY/SELL gates, and ledger code as the main bot.
- Variants differ only by config overrides, so comparisons are apples-to-apples when the candidate universe is held constant.

## How it works

### 1. Variants are config files

Each `configs/farm/*.toml` file is one experiment. Most variants start with:

```toml
extends = "../../config.paper.toml"
```

Then the variant overrides only the variables being tested, for example:

```toml
name = "intraday-30m-1h"
description = "1h analysis with calmer 30m cadence."
ledger_path = "data/farm/intraday-30m-1h.sqlite3"

[execution]
scan_interval_seconds = 1800
initial_delay_seconds = 480

[provider]
timeframe = "1h"
```

`extends` is a deep merge. If a variant overrides `[provider].timeframe`, it inherits the rest of `[provider]` from `config.paper.toml`. If it overrides `[strategy].entry_score`, it inherits all other strategy defaults unless it explicitly changes them.

Daily aliases are normalized by the config loader: `1D` becomes `1d`. The local TradingView MCP wrapper in `scripts/tradingview-mcp-fixed` keeps daily variants warning-free with `tradingview-ta`.

### 2. Farm variants are safety-checked before every run

Before `farm-watch`, `farm-run-once`, `farm-status`, or the dashboard treats a variant as usable, `assert_shadow_safe()` checks that it cannot submit broker or options orders:

```toml
[execution]
dry_run = true
live_trading_enabled = false

[broker]
submit_orders = false

[options]
auto_trade = false
submit_orders = false
```

If a variant violates those rules, the farm skips it and reports it as unsafe. The farm also calls the runner with `broker=None`, so there is no broker adapter in the farm execution path.

This means a farm variant can still simulate BUY/SELL fills in its own SQLite ledger, but it cannot touch Alpaca paper or live endpoints.

### 3. `farm-watch` is a small scheduler

`stonks-paper farm-watch --farm-dir configs/farm --poll-seconds 30` runs forever and does this loop:

1. Discover enabled `*.toml` files under `configs/farm`.
2. Drop files whose name starts with `_`.
3. Load each enabled variant config.
4. Verify the shadow-mode safety boundary.
5. Track a `next_due` timestamp per variant name.
6. When a variant is due, run exactly one normal scan for that variant.
7. Schedule that variant's next run for `now + execution.scan_interval_seconds` after the scan completes.

Runs are sequential, not parallel. That is intentional: it avoids several variants hammering TradingView MCP at the same instant and keeps logs easier to read. `execution.initial_delay_seconds` staggers the first scan after service restart.

Because the next run is scheduled after a scan finishes, the true start-to-start cadence is:

```text
scan duration + execution.scan_interval_seconds
```

### 4. A farm scan is a normal bot scan with a separate ledger

For each due variant, the farm calls the same core flow used by `stonks-paper run-once`:

1. Open the variant ledger at `ledger_path`.
2. Start the configured TradingView MCP provider.
3. Build the candidate universe from the configured screener source.
4. Deep-score candidates with `combined_analysis` on the variant's `provider.timeframe`.
5. Process SELL exits first for that variant's open positions.
6. Rank BUY candidates.
7. Record local simulated fills in the variant ledger.
8. Close the ledger.

The main bot's ledger and the farm ledgers never share positions or cash. A symbol can be open in `balanced-30m-4h.sqlite3` and absent in `original-15m-1d.sqlite3`; that is the point of the split test.

### 5. Status and dashboard are local-only

`stonks-paper farm-status --farm-dir configs/farm` does not call TradingView MCP, Alpaca, or any market data API. It only reads configs and local SQLite ledgers, then prints a leaderboard sorted by total return.

The dashboard uses the same local farm-status path when `configs/farm` exists. The farm table is read-only and quota-neutral.

## Current variants

| Variant | Timeframe | Cadence | Purpose |
| --- | --- | ---: | --- |
| `original-15m-1d` | `1d` | 15m | Original baseline before the 30m/4h change: frequent polling of daily signals. |
| `balanced-30m-4h` | `4h` | 30m | Current balanced control shape. |
| `active-15m-4h` | `4h` | 15m | Same 4h signal bars as control, but checks twice as often. |
| `intraday-15m-1h` | `1h` | 15m | More active intraday-ish test. |
| `intraday-30m-1h` | `1h` | 30m | Same 1h signals with less churn/API load. |
| `swing-60m-1d` | `1d` | 60m | Conservative swing-style daily-signal test. |
| `strict-30m-4h` | `4h` | 30m | Higher entry threshold and lower RSI ceiling. |
| `aggressive-15m-1h` | `1h` | 15m | Easier entries, looser RSI ceiling, tighter stop/take-profit. |

The farm variants intentionally cap discovery load lower than the main bot:

```toml
[screener]
per_source_limit = 10
max_candidates = 12
dynamic_sources = ["rating_strong_buy", "rating_buy", "volume_breakout", "top_gainers"]
```

That keeps eight variants from multiplying the full production candidate cap by eight. If you add many more variants, lower these caps or stagger cadences further.

## Commands

Show the leaderboard without network calls:

```bash
stonks-paper farm-status --farm-dir configs/farm
```

Print JSON for dashboards/scripts:

```bash
stonks-paper farm-status --farm-dir configs/farm --json
```

Run every enabled variant once, sequentially:

```bash
stonks-paper farm-run-once --farm-dir configs/farm
```

Run the scheduler forever:

```bash
stonks-paper farm-watch --farm-dir configs/farm --poll-seconds 30
```

Check the user service:

```bash
systemctl --user status stonks-paper-farm.service
journalctl --user -u stonks-paper-farm.service -n 120 --no-pager
```

Restart the farm after code/config changes:

```bash
systemctl --user restart stonks-paper-farm.service
```

## Adding a new split test

Copy a nearby variant:

```bash
cp configs/farm/balanced-30m-4h.toml configs/farm/my-new-test.toml
```

Then change:

- `name` to a unique human-readable experiment name.
- `description` to explain exactly what is being tested.
- `ledger_path` to a unique SQLite path, usually `data/farm/my-new-test.sqlite3`.
- One or two experiment variables.
- `execution.initial_delay_seconds` so the first scan does not collide with existing variants.

Good first variables to test:

- `[provider].timeframe`: `1h`, `4h`, `1d`.
- `[execution].scan_interval_seconds`: `900`, `1800`, `3600`.
- `[strategy].entry_score` and `exit_score`.
- `[strategy].max_rsi_for_entry`.
- `[strategy].stop_loss_pct` and `take_profit_pct`.
- `[optimizer].max_new_buys_per_scan`.
- `[screener].dynamic_sources`, `per_source_limit`, and `max_candidates`.

Keep the shadow safety block intact:

```toml
[broker]
submit_orders = false

[options]
auto_trade = false
submit_orders = false
```

Set `enabled = false` at the top of a variant to park it without deleting the file.

## How to compare results

Use `farm-status` as the quick leaderboard, but do not promote a winner from one or two scans. Compare after enough time for each variant to experience similar market conditions.

Important fields:

- `total_return_pct`: headline score, based on cash plus marked open positions.
- `equity`: starting cash plus realized/unrealized value.
- `realized_pnl`: closed-trade PnL.
- `unrealized_pnl`: open-position mark-to-market PnL.
- `open_positions`: how much risk the variant is currently carrying.
- `trades_recorded`: whether the variant is active enough to produce a meaningful sample.
- `latest_trade_at`: freshness.

For cleaner experiments:

1. Keep starting cash, universe, dynamic sources, and position caps identical unless those are the tested variables.
2. Change one family of variables at a time.
3. Keep every variant on its own ledger.
4. Run for at least several market sessions before judging timeframe/cadence.
5. Promote one winning config to `config.paper.toml`; do not let multiple farm variants submit broker orders.

## Operational cautions

"Infinite" split tests are possible in the sense that adding a new `*.toml` file creates another experiment, but the practical limits are TradingView MCP/API load, scan duration, log volume, and SQLite disk growth.

Rules of thumb:

- Keep farm variants local-only.
- Keep `max_candidates` small for broad farms.
- Prefer sequential farm runs over parallel runs.
- Add initial delays for new variants.
- Watch logs after adding a high-frequency variant.
- Treat the main broker-backed bot as the only candidate for Alpaca paper execution.

## Why not just clone the initial pre-Alpaca commit?

Commit `29457a70c9c34f630bc254c6243a966d9108f7b1` is a clean pre-Alpaca baseline. Cloning it could be a useful sandbox if the current repo becomes too coupled to broker execution.

For the current strategy farm, though, cloning that commit would not be easier. The initial commit does not have the later dynamic MCP stock screener, ranked full-universe selection, config `extends`, farm scheduler, farm dashboard/status integration, Alpaca/24-5 safety guards, options separation, or the TradingView daily-interval wrapper. We would have to re-add most of the machinery that makes the farm useful.

The current design keeps the main repo as the source of truth while using config-level isolation for experiments. If we later want an even cleaner research-only branch, the best path is probably a dedicated `strategy-lab` branch or package extracted from the current runner/screener/ledger modules, not a hard reset to the initial commit.
