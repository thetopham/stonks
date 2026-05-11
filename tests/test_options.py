import asyncio
from datetime import date

from stonks_bot.config import (
    BotConfig,
    BrokerConfig,
    ExecutionConfig,
    OptimizerConfig,
    OptionsConfig,
    ProviderConfig,
    ScreenerConfig,
    SelectionConfig,
    StrategyConfig,
    WatchItem,
    load_config,
)
from stonks_bot.ledger import PaperLedger
from stonks_bot.options import (
    OptionContract,
    OptionQuote,
    OptionsPaperLedger,
    filter_option_candidates,
    format_options_status,
    options_paper_once,
    options_scan_once,
    sync_options_cash_from_account,
)


def _analysis(*, price=100.0, bias="Bullish", rsi=55.0, macd="Bullish", sma_signals=None, ema_signals=None):
    return {
        "technical": {
            "price_data": {"current_price": price},
            "timeframe_context": {"bias": bias, "bias_reasons": []},
            "rsi": {"value": rsi},
            "macd": {"crossover": macd},
            "sma": {"signals": list(sma_signals or ["Price above SMA50 (bullish)"])},
            "ema": {"signals": list(ema_signals or ["Price above EMA20 (short-term bullish)"])},
        }
    }


class FakeAnalysisProvider:
    def __init__(self, analyses):
        self.analyses = analyses
        self.calls = []

    async def combined_analysis(self, symbol, exchange, timeframe):
        self.calls.append((symbol, exchange, timeframe))
        return self.analyses[symbol]


class FakeOptionsClient:
    def __init__(self, contracts_by_key, quotes_by_symbol):
        self.contracts_by_key = contracts_by_key
        self.quotes_by_symbol = quotes_by_symbol
        self.contract_requests = []
        self.quote_requests = []

    def get_options_contracts(self, underlying_symbols, contract_type, expiration_date_gte, expiration_date_lte, strike_price_gte=None, strike_price_lte=None, limit=1000):
        self.contract_requests.append(
            {
                "underlying_symbols": underlying_symbols,
                "contract_type": contract_type,
                "expiration_date_gte": expiration_date_gte,
                "expiration_date_lte": expiration_date_lte,
                "strike_price_gte": strike_price_gte,
                "strike_price_lte": strike_price_lte,
                "limit": limit,
            }
        )
        return self.contracts_by_key.get((tuple(underlying_symbols), contract_type), [])

    def get_latest_option_quotes(self, symbols, feed="indicative"):
        self.quote_requests.append((list(symbols), feed))
        return {symbol: self.quotes_by_symbol[symbol] for symbol in symbols if symbol in self.quotes_by_symbol}


class FakeBrokerFill:
    def __init__(self, order_id, symbol, side, quantity, price):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.status = "filled"
        self.quantity = quantity
        self.price = price
        self.notional = quantity * price * 100


class FakeOptionsBroker:
    def __init__(self, *, market_open=True):
        self.market_open = market_open
        self.buy_orders = []
        self.sell_orders = []

    def market_is_open(self):
        return self.market_open, "market open" if self.market_open else "market closed"

    def submit_option_buy_to_open(self, symbol, *, contracts, limit_price):
        self.buy_orders.append((symbol, contracts, limit_price))
        return FakeBrokerFill("buy-order-1", symbol, "buy", contracts, limit_price - 0.05)

    def submit_option_sell_to_close(self, symbol, *, contracts, limit_price):
        self.sell_orders.append((symbol, contracts, limit_price))
        return FakeBrokerFill("sell-order-1", symbol, "sell", contracts, limit_price)


def _config(tmp_path, *, options=None, broker=None):
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
        screener=ScreenerConfig(enabled=False),
        optimizer=OptimizerConfig(enabled=True, cash_reserve_pct=0.05, max_new_buys_per_scan=3, min_position_notional=25.0),
        provider=ProviderConfig(command="fake", args=[], timeframe="1D"),
        broker=broker or BrokerConfig(name="alpaca", submit_orders=False, paper_only=True),
        options=options or OptionsConfig(enabled=True, underlying_symbols=["AAPL"], max_open_positions=2),
        watchlist=[WatchItem(symbol="AAPL", exchange="NASDAQ")],
    )


