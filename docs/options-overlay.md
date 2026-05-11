# Options overlay

The options overlay is a v1 paper-only layer on top of the equity signal engine. It does not submit Alpaca option orders.

## Commands

- `stonks-paper options-scan --config config.paper.toml`: read-only scan. Uses equity signals, reads Alpaca option contracts and quotes, prints candidates, writes no trades.
- `stonks-paper options-paper --config config.paper.toml`: local options paper loop. It can write simulated option opens/closes to SQLite, but still sends no option orders to Alpaca.
- `stonks-paper options-status --config config.paper.toml`: local SQLite status only; no network calls.

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

## Paper-risk rules

`options-paper` buys to open only when:

- `[options].enabled=true`
- the local options ledger has room under `max_open_positions`
- there is not already an open option for that underlying
- max loss is within `starting_cash * max_trade_risk_pct`
- the options ledger has enough `options_cash`

It closes local paper positions when:

- bid price reaches `entry_price * (1 - stop_loss_pct)`
- bid price reaches `entry_price * (1 + take_profit_pct)`
- DTE is at or below `min_exit_dte`

## Ledger tables

The overlay shares the configured SQLite file but uses separate records:

- `state.options_cash`
- `option_positions`
- `option_trades`

This keeps equity paper cash/positions separate from the options overlay.

## Approval boundary

No option order submission code exists in v1. Moving from local options paper trades to Alpaca paper option orders would be a separate approval step and should add broker-order tests first.
