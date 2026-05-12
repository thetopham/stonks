# stonks-paper-bot

Autonomous stock/crypto paper trader using the TradingView MCP server for market data and technical analysis.

Safety boundary: paper trading only. By default, the trading loop records local SQLite paper fills only. If `[broker] submit_orders = true` with `paper_only = true`, the loop submits Alpaca paper equity/crypto orders to `https://paper-api.alpaca.markets/v2`, records only filled paper orders in SQLite, and still rejects real-money Alpaca endpoints plus `execution.live_trading_enabled=true`. Equity orders use regular market orders unless `broker.equity_extended_hours=true`, in which case they use Alpaca 24/5-compatible limit orders with `extended_hours=true` and `day`/`gtc` time-in-force. Crypto orders use Alpaca slash symbols such as `BTC/USD` with `gtc` time-in-force and do not wait for the equity market clock. The options overlay uses the same hard live-trading boundary: `options-scan` is read-only, `options-paper` records local long-call/long-put fills by default, and it submits single-leg Alpaca paper option limit orders only when `[broker].submit_orders=true` and `[options].submit_orders=true`.

## Strategy summary

The bot builds a candidate universe, asks TradingView MCP for `combined_analysis`, converts every response into a 0-100 technical score, ranks candidates, then records simulated paper BUY/SELL fills in SQLite. With `screener.source = "mcp"`, the universe starts with dynamic TradingView MCP scanner output across configured equity and crypto exchanges, so you do not need to manually add every possible symbol to the config; the watchlist is only a guaranteed seed/fallback.

Default behavior:

- Starts each symbol at score 50.
- Adds/subtracts points for trend bias, RSI, MACD crossover, SMA/EMA signals, Bollinger Band position, and sentiment.
- Buys only when score is at least `entry_score`, RSI is not above `max_rsi_for_entry`, price is usable, cash is available, and optimizer caps allow the trade.
- Sells open paper positions on stop loss, take profit, or score falling to `exit_score` or lower.
- Scores the full universe before acting, processes SELL exits first, then buys the highest ranked candidates.
- Allocates up to `max_position_pct` of remaining paper cash to each accepted BUY while preserving the optimizer cash reserve.

See [docs/strategy-and-scoring.md](docs/strategy-and-scoring.md) for the exact score table and gates.

## Docs

- [docs/index.md](docs/index.md) — docs map.
- [docs/strategy-and-scoring.md](docs/strategy-and-scoring.md) — exact strategy, scoring, entry/exit, sizing, and limitations.
- [docs/options-overlay.md](docs/options-overlay.md) — Alpaca options scanner, local options paper ledger, dashboard overlay, and guarded Alpaca paper option execution.
- [docs/operator-descriptions.md](docs/operator-descriptions.md) — dashboard, CLI, config, ledger, and service field descriptions.
- [docs/strategy-farm.md](docs/strategy-farm.md) — local-only split tests for timeframe, cadence, and strategy-variable experiments.
- [docs/research-farm.md](docs/research-farm.md) — read-only TradingView MCP backtest matrix for RSI/Bollinger/MACD/EMA/Supertrend/Donchian research.
- [docs/roadmap.md](docs/roadmap.md) — future TODOs for ranked screening, portfolio optimization, backtesting, sentiment/news, and dashboard explainability.

## Quick start

```bash
cd /home/matt/workspace/stonks-paper-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env  # optional; the bot also loads ~/.hermes/.env and ~/hermes-workspace/.env
chmod 600 .env
stonks-paper init --config config.paper.toml
stonks-paper alpaca-check --config config.paper.toml --env .env
stonks-paper run-once --config config.paper.toml
stonks-paper status --config config.paper.toml
```

The local paper config launches TradingView MCP through a small wrapper that keeps daily `1d` strategy-farm variants warning-free:

```toml
[provider]
command = "/home/matt/workspace/stonks-paper-bot/scripts/tradingview-mcp-fixed"
args = []
timeframe = "4h"
```

## Commands

`stonks-paper screen --config config.paper.toml` runs the read-only ranked screener. In `screener.source = "mcp"` mode it first asks TradingView MCP scanner tools for broad-market stock and crypto candidates across configured exchanges, then deep-scores the capped candidate set and prints the sorted list without broker orders or ledger trades.

`stonks-paper run-once --config config.paper.toml` scans the configured universe one time. With `broker.submit_orders=false`, it creates simulated local paper BUY/SELL fills. With `broker.submit_orders=true`, equity orders use the Alpaca paper endpoint and record only confirmed fills; regular mode uses market orders only when the Alpaca clock is open, while `broker.equity_extended_hours=true` switches equities to 24/5-compatible limit orders with `extended_hours=true`. Crypto paper orders submit directly to supported Alpaca crypto pairs such as `BTC/USD`; only confirmed filled paper orders are recorded in SQLite.

`stonks-paper status --config config.paper.toml` reads only the local ledger and prints cash, open paper positions, and realized PnL.

`stonks-paper options-scan --config config.paper.toml` is a read-only options overlay. It uses the same equity technical signals, reads Alpaca `/v2/options/contracts` plus latest option quotes, filters for liquid defined-risk long calls/puts, and writes no ledger trades.

