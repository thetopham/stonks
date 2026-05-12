import asyncio
import json
from pathlib import Path

from stonks_bot.research import (
    BACKTEST_RESEARCH_BOUNDARY,
    BacktestFarmConfig,
    build_backtest_experiments,
    format_backtest_report,
    run_backtest_farm,
)


class FakeBacktestProvider:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        strategy_return = {
            "rsi": 4.2,
            "bollinger": 7.5,
            "macd": -1.0,
            "ema_cross": 1.5,
            "supertrend": 12.0,
            "donchian": 8.0,
        }[arguments["strategy"]]
        return {
            "symbol": arguments["symbol"],
            "strategy": arguments["strategy"],
            "period": arguments["period"],
            "interval": arguments["interval"],
            "metrics": {
                "total_return_pct": strategy_return,
                "sharpe_ratio": strategy_return / 4,
                "win_rate": 55.0,
                "max_drawdown_pct": -6.5,
            },
        }


def test_build_backtest_experiments_expands_strategy_and_market_variable_matrix():
    config = BacktestFarmConfig(
        symbols=["SPY", "BTC-USD"],
        strategies=["rsi", "supertrend"],
        periods=["1y", "2y"],
        intervals=["1d"],
        commission_pct_values=[0.05, 0.10],
        slippage_pct_values=[0.02],
        max_runs=5,
    )

    experiments = build_backtest_experiments(config)

    assert len(experiments) == 5
    assert experiments[0].symbol == "SPY"
    assert experiments[0].strategy == "rsi"
    assert experiments[0].period == "1y"
    assert experiments[0].interval == "1d"
    assert experiments[0].commission_pct == 0.05
    assert experiments[1].commission_pct == 0.10
    assert experiments[-1].symbol == "SPY"
    assert experiments[-1].strategy == "supertrend"


def test_run_backtest_farm_is_read_only_and_writes_jsonl_leaderboard(tmp_path):
    provider = FakeBacktestProvider()
    output_path = tmp_path / "backtests.jsonl"
    config = BacktestFarmConfig(
        symbols=["SPY"],
        strategies=["rsi", "bollinger", "supertrend"],
        periods=["1y"],
        intervals=["1d"],
        initial_capital_values=[10_000],
        commission_pct_values=[0.1],
        slippage_pct_values=[0.05],
        output_path=output_path,
    )

    result = asyncio.run(run_backtest_farm(config, provider))

    assert result["boundary"] == BACKTEST_RESEARCH_BOUNDARY
    assert result["experiment_count"] == 3
    assert [call[0] for call in provider.calls] == ["backtest_strategy", "backtest_strategy", "backtest_strategy"]
    assert provider.calls[0][1] == {
        "symbol": "SPY",
        "strategy": "rsi",
        "period": "1y",
        "initial_capital": 10000,
        "commission_pct": 0.1,
        "slippage_pct": 0.05,
        "interval": "1d",
        "include_trade_log": False,
        "include_equity_curve": False,
    }
    assert [row["strategy"] for row in result["leaderboard"]] == ["supertrend", "bollinger", "rsi"]
    assert output_path.exists()
    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert [row["strategy"] for row in rows] == ["rsi", "bollinger", "supertrend"]
    assert all(row["research_only"] is True for row in rows)


def test_run_backtest_farm_treats_tool_error_payload_as_failed_experiment(tmp_path):
    class ErrorProvider:
        async def call_tool(self, name, arguments):
            return {"error": "Not enough data"}

    config = BacktestFarmConfig(
        symbols=["SPY"],
        strategies=["rsi"],
        periods=["1mo"],
        intervals=["1d"],
        output_path=tmp_path / "errors.jsonl",
    )

    result = asyncio.run(run_backtest_farm(config, ErrorProvider()))

    assert result["completed_count"] == 0
    assert result["error_count"] == 1
    assert result["leaderboard"] == []
    assert result["errors"][0]["error"] == "Not enough data"


def test_format_backtest_report_shows_research_boundary_and_metrics():
    result = {
        "boundary": BACKTEST_RESEARCH_BOUNDARY,
        "experiment_count": 2,
        "output_path": "data/research/backtests.jsonl",
        "leaderboard": [
            {
                "rank": 1,
                "symbol": "SPY",
                "strategy": "supertrend",
                "period": "2y",
                "interval": "1d",
                "total_return_pct": 12.34,
                "sharpe_ratio": 1.7,
                "win_rate": 62.0,
                "max_drawdown_pct": -4.5,
            }
        ],
        "errors": [],
    }

    text = format_backtest_report(result)

    assert BACKTEST_RESEARCH_BOUNDARY in text
    assert "SPY" in text
    assert "supertrend" in text
    assert "+12.34%" in text
    assert "data/research/backtests.jsonl" in text
