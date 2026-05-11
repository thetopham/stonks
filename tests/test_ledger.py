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


def test_reset_to_broker_snapshot_replaces_cash_positions_and_trades(tmp_path):
    ledger = PaperLedger(tmp_path / "ledger.sqlite3", starting_cash=10_000)
    ledger.buy("OLD", "NASDAQ", price=50.0, notional=1_000.0, reason="old local paper trade", metadata={"score": 80})

    ledger.reset_to_broker_snapshot(
        cash=1234.56,
        positions=[
            {
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "quantity": 2.5,
                "entry_price": 150.25,
                "last_price": 151.00,
                "metadata": {"broker_synced": True, "asset_id": "asset-aapl"},
            }
        ],
    )

    assert ledger.cash == 1234.56
    assert ledger.get_position("OLD") is None
    position = ledger.get_position("AAPL")
    assert position is not None
    assert position.quantity == 2.5
    assert position.entry_price == 150.25
    assert position.last_price == 151.00
    assert position.metadata["broker_synced"] is True
    assert position.metadata["asset_id"] == "asset-aapl"
    assert ledger.list_trades() == []
