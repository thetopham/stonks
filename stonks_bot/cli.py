from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

from .alpaca import (
    AlpacaConfigError,
    AlpacaPaperClient,
    format_account_summary,
    load_alpaca_credentials,
    load_default_env_files,
)
from .config import BotConfig, load_config
from .farm import FarmVariant, assert_shadow_safe, collect_farm_status, discover_farm_variants, format_farm_status
from .ledger import PaperLedger
from .market_hours import MarketHoursState, equity_market_hours_state, seconds_until_next_open
from .options import OptionsPaperLedger, format_options_status, options_paper_once, options_scan_once, sync_options_cash_from_account
from .dashboard import serve_dashboard
from .mcp_client import TradingViewMCPProvider
from .models import WatchItem, infer_asset_class
from .research import (
    build_backtest_experiments,
    config_to_jsonable,
    format_backtest_report,
    load_backtest_farm_config,
    run_backtest_farm,
)
from .runner import format_status, run_once, screen_once
from .screener import build_static_candidate_universe


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


def _tradingview_provider(config):
    provider = config.provider
    return TradingViewMCPProvider(
        provider.command,
        provider.args,
        provider.timeframe,
        cache_enabled=provider.cache_enabled,
        cache_path=provider.cache_path,
        cache_ttl_seconds=provider.cache_ttl_seconds,
        cache_stale_seconds=provider.cache_stale_seconds,
        rate_limit_cooldown_seconds=provider.rate_limit_cooldown_seconds,
        min_upstream_interval_seconds=provider.min_upstream_interval_seconds,
        allow_stale_on_error=provider.allow_stale_on_error,
    )


class _NoopProvider:
    last_discovery_notes: list[str] = []

    async def discover_candidates(self, exchanges, timeframe, sources, per_source_limit):
        self.last_discovery_notes = []
        return []

    async def combined_analysis(self, symbol, exchange, timeframe):  # pragma: no cover - defensive guard
        raise RuntimeError(f"provider should not be called while equity scan is paused: {symbol}:{exchange}")


def _equity_hours_state(config: BotConfig, *, now: datetime | None = None) -> MarketHoursState | None:
    if not config.execution.market_hours_only:
        return None
    return equity_market_hours_state(
        now=now,
        timezone_name=config.execution.market_timezone,
        open_time=config.execution.market_open,
        close_time=config.execution.market_close,
    )


def _item_is_crypto(item: WatchItem) -> bool:
    return infer_asset_class(item.exchange, item.asset_class) == "crypto"


def _ledger_items(ledger: PaperLedger) -> list[WatchItem]:
    return [
        WatchItem(
            position.symbol,
            position.exchange,
            str(position.metadata.get("asset_class") or "auto"),
            str(position.metadata.get("broker_symbol") or "") or None,
        )
        for position in ledger.list_positions()
    ]


def _has_crypto_work(config: BotConfig, ledger: PaperLedger) -> bool:
    candidates = [*_ledger_items(ledger), *build_static_candidate_universe(config, cap=False)]
    if any(_item_is_crypto(item) for item in candidates):
        return True
    if config.screener.enabled and config.screener.source in {"mcp", "hybrid"} and config.screener.dynamic_sources:
        return any(infer_asset_class(exchange) == "crypto" for exchange in config.screener.exchanges)
    return False


def _provider_pause_state(
    config: BotConfig,
    ledger: PaperLedger,
    *,
    now: datetime | None = None,
    pause_all_after_hours: bool = False,
) -> MarketHoursState | None:
    state = _equity_hours_state(config, now=now)
    if state is None or state.is_open:
        return None
    if pause_all_after_hours:
        return state
    if _has_crypto_work(config, ledger):
        return None
    return state


def _paused_scan_report(config: BotConfig, ledger: PaperLedger, state: MarketHoursState) -> str:
    return "\n".join(
        [
            "stonks-paper-bot scan",
            "Boundary: LOCAL PAPER ONLY — no broker orders, no live execution",
            f"Discovery: {state.reason}; provider scan paused; no MCP calls submitted",
            format_status(config, ledger, compact=True),
        ]
    )


