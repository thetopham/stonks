from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Position


@dataclass(slots=True)
class Signal:
    symbol: str
    exchange: str
    action: str
    score: float
    price: float
    reasons: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    rsi: float | None = None


def _technical(analysis: dict[str, Any]) -> dict[str, Any]:
    technical = analysis.get("technical") if isinstance(analysis, dict) else None
    return technical if isinstance(technical, dict) else analysis


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _signals_from(section: dict[str, Any]) -> list[str]:
    signals = section.get("signals", []) if isinstance(section, dict) else []
    return [str(signal) for signal in signals if signal]


def score_analysis(analysis: dict[str, Any]) -> tuple[float, list[str], float, float | None]:
    technical = _technical(analysis)
    price_data = technical.get("price_data", {}) if isinstance(technical.get("price_data"), dict) else {}
    price = _as_float(
        price_data.get("current_price", price_data.get("close", analysis.get("price"))),
        0.0,
    ) or 0.0

    score = 50.0
    reasons: list[str] = []

    context = technical.get("timeframe_context", {}) if isinstance(technical.get("timeframe_context"), dict) else {}
    bias = str(context.get("bias", "")).lower()
    if "bullish" in bias:
        score += 20
        reasons.append("Bullish trend bias")
    elif "bearish" in bias:
        score -= 20
        reasons.append("Bearish trend bias")

    rsi_section = technical.get("rsi", {}) if isinstance(technical.get("rsi"), dict) else {}
    rsi = _as_float(rsi_section.get("value"))
    if rsi is not None:
        if rsi >= 70:
            score -= 20
            reasons.append(f"RSI {rsi:.1f} overbought")
        elif 50 <= rsi < 70:
            score += 5
            reasons.append(f"RSI {rsi:.1f} supports momentum")
        elif rsi <= 30:
            score += 5
            reasons.append(f"RSI {rsi:.1f} oversold bounce candidate")

    macd = technical.get("macd", {}) if isinstance(technical.get("macd"), dict) else {}
    crossover = str(macd.get("crossover", "")).lower()
    if "bullish" in crossover:
        score += 10
        reasons.append("Bullish MACD crossover")
    elif "bearish" in crossover:
        score -= 10
        reasons.append("Bearish MACD crossover")

    trend_signals = _signals_from(technical.get("sma", {})) + _signals_from(technical.get("ema", {}))
    bullish_count = sum(1 for signal in trend_signals if "bullish" in signal.lower() or "above" in signal.lower() or "golden" in signal.lower())
    bearish_count = sum(1 for signal in trend_signals if "bearish" in signal.lower() or "below" in signal.lower() or "death" in signal.lower())
    if bullish_count:
        add = min(20, bullish_count * 5)
        score += add
        reasons.append(f"{bullish_count} bullish moving-average signals")
    if bearish_count:
        sub = min(20, bearish_count * 5)
        score -= sub
        reasons.append(f"{bearish_count} bearish moving-average signals")

    bollinger = technical.get("bollinger_bands", {}) if isinstance(technical.get("bollinger_bands"), dict) else {}
    band_position = str(bollinger.get("position", "")).lower()
    if "above upper" in band_position:
        score -= 5
        reasons.append("Price extended above upper Bollinger Band")
    elif "below lower" in band_position:
        score += 5
        reasons.append("Price below lower Bollinger Band")

    sentiment = analysis.get("sentiment", {}) if isinstance(analysis.get("sentiment"), dict) else {}
    sentiment_text = str(sentiment.get("overall_sentiment", sentiment.get("sentiment", ""))).lower()
    if "positive" in sentiment_text or "bullish" in sentiment_text:
        score += 5
        reasons.append("Positive sentiment")
    elif "negative" in sentiment_text or "bearish" in sentiment_text:
        score -= 5
        reasons.append("Negative sentiment")

    score = max(0.0, min(100.0, score))
    if not reasons:
        reasons.append("No strong technical edge detected")
    return score, reasons, price, rsi


def generate_signal(
    symbol: str,
    exchange: str,
    analysis: dict[str, Any],
    position: Position | None,
    entry_score: float,
    exit_score: float,
    max_rsi_for_entry: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Signal:
    score, reasons, price, rsi = score_analysis(analysis)
    symbol = symbol.upper()
    exchange = exchange.upper()

    if price <= 0:
        return Signal(symbol, exchange, "HOLD", score, price, ["No usable current price"], analysis, rsi)

    if position is not None:
        if price <= position.entry_price * (1 - stop_loss_pct):
            return Signal(symbol, exchange, "SELL", score, price, [f"Stop loss hit: {price:.2f} <= {position.entry_price * (1 - stop_loss_pct):.2f}"] + reasons, analysis, rsi)
        if price >= position.entry_price * (1 + take_profit_pct):
            return Signal(symbol, exchange, "SELL", score, price, [f"Take profit hit: {price:.2f} >= {position.entry_price * (1 + take_profit_pct):.2f}"] + reasons, analysis, rsi)
        if score <= exit_score:
            return Signal(symbol, exchange, "SELL", score, price, [f"Score {score:.1f} <= exit score {exit_score:.1f}"] + reasons, analysis, rsi)
        return Signal(symbol, exchange, "HOLD", score, price, ["Position remains within risk rules"] + reasons, analysis, rsi)

    if rsi is not None and rsi > max_rsi_for_entry:
        return Signal(symbol, exchange, "HOLD", score, price, [f"RSI {rsi:.1f} above entry cap {max_rsi_for_entry:.1f}"] + reasons, analysis, rsi)
    if score >= entry_score:
        return Signal(symbol, exchange, "BUY", score, price, [f"Score {score:.1f} >= entry score {entry_score:.1f}"] + reasons, analysis, rsi)
    return Signal(symbol, exchange, "HOLD", score, price, [f"Score {score:.1f} below entry score {entry_score:.1f}"] + reasons, analysis, rsi)
