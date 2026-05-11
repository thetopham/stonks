# stonks-paper-bot

Autonomous dry-run stock-market paper trader using the TradingView MCP server for market data and technical analysis.

Safety boundary: paper trading only. There is no broker integration, no account custody, no private-key handling, and no live order submission path. Config that attempts to enable live trading is rejected.

## Strategy summary

The bot scans each watchlist symbol in order, asks TradingView MCP for `combined_analysis`, converts that response into a 0-100 technical score, and records simulated paper BUY/SELL fills in SQLite.

Default behavior:

- Starts each symbol at score 50.
- Adds/subtracts points for trend bias, RSI, MACD crossover, SMA/EMA signals, Bollinger Band position, and sentiment.
- Buys only when score is at least `entry_score`, RSI is not above `max_rsi_for_entry`, price is usable, cash is available, and the max-open-position ceiling is not reached.
- Sells open paper positions on stop loss, take profit, or score falling to `exit_score` or lower.
- Allocates `max_position_pct` of remaining paper cash to each accepted BUY.

See [docs/strategy-and-scoring.md](docs/strategy-and-scoring.md) for the exact score table and gates.

## Docs

- [docs/index.md](docs/index.md) — docs map.
- [docs/strategy-and-scoring.md](docs/strategy-and-scoring.md) — exact strategy, scoring, entry/exit, sizing, and limitations.
- [docs/operator-descriptions.md](docs/operator-descriptions.md) — dashboard, CLI, config, ledger, and service field descriptions.

## Quick start

```bash
cd /home/matt/workspace/stonks-paper-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
stonks-paper init --config config.paper.toml
stonks-paper run-once --config config.paper.toml
stonks-paper status --config config.paper.toml
```

The TradingView MCP command is configured as:

```toml
[provider]
command = "/home/matt/.local/bin/uvx"
args = ["--from", "tradingview-mcp-server", "tradingview-mcp"]
timeframe = "1D"
```

## Commands

`stonks-paper run-once --config config.paper.toml` scans the watchlist one time, creates simulated paper BUY/SELL fills, and records them in SQLite.

`stonks-paper status --config config.paper.toml` reads only the local ledger and prints cash, open paper positions, and realized PnL.

`stonks-paper watch --config config.paper.toml` loops forever at `execution.scan_interval_seconds`.

`stonks-paper dashboard --config config.paper.toml --host 0.0.0.0 --port 8791` serves a read-only dashboard from the local SQLite paper ledger. If `STONKS_DASHBOARD_TOKEN` is set, the dashboard requires `?token=...` or an `Authorization: Bearer ...` header.

## User services

Installed templates live in `deploy/`:

- `deploy/stonks-paper-bot.service` runs the autonomous paper-trading loop.
- `deploy/stonks-paper-dashboard.service` runs the read-only dashboard.

The dashboard token is stored locally in `.dashboard.env` and dashboard URLs are stored in `dashboard.url`; both files should stay mode `600` and should not be committed.

## Ledger

Default ledger path: `data/paper-ledger.sqlite3`.

Tables:

- `state`: paper cash
- `positions`: currently open simulated positions
- `trades`: append-only simulated fills

## Approval boundary

Live trading remains explicitly out of scope. Starting/stopping the paper bot service is an operator action. Any broker integration, real order placement, private-key handling, or live execution path requires separate explicit approval and a safety review.
