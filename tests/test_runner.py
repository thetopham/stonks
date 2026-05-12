import asyncio
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from stonks_bot.config import BotConfig, BrokerConfig, ExecutionConfig, OptimizerConfig, ProviderConfig, ScreenerConfig, SelectionConfig, StrategyConfig, WatchItem
from stonks_bot.ledger import PaperLedger
from stonks_bot.runner import run_once, screen_once


MARKET_OPEN_NOW = datetime(2026, 5, 12, 12, 0, tzinfo=ZoneInfo("America/New_York"))
EQUITY_CLOSED_NOW = datetime(2026, 5, 12, 20, 30, tzinfo=ZoneInfo("America/New_York"))


def run_paper_once(config, ledger, provider, broker=None, *, now=MARKET_OPEN_NOW):
    return asyncio.run(run_once(config, ledger, provider, broker=broker, now=now))


def screen_paper_once(config, ledger, provider, *, now=MARKET_OPEN_NOW):
    return asyncio.run(screen_once(config, ledger, provider, now=now))


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


def _analysis(*, price=100.0, bias="Bullish", rsi=55.0, macd="Bullish", sma_signals=None, ema_signals=None):
    return {
        "technical": {
            "price_data": {"current_price": price},
            "timeframe_context": {"bias": bias, "bias_reasons": []},
            "rsi": {"value": rsi},
            "macd": {"crossover": macd},
            "sma": {"signals": list(sma_signals or [])},
            "ema": {"signals": list(ema_signals or [])},
        }
    }


class MappingProvider:
    def __init__(self, analyses):
        self.analyses = analyses
        self.calls = []

    async def combined_analysis(self, symbol, exchange, timeframe):
        self.calls.append(symbol)
        return self.analyses[symbol]


class DiscoveringProvider(MappingProvider):
    def __init__(self, analyses, discovered):
        super().__init__(analyses)
        self.discovered = discovered
        self.discovery_requests = []
        self.last_discovery_notes = []

    async def discover_candidates(self, exchanges, timeframe, sources, per_source_limit):
        self.discovery_requests.append((exchanges, timeframe, sources, per_source_limit))
        self.last_discovery_notes = ["NASDAQ:rating_buy=1"]
        return self.discovered


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

    def market_is_open(self, *, extended_hours=False):
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

    def market_is_open(self, *, extended_hours=False):
        return False, "market closed"

    def submit_buy(self, symbol, notional):
        self.buy_requests.append((symbol, notional))
        raise AssertionError("closed market should not submit orders")


class ExtendedHoursFakeBroker:
    def __init__(self):
        self.market_requests = []
        self.buy_requests = []
        self.sell_requests = []
        self.extended_buy_requests = []
        self.extended_sell_requests = []

    def market_is_open(self, *, extended_hours=False):
        self.market_requests.append(extended_hours)
        if extended_hours:
            return True, "24/5 extended-hours session"
        return False, "regular market closed"

    def submit_buy(self, symbol, notional):
        self.buy_requests.append((symbol, notional))
        raise AssertionError("extended-hours mode should not submit equity market orders")

    def submit_sell(self, symbol, quantity):
        self.sell_requests.append((symbol, quantity))
        raise AssertionError("extended-hours mode should not submit equity market orders")

    def submit_extended_hours_buy(self, symbol, notional, limit_price, time_in_force):
        self.extended_buy_requests.append((symbol, notional, limit_price, time_in_force))
        return FakeFill("extended-buy-1", symbol, "buy", "filled", 10.0, 101.0, 1010.0)

    def submit_extended_hours_sell(self, symbol, quantity, limit_price, time_in_force):
        self.extended_sell_requests.append((symbol, quantity, limit_price, time_in_force))
        return FakeFill("extended-sell-1", symbol, "sell", "filled", quantity, 89.0, quantity * 89.0)


