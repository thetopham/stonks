from stonks_bot.ledger import PaperLedger


def test_buy_and_sell_paper_trade_records_realized_pnl(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite3", starting_cash=10_000)

    buy = ledger.buy("AAPL", "NASDAQ", price=100, notional=1_000, reason="test entry", metadata={})
    position = ledger.get_position("AAPL")

    assert buy.side == "BUY"
    assert buy.quantity == 10
    assert ledger.cash == 9_000
    assert position is not None
    assert position.entry_price == 100

    sell = ledger.sell("AAPL", price=110, reason="take profit", metadata={})

    assert sell.side == "SELL"
    assert sell.pnl_realized == 100
    assert ledger.cash == 10_100
    assert ledger.get_position("AAPL") is None
    assert [trade.side for trade in ledger.list_trades()] == ["BUY", "SELL"]


def test_refuses_duplicate_open_position(tmp_path):
    ledger = PaperLedger(tmp_path / "paper.sqlite3", starting_cash=10_000)
    ledger.buy("AAPL", "NASDAQ", price=100, notional=1_000, reason="entry", metadata={})

    try:
        ledger.buy("AAPL", "NASDAQ", price=101, notional=1_000, reason="duplicate", metadata={})
    except ValueError as exc:
        assert "already open" in str(exc)
    else:
        raise AssertionError("expected duplicate position to be refused")