async def _run_once(config_path: Path) -> str:
    config, broker = _build_broker_if_enabled(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        pause_state = _provider_pause_state(config, ledger)
        if pause_state is not None:
            return await run_once(config, ledger, _NoopProvider(), broker=broker, now=pause_state.local_now)
        async with _tradingview_provider(config) as provider:
            return await run_once(config, ledger, provider, broker=broker)
    finally:
        ledger.close()


async def _screen_once(config_path: Path) -> str:
    config = load_config(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        pause_state = _provider_pause_state(config, ledger)
        if pause_state is not None:
            return await screen_once(config, ledger, _NoopProvider(), now=pause_state.local_now)
        async with _tradingview_provider(config) as provider:
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
        async with _tradingview_provider(config) as provider:
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
        async with _tradingview_provider(config) as provider:
            return await options_paper_once(config, ledger, options_ledger, provider, client, broker=broker)
    finally:
        ledger.close()
        options_ledger.close()


async def _watch(config_path: Path) -> None:
    initial_config = load_config(config_path)
    if initial_config.execution.initial_delay_seconds > 0:
        print(
            f"stonks-paper-bot initial delay: {initial_config.execution.initial_delay_seconds}s before first scan",
            flush=True,
        )
        await asyncio.sleep(initial_config.execution.initial_delay_seconds)
    while True:
        config = load_config(config_path)
        try:
            print(await _run_once(config_path), flush=True)
        except Exception as exc:  # pragma: no cover - defensive around long-running service loop
            print(f"stonks-paper-bot scan failed: {type(exc).__name__}: {exc}", flush=True)
        if config.options.auto_trade:
            options_pause_state = _equity_hours_state(config)
            if options_pause_state is not None and not options_pause_state.is_open:
                print(f"stonks-paper-bot options auto-trade paused: {options_pause_state.reason}", flush=True)
            else:
                try:
                    print(await _options_paper(config_path), flush=True)
                except Exception as exc:  # pragma: no cover - defensive around long-running service loop
                    print(f"stonks-paper-bot options auto-trade failed: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(config.execution.scan_interval_seconds)


async def _farm_run_variant(variant: FarmVariant, *, now: datetime | None = None) -> str:
    config = load_config(variant.path)
    assert_shadow_safe(config, variant_name=variant.name)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    try:
        pause_state = _provider_pause_state(config, ledger, now=now)
        if pause_state is not None:
            result = _paused_scan_report(config, ledger, pause_state)
        else:
            async with _tradingview_provider(config) as provider:
                result = await run_once(config, ledger, provider, broker=None, now=now)
    finally:
        ledger.close()
    return f"=== farm variant: {variant.name} ({config.provider.timeframe}, {config.execution.scan_interval_seconds // 60}m) ===\n{result}"


async def _farm_run_once(farm_dir: Path) -> str:
    variants = [variant for variant in discover_farm_variants(farm_dir) if variant.enabled]
    if not variants:
        return f"No enabled strategy variants found in {farm_dir}"
    reports: list[str] = []
    for variant in variants:
        try:
            reports.append(await _farm_run_variant(variant))
        except Exception as exc:  # pragma: no cover - defensive around operator batch command
            reports.append(f"=== farm variant: {variant.name} ===\nERROR {type(exc).__name__}: {exc}")
    return "\n\n".join(reports)


async def _backtest_farm(bot_config_path: Path, research_config_path: Path) -> dict:
    bot_config = load_config(bot_config_path)
    research_config = load_backtest_farm_config(research_config_path)
    async with _tradingview_provider(bot_config) as provider:
        return await run_backtest_farm(research_config, provider)


async def _farm_watch(farm_dir: Path, poll_seconds: int) -> None:
    print(f"stonks-paper strategy farm starting: dir={farm_dir}, poll={poll_seconds}s", flush=True)
    next_due: dict[str, float] = {}
    while True:
        now = time.monotonic()
        variants = [variant for variant in discover_farm_variants(farm_dir) if variant.enabled]
        active_names = {variant.name for variant in variants}
        for stale_name in set(next_due) - active_names:
            next_due.pop(stale_name, None)
        for variant in variants:
            try:
                config = load_config(variant.path)
                assert_shadow_safe(config, variant_name=variant.name)
            except Exception as exc:
                print(f"strategy farm skipped {variant.name}: {type(exc).__name__}: {exc}", flush=True)
                next_due[variant.name] = now + max(poll_seconds, 60)
                continue
            if variant.name not in next_due:
                next_due[variant.name] = now + config.execution.initial_delay_seconds
            if now < next_due[variant.name]:
                continue
            pause_state = None
            ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
            try:
                pause_state = _provider_pause_state(config, ledger)
            finally:
                ledger.close()
            try:
                print(await _farm_run_variant(variant, now=pause_state.local_now if pause_state is not None else None), flush=True)
            except Exception as exc:  # pragma: no cover - defensive around long-running farm loop
                print(f"strategy farm scan failed for {variant.name}: {type(exc).__name__}: {exc}", flush=True)
            if pause_state is not None:
                sleep_until_open = seconds_until_next_open(pause_state)
                next_delay = (sleep_until_open + 5) if sleep_until_open is not None else config.execution.scan_interval_seconds
                next_due[variant.name] = time.monotonic() + max(poll_seconds, next_delay)
            else:
                next_due[variant.name] = time.monotonic() + config.execution.scan_interval_seconds
        await asyncio.sleep(max(1, poll_seconds))


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


def _broker_position_snapshots(raw_positions: list[dict], watchlist: list[WatchItem]) -> list[dict]:
    snapshots: list[dict] = []
    watch_by_symbol = {item.symbol: item for item in watchlist}
    watch_by_broker_symbol = {item.broker_symbol: item for item in watchlist if item.broker_symbol}
    for raw in raw_positions:
        raw_symbol = str(raw.get("symbol") or "").upper()
        watch_item = watch_by_symbol.get(raw_symbol) or watch_by_broker_symbol.get(raw_symbol)
        symbol = watch_item.symbol if watch_item is not None else raw_symbol
        quantity = _as_float(raw.get("qty", raw.get("quantity")), 0.0)
        entry_price = _as_float(raw.get("avg_entry_price", raw.get("entry_price")), 0.0)
        if not symbol or quantity == 0 or entry_price <= 0:
            continue
        last_price = _as_float(raw.get("current_price", raw.get("last_price")), entry_price)
        exchange = str(raw.get("exchange") or (watch_item.exchange if watch_item is not None else "NASDAQ")).upper()
        asset_class = watch_item.asset_class if watch_item is not None else ("crypto" if "/" in raw_symbol else "equity")
        broker_symbol = watch_item.broker_symbol if watch_item is not None else (raw_symbol if asset_class == "crypto" else None)
        metadata = {
            "broker_synced": True,
            "broker": "alpaca",
            "asset_id": raw.get("asset_id"),
            "asset_class": asset_class,
        }
        if broker_symbol and broker_symbol != symbol:
            metadata["broker_symbol"] = broker_symbol
        snapshots.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "quantity": quantity,
                "entry_price": entry_price,
                "last_price": last_price,
                "metadata": metadata,
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

    farm_status = sub.add_parser("farm-status", help="summarize all local-only strategy-farm variant ledgers")
    farm_status.add_argument("--farm-dir", type=Path, default=Path("configs/farm"))
    farm_status.add_argument("--include-disabled", action="store_true")
    farm_status.add_argument("--json", action="store_true")

    farm_run_once = sub.add_parser("farm-run-once", help="run every enabled local-only strategy-farm variant once, sequentially")
    farm_run_once.add_argument("--farm-dir", type=Path, default=Path("configs/farm"))

    farm_watch = sub.add_parser("farm-watch", help="run the local-only strategy farm scheduler forever")
    farm_watch.add_argument("--farm-dir", type=Path, default=Path("configs/farm"))
    farm_watch.add_argument("--poll-seconds", type=int, default=30)

    backtest_farm = sub.add_parser("backtest-farm", help="run a read-only TradingView MCP strategy backtest matrix")
    backtest_farm.add_argument("--config", type=Path, default=Path("config.paper.toml"), help="bot config used only for the MCP provider command")
    backtest_farm.add_argument("--research-config", type=Path, default=Path("configs/research/backtest-farm.toml"))
    backtest_farm.add_argument("--json", action="store_true")
    backtest_farm.add_argument("--dry-plan", action="store_true", help="print the parsed backtest matrix without running MCP calls")

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

    if args.command == "farm-status":
        status_data = collect_farm_status(args.farm_dir, include_disabled=args.include_disabled)
        if args.json:
            print(json.dumps(status_data, indent=2, sort_keys=True))
        else:
            print(format_farm_status(status_data))
        return 0

    if args.command == "farm-run-once":
        print(asyncio.run(_farm_run_once(args.farm_dir)))
        return 0

    if args.command == "farm-watch":
        try:
            asyncio.run(_farm_watch(args.farm_dir, args.poll_seconds))
        except KeyboardInterrupt:
            print("stopped")
        return 0

    if args.command == "backtest-farm":
        if args.dry_plan:
            research_config = load_backtest_farm_config(args.research_config)
            experiments = build_backtest_experiments(research_config)
            payload = config_to_jsonable(research_config)
            payload["boundary"] = "RESEARCH ONLY — dry plan; no MCP calls"
            payload["experiment_count"] = len(experiments)
            payload["preview"] = [
                {
                    "symbol": experiment.symbol,
                    "strategy": experiment.strategy,
                    "period": experiment.period,
                    "interval": experiment.interval,
                    "initial_capital": experiment.initial_capital,
                    "commission_pct": experiment.commission_pct,
                    "slippage_pct": experiment.slippage_pct,
                }
                for experiment in experiments[:25]
            ]
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        result = asyncio.run(_backtest_farm(args.config, args.research_config))
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
        else:
            print(format_backtest_report(result))
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

        snapshots = _broker_position_snapshots(raw_positions, config.watchlist)
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
