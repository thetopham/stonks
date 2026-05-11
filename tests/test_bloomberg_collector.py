from __future__ import annotations

import json
from pathlib import Path

from stonks_bot.bloomberg_collector import collect, parse_rss_items, parse_stooq_csv_quotes, render_markdown, write_outputs
from stonks_bot.ledger import PaperLedger


def make_wiki(root: Path) -> Path:
    wiki = root / "wiki"
    (wiki / "daily").mkdir(parents=True)
    (wiki / "projects").mkdir()
    (wiki / "concepts").mkdir()
    (wiki / "SCHEMA.md").write_text("# Schema\n\n## Core Rules\nKeep notes concise.\n", encoding="utf-8")
    (wiki / "index.md").write_text(
        "# Wiki Index\n\n"
        "## Daily Notes\n- [[2026-05-11]]\n\n"
        "## Projects\n- [[hermes-bloomberg-terminal]]\n- [[ai-trading-automation]]\n\n"
        "## Concepts\n- [[llm-wiki-exocortex]]\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text(
        "## 2026-05-11\n\n"
        "### Changed\n"
        "- Created or updated: `projects/hermes-bloomberg-terminal.md`\n"
        "- Created or updated: `daily/2026-05-11.md`\n\n"
        "### Notes\n"
        "- Captured the personal Bloomberg terminal direction.\n",
        encoding="utf-8",
    )
    (wiki / "daily" / "2026-05-11.md").write_text(
        "# 2026-05-11 Daily Note\n\n"
        "**Type**: daily-note\n\n"
        "## Summary\n"
        "Hermes should combine daily wiki ingestion with paper bot state.\n\n"
        "## Tasks\n"
        "- Implement the first read-only collector.\n\n"
        "## Related Pages\n"
        "- [[hermes-bloomberg-terminal]]\n",
        encoding="utf-8",
    )
    (wiki / "projects" / "hermes-bloomberg-terminal.md").write_text(
        "# Hermes Bloomberg Terminal\n\n"
        "**Type**: project\n\n"
        "## Summary\n"
        "A personal analyst loop over wiki memory, paper bot records, and market data.\n\n"
        "## Product Shape\n"
        "Read-only Markdown brief first; dashboard later.\n\n"
        "## Open Questions\n"
        "- What moved in the watchlist today?\n\n"
        "## Related Pages\n"
        "- [[ai-trading-automation]]\n- [[llm-wiki-exocortex]]\n",
        encoding="utf-8",
    )
    (wiki / "projects" / "ai-trading-automation.md").write_text(
        "# AI Trading Automation\n\n## Summary\nPaper trading and broker experiments.\n",
        encoding="utf-8",
    )
    (wiki / "concepts" / "llm-wiki-exocortex.md").write_text(
        "# LLM Wiki Exocortex\n\n## Summary\nDurable local memory compounds over time.\n",
        encoding="utf-8",
    )
    return wiki


def make_bot(root: Path) -> tuple[Path, Path]:
    bot = root / "stonks-paper-bot"
    (bot / "data").mkdir(parents=True)
    config = bot / "config.paper.toml"
    config.write_text(
        "ledger_path = \"data/paper-ledger.sqlite3\"\n"
        "starting_cash = 100000.0\n"
        "max_position_pct = 0.10\n"
        "max_open_positions = 5\n"
        "commission_pct = 0.0\n"
        "slippage_pct = 0.02\n\n"
        "[execution]\n"
        "dry_run = true\n"
        "live_trading_enabled = false\n"
        "scan_interval_seconds = 900\n\n"
        "[strategy]\n"
        "entry_score = 65\n"
        "exit_score = 35\n"
        "max_rsi_for_entry = 70\n"
        "stop_loss_pct = 0.07\n"
        "take_profit_pct = 0.15\n\n"
        "[provider]\n"
        "command = \"fake\"\n"
        "args = []\n"
        "timeframe = \"1D\"\n\n"
        "[[watchlist]]\n"
        "symbol = \"AAPL\"\n"
        "exchange = \"NASDAQ\"\n\n"
        "[[watchlist]]\n"
        "symbol = \"NVDA\"\n"
        "exchange = \"NASDAQ\"\n",
        encoding="utf-8",
    )
    ledger = PaperLedger(bot / "data" / "paper-ledger.sqlite3", starting_cash=100000.0)
    ledger.buy("AAPL", "NASDAQ", price=100.0, notional=10000.0, reason="test entry", metadata={"score": 72})
    ledger.mark_price("AAPL", 108.0)
    ledger.close()
    return bot, config


