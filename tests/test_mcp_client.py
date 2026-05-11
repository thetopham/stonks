import asyncio

from stonks_bot.mcp_client import TradingViewMCPProvider
from stonks_bot.models import WatchItem


class FakeTradingViewProvider(TradingViewMCPProvider):
    def __init__(self):
        super().__init__("fake", [])
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "combined_analysis":
            return {
                "technical": {
                    "price_data": {"current_price": 0},
                    "timeframe_context": {"bias": "Unknown", "bias_reasons": []},
                    "rsi": {"value": None},
                    "macd": {"crossover": "Unknown"},
                    "sma": {"signals": []},
                    "ema": {"signals": []},
                }
            }
        if name == "yahoo_price":
            return {"symbol": arguments["symbol"], "price": 123.45}
        raise AssertionError(name)


def test_combined_analysis_falls_back_to_yahoo_price_when_analysis_has_no_price():
    provider = FakeTradingViewProvider()

    result = asyncio.run(provider.combined_analysis("SPY", "NYSE", "1D"))

    assert result["technical"]["price_data"]["current_price"] == 123.45
    assert [name for name, _ in provider.calls] == ["combined_analysis", "yahoo_price"]


def test_extract_watch_items_normalizes_common_mcp_payload_shapes():
    payload = {
        "result": {
            "result": [
                {"symbol": "NASDAQ:AAPL"},
                {"ticker": "MSFT", "exchange": "NASDAQ"},
                {"symbol": "NYSE:BRK.B"},
                {"symbol": "NYSE:ABR/PD"},
            ]
        }
    }

    items = TradingViewMCPProvider._extract_watch_items(payload, "NASDAQ")

    assert items == [WatchItem("AAPL", "NASDAQ"), WatchItem("MSFT", "NASDAQ"), WatchItem("BRK.B", "NYSE")]


def test_tool_call_for_source_maps_dynamic_sources_to_mcp_tools():
    assert TradingViewMCPProvider._tool_call_for_source("rating_strong_buy", "NASDAQ", "1D", 25) == (
        "rating_filter",
        {"exchange": "NASDAQ", "timeframe": "1D", "rating": 3, "limit": 25},
    )
    assert TradingViewMCPProvider._tool_call_for_source("bollinger_squeeze", "NYSE", "1D", 10) == (
        "bollinger_scan",
        {"exchange": "NYSE", "timeframe": "1D", "bbw_threshold": 0.04, "limit": 10},
    )
