# Roadmap and Future TODOs

This file tracks implemented improvements plus remaining future work. The live safety boundary remains unchanged: paper-only, no real-money endpoints, and no live trading.

## Implemented in the current runtime

- Ranked candidate selection via `[selection] mode = "ranked"`.
- Full-universe scoring before paper BUY actions.
- SELL exits processed before new BUY allocation.
- Read-only `stonks-paper screen` command for manual screener review without broker orders or ledger trades.
- Dynamic MCP broad-screening mode via `screener.source = "mcp"`, `screener.exchanges`, `screener.dynamic_sources`, and `screener.per_source_limit`.
- Local curated fallback/hybrid screener universes: `watchlist`, `etf_core`, `nasdaq_mega`, `nyse_mega`, and `ai_infra`.
- Portfolio optimizer guardrails: cash reserve, max new buys per scan, minimum notional, max positions, one-position-per-symbol.

## Near-term: ranked candidate selection

Goal: remove watchlist-order bias by scoring the whole universe before opening new positions.

Status: implemented for the local paper/Alpaca-paper loop. Remaining work is dashboard visualization and richer portfolio score components.

TODO:

- Add dashboard cards for ranked candidates and skipped-candidate reasons.
- Add richer portfolio-adjusted score dimensions beyond same-exchange diversification.

## Stock screener / expanded universe

Goal: move from a fixed watchlist toward a broad stock screener that finds candidates automatically.

Status: implemented as a two-stage process. Dynamic MCP scanner tools discover broad candidates across configured exchanges; the bot dedupes/caps that set, keeps watchlist/open positions as protected seeds, then deep-scores candidates with `combined_analysis`.

TODO:

- Add quote/liquidity/volume prefilters before expensive analysis.
- Add market-cap and sector metadata when available.
- Store per-scan candidate snapshots locally so dashboard/status can explain the opportunity set, not only executed trades.
- Track provider failures separately from strategy rejects.

Active MCP discovery sources:

- `rating_filter` via `rating_strong_buy`, `rating_buy`, and `rating_weak_buy`.
- `volume_breakout_scanner` via `volume_breakout`.
- `smart_volume_scanner` via `smart_volume`.
- `top_gainers` and `top_losers`.
- `bollinger_scan` via `bollinger_squeeze`.

Other TradingView MCP tools to evaluate:

- `yahoo_price` for quick quote and 52-week context.
- `market_snapshot` for market regime context: indices, VIX, crypto, FX, ETFs.
- `combined_analysis` for technicals + Reddit sentiment + news confluence.
- `market_sentiment` and `financial_news` for separate sentiment/news inputs.
- `multi_timeframe_analysis`, `volume_confirmation_analysis`, and `multi_agent_analysis` for richer confirmation on the final candidate shortlist.

## Portfolio optimizer

Goal: make the bot choose the best portfolio shape, not just the next qualifying trade.

Status: first guardrail optimizer implemented. It caps new buys per scan, preserves a cash reserve, enforces minimum notional, and ranks buys after exits.

TODO:

- Add portfolio-level constraints: sector caps, max correlated exposure, and max total risk.
- Convert raw signal score into a portfolio-adjusted score that includes current holdings, concentration, volatility, drawdown, and market regime.
- Rank sell/hold decisions too: weak score, stop loss, take profit, risk reduction, rebalance, and better-use-of-capital reasons.
- Add target allocations for top-ranked candidates instead of using the same `max_position_pct` for every accepted buy.
- Add rebalance preview mode before any paper sell/buy actions.
- Keep all optimizer decisions auditable: inputs, score components, constraints, selected action, and rejected alternatives.

## Backtesting and strategy research

Goal: learn which strategy family works for each symbol before allocating paper capital.

TODO:

- Add a research command that runs historical backtests outside the live scan loop.
- Compare strategy families per symbol: RSI, Bollinger, MACD, EMA cross, Supertrend, and Donchian.
- Rank strategies with realistic commission and slippage, not only total return.
- Prefer robust metrics: Sharpe, Calmar, max drawdown, profit factor, expectancy, win rate, and vs buy-and-hold.
- Store backtest summaries locally and expose them in status/dashboard as research context, not as automatic live-trading permission.
- Add walk-forward validation before trusting a strategy for optimizer weights.

Candidate tools to evaluate:

- `backtest_strategy`
- `compare_strategies`
- `walk_forward_backtest_strategy`

## Sentiment, news, and confluence

Goal: avoid buying technically strong names into obvious negative news or weak sentiment.

TODO:

- Keep technical score as the core signal, then add separate sentiment/news/confluence fields.
- Penalize severe negative news, unusually bearish Reddit sentiment, or broad market risk-off regimes.
- Reward confluence only when the technical setup already passes quality gates.
- Store headlines/post references as summarized reasons, not raw feed dumps.
- Add dashboard cards for top positive/negative confluence drivers.

## Dashboard and operator UX

Goal: make every autonomous decision explainable at a glance.

TODO:

- Add a ranked candidates table: symbol, score, action, price, RSI, portfolio-adjusted score, and reject reason.
- Add a portfolio optimizer card: slots available, cash reserve, sector exposure, top proposed buys, top proposed sells.
- Add a research/backtest card per symbol once backtest summaries exist.
- Add a read-only `stonks-paper screen` command for manual review before the autonomous loop uses screener output.

## Safety and approval boundaries

- Real-money live trading remains out of scope.
- Any broad screener or optimizer should run read-only first, then local paper only, then Alpaca paper only after explicit approval.
- The bot should never place more paper orders just because the candidate universe gets larger; order count, position count, and cash/risk caps must stay explicit.
- Secrets stay in `.env` / local env files only and are never printed in reports or docs.