def test_collects_wiki_and_paper_context_without_network_or_writes(tmp_path):
    wiki = make_wiki(tmp_path)
    bot, config = make_bot(tmp_path)

    snapshot = collect(
        wiki_path=wiki,
        bot_root=bot,
        config_path=config,
        include_quotes=False,
        check_services=False,
    )

    assert "READ ONLY" in snapshot["boundary"]
    assert snapshot["wiki"]["path"] == str(wiki)
    assert snapshot["wiki"]["counts"] == {"daily": 1, "projects": 2, "concepts": 1}
    assert snapshot["wiki"]["priority_pages"][0]["path"] == "projects/hermes-bloomberg-terminal.md"
    assert "personal analyst loop" in snapshot["wiki"]["priority_pages"][0]["summary"]
    assert snapshot["paper_bot"]["portfolio"]["open_positions"] == 1
    assert snapshot["paper_bot"]["portfolio"]["watchlist_count"] == 2
    assert snapshot["paper_bot"]["positions"][0]["symbol"] == "AAPL"
    assert snapshot["paper_bot"]["recent_trades"][0]["side"] == "BUY"
    assert snapshot["market_snapshot"]["enabled"] is False
    assert snapshot["news_snapshot"]["enabled"] is False
    assert any("no live trading" in note.lower() for note in snapshot["collector_notes"])


def test_render_markdown_includes_terminal_questions_and_boundaries(tmp_path):
    wiki = make_wiki(tmp_path)
    bot, config = make_bot(tmp_path)
    snapshot = collect(wiki_path=wiki, bot_root=bot, config_path=config, include_quotes=False, check_services=False)

    markdown = render_markdown(snapshot)

    assert "# Hermes Bloomberg Terminal Collector Snapshot" in markdown
    assert "What moved?" in markdown
    assert "PAPER ONLY" in markdown
    assert "READ ONLY" in markdown
    assert "AAPL" in markdown
    assert "projects/hermes-bloomberg-terminal.md" in markdown


def test_write_outputs_persists_latest_json_and_markdown(tmp_path):
    wiki = make_wiki(tmp_path)
    bot, config = make_bot(tmp_path)
    snapshot = collect(wiki_path=wiki, bot_root=bot, config_path=config, include_quotes=False, check_services=False)

    paths = write_outputs(snapshot, tmp_path / "out")

    assert paths["latest_json"].exists()
    assert paths["latest_markdown"].exists()
    assert json.loads(paths["latest_json"].read_text(encoding="utf-8"))["boundary"] == snapshot["boundary"]
    assert "Hermes Bloomberg Terminal Collector Snapshot" in paths["latest_markdown"].read_text(encoding="utf-8")


def test_parse_stooq_csv_quotes_normalizes_public_quote_rows():
    csv_text = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\r\n"
        "AAPL.US,2026-05-11,18:14:09,100,110,90,105,123456\r\n"
    )

    quotes = parse_stooq_csv_quotes(csv_text)

    assert quotes == [
        {
            "symbol": "AAPL",
            "name": "",
            "exchange": "STOOQ",
            "price": 105.0,
            "change_pct": 5.0,
            "volume": 123456,
            "market_time": "2026-05-11T18:14:09Z",
        }
    ]


def test_parse_rss_items_extracts_public_news_rows():
    xml_text = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>NVDA rises on AI data center demand</title>
        <link>https://example.com/nvda</link>
        <pubDate>Mon, 11 May 2026 15:00:00 GMT</pubDate>
        <source url="https://example.com">Example Wire</source>
      </item>
    </channel></rss>
    """

    items = parse_rss_items(xml_text)

    assert items == [
        {
            "title": "NVDA rises on AI data center demand",
            "url": "https://example.com/nvda",
            "published": "Mon, 11 May 2026 15:00:00 GMT",
            "source": "Example Wire",
        }
    ]
