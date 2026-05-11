import asyncio

from stonks_bot.mcp_client import TradingViewMCPProvider


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
