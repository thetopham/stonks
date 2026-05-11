# Options overlay

The options overlay is a paper-only layer on top of the equity signal engine. It scans liquid long-call and long-put candidates from Alpaca option contracts/quotes, tracks them in the local SQLite options ledger, and can optionally submit single-leg Alpaca paper option limit orders.

Hard boundary: no live endpoint, no naked shorts, no multi-leg orders, no 0DTE. Config loading and Alpaca credential loading still reject `execution.live_trading_enabled=true`, `execution.dry_run=false`, non-paper broker config, and live Alpaca URLs.

## Commands

- `stonks-paper options-scan --config config.paper.toml`: read-only scan. Uses equity signals, reads Alpaca option contracts and quotes, prints candidates, writes no trades, and submits no orders.
- `stonks-paper options-paper --config config.paper.toml`: runs the options paper loop once. With `[options].submit_orders=false`, writes local simulated options paper opens/closes only. With `[broker].submit_orders=true` and `[options].submit_orders=true`, syncs options cash from Alpaca paper `options_buying_power` (fallback: `non_marginable_buying_power`, then `buying_power`), checks the Alpaca clock, submits buy-to-open/sell-to-close single-leg paper limit orders, and records only confirmed filled orders in SQLite.
- `stonks-paper options-sync-cash --config config.paper.toml --env .env`: read-only account call that updates the local options cash snapshot from Alpaca `options_buying_power`; no orders and no option-chain scan.
- `stonks-paper options-status --config config.paper.toml`: local SQLite status only; no network calls.
- `stonks-paper watch --config config.paper.toml`: runs the normal equity loop; when `[options].auto_trade=true`, each watch tick also runs `options-paper` automatically.

## Config gates for automatic Alpaca paper options

Alpaca paper option order submission requires all of these:

- `[execution].dry_run = true`
- `[execution].live_trading_enabled = false`
- `[broker].name = "alpaca"`
- `[broker].paper_only = true`
- `[broker].submit_orders = true`
- Alpaca endpoint env resolves to `https://paper-api.alpaca.markets/v2`
- `[options].enabled = true`
- `[options].auto_trade = true` for the watch service to run the loop automatically
- `[options].submit_orders = true` for options-paper to submit paper option orders instead of local-only paper fills

If an Alpaca paper option order is not filled after polling, the bot cancels it and does not update the local options ledger.

## Candidate rules

For each configured `[options].underlying_symbols` item, the bot:

1. Gets the existing TradingView MCP combined analysis for the underlying.
2. Uses bullish BUY signals for long-call candidates.
3. Uses strongly bearish scores at or below `strategy.exit_score` for long-put candidates when `allow_puts=true`.
4. Fetches Alpaca `/v2/options/contracts` for active contracts in the configured DTE and strike window.
5. Fetches latest option quotes from Alpaca data.
6. Keeps only contracts that pass:
   - `min_dte <= DTE <= max_dte`
   - `open_interest >= min_open_interest`
   - `(ask - bid) / mid <= max_spread_pct`
   - `ask * 100 * contracts_per_trade <= max_contract_debit`
   - strike inside `underlying_price * (1 +/- strike_pct_window)`
7. Ranks by closeness to `target_dte`, tight spread, strike distance, open interest, and ask price.

## Risk rules

`options-paper` buys to open only when:

- `[options].enabled=true`
- the local options ledger has room under `max_open_positions`
- there is not already an open option for that underlying
- max loss is within `options_cash * max_trade_risk_pct` (with broker execution enabled, `options_cash` is refreshed from Alpaca paper account `options_buying_power` before entries)
- the options ledger has enough `options_cash`
- when broker execution is enabled, Alpaca clock says the market is open

It closes options positions when:

- bid price reaches `entry_price * (1 - stop_loss_pct)`
- bid price reaches `entry_price * (1 + take_profit_pct)`
- DTE is at or below `min_exit_dte`

## Dashboard

The read-only dashboard includes an Options Overlay section showing:

- options cash/equity/position value, including whether cash came from configured starting cash or Alpaca options buying power
- open option contracts, marks, DTE, max loss, and unrealized PnL
- recent option paper trades
- current DTE, spread, OI, debit, stop, and target settings
- whether the overlay is disabled, local-paper-only, or Alpaca-paper-order enabled

The dashboard never submits orders; it only reads the local SQLite ledgers.

## Ledger tables

The overlay shares the configured SQLite file but uses separate records:

- `state.options_cash`, `state.options_cash_source`, `state.options_cash_synced_at`
- `option_positions`
- `option_trades`

This keeps equity paper cash/positions separate from the options overlay. When Alpaca paper execution is enabled, broker order IDs are stored in option trade/position metadata.