class CryptoFakeBroker:
    def __init__(self):
        self.crypto_buy_requests = []
        self.crypto_sell_requests = []
        self.clock_checks = 0

    def market_is_open(self, *, extended_hours=False):
        self.clock_checks += 1
        return False, "equity market closed"

    def submit_crypto_buy(self, symbol, notional):
        self.crypto_buy_requests.append((symbol, notional))
        return FakeFill("crypto-buy-1", symbol, "buy", "filled", 0.02, 50_000.0, 1_000.0)

    def submit_crypto_sell(self, symbol, quantity):
        self.crypto_sell_requests.append((symbol, quantity))
        return FakeFill("crypto-sell-1", symbol, "sell", "filled", quantity, 55_000.0, quantity * 55_000.0)


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
        selection=SelectionConfig(mode="ranked", preview_top=5),
        optimizer=OptimizerConfig(enabled=True, cash_reserve_pct=0.05, max_new_buys_per_scan=3, min_position_notional=25.0),
        provider=ProviderConfig(command="fake", args=[], timeframe="1D"),
        broker=BrokerConfig(name="alpaca" if submit_orders else "none", submit_orders=submit_orders, paper_only=True),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ")],
    )


def test_ranked_selection_buys_later_higher_score_before_earlier_candidate(tmp_path):
    config = _config(tmp_path)
    config.max_open_positions = 1
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider(
        {
            "AAPL": _analysis(price=100, bias="Bullish", rsi=45, macd="Neutral"),
            "MSFT": _analysis(price=200, bias="Bullish", rsi=55, macd="Bullish", sma_signals=["Price above SMA50 (bullish)"]),
        }
    )

    report = run_paper_once(config, ledger, provider)

    assert provider.calls == ["AAPL", "MSFT"]
    assert "Ranked candidates:" in report
    assert "PAPER BUY MSFT" in report
    assert ledger.get_position("MSFT") is not None
    assert ledger.get_position("AAPL") is None


def test_mcp_screener_discovers_dynamic_candidates_before_deep_scoring(tmp_path):
    config = _config(tmp_path)
    config.screener = ScreenerConfig(
        enabled=True,
        source="mcp",
        universes=["watchlist"],
        exchanges=["NASDAQ"],
        dynamic_sources=["rating_buy"],
        per_source_limit=10,
        max_candidates=5,
        exclude_symbols=[],
    )
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = DiscoveringProvider(
        {
            "AAPL": _analysis(price=100, bias="Bearish", rsi=55, macd="Neutral"),
            "MSFT": _analysis(price=200, bias="Bullish", rsi=55, macd="Bullish", sma_signals=["Price above SMA50 (bullish)"]),
        },
        [WatchItem("NASDAQ:MSFT", "NASDAQ")],
    )

    report = screen_paper_once(config, ledger, provider)

    assert provider.discovery_requests == [(["NASDAQ"], "1D", ["rating_buy"], 10)]
    assert provider.calls == ["MSFT", "AAPL"]
    assert "Discovery: NASDAQ:rating_buy=1" in report
    assert report.index("MSFT") < report.index("AAPL")


def test_mcp_screener_prefers_dynamic_candidates_before_watchlist_when_capped(tmp_path):
    config = _config(tmp_path)
    config.screener = ScreenerConfig(
        enabled=True,
        source="mcp",
        universes=["watchlist"],
        exchanges=["NASDAQ"],
        dynamic_sources=["rating_buy"],
        per_source_limit=10,
        max_candidates=1,
        exclude_symbols=[],
    )
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = DiscoveringProvider(
        {"MSFT": _analysis(price=200, bias="Bullish", rsi=55, macd="Bullish")},
        [WatchItem("NASDAQ:MSFT", "NASDAQ")],
    )

    report = screen_paper_once(config, ledger, provider)

    assert provider.calls == ["MSFT"]
    assert "Candidates scored: 1" in report
    assert "MSFT" in report
    assert "AAPL" not in report


