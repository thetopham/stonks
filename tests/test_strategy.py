from stonks_bot.models import Position
from stonks_bot.strategy import generate_signal, score_analysis


def bullish_analysis(price=100.0, rsi=58.0):
    return {
        "technical": {
            "price_data": {"current_price": price},
            "timeframe_context": {"bias": "Bullish", "bias_reasons": ["Golden Cross"]},
            "rsi": {"value": rsi, "signal": "Neutral"},
            "macd": {"crossover": "Bullish"},
            "sma": {"signals": ["Price above SMA50 (bullish)", "Price above SMA200 (long-term bullish)"]},
            "ema": {"signals": ["Price above EMA20 (short-term bullish)", "Golden Cross (EMA50 > EMA200)"]},
            "bollinger_bands": {"position": "Inside Bands"},
        },
        "sentiment": {"overall_sentiment": "positive"},
    }


def test_buy_signal_when_trend_is_bullish_and_not_overbought():
    signal = generate_signal(
        symbol="AAPL",
        exchange="NASDAQ",
        analysis=bullish_analysis(),
        position=None,
        entry_score=65,
        exit_score=35,
        max_rsi_for_entry=70,
        stop_loss_pct=0.07,
        take_profit_pct=0.15,
    )

    assert signal.action == "BUY"
    assert signal.score >= 65
    assert signal.price == 100.0
    assert any("Bullish trend bias" in reason for reason in signal.reasons)


def test_overbought_rsi_blocks_new_buy_even_with_high_score():
    signal = generate_signal(
        symbol="AAPL",
        exchange="NASDAQ",
        analysis=bullish_analysis(rsi=76.0),
        position=None,
        entry_score=65,
        exit_score=35,
        max_rsi_for_entry=70,
        stop_loss_pct=0.07,
        take_profit_pct=0.15,
    )

    assert signal.action == "HOLD"
    assert any("RSI 76.0 above entry cap" in reason for reason in signal.reasons)


def test_stop_loss_sell_overrides_otherwise_bullish_score():
    position = Position(symbol="AAPL", exchange="NASDAQ", quantity=10, entry_price=100, entry_time="t0")

    signal = generate_signal(
        symbol="AAPL",
        exchange="NASDAQ",
        analysis=bullish_analysis(price=92.0),
        position=position,
        entry_score=65,
        exit_score=35,
        max_rsi_for_entry=70,
        stop_loss_pct=0.07,
        take_profit_pct=0.15,
    )

    assert signal.action == "SELL"
    assert "Stop loss hit" in signal.reasons[0]


def test_score_analysis_returns_bounded_score_and_reason_list():
    score, reasons, price, rsi = score_analysis(bullish_analysis())

    assert 0 <= score <= 100
    assert price == 100.0
    assert rsi == 58.0
    assert reasons