def _contract(symbol, *, underlying="AAPL", strike=105, expiration="2026-06-20", type="call", open_interest=500, tradable=True):
    return OptionContract(
        symbol=symbol,
        underlying_symbol=underlying,
        type=type,
        expiration_date=expiration,
        strike_price=float(strike),
        open_interest=float(open_interest),
        tradable=tradable,
    )


def test_load_config_reads_options_overlay_settings(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
ledger_path = "ledger.sqlite3"
starting_cash = 10000

[execution]
dry_run = true
live_trading_enabled = false

[options]
enabled = true
auto_trade = true
submit_orders = false
underlying_symbols = ["SPY", "AAPL"]
min_dte = 30
max_dte = 60
target_dte = 45
min_open_interest = 250
max_spread_pct = 0.18
max_contract_debit = 650
max_trade_risk_pct = 0.015
contracts_per_trade = 1
max_open_positions = 3
allow_calls = true
allow_puts = false
quote_feed = "indicative"
stop_loss_pct = 0.45
take_profit_pct = 0.60
min_exit_dte = 14

[[watchlist]]
symbol = "AAPL"
exchange = "NASDAQ"
"""
    )

    config = load_config(cfg_path)

    assert config.options.enabled is True
    assert config.options.auto_trade is True
    assert config.options.submit_orders is False
    assert config.options.underlying_symbols == ["SPY", "AAPL"]
    assert config.options.min_dte == 30
    assert config.options.max_dte == 60
    assert config.options.target_dte == 45
    assert config.options.min_open_interest == 250
    assert config.options.max_spread_pct == 0.18
    assert config.options.max_contract_debit == 650
    assert config.options.max_trade_risk_pct == 0.015
    assert config.options.max_open_positions == 3
    assert config.options.allow_calls is True
    assert config.options.allow_puts is False


def test_filter_option_candidates_rejects_wide_spreads_and_low_open_interest():
    options = OptionsConfig(
        enabled=True,
        min_dte=30,
        max_dte=60,
        target_dte=45,
        min_open_interest=100,
        max_spread_pct=0.20,
        max_contract_debit=750,
    )
    contracts = [
        _contract("AAPL260620C00105000", expiration="2026-06-20", strike=105, open_interest=400),
        _contract("AAPL260620C00110000", expiration="2026-06-20", strike=110, open_interest=400),
        _contract("AAPL260620C00115000", expiration="2026-06-20", strike=115, open_interest=10),
    ]
    quotes = {
        "AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=5.10, ask=5.30),
        "AAPL260620C00110000": OptionQuote(symbol="AAPL260620C00110000", bid=3.00, ask=4.50),
        "AAPL260620C00115000": OptionQuote(symbol="AAPL260620C00115000", bid=1.00, ask=1.10),
    }

    candidates = filter_option_candidates(
        "AAPL",
        "call",
        underlying_price=100,
        signal_score=82,
        contracts=contracts,
        quotes=quotes,
        config=options,
        today=date(2026, 5, 11),
    )

    assert [candidate.contract_symbol for candidate in candidates] == ["AAPL260620C00105000"]
    assert candidates[0].max_loss == 530.0
    assert candidates[0].spread_pct < 0.05
    assert "long call" in candidates[0].strategy


def test_options_scan_is_read_only_and_does_not_write_positions(tmp_path):
    config = _config(tmp_path)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    analysis = FakeAnalysisProvider({"AAPL": _analysis(price=100)})
    option_client = FakeOptionsClient(
        {(('AAPL',), "call"): [_contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)]},
        {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=5.10, ask=5.30)},
    )

    report = asyncio.run(options_scan_once(config, ledger, options_ledger, analysis, option_client, today=date(2026, 5, 11)))

    assert "Boundary: READ-ONLY OPTIONS SCAN" in report
    assert "AAPL long call AAPL260620C00105000" in report
    assert options_ledger.list_positions() == []
    assert option_client.contract_requests[0]["underlying_symbols"] == ["AAPL"]
    assert option_client.quote_requests == [(["AAPL260620C00105000"], "indicative")]


def test_options_paper_opens_local_defined_risk_position_without_broker_orders(tmp_path):
    config = _config(tmp_path, options=OptionsConfig(enabled=True, underlying_symbols=["AAPL"], max_trade_risk_pct=0.01, max_open_positions=2))
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    analysis = FakeAnalysisProvider({"AAPL": _analysis(price=100)})
    option_client = FakeOptionsClient(
        {(('AAPL',), "call"): [_contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)]},
        {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
    )

    report = asyncio.run(options_paper_once(config, ledger, options_ledger, analysis, option_client, today=date(2026, 5, 11)))

    assert "Boundary: LOCAL OPTIONS PAPER ONLY" in report
    assert "PAPER OPTIONS BUY_TO_OPEN AAPL" in report
    position = options_ledger.get_position("AAPL260620C00105000")
    assert position is not None
    assert position.underlying_symbol == "AAPL"
    assert position.contracts == 1
    assert position.entry_debit == 100.0
    assert options_ledger.cash == 9900.0


def test_options_ledger_can_sync_cash_to_alpaca_buying_power(tmp_path):
    config = _config(tmp_path)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    options_ledger.sync_cash(61_164.94, source="alpaca_buying_power")

    assert options_ledger.cash == 61_164.94
    assert options_ledger.cash_source == "alpaca_buying_power"
    status = format_options_status(config, options_ledger)
    assert "cash_source=alpaca_buying_power" in status


def test_sync_options_cash_prefers_alpaca_options_buying_power(tmp_path):
    config = _config(tmp_path)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    synced_cash = sync_options_cash_from_account(
        options_ledger,
        {
            "buying_power": "126337.95",
            "non_marginable_buying_power": "61168.55",
            "options_buying_power": "63168.55",
        },
    )

    assert synced_cash == 63_168.55
    assert options_ledger.cash == 63_168.55
    assert options_ledger.cash_source == "alpaca_options_buying_power"


def test_options_paper_uses_current_options_cash_for_risk_cap(tmp_path):
    config = _config(tmp_path, options=OptionsConfig(enabled=True, underlying_symbols=["AAPL"], max_trade_risk_pct=0.01, max_open_positions=2))
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger.sync_cash(5_000.0, source="alpaca_buying_power")
    analysis = FakeAnalysisProvider({"AAPL": _analysis(price=100)})
    option_client = FakeOptionsClient(
        {(("AAPL",), "call"): [_contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)]},
        {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
    )

    report = asyncio.run(options_paper_once(config, ledger, options_ledger, analysis, option_client, today=date(2026, 5, 11)))

    assert "risk_cap=50.00" in report
    assert "PAPER OPTIONS BUY_TO_OPEN" not in report
    assert options_ledger.list_positions() == []
    assert options_ledger.cash == 5_000.0


def test_options_paper_submits_alpaca_paper_buy_order_when_enabled(tmp_path):
    options = OptionsConfig(enabled=True, submit_orders=True, underlying_symbols=["AAPL"], max_trade_risk_pct=0.01, max_open_positions=2)
    broker_config = BrokerConfig(name="alpaca", submit_orders=True, paper_only=True)
    config = _config(tmp_path, options=options, broker=broker_config)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    analysis = FakeAnalysisProvider({"AAPL": _analysis(price=100)})
    option_client = FakeOptionsClient(
        {(('AAPL',), "call"): [_contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)]},
        {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
    )
    broker = FakeOptionsBroker()

    report = asyncio.run(options_paper_once(config, ledger, options_ledger, analysis, option_client, broker=broker, today=date(2026, 5, 11)))

    assert "Boundary: ALPACA PAPER OPTIONS ORDERS ENABLED" in report
    assert "ALPACA PAPER OPTIONS BUY_TO_OPEN AAPL" in report
    assert broker.buy_orders == [("AAPL260620C00105000", 1, 1.00)]
    position = options_ledger.get_position("AAPL260620C00105000")
    assert position is not None
    assert position.entry_price == 0.95
    assert position.entry_debit == 95.0
    assert position.metadata["broker_order_id"] == "buy-order-1"
    assert options_ledger.cash == 9905.0


def test_options_paper_submission_mode_requires_broker_executor(tmp_path):
    options = OptionsConfig(enabled=True, submit_orders=True, underlying_symbols=["AAPL"], max_trade_risk_pct=0.01, max_open_positions=2)
    broker_config = BrokerConfig(name="alpaca", submit_orders=True, paper_only=True)
    config = _config(tmp_path, options=options, broker=broker_config)
    ledger = PaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)

    try:
        asyncio.run(options_paper_once(config, ledger, options_ledger, FakeAnalysisProvider({"AAPL": _analysis(price=100)}), FakeOptionsClient({}, {}), today=date(2026, 5, 11)))
    except ValueError as exc:
        assert "no Alpaca paper options broker executor" in str(exc)
    else:
        raise AssertionError("expected options broker executor to be required")


def test_options_paper_closes_position_on_profit_target(tmp_path):
    config = _config(tmp_path, options=OptionsConfig(enabled=True, underlying_symbols=["AAPL"], take_profit_pct=0.50, max_open_positions=2))
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    candidate_contract = _contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)
    option_client = FakeOptionsClient({}, {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=1.55, ask=1.65)})
    candidates = filter_option_candidates(
        "AAPL",
        "call",
        underlying_price=100,
        signal_score=80,
        contracts=[candidate_contract],
        quotes={"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
        config=config.options,
        today=date(2026, 5, 11),
    )
    options_ledger.buy_to_open(candidates[0], reason="test entry", metadata={"score": 80})

    report = asyncio.run(options_paper_once(config, PaperLedger(config.ledger_path, starting_cash=config.starting_cash), options_ledger, FakeAnalysisProvider({"AAPL": _analysis(price=100)}), option_client, today=date(2026, 5, 12)))

    assert "PAPER OPTIONS SELL_TO_CLOSE AAPL260620C00105000" in report
    assert options_ledger.get_position("AAPL260620C00105000") is None
    assert options_ledger.cash == 10055.0
    assert "realized_pnl=55.00" in report


def test_options_paper_submits_alpaca_paper_sell_order_on_exit_when_enabled(tmp_path):
    options = OptionsConfig(enabled=True, submit_orders=True, underlying_symbols=["AAPL"], take_profit_pct=0.50, max_open_positions=2)
    broker_config = BrokerConfig(name="alpaca", submit_orders=True, paper_only=True)
    config = _config(tmp_path, options=options, broker=broker_config)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    candidate_contract = _contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)
    candidates = filter_option_candidates(
        "AAPL",
        "call",
        underlying_price=100,
        signal_score=80,
        contracts=[candidate_contract],
        quotes={"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
        config=config.options,
        today=date(2026, 5, 11),
    )
    options_ledger.buy_to_open(candidates[0], reason="test entry", metadata={"score": 80})
    option_client = FakeOptionsClient({}, {"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=1.55, ask=1.65)})
    broker = FakeOptionsBroker()

    report = asyncio.run(options_paper_once(config, PaperLedger(config.ledger_path, starting_cash=config.starting_cash), options_ledger, FakeAnalysisProvider({"AAPL": _analysis(price=100)}), option_client, broker=broker, today=date(2026, 5, 12)))

    assert "ALPACA PAPER OPTIONS SELL_TO_CLOSE AAPL260620C00105000" in report
    assert broker.sell_orders == [("AAPL260620C00105000", 1, 1.55)]
    assert options_ledger.get_position("AAPL260620C00105000") is None
    close_trade = options_ledger.list_trades()[-1]
    assert close_trade.metadata["broker_order_id"] == "sell-order-1"
    assert close_trade.pnl_realized == 55.0


def test_format_options_status_reports_defined_risk_position(tmp_path):
    config = _config(tmp_path)
    options_ledger = OptionsPaperLedger(config.ledger_path, starting_cash=config.starting_cash)
    candidates = filter_option_candidates(
        "AAPL",
        "call",
        underlying_price=100,
        signal_score=82,
        contracts=[_contract("AAPL260620C00105000", expiration="2026-06-20", strike=105)],
        quotes={"AAPL260620C00105000": OptionQuote(symbol="AAPL260620C00105000", bid=0.95, ask=1.00)},
        config=config.options,
        today=date(2026, 5, 11),
    )
    options_ledger.buy_to_open(candidates[0], reason="test entry", metadata={"score": 82})

    status = format_options_status(config, options_ledger)

    assert "Boundary: LOCAL OPTIONS PAPER ONLY" in status
    assert "OPTIONS PAPER OPEN AAPL" in status
    assert "max_loss=100.00" in status
    assert "entry_debit=100.00" in status
