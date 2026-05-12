# Research configs

`backtest-farm.toml` defines the read-only TradingView MCP backtest matrix used by:

```bash
stonks-paper backtest-farm --research-config configs/research/backtest-farm.toml --dry-plan
stonks-paper backtest-farm --config config.paper.toml --research-config configs/research/backtest-farm.toml
```

This is separate from `configs/farm/*.toml`:

- `configs/farm/*.toml` runs live shadow-paper strategy variants with local SQLite ledgers.
- `configs/research/backtest-farm.toml` runs historical MCP backtests and appends JSONL research rows.

Safety boundary: research configs never submit Alpaca orders and never write paper-ledger trades.

Start with `--dry-plan` before increasing symbols, intervals, or friction grids. Matrix size is multiplicative, and `max_runs` is the guardrail against accidental huge sweeps.
