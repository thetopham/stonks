from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

from .alpaca import (
    AlpacaConfigError,
    AlpacaPaperClient,
    format_account_summary,
    load_alpaca_credentials,
    load_default_env_files,
)
from .config import load_config
from .ledger import PaperLedger
from .options import OptionsPaperLedger, format_options_status, options_paper_once, options_scan_once, sync_options_cash_from_account
from .dashboard import serve_dashboard
from .mcp_client import TradingViewMCPProvider
from .runner import format_status, run_once, screen_once


def _copy_example(destination: Path) -> None:
    source = Path(__file__).resolve().parent.parent / "config.example.toml"
    shutil.copyfile(source, destination)


def _build_broker_if_enabled(config_path: Path):
    config = load_config(config_path)
    if not config.broker.submit_orders:
        return config, None
    load_default_env_files(config_path)
    credentials = load_alpaca_credentials(config.broker)
    return config, AlpacaPaperClient(credentials)


async def _run_once(config_path: Path) -> str:
    config, broker = _build_broker_if_enabled(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    async with TradingViewMCPProvider(config.provider.command, config.provider.args, config.provider.timeframe) as provider:
        return await run_once(config, ledger, provider, broker=broker)


async def _screen_once(config_path: Path) -> str:
    config = load_config(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        async with TradingViewMCPProvider(config.provider.command, config.provider.args, config.provider.timeframe) as provider:
            return await screen_once(config, ledger, provider)
    finally:
        ledger.close()


def _build_options_data_client(config_path: Path, env_paths=None):
    load_default_env_files(config_path, extra_paths=env_paths)
    config = load_config(config_path)
    credentials = load_alpaca_credentials(config.broker)
    return config, AlpacaPaperClient(credentials)


async def _options_scan(config_path: Path, env_paths=None) -> str:
    config, client = _build_options_data_client(config_path, env_paths)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        async with TradingViewMCPProvider(config.provider.command, config.provider.args, config.provider.timeframe) as provider:
            return await options_scan_once(config, ledger, options_ledger, provider, client)
    finally:
        ledger.close()
        options_ledger.close()


async def _options_paper(config_path: Path, env_paths=None) -> str:
    config, client = _build_options_data_client(config_path, env_paths)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = client if config.options.submit_orders else None
    try:
        if config.options.submit_orders:
            sync_options_cash_from_account(options_ledger, client.get_account())
        async with TradingViewMCPProvider(config.provider.command, config.provider.args, config.provider.timeframe) as provider:
            return await options_paper_once(config, ledger, options_ledger, provider, client, broker=broker)
    finally:
        ledger.close()
        options_ledger.close()


async def _watch(config_path: Path) -> None:
    while True:
        config = load_config(config_path)
        try:
            print(await _run_once(config_path), flush=True)
        except Exception as exc:  # pragma: no cover - defensive around long-running service loop
            print(f"stonks-paper-bot scan failed: {type(exc).__name__}: {exc}", flush=True)
        if config.options.auto_trade:
            try:
                print(await _options_paper(config_path), flush=True)
            except Exception as exc:  # pragma: no cover - defensive around long-running service loop
                print(f"stonks-paper-bot options auto-trade failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(config.execution.scan_interval_seconds)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _backup_ledger(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _broker_position_snapshots(raw_positions: list[dict], watch_exchange_by_symbol: dict[str, str]) -> list[dict]:
    snapshots: list[dict] = []
    for raw in raw_positions:
        symbol = str(raw.get("symbol") or "").upper()
        quantity = _as_float(raw.get("qty", raw.get("quantity")), 0.0)
        entry_price = _as_float(raw.get("avg_entry_price", raw.get("entry_price")), 0.0)
        if not symbol or quantity == 0 or entry_price <= 0:
            continue
        last_price = _as_float(raw.get("current_price", raw.get("last_price")), entry_price)
        snapshots.append(
            {
                "symbol": symbol,
                "exchange": str(raw.get("exchange") or watch_exchange_by_symbol.get(symbol) or "NASDAQ").upper(),
                "quantity": quantity,
                "entry_price": entry_price,
                "last_price": last_price,
                "metadata": {
                    "broker_synced": True,
                    "broker": "alpaca",
                    "asset_id": raw.get("asset_id"),
                },
            }
        )
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stock-market paper trader powered by TradingView MCP")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a local config file from config.example.toml")
    init.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    run = sub.add_parser("run-once", help="scan watchlist once and apply paper trades")
    run.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    screen = sub.add_parser("screen", help="read-only ranked screener; scores candidates without broker orders or ledger trades")
    screen.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    status = sub.add_parser("status", help="print local paper ledger status without network calls")
    status.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    options_scan = sub.add_parser("options-scan", help="read-only Alpaca option-chain scan from equity signals; no ledger writes")
    options_scan.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    options_scan.add_argument("--env", type=Path, action="append", default=None, help="extra env file to load before .env and Hermes defaults; can be repeated")

    options_paper = sub.add_parser("options-paper", help="run the options overlay once; submits Alpaca paper option orders only when [options].submit_orders=true")
    options_paper.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    options_paper.add_argument("--env", type=Path, action="append", default=None, help="extra env file to load before .env and Hermes defaults; can be repeated")

    options_status = sub.add_parser("options-status", help="print local options paper ledger status without network calls")
    options_status.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    options_sync_cash = sub.add_parser("options-sync-cash", help="read-only sync of options cash to Alpaca paper options buying power")
    options_sync_cash.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    options_sync_cash.add_argument("--env", type=Path, action="append", default=None, help="extra env file to load before .env and Hermes defaults; can be repeated")

    watch = sub.add_parser("watch", help="run autonomous paper scans forever until interrupted")
    watch.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    dashboard = sub.add_parser("dashboard", help="serve the read-only paper ledger dashboard")
    dashboard.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8791)

    alpaca_check = sub.add_parser("alpaca-check", help="read-only Alpaca paper account credential smoke check")
    alpaca_check.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    alpaca_check.add_argument("--env", type=Path, action="append", default=None, help="extra env file to load before .env and Hermes defaults; can be repeated")

    alpaca_sync = sub.add_parser("alpaca-sync", help="read-only sync of local paper ledger to Alpaca paper account positions")
    alpaca_sync.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    alpaca_sync.add_argument("--env", type=Path, action="append", default=None, help="extra env file to load before .env and Hermes defaults; can be repeated")
    alpaca_sync.add_argument("--yes", action="store_true", help="confirm replacing the local paper ledger with the Alpaca account snapshot")

    args = parser.parse_args(argv)

    if args.command == "init":
        if args.config.exists():
            raise SystemExit(f"refusing to overwrite existing {args.config}")
        _copy_example(args.config)
        print(f"wrote {args.config}; review watchlist and risk settings, then run stonks-paper run-once")
        return 0

    if args.command == "run-once":
        try:
            print(asyncio.run(_run_once(args.config)))
        except AlpacaConfigError as exc:
            print(f"Alpaca paper broker failed: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "screen":
        print(asyncio.run(_screen_once(args.config)))
        return 0

    if args.command == "status":
        config = load_config(args.config)
        ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
        print(format_status(config, ledger))
        return 0

    if args.command == "options-scan":
        try:
            print(asyncio.run(_options_scan(args.config, args.env)))
        except AlpacaConfigError as exc:
            print(f"Alpaca options scan failed: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "options-paper":
        try:
            print(asyncio.run(_options_paper(args.config, args.env)))
        except AlpacaConfigError as exc:
            print(f"Alpaca options paper failed: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.command == "options-status":
        config = load_config(args.config)
        options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
        print(format_options_status(config, options_ledger))
        return 0

    if args.command == "options-sync-cash":
        try:
            load_default_env_files(args.config, extra_paths=args.env)
            config = load_config(args.config)
            credentials = load_alpaca_credentials(config.broker)
            client = AlpacaPaperClient(credentials)
            account = client.get_account()
            options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
            try:
                cash_value = sync_options_cash_from_account(options_ledger, account)
                cash_source = options_ledger.cash_source
            finally:
                options_ledger.close()
        except (AlpacaConfigError, ValueError) as exc:
            print(f"Options cash sync failed: {exc}", file=sys.stderr)
            return 2
        print("Boundary: READ-ONLY Alpaca paper options buying-power sync — no orders submitted")
        print(f"Endpoint: {credentials.endpoint}")
        print(f"Account: {format_account_summary(account)}")
        print(f"Options cash sync complete: cash={cash_value:.2f}, source={cash_source}")
        return 0

    if args.command == "watch":
        try:
            asyncio.run(_watch(args.config))
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.command == "dashboard":
        serve_dashboard(args.config, host=args.host, port=args.port)
        return 0

    if args.command == "alpaca-check":
        try:
            load_default_env_files(args.config, extra_paths=args.env)
            config = load_config(args.config)
            credentials = load_alpaca_credentials(config.broker)
            account = AlpacaPaperClient(credentials).get_account()
        except AlpacaConfigError as exc:
            print(f"Alpaca check failed: {exc}", file=sys.stderr)
            return 2
        print("Boundary: READ-ONLY Alpaca paper account check — no orders submitted")
        print(f"Endpoint: {credentials.endpoint}")
        print(f"Account: {format_account_summary(account)}")
        return 0

    if args.command == "alpaca-sync":
        try:
            load_default_env_files(args.config, extra_paths=args.env)
            config = load_config(args.config)
            credentials = load_alpaca_credentials(config.broker)
            client = AlpacaPaperClient(credentials)
            account = client.get_account()
            raw_positions = client.get_positions()
        except AlpacaConfigError as exc:
            print(f"Alpaca sync failed: {exc}", file=sys.stderr)
            return 2

        watch_exchange_by_symbol = {item.symbol: item.exchange for item in config.watchlist}
        snapshots = _broker_position_snapshots(raw_positions, watch_exchange_by_symbol)
        cash = _as_float(account.get("cash"), config.starting_cash)
        print("Boundary: READ-ONLY Alpaca paper account sync — no orders submitted")
        print(f"Endpoint: {credentials.endpoint}")
        print(f"Account: {format_account_summary(account)}")
        if not args.yes:
            print(f"Preview: would replace local ledger with cash={cash:.2f}, positions={len(snapshots)}; re-run with --yes to write")
            return 2

        backup_path = _backup_ledger(config.ledger_path)
        ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
        ledger.reset_to_broker_snapshot(cash=cash, positions=snapshots)
        backup_text = str(backup_path) if backup_path is not None else "none"
        print(f"Ledger sync complete: synced_positions={len(snapshots)}, cash={cash:.2f}, backup={backup_text}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
