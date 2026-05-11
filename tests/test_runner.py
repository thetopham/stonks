import asyncio
from dataclasses import dataclass

from stonks_bot.config import BotConfig, BrokerConfig, ExecutionConfig, ProviderConfig, StrategyConfig, WatchItem
from stonks_bot.ledger import PaperLedger
from stonks_bot.runner import run_once


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


@dataclass(slots=True)
class FakeFill:
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: float
    price: float
    notional: float


class OpenFakeBroker:
    def __init__(self):
        self.buy_requests = []
        self.sell_requests = []

    def market_is_open(self):
        return True, "market open"

    def submit_buy(self, symbol, notional):
        self.buy_requests.append((symbol, notional))
        return FakeFill("buy-order-1", symbol, "buy", "filled", 10.0, 100.0, 1000.0)

    def submit_sell(self, symbol, quantity):
        self.sell_requests.append((symbol, quantity))
        return FakeFill("sell-order-1", symbol, "sell", "filled", quantity, 115.0, quantity * 115.0)


class ClosedFakeBroker:
    def __init__(self):
        self.buy_requests = []

    def market_is_open(self):
        return False, "market closed"

    def submit_buy(self, symbol, notional):
        self.buy_requests.append((symbol, notional))
        raise AssertionError("closed market should not submit orders")


def _config(tmp_path, *, submit_orders=False):
    return BotConfig(
        ledger_path=tmp_path / "ledger.sqlite3",
        starting_cash=10_000,
        max_position_pct=0.10,
        max_open_positions=5,
        commission_pct=0,
        slippage_pct=0,
        execution=ExecutionConfig(dry_run=True, live_trading_enabled=False, scan_interval_seconds=60),
        strategy=StrategyConfig(entry_score=65, exit_score=35, max_rsi_for_entry=70, stop_loss_pct=0.07, take_profit_pct=0.15),
        provider=ProviderConfig(command="fake", args=[], timeframe="1D"),
        broker=BrokerConfig(name="alpaca" if submit_orders else "none", submit_orders=submit_orders, paper_only=True),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ")],
    )


def test_run_once_executes_only_paper_buy_and_returns_report(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    report = asyncio.run(run_once(config, ledger, FakeProvider()))

    assert "PAPER BUY AAPL" in report
    assert ledger.get_position("AAPL") is not None
    assert ledger.cash == 9_000


def test_run_once_submits_alpaca_paper_buy_when_broker_orders_enabled(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = OpenFakeBroker()

    report = asyncio.run(run_once(config, ledger, FakeProvider(), broker=broker))

    assert broker.buy_requests == [("AAPL", 1000.0)]
    assert "ALPACA PAPER BUY AAPL" in report
    position = ledger.get_position("AAPL")
    assert position is not None
    assert position.quantity == 10.0
    assert position.entry_price == 100.0
    assert position.metadata["broker_order_id"] == "buy-order-1"
    assert ledger.cash == 9_000


def test_run_once_fails_closed_when_broker_orders_enabled_without_broker(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    try:
        asyncio.run(run_once(config, ledger, FakeProvider()))
    except ValueError as exc:
        assert "broker order submission is enabled but no broker executor was provided" in str(exc)
    else:
        raise AssertionError("expected submit_orders without broker executor to fail closed")


def test_run_once_does_not_submit_broker_buy_when_market_is_closed(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = ClosedFakeBroker()

    report = asyncio.run(run_once(config, ledger, FakeProvider(), broker=broker))

    assert broker.buy_requests == []
    assert "BROKER HOLD AAPL: market closed" in report
    assert ledger.get_position("AAPL") is None
    assert ledger.cash == 10_000
