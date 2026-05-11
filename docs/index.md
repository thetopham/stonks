# Stonks Paper Bot Docs

Start here if you are trying to understand what the autonomous paper trader is doing.

## Reading order

1. [Strategy and scoring](strategy-and-scoring.md) — exact score formula, BUY/HOLD/SELL gates, exits, sizing, and current limitations.
2. [Options overlay](options-overlay.md) — Alpaca option-chain scanner, local options ledger, dashboard overlay, and guarded Alpaca paper option execution.
3. [Operator descriptions](operator-descriptions.md) — plain-English descriptions of dashboard fields, CLI output, config knobs, ledger tables, and service behavior.
4. [Roadmap and future TODOs](roadmap.md) — implemented ranked screening/optimizer guardrails plus remaining backtesting, sentiment/news, and dashboard explainability work.
5. [README](../README.md) — quick start, service commands, dashboard command, and safety boundary.

## Safety boundary

This project is paper-only. It records local simulated trades in SQLite by default. When `[broker] submit_orders = true`, it can submit Alpaca paper market orders to the paper endpoint and records only confirmed filled paper orders in SQLite. When `[options].submit_orders=true` as well, it can submit single-leg long-call/long-put Alpaca paper option limit orders. It still rejects real-money Alpaca endpoints, disabled dry-run mode, `execution.live_trading_enabled=true`, naked shorts, and multi-leg option orders.
