# Strategy and Scoring

Stonks Paper Bot is a ranked stock screener plus paper ledger. On each scan it builds a candidate universe, asks TradingView MCP for `combined_analysis` on each configured symbol and timeframe, turns every response into a 0-100 technical score, then applies deterministic BUY/HOLD/SELL gates.

By default, BUY/SELL fills are local SQLite simulations. If `[broker] submit_orders = true` with Alpaca `paper_only = true`, accepted BUY/SELL signals submit Alpaca paper equity/crypto orders and only confirmed filled paper orders are recorded in SQLite. Regular equity mode uses paper market orders while the Alpaca clock is open; `broker.equity_extended_hours=true` switches equities to 24/5-compatible limit orders with `extended_hours=true` and `day`/`gtc` time-in-force.

The current runtime scores all candidates before acting, processes SELL exits first, then ranks BUY candidates by portfolio-adjusted score before allocating paper cash.

## Candidate universe and screener mode

The baseline universe is always the configured `[[watchlist]]`. With `[screener] enabled = true`, `screener.source` controls where additional candidates come from:

- `curated` — add local baskets before making expensive market-data calls.
- `mcp` — query TradingView MCP scanner tools across `screener.exchanges`, dedupe the discovered symbols, then deep-score the best candidates. This is the broad “screen the market, then rank opportunities” mode, but it can burn many TradingView requests without shared caching.
- `hybrid` — use both dynamic MCP scanner output and local curated baskets.

Supported local screener universes:

- `watchlist` — the configured `[[watchlist]]` entries.
- `indices` — the core index ETFs SPY, QQQ, IWM, DIA, and VTI.
- `etf_core` — broader ETFs such as SPY, QQQ, IWM, DIA, VTI, sector/semiconductor/treasury/gold ETFs.
- `nasdaq_mega` — liquid mega-cap NASDAQ names.
- `nyse_mega` — liquid mega-cap NYSE names.
- `ai_infra` — AI infrastructure, semis, power/grid, networking, and space/defense names.

Supported MCP discovery sources:

- `rating_strong_buy`
- `rating_buy`
- `rating_weak_buy`
- `volume_breakout`
- `smart_volume`
- `top_gainers`
- `top_losers`
- `bollinger_squeeze`

The screener dedupes by symbol, normalizes TradingView symbols such as `NASDAQ:AAPL` to broker symbols such as `AAPL`, applies `screener.exclude_symbols`, and caps the slower `combined_analysis` pass with `screener.max_candidates`. Existing open positions are protected so exits are still checked even if the broad screener does not rediscover them.

## Data source

For each candidate item, the runner calls:

- MCP tool: `combined_analysis`
- Arguments: `symbol`, `exchange`, and `provider.timeframe`
- Default timeframe: `4h`

If `combined_analysis` fails or returns no usable current price, the provider falls back to `yahoo_price`. A quote-only fallback supplies price but has unknown bias, no RSI, no MACD, and no moving-average signals, so it usually scores close to the neutral base of 50.

## Score formula

The score starts at 50.0 and is clamped to the 0-100 range after all adjustments.

| Input | Adjustment | Notes |
| --- | ---: | --- |
| `timeframe_context.bias` contains `bullish` | +20 | Broad trend bias from TradingView MCP. |
| `timeframe_context.bias` contains `bearish` | -20 | Broad trend bias from TradingView MCP. |
| RSI >= 70 | -20 | Treats overbought RSI as extension risk. |
| 50 <= RSI < 70 | +5 | Momentum confirmation. |
| RSI <= 30 | +5 | Oversold bounce candidate. |
| MACD crossover contains `bullish` | +10 | Momentum confirmation. |
| MACD crossover contains `bearish` | -10 | Momentum warning. |
| SMA/EMA bullish signal | +5 each, max +20 | A signal is bullish if it contains `bullish`, `above`, or `golden`. |
| SMA/EMA bearish signal | -5 each, max -20 | A signal is bearish if it contains `bearish`, `below`, or `death`. |
| Bollinger position contains `above upper` | -5 | Price is extended above the band. |
| Bollinger position contains `below lower` | +5 | Mean-reversion/bounce candidate. |
| Sentiment contains `positive` or `bullish` | +5 | From `analysis.sentiment`. |
| Sentiment contains `negative` or `bearish` | -5 | From `analysis.sentiment`. |

If none of those inputs produce a reason, the signal reason is `No strong technical edge detected`.

## Entry logic

A symbol with no open paper position can only BUY when every gate passes:

1. Current price must be greater than 0.
2. If RSI is present, RSI must be less than or equal to `strategy.max_rsi_for_entry`.
3. Score must be greater than or equal to `strategy.entry_score`.
4. The ledger must have fewer than `max_open_positions` open positions.
5. The symbol must not already have an open position. The ledger stores one open position per symbol.
6. Paper cash must be available after preserving the optimizer cash reserve.
7. The scan must not have already reached `optimizer.max_new_buys_per_scan`.

Default entry settings:

- `entry_score = 65`
- `max_rsi_for_entry = 70`

