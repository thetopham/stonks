# Operator Descriptions

This page describes what the bot's fields and outputs mean in plain English.

## Bot identity

Stonks Paper Bot is a paper-only ranked stock screener. It builds a configured candidate universe, scores each symbol with TradingView MCP technical analysis, ranks candidates, and records BUY/SELL fills in SQLite.

By default, those fills are local simulations only. When `[broker] submit_orders = true`, it submits Alpaca paper orders to the Alpaca paper endpoint and records only confirmed filled paper orders in the same local ledger. Regular equity mode uses market orders while the Alpaca clock is open; `broker.equity_extended_hours=true` uses Alpaca 24/5-compatible equity limit orders with `extended_hours=true`. Dashboard and status output are read-only views over the local paper ledger. The options overlay reads Alpaca option chains/quotes, writes a separate options paper ledger, appears on the dashboard, and can submit single-leg Alpaca paper long-call/long-put orders only when both broker and options paper submission are explicitly enabled.

## Signal actions

- BUY: The symbol has no open paper position, has a usable price, is not above the RSI entry cap, meets or exceeds `entry_score`, and passes position-count/cash/optimizer guards.
- HOLD: The symbol either does not qualify for entry, is already open and remains within risk rules, has no usable price, or is blocked by a guard such as RSI or max positions.
- SELL: The symbol has an open paper position and hit stop loss, take profit, or the weak-score exit.

## Dashboard fields

