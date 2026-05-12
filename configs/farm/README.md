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

Farm runs multiply TradingView MCP usage across variants. Keep these lower than the main bot unless you intentionally want a heavier test:

```toml
[screener]
per_source_limit = 10
max_candidates = 12
dynamic_sources = ["rating_strong_buy", "rating_buy", "volume_breakout", "top_gainers"]
```

## Add a variant

1. Copy a nearby file, for example `balanced-30m-4h.toml`.
2. Change `name`, `description`, and `ledger_path`.
3. Change one or two experiment variables.
4. Keep the safety block intact.
5. Run `stonks-paper farm-status --farm-dir configs/farm` to confirm it is safe.

Full docs: `../../docs/strategy-farm.md`.
