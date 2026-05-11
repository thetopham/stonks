# stonks-paper-bot

Autonomous dry-run stock-market paper trader using the TradingView MCP server for market data and technical analysis.

Safety boundary: paper trading only. There is no broker integration, no account custody, no private-key handling, and no live order submission path. Config that attempts to enable live trading is rejected.

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

The dashboard token is stored locally in `.dashboard.env` and dashboard URLs are stored in `dashboard.url`; both files should stay mode `600`.

## Ledger

Default ledger path: `data/paper-ledger.sqlite3`.

Tables:

- `state`: paper cash
- `positions`: currently open simulated positions
- `trades`: append-only simulated fills

## Approval boundary

Live trading remains explicitly out of scope. The next approval-needed step would be picking the watchlist/strategy risk settings and deciding whether to install the provided user-service template. Any broker integration would require a separate explicit approval and safety review.
