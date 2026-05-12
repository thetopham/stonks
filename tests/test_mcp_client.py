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

def test_combined_analysis_falls_back_to_yahoo_crypto_pair_when_crypto_analysis_has_no_price():
    class FakeCryptoProvider(TradingViewMCPProvider):
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
                assert arguments["symbol"] == "BTC-USD"
                return {"symbol": arguments["symbol"], "price": 81_759.06}
            raise AssertionError(name)

    provider = FakeCryptoProvider()

    result = asyncio.run(provider.combined_analysis("BTCUSDT", "BINANCE", "1D"))

    assert result["technical"]["price_data"]["current_price"] == 81_759.06
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
        {"exchange": "NASDAQ", "timeframe": "1d", "rating": 3, "limit": 25},
    )
    assert TradingViewMCPProvider._tool_call_for_source("bollinger_squeeze", "NYSE", "1D", 10) == (
        "bollinger_scan",
        {"exchange": "NYSE", "timeframe": "1d", "bbw_threshold": 0.04, "limit": 10},
    )


class CachedFakeProvider(TradingViewMCPProvider):
    def __init__(self, cache_path, *, fail=False, ttl_seconds=3600):
        super().__init__(
            "fake",
            [],
            cache_enabled=True,
            cache_path=cache_path,
            cache_ttl_seconds=ttl_seconds,
            cache_stale_seconds=3600,
            rate_limit_cooldown_seconds=60,
        )
        self.fail = fail
        self.upstream_calls = []

    async def _call_tool_uncached(self, name, arguments):
        self.upstream_calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("TradingView upstream 429 rate limit")
        return {"name": name, "arguments": arguments, "sequence": len(self.upstream_calls)}


def test_call_tool_reuses_fresh_sqlite_cache_across_provider_instances(tmp_path):
    cache_path = tmp_path / "provider-cache.sqlite3"
    first = CachedFakeProvider(cache_path)
    second = CachedFakeProvider(cache_path)

    result1 = asyncio.run(first.call_tool("custom_tool", {"symbol": "AAPL", "timeframe": "4h"}))
    result2 = asyncio.run(second.call_tool("custom_tool", {"timeframe": "4h", "symbol": "AAPL"}))

    assert result1 == result2
    assert len(first.upstream_calls) == 1
    assert second.upstream_calls == []


def test_call_tool_serves_stale_cache_when_upstream_rate_limited(tmp_path):
    cache_path = tmp_path / "provider-cache.sqlite3"
    seed = CachedFakeProvider(cache_path, ttl_seconds=0)
    failing = CachedFakeProvider(cache_path, fail=True, ttl_seconds=0)

    seeded = asyncio.run(seed.call_tool("custom_tool", {"symbol": "AAPL"}))
    served = asyncio.run(failing.call_tool("custom_tool", {"symbol": "AAPL"}))

    assert served == seeded
    assert len(failing.upstream_calls) == 1
    assert any("serving stale" in note for note in failing.last_cache_notes)


def test_call_tool_waits_for_in_flight_cold_cache_refresh_instead_of_duplicate_upstream(tmp_path):
    cache_path = tmp_path / "provider-cache.sqlite3"
    upstream_calls = []

    class SlowCachedProvider(TradingViewMCPProvider):
        def __init__(self, label, started, release):
            super().__init__(
                "fake",
                [],
                cache_enabled=True,
                cache_path=cache_path,
                cache_ttl_seconds=3600,
                cache_stale_seconds=3600,
                min_upstream_interval_seconds=2,
            )
            self.label = label
            self.started = started
            self.release = release

        async def _call_tool_uncached(self, name, arguments):
            upstream_calls.append(self.label)
            self.started.set()
            await self.release.wait()
            return {"name": name, "arguments": arguments, "source": self.label}

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        first = SlowCachedProvider("first", started, release)
        second = SlowCachedProvider("second", started, release)

        first_task = asyncio.create_task(first.call_tool("custom_tool", {"symbol": "AAPL"}))
        await started.wait()
        second_task = asyncio.create_task(second.call_tool("custom_tool", {"symbol": "AAPL"}))
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.gather(first_task, second_task)

    first_result, second_result = asyncio.run(scenario())

    assert upstream_calls == ["first"]
    assert second_result == first_result
