# Operator Descriptions

This page describes what the bot's fields and outputs mean in plain English.

## Bot identity

Stonks Paper Bot is a paper-only stock-market scanner. It observes a configured watchlist, scores each symbol with TradingView MCP technical analysis, and records simulated BUY/SELL fills in SQLite.

It does not submit orders to a broker. Dashboard and status output are read-only views over the local paper ledger.

## Signal actions

- BUY: The symbol has no open paper position, has a usable price, is not above the RSI entry cap, meets or exceeds `entry_score`, and passes position-count/cash guards.
- HOLD: The symbol either does not qualify for entry, is already open and remains within risk rules, has no usable price, or is blocked by a guard such as RSI or max positions.
- SELL: The symbol has an open paper position and hit stop loss, take profit, or the weak-score exit.

## Dashboard fields

- Equity: Paper cash plus marked value of open paper positions.
- Cash: Remaining simulated cash in the ledger.
- Open Position Value: Marked market value of open paper positions using the latest stored mark price.
- PnL: Realized paper PnL from closed SELL trades plus unrealized PnL on open positions.
- Open Positions: Current simulated holdings. These are not broker positions.
- Entry: Simulated fill price for the paper BUY, including configured buy-side slippage.
- Mark: Last stored price from the latest scan that touched the open symbol.
- Peak DD: Drawdown from the highest stored price seen while the paper position has been open.
- Score: Score recorded at paper entry or trade time.
- Recent Paper Trades: Append-only simulated fills from the `trades` table.

The dashboard reads the SQLite ledger and config. It does not call TradingView, Yahoo, or any broker.

## Config knobs

- `ledger_path`: SQLite file used for paper cash, positions, and trades.
- `starting_cash`: Initial simulated cash inserted into a new ledger.
- `max_position_pct`: Fraction of current paper cash allocated to each accepted BUY.
- `max_open_positions`: Maximum number of simultaneously open paper positions. It is a ceiling, not a target.
- `commission_pct`: Reserved. Parsed but not currently applied to fills.
- `slippage_pct`: Percent slippage applied to paper fills. The code divides this by 100, so `0.02` means 0.02%.
- `execution.dry_run`: Must remain true.
- `execution.live_trading_enabled`: Must remain false. If true, config loading fails.
- `execution.scan_interval_seconds`: Sleep duration between autonomous watch-loop scans.
- `strategy.entry_score`: Minimum score required for a new paper BUY.
- `strategy.exit_score`: Score at or below which an open paper position is sold.
- `strategy.max_rsi_for_entry`: RSI cap for new BUY entries when RSI is available.
- `strategy.stop_loss_pct`: Fractional stop-loss threshold from entry price. `0.07` means 7%.
- `strategy.take_profit_pct`: Fractional take-profit threshold from entry price. `0.15` means 15%.
- `provider.command` and `provider.args`: Command used to launch the TradingView MCP server.
- `provider.timeframe`: Timeframe sent to TradingView MCP, such as `1D`.
- `[[watchlist]]`: Symbol and exchange pairs scanned in file order.

## CLI commands

- `stonks-paper run-once --config config.paper.toml`: Starts TradingView MCP, scans the watchlist once, records paper fills, and prints a scan report.
- `stonks-paper watch --config config.paper.toml`: Repeats `run-once` forever using `execution.scan_interval_seconds`.
- `stonks-paper status --config config.paper.toml`: Reads only the local SQLite ledger and prints cash, open positions, equity estimate, and realized PnL.
- `stonks-paper dashboard --config config.paper.toml --host 0.0.0.0 --port 8791`: Serves the read-only dashboard.

## Ledger tables

- `state`: Stores paper cash.
- `positions`: Stores currently open paper positions, one row per symbol.
- `trades`: Stores append-only simulated BUY/SELL fills.

## Service descriptions

- `stonks-paper-bot.service`: Runs the autonomous paper-trading watch loop.
- `stonks-paper-dashboard.service`: Runs the read-only dashboard.

Use systemd user commands to operate them:

- `systemctl --user status stonks-paper-bot.service stonks-paper-dashboard.service`
- `systemctl --user restart stonks-paper-bot.service stonks-paper-dashboard.service`
- `journalctl --user -u stonks-paper-bot.service -n 100 --no-pager`

## Common operator interpretations

- If max positions is huge but no new BUY appears, the strategy filters are blocking entries; inspect score, RSI, no-price warnings, and existing one-position-per-symbol state.
- If a symbol says score 50, TradingView MCP may have returned neutral/unknown technicals or the bot may have fallen back to a price-only Yahoo quote.
- If the dashboard changes slowly, remember the dashboard does not fetch market data. It updates when the bot scan writes new marks/trades to SQLite.
- If early watchlist symbols dominate entries, that is expected when `max_open_positions` is small because the bot acts in watchlist order.
