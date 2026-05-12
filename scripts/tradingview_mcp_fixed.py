"""Run tradingview-mcp-server with a small compatibility patch.

tradingview-mcp-server normalizes daily aliases to "1D", but tradingview-ta
expects the daily interval as lowercase "1d" and emits noisy warnings for
"1D" before defaulting back to daily. Patch the alias before importing the
server so daily strategy-farm variants stay warning-free.
"""

from __future__ import annotations

from tradingview_mcp.core.utils import validators

validators.ALLOWED_TIMEFRAMES.add("1d")
validators._TIMEFRAME_ALIASES["1d"] = "1d"

from tradingview_mcp.server import main  # noqa: E402


if __name__ == "__main__":
    main()
