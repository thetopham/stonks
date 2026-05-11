from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class TradingViewMCPProvider:
    def __init__(self, command: str, args: list[str], timeframe: str = "1D"):
        self.command = command
        self.args = args
        self.timeframe = timeframe
        self._stdio_context = None
        self._session_context = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "TradingViewMCPProvider":
        params = StdioServerParameters(command=self.command, args=self.args)
        self._stdio_context = stdio_client(params)
        read, write = await self._stdio_context.__aenter__()
        self._session_context = ClientSession(read, write)
        self._session = await self._session_context.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(exc_type, exc, tb)
        if self._stdio_context is not None:
            await self._stdio_context.__aexit__(exc_type, exc, tb)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("TradingViewMCPProvider must be used as an async context manager")
        result = await self._session.call_tool(name, arguments)
        texts = []
        for content in result.content:
            text = getattr(content, "text", None)
            if text is not None:
                texts.append(text)
        raw = "\n".join(texts).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}

    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict[str, Any]:
        try:
            analysis = await self.call_tool("combined_analysis", {"symbol": symbol, "exchange": exchange, "timeframe": timeframe or self.timeframe})
        except Exception as exc:
            return await self._analysis_from_quote(symbol, f"combined_analysis failed; fell back to yahoo_price: {type(exc).__name__}: {exc}")

        if self._current_price(analysis) <= 0:
            quote_analysis = await self._analysis_from_quote(symbol, "combined_analysis returned no usable price; fell back to yahoo_price")
            technical = analysis.setdefault("technical", {})
            price_data = technical.setdefault("price_data", {})
            price_data["current_price"] = quote_analysis["technical"]["price_data"]["current_price"]
            analysis["provider_warning"] = quote_analysis["provider_warning"]
        return analysis

    @staticmethod
    def _current_price(analysis: dict[str, Any]) -> float:
        try:
            return float(analysis.get("technical", {}).get("price_data", {}).get("current_price") or analysis.get("price") or 0)
        except (TypeError, ValueError):
            return 0.0

    async def _analysis_from_quote(self, symbol: str, warning: str) -> dict[str, Any]:
        quote = await self.call_tool("yahoo_price", {"symbol": symbol})
        price = quote.get("price", 0)
        return {
            "provider_warning": warning,
            "technical": {
                "price_data": {"current_price": price},
                "timeframe_context": {"bias": "Unknown", "bias_reasons": []},
                "rsi": {"value": None},
                "macd": {"crossover": "Unknown"},
                "sma": {"signals": []},
                "ema": {"signals": []},
            },
        }
