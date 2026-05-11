import asyncio

from stonks_bot.config import BotConfig, ExecutionConfig, ProviderConfig, StrategyConfig, WatchItem
from stonks_bot.ledger import PaperLedger
from stonks_bot.runner import run_once
from stonks_bot.strategy import Signal


class FakeProvider:
    async def combined_analysis(self, symbol, exchange, timeframe):
        return {
            "technical": {
                "price_data": {"current_price": 100.0},
                "timeframe_context": {"bias": "Bullish", "bias_reasons": []},
                "rsi": {"value": 55.0},
                "macd": {"crossover": "Bullish"},
                "sma": {"signals": ["Price above SMA50 (bullish)"]},
                "ema": {"signals": ["Price above EMA20 (short-term bullish)"]},
            }
        }


def test_run_once_executes_only_paper_buy_and_returns_report(tmp_path):
    config = BotConfig(
        ledger_path=tmp_path / "ledger.sqlite3",
        starting_cash=10_000,
        max_position_pct=0.10,
        max_open_positions=5,
        commission_pct=0,
        slippage_pct=0,
        execution=ExecutionConfig(dry_run=True, live_trading_enabled=False, scan_interval_seconds=60),
        strategy=StrategyConfig(entry_score=65, exit_score=35, max_rsi_for_entry=70, stop_loss_pct=0.07, take_profit_pct=0.15),
        provider=ProviderConfig(command="fake", args=[], timeframe="1D"),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ")],
    )
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    report = asyncio.run(run_once(config, ledger, FakeProvider()))

    assert "PAPER BUY AAPL" in report
    assert ledger.get_position("AAPL") is not None
    assert ledger.cash == 9_000