def test_screen_once_honors_zero_preview_limit(tmp_path):
    config = _config(tmp_path)
    config.selection.preview_top = 0
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider(
        {
            "AAPL": _analysis(price=100, bias="Bullish", rsi=45, macd="Neutral"),
            "MSFT": _analysis(price=200, bias="Bullish", rsi=55, macd="Bullish"),
        }
    )

    report = screen_paper_once(config, ledger, provider)

    assert "Candidates scored: 2" in report
    assert "#1" not in report
    assert "AAPL:NASDAQ" not in report
    assert "MSFT:NASDAQ" not in report


def test_optimizer_reserves_cash_and_caps_position_notional(tmp_path):
    config = _config(tmp_path)
    config.max_position_pct = 0.60
    config.max_open_positions = 5
    config.optimizer = OptimizerConfig(enabled=True, cash_reserve_pct=0.50, max_new_buys_per_scan=5, min_position_notional=25.0)
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider(
        {
            "AAPL": _analysis(price=100, bias="Bullish", rsi=55, macd="Bullish"),
            "MSFT": _analysis(price=100, bias="Bullish", rsi=55, macd="Bullish"),
        }
    )

    report = run_paper_once(config, ledger, provider)

    assert "PAPER BUY AAPL" in report
    assert "SKIP MSFT: optimizer cash reserve reached" in report
    assert ledger.cash == 5_000
    assert ledger.get_position("AAPL") is not None
    assert ledger.get_position("MSFT") is None