- Equity: Paper cash plus marked value of open paper positions.
- Cash: Remaining simulated cash in the ledger.
- Open Position Value: Marked market value of open paper positions using the latest stored mark price.
- PnL: Realized paper PnL from closed SELL trades plus unrealized PnL on open positions.
- Open Positions: Current local paper-ledger holdings. In Alpaca paper-order mode, they represent confirmed filled paper broker orders recorded locally.
- Options Overlay: Separate options paper ledger summary: options cash/equity, cash source, open contracts, option PnL, DTE/liquidity/risk settings, and recent options trades.
- Entry: Local simulated fill price, or confirmed Alpaca paper fill price when broker paper orders are enabled.
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
- `execution.dry_run`: Must remain true. Broker paper orders are controlled separately by `broker.submit_orders`.
- `execution.live_trading_enabled`: Must remain false. If true, config loading fails.
- `execution.scan_interval_seconds`: Sleep duration between autonomous watch-loop scans.
- `execution.initial_delay_seconds`: Optional one-time startup delay used to stagger strategy-farm variants after service restarts.
- `strategy.entry_score`: Minimum score required for a new paper BUY.
- `strategy.exit_score`: Score at or below which an open paper position is sold.
- `strategy.max_rsi_for_entry`: RSI cap for new BUY entries when RSI is available.
- `strategy.stop_loss_pct`: Fractional stop-loss threshold from entry price. `0.07` means 7%.
- `strategy.take_profit_pct`: Fractional take-profit threshold from entry price. `0.15` means 15%.
- `selection.mode`: `ranked` scores all candidates before buying so later high-score symbols can beat earlier low-score symbols. `sequential` preserves old watchlist-order behavior for comparison.
- `selection.preview_top`: Number of ranked candidates printed in scan/screener reports.
- `screener.enabled`: When false, only the watchlist is scored. When true, `screener.source` decides how additional candidates are discovered before deep analysis.
- `screener.source`: `curated` uses local baskets, `mcp` uses dynamic TradingView MCP scanner output across configured exchanges, and `hybrid` uses both. `mcp` is the broad-screening mode; symbols do not need to be manually added one by one.
- `screener.universes`: Local candidate baskets for `curated`/`hybrid`: `watchlist`, `etf_core`, `nasdaq_mega`, `nyse_mega`, and `ai_infra`. In `mcp` mode the watchlist acts as a guaranteed seed/fallback.
- `screener.exchanges`: Exchanges sent to the MCP scanner discovery pass, such as `NASDAQ` and `NYSE`.
- `screener.dynamic_sources`: MCP scanner families used for discovery, such as `rating_strong_buy`, `rating_buy`, `volume_breakout`, `smart_volume`, `top_gainers`, `top_losers`, and `bollinger_squeeze`.
- `screener.per_source_limit`: Max symbols requested from each dynamic MCP source per exchange.
- `screener.max_candidates`: Safety cap on symbols sent to the slower deep `combined_analysis` scoring pass per scan.
- `screener.exclude_symbols`: Symbols removed before deep scoring unless already open; open positions are still checked for exits.
- `optimizer.enabled`: Enables portfolio-aware caps.
- `optimizer.cash_reserve_pct`: Fraction of starting cash held back from new BUY allocation.
- `optimizer.max_new_buys_per_scan`: Maximum new BUY fills allowed in one scan, regardless of universe size.
- `optimizer.min_position_notional`: Minimum accepted BUY size after reserve/cash caps.
- `provider.command` and `provider.args`: Command used to launch the TradingView MCP server. The paper config uses `scripts/tradingview-mcp-fixed`, a thin wrapper around `tradingview-mcp-server` that keeps daily `1d` intervals warning-free for `tradingview-ta`.
- `provider.timeframe`: Timeframe sent to TradingView MCP, such as `4h`, `1h`, or `1d`. The loader normalizes common daily aliases like `1D` to the lowercase `1d` value expected by tradingview-ta.
- `broker.name`: Optional broker credential namespace. `alpaca` enables `alpaca-check` and, if `broker.submit_orders=true`, Alpaca paper order submission.
- `broker.endpoint_env`, `broker.key_env`, `broker.secret_env`: Environment variable names used for the Alpaca paper endpoint and credentials.
- `broker.paper_only`: Must remain true. Credential loading rejects live Alpaca URLs when this is true.
- `broker.submit_orders`: Default false. If true with `broker.name="alpaca"`, `run-once` and `watch` submit paper broker orders. Regular equity mode sends market orders only after the Alpaca clock reports the market is open; crypto uses Alpaca crypto symbols/time-in-force; equity 24/5 mode sends limit orders with `extended_hours=true`.
- `broker.equity_extended_hours`: Opt-in Alpaca 24/5/overnight equity submission. When true, equity BUY/SELL broker submissions use limit orders instead of market orders and can run during Alpaca's Sunday 8 PM ET through Friday 8 PM ET window.
- `broker.equity_extended_hours_time_in_force`: Time-in-force for those 24/5 equity limit orders; `day` or `gtc`.
- `[options]`: Defines the options overlay: underlying list, DTE window, spread/open-interest filters, max debit, risk cap based on current options cash/buying power, calls/puts enablement, quote feed, and paper close rules.
- `options.auto_trade`: If true, the long-running `watch` service runs the options paper loop after each equity scan.
- `options.submit_orders`: If true with `broker.submit_orders=true`, `options-paper` submits single-leg Alpaca paper option limit orders. If false, it records local options paper fills only.
- `[[watchlist]]`: Symbol and exchange pairs always available to the candidate universe.

## CLI commands

