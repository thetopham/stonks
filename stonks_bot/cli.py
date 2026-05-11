from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import shutil

from .config import load_config
from .ledger import PaperLedger
from .dashboard import serve_dashboard
from .mcp_client import TradingViewMCPProvider
from .runner import format_status, run_once


def _copy_example(destination: Path) -> None:
    source = Path(__file__).resolve().parent.parent / "config.example.toml"
    shutil.copyfile(source, destination)


async def _run_once(config_path: Path) -> str:
    config = load_config(config_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    async with TradingViewMCPProvider(config.provider.command, config.provider.args, config.provider.timeframe) as provider:
        return await run_once(config, ledger, provider)


async def _watch(config_path: Path) -> None:
    config = load_config(config_path)
    while True:
        print(await _run_once(config_path), flush=True)
        await asyncio.sleep(config.execution.scan_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run stock-market paper trader powered by TradingView MCP")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a local config file from config.example.toml")
    init.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    run = sub.add_parser("run-once", help="scan watchlist once and apply paper trades")
    run.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    status = sub.add_parser("status", help="print local paper ledger status without network calls")
    status.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    watch = sub.add_parser("watch", help="run autonomous dry-run scans forever until interrupted")
    watch.add_argument("--config", type=Path, default=Path("config.paper.toml"))

    dashboard = sub.add_parser("dashboard", help="serve the read-only paper ledger dashboard")
    dashboard.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8791)

    args = parser.parse_args(argv)

    if args.command == "init":
        if args.config.exists():
            raise SystemExit(f"refusing to overwrite existing {args.config}")
        _copy_example(args.config)
        print(f"wrote {args.config}; review watchlist and risk settings, then run stonks-paper run-once")
        return 0

    if args.command == "run-once":
        print(asyncio.run(_run_once(args.config)))
        return 0

    if args.command == "status":
        config = load_config(args.config)
        ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
        print(format_status(config, ledger))
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

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
