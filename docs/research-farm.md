# TradingView Research Farm

The research farm is the offline/history side of the strategy farm. The live strategy farm answers: “what happens if this bot runs with a different timeframe/cadence/gate?” The research farm answers: “which TradingView MCP strategy family has historically worked best for this symbol and market window?”

Boundary: RESEARCH ONLY — TradingView MCP backtests; no broker orders, no ledger trades.

## What is implemented

`stonks-paper backtest-farm` reads `configs/research/backtest-farm.toml`, starts the same TradingView MCP provider configured in `config.paper.toml`, runs a bounded matrix of `backtest_strategy` calls, appends each result to JSONL, and prints a leaderboard.

It can split-test:

- Yahoo Finance symbol: `SPY`, `QQQ`, `AAPL`, `NVDA`, `BTC-USD`, etc.
- Strategy family: `rsi`, `bollinger`, `macd`, `ema_cross`, `supertrend`, `donchian`.
- Historical period: `1mo`, `3mo`, `6mo`, `1y`, `2y`.
- Backtest interval: `1d` or `1h`.
- Initial capital.
- Commission percent.
- Slippage percent.

The default config uses all six strategies across a small stock/crypto symbol set with 1y/2y daily backtests and a commission/slippage grid. `max_runs` caps the run so an accidental giant matrix does not hammer TradingView MCP/Yahoo.

## Commands

Preview the matrix without MCP calls:

```bash
stonks-paper backtest-farm --research-config configs/research/backtest-farm.toml --dry-plan
```

Run the backtest matrix:

```bash
stonks-paper backtest-farm --config config.paper.toml --research-config configs/research/backtest-farm.toml
```

Get machine-readable output:

```bash
stonks-paper backtest-farm --config config.paper.toml --research-config configs/research/backtest-farm.toml --json
```

Results append to:

```text
data/research/backtest-farm-results.jsonl
```

That file is local research data, not a paper-trading ledger. It can be deleted, archived, or loaded into a notebook later.

## How it relates to the live strategy farm

Use both layers:

1. Research farm: broad historical sweep of strategy families and friction assumptions.
2. Strategy farm: live shadow-paper split tests of the actual bot logic, screener sources, timeframe, cadence, score gates, stops, take-profits, and optimizer caps.
3. Main bot: one promoted config only, optionally Alpaca-paper-backed.

Do not let backtest winners auto-trade. Backtests should be treated as research context until a variant also behaves well in the local-only strategy farm.

## Practical matrix sizing

A full matrix grows fast:

```text
symbols × strategies × periods × intervals × initial_capital_values × commission_pct_values × slippage_pct_values
```

Example:

```text
8 symbols × 6 strategies × 2 periods × 1 interval × 1 capital × 2 commissions × 2 slippage values = 192 possible experiments
```

If `max_runs = 96`, only the first 96 are run. Set `max_runs = 0` only after reviewing `--dry-plan`.

## Symbol caveat

Backtesting uses Yahoo Finance symbols, not TradingView exchange-prefixed symbols:

- Stocks/ETFs: `AAPL`, `SPY`, `QQQ`, `NVDA`.
- Crypto: `BTC-USD`, `ETH-USD`, `SOL-USD`.
- Indices: `^GSPC`, `^IXIC`, `^VIX`.
- FX: `EURUSD=X`.

The runtime scanner still uses TradingView-style exchange/symbol pairs for `combined_analysis` and screeners.

## What is not automatic yet

The command currently uses `backtest_strategy`, because that exposes commission and slippage. Next research extensions to add are:

- `compare_strategies` quick sweeps for faster “all six at once” comparisons.
- `walk_forward_backtest_strategy` for overfitting checks before promoting winners.
- `market_snapshot`, `market_sentiment`, `financial_news`, and `combined_analysis` snapshots as market-regime/confluence context.
- Dashboard cards showing latest research winners beside live farm results.
