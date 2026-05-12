from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import WatchItem, default_crypto_broker_symbol, infer_asset_class
from .screener import normalize_watch_item


class TradingViewMCPProvider:
    def __init__(self, command: str, args: list[str], timeframe: str = "1d"):
        self.command = command
        self.args = args
        self.timeframe = self._normalize_timeframe(timeframe)
        self.last_discovery_notes: list[str] = []
        self._stdio_context = None
        self._session_context = None
        self._session: ClientSession | None = None

    @staticmethod
    def _normalize_timeframe(value: object) -> str:
        text = str(value or "1d").strip() or "1d"
        lower = text.lower()
        if lower in {"1m", "5m", "15m", "30m", "1h", "2h", "4h"}:
            return lower
        if lower in {"1d", "d", "day", "daily"}:
            return "1d"
        if lower in {"1w", "w", "week", "weekly"}:
            return "1W"
        if lower in {"1mth", "1mo", "1mon", "month", "monthly"}:
            return "1M"
        if text == "1M":
            return "1M"
        return text

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

    @staticmethod
    def _tool_call_for_source(source: str, exchange: str, timeframe: str, limit: int) -> tuple[str, dict[str, Any]] | None:
        source = source.strip().lower()
        timeframe = TradingViewMCPProvider._normalize_timeframe(timeframe)
        if source == "top_gainers":
            return "top_gainers", {"exchange": exchange, "timeframe": timeframe, "limit": limit}
        if source == "top_losers":
            return "top_losers", {"exchange": exchange, "timeframe": timeframe, "limit": limit}
        if source == "bollinger_squeeze":
            return "bollinger_scan", {"exchange": exchange, "timeframe": timeframe, "bbw_threshold": 0.04, "limit": limit}
        if source == "rating_strong_buy":
            return "rating_filter", {"exchange": exchange, "timeframe": timeframe, "rating": 3, "limit": limit}
        if source == "rating_buy":
            return "rating_filter", {"exchange": exchange, "timeframe": timeframe, "rating": 2, "limit": limit}
        if source == "rating_weak_buy":
            return "rating_filter", {"exchange": exchange, "timeframe": timeframe, "rating": 1, "limit": limit}
        if source == "volume_breakout":
            return "volume_breakout_scanner", {
                "exchange": exchange,
                "timeframe": timeframe,
                "volume_multiplier": 2.0,
                "price_change_min": 3.0,
                "limit": limit,
            }
        if source == "smart_volume":
            return "smart_volume_scanner", {
                "exchange": exchange,
                "min_volume_ratio": 2.0,
                "min_price_change": 2.0,
                "rsi_range": "any",
                "limit": limit,
            }
        return None

    @classmethod
    def _extract_watch_items(cls, payload: Any, fallback_exchange: str) -> list[WatchItem]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                item = normalize_watch_item(payload, fallback_exchange)
                return [item] if item is not None else []

        if isinstance(payload, dict):
            for key in ("result", "results", "data", "rows", "stocks", "coins"):
                if key in payload:
                    return cls._extract_watch_items(payload[key], fallback_exchange)
            raw_symbol = payload.get("symbol") or payload.get("ticker")
            if raw_symbol:
                item = normalize_watch_item(str(raw_symbol), str(payload.get("exchange") or fallback_exchange))
                return [item] if item is not None else []
            return []

        if isinstance(payload, list):
            items: list[WatchItem] = []
            seen: set[str] = set()
            for entry in payload:
                for item in cls._extract_watch_items(entry, fallback_exchange):
                    if item.symbol not in seen:
                        seen.add(item.symbol)
                        items.append(item)
            return items

        return []

    async def discover_candidates(
        self,
        exchanges: list[str],
        timeframe: str,
        sources: list[str],
        per_source_limit: int,
    ) -> list[WatchItem]:
        """Use MCP screeners to discover broad-market candidates before deep scoring."""
        self.last_discovery_notes = []
        discovered: list[WatchItem] = []
        seen: set[str] = set()
        for exchange in exchanges:
            exchange = exchange.upper()
            for source in sources:
                tool_call = self._tool_call_for_source(source, exchange, timeframe or self.timeframe, per_source_limit)
                if tool_call is None:
                    self.last_discovery_notes.append(f"{exchange}:{source}=unknown-source")
                    continue
                tool_name, arguments = tool_call
                try:
                    payload = await self.call_tool(tool_name, arguments)
                except Exception as exc:
                    self.last_discovery_notes.append(f"{exchange}:{source}=failed:{type(exc).__name__}")
                    continue
                items = self._extract_watch_items(payload, exchange)
                self.last_discovery_notes.append(f"{exchange}:{source}={len(items)}")
                for item in items:
                    if item.symbol not in seen:
                        seen.add(item.symbol)
                        discovered.append(item)
        return discovered

    async def combined_analysis(self, symbol: str, exchange: str, timeframe: str) -> dict[str, Any]:
        timeframe = self._normalize_timeframe(timeframe or self.timeframe)
        try:
            analysis = await self.call_tool("combined_analysis", {"symbol": symbol, "exchange": exchange, "timeframe": timeframe})
        except Exception as exc:
            quote_symbol = self._quote_symbol(symbol, exchange)
            return await self._analysis_from_quote(quote_symbol, f"combined_analysis failed; fell back to yahoo_price: {type(exc).__name__}: {exc}")

        if self._current_price(analysis) <= 0:
            quote_symbol = self._quote_symbol(symbol, exchange)
            quote_analysis = await self._analysis_from_quote(quote_symbol, "combined_analysis returned no usable price; fell back to yahoo_price")
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

    @staticmethod
    def _quote_symbol(symbol: str, exchange: str) -> str:
        if infer_asset_class(exchange) == "crypto":
            broker_symbol = default_crypto_broker_symbol(symbol)
            if broker_symbol:
                return broker_symbol.replace("/", "-")
        return symbol

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