- `stonks-paper screen --config config.paper.toml`: Starts TradingView MCP, scores the configured candidate universe, prints the ranked list, and writes no broker orders or ledger trades.
- `stonks-paper run-once --config config.paper.toml`: Starts TradingView MCP, scans the configured universe once, records local simulated fills or confirmed Alpaca paper fills, and prints a scan report.
- `stonks-paper watch --config config.paper.toml`: Repeats `run-once` forever using `execution.scan_interval_seconds`; with `broker.submit_orders=true`, this is the autonomous Alpaca paper-order loop; with `options.auto_trade=true`, it also runs the options paper loop each tick.
- `stonks-paper status --config config.paper.toml`: Reads only the local SQLite ledger and prints cash, open positions, equity estimate, and realized PnL.
- `stonks-paper options-scan --config config.paper.toml`: Read-only options scan; starts TradingView MCP, reads Alpaca option chains/quotes, prints defined-risk candidates, and writes no trades.
- `stonks-paper options-paper --config config.paper.toml`: Options paper loop; records simulated long call/put opens/closes in `option_*` tables by default, or syncs cash from Alpaca paper `options_buying_power` and submits confirmed Alpaca paper buy-to-open/sell-to-close limit orders when both broker and options submission are enabled.
- `stonks-paper options-sync-cash --config config.paper.toml --env .env`: Read-only Alpaca paper account call; updates local options cash from options buying power and submits no orders.
- `stonks-paper options-status --config config.paper.toml`: Reads only the local options paper ledger and prints option cash/source, open option positions, max loss, and PnL.
- `stonks-paper dashboard --config config.paper.toml --host 0.0.0.0 --port 8791`: Serves the read-only dashboard.
- `stonks-paper farm-status --farm-dir configs/farm`: Reads all strategy-farm shadow ledgers and prints a local-only split-test leaderboard without network calls.
- `stonks-paper farm-watch --farm-dir configs/farm --poll-seconds 30`: Runs enabled `configs/farm/*.toml` variants on their own cadences. Farm variants are forced to local-paper mode and are skipped if broker/options order submission is enabled.
- `stonks-paper backtest-farm --research-config configs/research/backtest-farm.toml --dry-plan`: Prints the read-only TradingView MCP backtest matrix without MCP calls.
- `stonks-paper backtest-farm --config config.paper.toml --research-config configs/research/backtest-farm.toml`: Runs historical `backtest_strategy` experiments and appends local JSONL research rows. It never submits broker orders or paper-ledger trades.
- `stonks-paper alpaca-check --config config.paper.toml --env .env`: Performs a read-only Alpaca paper account credential check and prints account status/equity/buying power without printing credentials or submitting orders. It also searches `.env`, `~/.hermes/.env`, `~/.hermes/hermes-agent/.env`, and `~/hermes-workspace/.env`.

## Ledger tables

- `state`: Stores equity paper cash plus separate options cash/source/sync timestamp (`options_cash`, `options_cash_source`, `options_cash_synced_at`).
- `positions`: Stores currently open equity paper positions, one row per symbol.
- `trades`: Stores append-only simulated equity BUY/SELL fills.
- `option_positions`: Stores currently open local options paper positions, one row per contract symbol.
- `option_trades`: Stores append-only local options BUY_TO_OPEN/SELL_TO_CLOSE fills.

## Service descriptions

- `stonks-paper-bot.service`: Runs the autonomous paper-trading watch loop.
- `stonks-paper-dashboard.service`: Runs the read-only dashboard.
- `stonks-paper-farm.service`: Runs the local-only strategy-farm scheduler.

Use systemd user commands to operate them:

- `systemctl --user status stonks-paper-bot.service stonks-paper-dashboard.service stonks-paper-farm.service`
- `systemctl --user restart stonks-paper-bot.service stonks-paper-dashboard.service stonks-paper-farm.service`
- `journalctl --user -u stonks-paper-bot.service -n 100 --no-pager`
- `journalctl --user -u stonks-paper-farm.service -n 100 --no-pager`

## Common operator interpretations

- If max positions is huge but no new BUY appears, the strategy filters are blocking entries; inspect score, RSI, no-price warnings, and existing one-position-per-symbol state.
- If a symbol says score 50, TradingView MCP may have returned neutral/unknown technicals or the bot may have fallen back to a price-only Yahoo quote.
- If the dashboard changes slowly, remember the dashboard does not fetch market data. It updates when the bot scan writes new marks/trades to SQLite.
- If few positions open from a large screener universe, check `optimizer.max_new_buys_per_scan`, `optimizer.cash_reserve_pct`, `max_open_positions`, RSI caps, and entry score before assuming the provider failed.
