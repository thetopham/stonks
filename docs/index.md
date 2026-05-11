# Stonks Paper Bot Docs

Start here if you are trying to understand what the autonomous paper trader is doing.

## Reading order

1. [Strategy and scoring](strategy-and-scoring.md) — exact score formula, BUY/HOLD/SELL gates, exits, sizing, and current limitations.
2. [Operator descriptions](operator-descriptions.md) — plain-English descriptions of dashboard fields, CLI output, config knobs, ledger tables, and service behavior.
3. [README](../README.md) — quick start, service commands, dashboard command, and safety boundary.

## Safety boundary

This project is paper-only. It records simulated trades in a local SQLite ledger. It has no broker adapter, no live order submission path, no private-key handling, and rejects config that disables dry-run mode or enables live trading.
