from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import WatchItem, default_crypto_broker_symbol, infer_asset_class
from .screener import normalize_watch_item


@dataclass(slots=True)
class CacheRead:
    key: str
    arguments_json: str
    payload: dict[str, Any] | None
    fresh: bool = False
    stale: bool = False


class SQLiteToolCache:
    """Small cross-process SQLite cache for MCP tool results."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @staticmethod
    def canonical_arguments(arguments: dict[str, Any]) -> str:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def cache_key(cls, tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
        arguments_json = cls.canonical_arguments(arguments)
        digest = hashlib.sha256(f"{tool}\0{arguments_json}".encode("utf-8")).hexdigest()
        return digest, arguments_json

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tool_cache (
                    key TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    stale_until REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_tool_cache_tool ON mcp_tool_cache(tool)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tool_refresh_locks (
                    key TEXT PRIMARY KEY,
                    locked_until REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tool_cooldowns (
                    scope TEXT PRIMARY KEY,
                    cooldown_until REAL NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def get(self, tool: str, arguments: dict[str, Any], *, now: float | None = None) -> CacheRead:
        now = time.time() if now is None else now
        key, arguments_json = self.cache_key(tool, arguments)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json, expires_at, stale_until FROM mcp_tool_cache WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return CacheRead(key=key, arguments_json=arguments_json, payload=None)
        try:
            payload = json.loads(str(row["response_json"]))
        except json.JSONDecodeError:
            self.delete(key)
            return CacheRead(key=key, arguments_json=arguments_json, payload=None)
        return CacheRead(
            key=key,
            arguments_json=arguments_json,
            payload=payload,
            fresh=float(row["expires_at"]) >= now,
            stale=float(row["stale_until"]) >= now,
        )

    def set(
        self,
        tool: str,
        arguments_json: str,
        key: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
        stale_seconds: int,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        expires_at = now + max(0, int(ttl_seconds))
        stale_until = expires_at + max(0, int(stale_seconds))
        response_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_tool_cache (key, tool, arguments_json, response_json, created_at, expires_at, stale_until)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    tool = excluded.tool,
                    arguments_json = excluded.arguments_json,
                    response_json = excluded.response_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    stale_until = excluded.stale_until
                """,
                (key, tool, arguments_json, response_json, now, expires_at, stale_until),
            )

    def delete(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM mcp_tool_cache WHERE key = ?", (key,))

    def try_acquire_refresh_lock(self, key: str, *, lock_seconds: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        locked_until = now + max(1, int(lock_seconds))
        with self._connect() as conn:
            row = conn.execute("SELECT locked_until FROM mcp_tool_refresh_locks WHERE key = ?", (key,)).fetchone()
            if row is not None and float(row["locked_until"]) > now:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO mcp_tool_refresh_locks (key, locked_until) VALUES (?, ?)",
                (key, locked_until),
            )
            return True

    def release_refresh_lock(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM mcp_tool_refresh_locks WHERE key = ?", (key,))

    def set_cooldown(self, scope: str, *, seconds: int, reason: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cooldown_until = now + max(1, int(seconds))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_tool_cooldowns (scope, cooldown_until, reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    cooldown_until = excluded.cooldown_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (scope, cooldown_until, reason, now),
            )

    def cooldown_active(self, scope: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute("SELECT cooldown_until FROM mcp_tool_cooldowns WHERE scope = ?", (scope,)).fetchone()
        return row is not None and float(row["cooldown_until"]) > now


class TradingViewMCPProvider:
    def __init__(
        self,
        command: str,
        args: list[str],
        timeframe: str = "1d",
        *,
        cache_enabled: bool = False,
        cache_path: str | Path = "data/provider-cache.sqlite3",
        cache_ttl_seconds: int = 300,
        cache_stale_seconds: int = 21600,
        rate_limit_cooldown_seconds: int = 900,
        min_upstream_interval_seconds: int = 5,
        allow_stale_on_error: bool = True,
    ):
        self.command = command
        self.args = args
        self.timeframe = self._normalize_timeframe(timeframe)
        self.last_discovery_notes: list[str] = []
        self.last_cache_notes: list[str] = []
        self.cache_enabled = bool(cache_enabled)
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.cache_stale_seconds = max(0, int(cache_stale_seconds))
        self.rate_limit_cooldown_seconds = max(1, int(rate_limit_cooldown_seconds))
        self.min_upstream_interval_seconds = max(1, int(min_upstream_interval_seconds))
        self.allow_stale_on_error = bool(allow_stale_on_error)
        self._cache = SQLiteToolCache(cache_path) if self.cache_enabled else None
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

    @staticmethod
    def _timeframe_ttl_seconds(timeframe: object, *, fallback: int) -> int:
        normalized = TradingViewMCPProvider._normalize_timeframe(timeframe)
        if normalized in {"1m", "5m", "15m", "30m", "1h"}:
            return 900
        if normalized in {"2h", "4h"}:
            return 1800
        if normalized == "1d":
            return 21600
        if normalized in {"1W", "1M"}:
            return 43200
        return fallback

    def _ttl_for_tool(self, name: str, arguments: dict[str, Any]) -> int:
        if name == "yahoo_price":
            return 60
        if name in {"combined_analysis", "coin_analysis", "multi_timeframe_analysis", "volume_confirmation_analysis"}:
            return self._timeframe_ttl_seconds(arguments.get("timeframe", self.timeframe), fallback=self.cache_ttl_seconds)
        if name in {
            "top_gainers",
            "top_losers",
            "rating_filter",
            "bollinger_scan",
            "volume_breakout_scanner",
            "smart_volume_scanner",
            "consecutive_candles_scan",
            "advanced_candle_pattern",
        }:
            return self._timeframe_ttl_seconds(arguments.get("timeframe", self.timeframe), fallback=self.cache_ttl_seconds)
        if name in {"financial_news", "market_sentiment"}:
            return 3600
        if name in {"market_snapshot"}:
            return 300
        if name in {"backtest_strategy", "compare_strategies", "walk_forward_backtest_strategy"}:
            return 86400
        return self.cache_ttl_seconds

    @staticmethod
    def _looks_rate_limited(exc: Exception) -> bool:
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in ("429", "rate limit", "too many", "cloudfront", "jsondecodeerror", "expecting value"))

    async def _wait_for_cache_fill(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: int,
    ) -> CacheRead | None:
        if self._cache is None:
            return None
        deadline = time.time() + max(1, int(timeout_seconds))
        poll_seconds = min(0.25, max(0.05, timeout_seconds / 10))
        while time.time() < deadline:
            await asyncio.sleep(poll_seconds)
            cache_read = self._cache.get(name, arguments)
            if cache_read.payload is not None and (cache_read.fresh or cache_read.stale):
                return cache_read
        return None

    async def _call_tool_uncached(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        self.last_cache_notes = []
        cache_read: CacheRead | None = None
        refresh_lock_acquired = False

        if self._cache is not None:
            now = time.time()
            cache_read = self._cache.get(name, arguments, now=now)
            if cache_read.payload is not None and cache_read.fresh:
                self.last_cache_notes.append(f"MCP cache hit: {name}")
                return cache_read.payload
            if (
                cache_read.payload is not None
                and cache_read.stale
                and self.allow_stale_on_error
                and self._cache.cooldown_active("tradingview-upstream", now=now)
            ):
                self.last_cache_notes.append(f"MCP provider cooldown: serving stale {name}")
                return cache_read.payload

            refresh_lock_acquired = self._cache.try_acquire_refresh_lock(
                cache_read.key,
                lock_seconds=self.min_upstream_interval_seconds,
                now=now,
            )
            if not refresh_lock_acquired:
                if cache_read.payload is not None and cache_read.stale and self.allow_stale_on_error:
                    self.last_cache_notes.append(f"MCP cache refresh locked: serving stale {name}")
                    return cache_read.payload
                filled = await self._wait_for_cache_fill(
                    name,
                    arguments,
                    timeout_seconds=self.min_upstream_interval_seconds,
                )
                if filled is not None and filled.payload is not None:
                    self.last_cache_notes.append(f"MCP cache filled by peer: {name}")
                    return filled.payload
                raise RuntimeError(f"MCP cache refresh locked for {name} and no cached payload is available")
            if cache_read.payload is not None:
                self.last_cache_notes.append(f"MCP cache stale: refreshing {name}")
            else:
                self.last_cache_notes.append(f"MCP cache miss: refreshing {name}")

        try:
            payload = await self._call_tool_uncached(name, arguments)
        except Exception as exc:
            if self._cache is not None and cache_read is not None:
                if self._looks_rate_limited(exc):
                    self._cache.set_cooldown(
                        "tradingview-upstream",
                        seconds=self.rate_limit_cooldown_seconds,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    self.last_cache_notes.append(f"MCP provider cooldown started: {name}")
                if cache_read.payload is not None and cache_read.stale and self.allow_stale_on_error:
                    self.last_cache_notes.append(f"MCP degraded: serving stale {name} after {type(exc).__name__}")
                    return cache_read.payload
            raise
        else:
            if self._cache is not None and cache_read is not None:
                self._cache.set(
                    name,
                    cache_read.arguments_json,
                    cache_read.key,
                    payload,
                    ttl_seconds=self._ttl_for_tool(name, arguments),
                    stale_seconds=self.cache_stale_seconds,
                )
                self.last_cache_notes.append(f"MCP upstream refresh: {name}")
            return payload
        finally:
            if self._cache is not None and cache_read is not None and refresh_lock_acquired:
                self._cache.release_refresh_lock(cache_read.key)

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