`stonks-paper options-paper --config config.paper.toml` runs the options paper loop once. With `[options].submit_orders=false`, it records local long-call/long-put paper fills only. With `[broker].submit_orders=true` and `[options].submit_orders=true`, it first syncs options cash from Alpaca paper `options_buying_power` (falling back to non-marginable/account buying power if needed), checks that the Alpaca market is open, submits single-leg Alpaca paper limit orders for buy-to-open/sell-to-close, and records only confirmed filled paper option orders in SQLite.

`stonks-paper options-sync-cash --config config.paper.toml --env .env` performs a read-only Alpaca paper account check and updates the local options cash snapshot from `options_buying_power`; it submits no orders. The dashboard reads this persisted snapshot instead of assuming the configured starting cash.

`stonks-paper options-status --config config.paper.toml` reads only the local options paper ledger and prints cash, cash source, open option positions, max loss, unrealized PnL, and realized PnL.

`stonks-paper watch --config config.paper.toml` loops forever at `execution.scan_interval_seconds`. If broker paper orders are enabled, this is the autonomous Alpaca paper-order loop. If `[options].auto_trade=true`, each watch tick also runs the options paper loop; with `[options].submit_orders=true` it can submit Alpaca paper option limit orders under the same paper-only safeguards.

`stonks-paper farm-status --farm-dir configs/farm` reads every strategy-farm shadow ledger and prints a local-only leaderboard. `stonks-paper farm-watch --farm-dir configs/farm --poll-seconds 30` runs the farm scheduler forever. Farm variants are separate `configs/farm/*.toml` files with unique ledgers, forced `broker.submit_orders=false`, and no options auto-trading.

`stonks-paper backtest-farm --research-config configs/research/backtest-farm.toml --dry-plan` previews the read-only TradingView MCP research matrix. `stonks-paper backtest-farm --config config.paper.toml --research-config configs/research/backtest-farm.toml` runs historical `backtest_strategy` experiments for RSI, Bollinger, MACD, EMA cross, Supertrend, and Donchian, then appends local JSONL results under `data/research/`. This is research-only and never submits broker orders or paper-ledger trades.

`stonks-paper dashboard --config config.paper.toml --host 0.0.0.0 --port 8791` serves a read-only dashboard from the local SQLite paper ledger, including the Options Overlay section with option cash, open contracts, recent option trades, current option risk settings, and the local-only Strategy Farm leaderboard when `configs/farm` exists. If `STONKS_DASHBOARD_TOKEN` is set, the dashboard requires `?token=...` or an `Authorization: Bearer ...` header.

`stonks-paper alpaca-check --config config.paper.toml --env .env` performs a read-only Alpaca paper account check with the configured endpoint/key/secret env names. It also loads `.env`, `~/.hermes/.env`, `~/.hermes/hermes-agent/.env`, and `~/hermes-workspace/.env` without overwriting non-empty values. It prints account status/equity/buying power and does not submit orders or print credentials.

`hermes-bloomberg-collect --config config.paper.toml --no-service-check` collects read-only context for the personal Hermes Bloomberg brief. It reads `/home/matt/wiki`, this bot's config, and the local SQLite paper ledger, then writes `~/.hermes/data/hermes-bloomberg/latest_context.{json,md}` plus a dated copy. Add `--quotes` for public Stooq watchlist quotes and `--news` for public Google News RSS headlines; neither mode submits orders or touches broker accounts.

## User services

Installed templates live in `deploy/`:

- `deploy/stonks-paper-bot.service` runs the autonomous paper-trading loop.
- `deploy/stonks-paper-dashboard.service` runs the read-only dashboard.
- `deploy/stonks-paper-farm.service` runs the local-only strategy-farm scheduler.

The dashboard token is stored locally in `.dashboard.env` and dashboard URLs are stored in `dashboard.url`; both files should stay mode `600` and should not be committed.

## Ledger

Default ledger path: `data/paper-ledger.sqlite3`.

Tables:

- `state`: equity paper cash plus `options_cash`, `options_cash_source`, and `options_cash_synced_at` for the separate options overlay ledger
- `positions`: currently open simulated equity positions
- `trades`: append-only simulated equity fills
- `option_positions`: currently open options paper positions, whether locally simulated or confirmed Alpaca paper fills
- `option_trades`: append-only options paper fills, including broker order IDs when Alpaca paper execution is enabled

## Approval boundary

Real-money live trading remains explicitly out of scope. Starting/stopping the paper bot service is an operator action. Read-only Alpaca paper account checks are allowed for credential validation. Alpaca paper broker order submission is allowed only through `[broker] submit_orders = true` while `paper_only = true`; live endpoints, custody actions, transfers, withdrawals, and real-money order placement remain rejected. Crypto support is spot-paper only through Alpaca's paper orders API; no custody/transfer actions are implemented. Options execution is restricted to long calls/puts on the Alpaca paper endpoint only, requires both `[broker].submit_orders=true` and `[options].submit_orders=true`, records only filled paper orders, and does not use naked shorts, multi-leg orders, or live trading.
