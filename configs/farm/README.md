# Strategy farm configs

Each `*.toml` file in this directory is one local-only shadow split test.

Most variants start with:

```toml
extends = "../../config.paper.toml"
```

Then they override only the experiment variables: timeframe, scan cadence, score gates, risk settings, screener caps, or dynamic source mix. The config loader deep-merges the parent and child TOML files, so omitted sections inherit from `config.paper.toml`.

## Safety rules

Every farm variant must remain local-only:

- `execution.dry_run = true`
- `execution.live_trading_enabled = false`
- `broker.submit_orders = false`
- `options.auto_trade = false`
- `options.submit_orders = false`
- Each variant needs a unique `ledger_path`, normally under `data/farm/`.

`stonks-paper farm-watch` checks these rules before every scan. Unsafe variants are skipped instead of traded.

## Load rules

- Files matching `*.toml` are discovered automatically.
- Files whose name starts with `_` are ignored.
- `enabled = false` parks a variant without deleting it.
- `execution.initial_delay_seconds` staggers the first scan after service restart.
- `execution.scan_interval_seconds` controls that variant's sleep interval after a scan completes.

## Keep MCP load bounded

Farm runs multiply TradingView MCP usage across variants. The current farm keeps discovery disabled and reuses the capped top-50 curated universe: core index ETFs first, then the configured watchlist.

```toml
[screener]
source = "curated"
universes = ["indices", "watchlist"]
max_candidates = 50
```

If you re-enable dynamic `screener.source = "mcp"`, keep the shared cache/throttle protection on or keep per-variant caps very small. All current farm variants inherit:

```toml
[provider.cache]
enabled = true
path = "data/provider-cache.sqlite3"
```

That means the main bot and farm variants on this host reuse the same MCP tool-result snapshots instead of refreshing the same symbol/timeframe independently.

## Add a variant

1. Copy a nearby file, for example `balanced-30m-4h.toml`.
2. Change `name`, `description`, and `ledger_path`.
3. Change one or two experiment variables.
4. Keep the safety block intact.
5. Run `stonks-paper farm-status --farm-dir configs/farm` to confirm it is safe.

Full docs: `../../docs/strategy-farm.md`.
