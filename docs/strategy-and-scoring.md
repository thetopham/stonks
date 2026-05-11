# Strategy and Scoring

Stonks Paper Bot is a watchlist scanner plus paper ledger. On each scan it asks TradingView MCP for `combined_analysis` on each configured symbol and timeframe, turns the response into a 0-100 technical score, then applies deterministic BUY/HOLD/SELL gates.

It is not currently a portfolio optimizer. It does not rank all candidates before buying. It scans the watchlist in file order and acts as soon as a symbol qualifies.

## Data source

For each `[[watchlist]]` item, the runner calls:

- MCP tool: `combined_analysis`
- Arguments: `symbol`, `exchange`, and `provider.timeframe`
- Default timeframe: `1D`

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
6. Paper cash must be available for the configured notional.

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

When a BUY is accepted, paper notional is:

`min(current_cash * max_position_pct, current_cash)`

With `max_position_pct = 0.10`, the bot allocates 10% of remaining paper cash to each accepted buy. This means position sizes shrink geometrically as cash is consumed: first buy is 10% of cash, next buy is 10% of the remaining cash, and so on.

Paper fill prices include slippage:

- BUY price = market price * (1 + `slippage_pct / 100`)
- SELL price = market price * (1 - `slippage_pct / 100`)

The default `slippage_pct = 0.02` means 0.02%, not 2%.

`commission_pct` is parsed from config but is not currently applied by the runner. Treat it as a reserved setting until commission accounting is implemented.

## Position-count behavior

`max_open_positions` is a ceiling, not a target. Increasing it only removes the count blocker. The bot still needs qualifying scores, RSI, price, cash, and one-position-per-symbol gates before it opens trades.

The practical maximum number of open positions is currently the number of unique symbols in the watchlist, because the ledger has one open position per symbol.

## Watchlist order bias

The runner scans symbols in config order and buys immediately when a symbol qualifies. It does not collect all candidates, sort by score, and buy the best ones. If `max_open_positions` is small, earlier symbols in the watchlist can fill all slots before later symbols are evaluated.

If this becomes a problem, future improvements could add ranked candidate selection, sector/phase caps, randomized rotation, or separate watchlist buckets.

## Current local config snapshot

At the time this documentation was written, `config.paper.toml` used:

- 51 watchlist symbols
- `max_open_positions = 100000`
- `max_position_pct = 0.10`
- `entry_score = 65`
- `exit_score = 35`
- `max_rsi_for_entry = 70`
- `scan_interval_seconds = 900`

These are config values, not hard-coded strategy constants. Check `config.paper.toml` and the dashboard API for the live settings.