def test_screen_once_ranks_candidates_read_only_without_trades(tmp_path):
    config = _config(tmp_path)
    config.watchlist = [WatchItem(symbol="AAPL", exchange="NASDAQ"), WatchItem(symbol="MSFT", exchange="NASDAQ")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider(
        {
            "AAPL": _analysis(price=100, bias="Bullish", rsi=45, macd="Neutral"),
            "MSFT": _analysis(price=200, bias="Bullish", rsi=55, macd="Bullish", sma_signals=["Price above SMA50 (bullish)"]),
        }
    )

    report = screen_paper_once(config, ledger, provider)

    assert "stonks-paper-bot screener" in report
    assert report.index("MSFT") < report.index("AAPL")
    assert ledger.list_trades() == []
    assert ledger.list_positions() == []


def test_run_once_executes_only_paper_buy_and_returns_report(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    report = run_paper_once(config, ledger, FakeProvider())

    assert "PAPER BUY AAPL" in report
    assert ledger.get_position("AAPL") is not None
    assert ledger.cash == 9_000


def test_run_once_submits_alpaca_paper_buy_when_broker_orders_enabled(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = OpenFakeBroker()

    report = run_paper_once(config, ledger, FakeProvider(), broker=broker)

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
        run_paper_once(config, ledger, FakeProvider())
    except ValueError as exc:
        assert "broker order submission is enabled but no broker executor was provided" in str(exc)
    else:
        raise AssertionError("expected submit_orders without broker executor to fail closed")


def test_run_once_does_not_submit_broker_buy_when_market_is_closed(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = ClosedFakeBroker()

    report = run_paper_once(config, ledger, FakeProvider(), broker=broker)

    assert broker.buy_requests == []
    assert "BROKER HOLD AAPL: market closed" in report
    assert ledger.get_position("AAPL") is None
    assert ledger.cash == 10_000


def test_run_once_submits_equity_24_5_limit_order_when_extended_hours_enabled(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    config.broker.equity_extended_hours = True
    config.broker.equity_extended_hours_time_in_force = "day"
    config.slippage_pct = 1.0
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = ExtendedHoursFakeBroker()
    provider = MappingProvider({"AAPL": _analysis(price=100, bias="Bullish", rsi=55, macd="Bullish")})

    report = run_paper_once(config, ledger, provider, broker=broker)

    assert broker.market_requests == [True]
    assert broker.buy_requests == []
    assert broker.extended_buy_requests == [("AAPL", 1000.0, 101.0, "day")]
    assert "ALPACA PAPER 24/5 BUY AAPL" in report
    position = ledger.get_position("AAPL")
    assert position is not None
    assert position.metadata["broker_order_id"] == "extended-buy-1"


def test_run_once_submits_equity_24_5_limit_sell_when_extended_hours_enabled(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    config.broker.equity_extended_hours = True
    config.broker.equity_extended_hours_time_in_force = "day"
    config.slippage_pct = 1.0
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    ledger.buy("AAPL", "NASDAQ", price=100.0, notional=1000.0, reason="seed", metadata={"asset_class": "equity"})
    broker = ExtendedHoursFakeBroker()
    provider = MappingProvider({"AAPL": _analysis(price=90, bias="Bearish", rsi=45, macd="Bearish")})

    report = run_paper_once(config, ledger, provider, broker=broker)

    assert broker.market_requests == [True]
    assert broker.sell_requests == []
    assert broker.extended_sell_requests == [("AAPL", 10.0, 89.1, "day")]
    assert "ALPACA PAPER 24/5 SELL AAPL" in report
    assert ledger.get_position("AAPL") is None


def test_run_once_submits_crypto_paper_buy_with_alpaca_symbol_without_equity_clock(tmp_path):
    config = _config(tmp_path, submit_orders=True)
    config.watchlist = [WatchItem(symbol="BTCUSDT", exchange="BINANCE", asset_class="crypto", broker_symbol="BTC/USD")]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    broker = CryptoFakeBroker()
    provider = MappingProvider({"BTCUSDT": _analysis(price=50_000, bias="Bullish", rsi=55, macd="Bullish")})

    report = run_paper_once(config, ledger, provider, broker=broker)

    assert broker.clock_checks == 0
    assert broker.crypto_buy_requests == [("BTC/USD", 1000.0)]
    assert "ALPACA PAPER CRYPTO BUY BTCUSDT" in report
    position = ledger.get_position("BTCUSDT")
    assert position is not None
    assert position.exchange == "BINANCE"
    assert position.metadata["asset_class"] == "crypto"
    assert position.metadata["broker_symbol"] == "BTC/USD"


def test_run_once_after_equity_session_scores_only_crypto_and_pauses_stocks(tmp_path):
    config = _config(tmp_path)
    config.watchlist = [
        WatchItem(symbol="AAPL", exchange="NASDAQ"),
        WatchItem(symbol="BTCUSDT", exchange="BINANCE", asset_class="crypto", broker_symbol="BTC/USD"),
    ]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider({"BTCUSDT": _analysis(price=50_000, bias="Bullish", rsi=55, macd="Bullish")})

    report = run_paper_once(config, ledger, provider, now=EQUITY_CLOSED_NOW)

    assert provider.calls == ["BTCUSDT"]
    assert "Equity session closed" in report
    assert "paused 1 equity candidate" in report
    assert "AAPL" not in report
    assert "PAPER BUY BTCUSDT" in report
    assert ledger.get_position("AAPL") is None
    assert ledger.get_position("BTCUSDT") is not None


def test_screen_once_after_equity_session_scores_only_crypto_and_pauses_stocks(tmp_path):
    config = _config(tmp_path)
    config.watchlist = [
        WatchItem(symbol="AAPL", exchange="NASDAQ"),
        WatchItem(symbol="BTCUSDT", exchange="BINANCE", asset_class="crypto", broker_symbol="BTC/USD"),
    ]
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    provider = MappingProvider({"BTCUSDT": _analysis(price=50_000, bias="Bullish", rsi=55, macd="Bullish")})

    report = screen_paper_once(config, ledger, provider, now=EQUITY_CLOSED_NOW)

    assert provider.calls == ["BTCUSDT"]
    assert "Equity session closed" in report
    assert "paused 1 equity candidate" in report
    assert "Candidates scored: 1" in report
    assert "BTCUSDT:BINANCE" in report
    assert "AAPL" not in report