Important exact behavior: RSI >= 70 applies a -20 score penalty, but the entry cap blocks only when RSI is greater than `max_rsi_for_entry`. With the default cap, RSI exactly 70 is penalized but not blocked by the cap.

## Exit logic

A symbol with an open paper position can SELL when any exit gate triggers. The gates are checked in this order:

1. Stop loss: current price <= entry price * (1 - `strategy.stop_loss_pct`)
2. Take profit: current price >= entry price * (1 + `strategy.take_profit_pct`)
3. Weak score: score <= `strategy.exit_score`
4. Otherwise HOLD

Default exit settings:

- `stop_loss_pct = 0.07`, meaning -7% from paper entry
- `take_profit_pct = 0.15`, meaning +15% from paper entry
- `exit_score = 35`

The RSI entry cap does not apply to positions that are already open. For open positions, RSI can still influence the score, and the score can trigger the weak-score exit.

## Position sizing and execution simulation

When a BUY is accepted and the optimizer is disabled, paper notional is:

`min(current_cash * max_position_pct, current_cash)`

With `max_position_pct = 0.10`, that allocates 10% of remaining paper cash to each accepted buy. This means position sizes shrink geometrically as cash is consumed: first buy is 10% of cash, next buy is 10% of the remaining cash, and so on.

When `[optimizer] enabled = true`, the notional is capped by both `max_position_pct` and the spendable cash after reserving `starting_cash * optimizer.cash_reserve_pct`. The optimizer also blocks buys below `optimizer.min_position_notional` and caps each scan with `optimizer.max_new_buys_per_scan`.

## Ranked candidate selection

The default `[selection] mode = "ranked"` removes watchlist-order bias. The runner now:

1. Builds the configured universe.
2. Scores every candidate before writing trades.
3. Processes SELL exits for open positions first.
4. Ranks new BUY candidates by portfolio-adjusted score, raw score, reason count, lower RSI extension risk, then symbol.
5. Buys only the best candidates that still fit cash, position, reserve, and per-scan caps.

Use `stonks-paper screen --config config.paper.toml` to print the read-only ranked screener without broker orders or ledger trades.

The current portfolio-adjusted score starts from the raw technical score and subtracts a small diversification penalty for existing same-exchange exposure. The hard optimizer constraints are more important than the penalty: max positions, max new buys, cash reserve, and minimum notional decide whether a ranked candidate can actually be bought.

Local simulated paper fill prices include slippage:

- BUY price = market price * (1 + `slippage_pct / 100`)
- SELL price = market price * (1 - `slippage_pct / 100`)

The default `slippage_pct = 0.02` means 0.02%, not 2%.

When Alpaca paper order submission is enabled, slippage is not applied locally to confirmed fills. The bot uses Alpaca's confirmed `filled_avg_price` and `filled_qty`; if an order is not filled after polling, the bot attempts to cancel it and does not write a ledger fill. In equity 24/5 mode, the same `slippage_pct` is used only to set a protective limit price around the signal price before Alpaca paper execution.

`commission_pct` is parsed from config but is not currently applied by the runner. Treat it as a reserved setting until commission accounting is implemented.

## Position-count behavior

`max_open_positions` is a ceiling, not a target. Increasing it only removes the count blocker. The bot still needs qualifying scores, RSI, price, cash, and one-position-per-symbol gates before it opens trades.

The practical maximum number of open positions is currently constrained by the unique symbols that reach the deep-scoring pass. In the current `screener.source = "curated"` setup, that means the capped index-plus-watchlist universe plus any currently open symbols protected for exit checks. In `screener.source = "mcp"` mode, the set becomes dynamic scanner output plus the watchlist seed and open symbols.

## Watchlist order bias

Watchlist-order bias is fixed when `[selection] mode = "ranked"`. Earlier symbols can no longer fill all available slots before later, higher-scoring candidates are evaluated. Set `selection.mode = "sequential"` only if you explicitly want old-style watchlist-order behavior for comparison.

## Current local config snapshot

At the time this documentation was written, `config.paper.toml` used:

- MCP scanner discovery disabled with `screener.source = "curated"`; the bot scores a capped top-50 universe made from core index ETFs plus the configured watchlist
- `screener.max_candidates = 50`
- `max_open_positions = 25`
- `max_position_pct = 0.10`
- `entry_score = 65`
- `exit_score = 35`
- `max_rsi_for_entry = 70`
- `selection.mode = "ranked"`
- `optimizer.cash_reserve_pct = 0.05`
- `optimizer.max_new_buys_per_scan = 10`
- `broker.equity_extended_hours = true` for Alpaca paper 24/5-compatible equity limit orders
- `scan_interval_seconds = 1800` — the main watch loop scans every 30 minutes, using the configured `4h` analysis timeframe

Strategy-farm variants under `configs/farm/` intentionally test other combinations such as original `15m/1d`, `15m/1h`, and stricter/aggressive score gates. Those variants use separate local SQLite ledgers and are forced to `broker.submit_orders=false`, so they do not compete for the Alpaca paper account.

These are config values, not hard-coded strategy constants. Check `config.paper.toml`, `configs/farm/*.toml`, and the dashboard API for the live settings.
