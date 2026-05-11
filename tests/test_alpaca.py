import json
import os
from pathlib import Path

import stonks_bot.alpaca as alpaca_module
from stonks_bot.alpaca import AlpacaConfigError, AlpacaPaperClient, default_env_paths, load_alpaca_credentials, load_dotenv, load_env_files
from stonks_bot.config import BrokerConfig


def test_load_dotenv_loads_lowercase_alpaca_keys_without_overwriting_existing_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "alpaca_endpoint=https://paper-api.alpaca.markets/v2",
                "alpaca_key=paper-key",
                "alpaca_secret=paper-secret",
            ]
        )
    )
    monkeypatch.setenv("alpaca_key", "already-set")
    monkeypatch.delenv("alpaca_endpoint", raising=False)
    monkeypatch.delenv("alpaca_secret", raising=False)

    loaded = load_dotenv(env_path)

    assert loaded is True
    assert os.environ["alpaca_endpoint"] == "https://paper-api.alpaca.markets/v2"
    assert os.environ["alpaca_key"] == "already-set"
    assert os.environ["alpaca_secret"] == "paper-secret"


def test_load_env_files_can_fill_empty_local_values_from_hermes_default_env(tmp_path, monkeypatch):
    local_env = tmp_path / "project.env"
    hermes_env = tmp_path / "hermes.env"
    local_env.write_text(
        "\n".join(
            [
                "alpaca_endpoint=https://paper-api.alpaca.markets/v2",
                "alpaca_key=local-paper-key",
                "alpaca_secret=",
            ]
        )
    )
    hermes_env.write_text("alpaca_key=hermes-paper-key\nalpaca_secret=hermes-paper-secret\n")
    for key in ["alpaca_endpoint", "alpaca_key", "alpaca_secret"]:
        monkeypatch.delenv(key, raising=False)

    loaded = load_env_files([local_env, hermes_env])

    assert loaded == [local_env, hermes_env]
    assert os.environ["alpaca_endpoint"] == "https://paper-api.alpaca.markets/v2"
    assert os.environ["alpaca_key"] == "local-paper-key"
    assert os.environ["alpaca_secret"] == "hermes-paper-secret"


def test_default_env_paths_include_active_hermes_cli_env_path(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    hermes_env = tmp_path / "active-hermes.env"
    monkeypatch.setattr(alpaca_module, "_hermes_cli_env_path", lambda: hermes_env, raising=False)

    paths = default_env_paths(config_path)

    assert hermes_env in paths
    assert Path(config_path).resolve().parent / ".env" in paths


def test_load_alpaca_credentials_accepts_lowercase_env_and_requires_paper_endpoint(monkeypatch):
    broker = BrokerConfig(name="alpaca", endpoint_env="alpaca_endpoint", key_env="alpaca_key", secret_env="alpaca_secret")
    monkeypatch.setenv("alpaca_endpoint", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("alpaca_key", "paper-key")
    monkeypatch.setenv("alpaca_secret", "paper-secret")

    credentials = load_alpaca_credentials(broker)

    assert credentials.endpoint == "https://paper-api.alpaca.markets/v2"
    assert credentials.key == "paper-key"
    assert credentials.secret == "paper-secret"

    monkeypatch.setenv("alpaca_endpoint", "https://api.alpaca.markets/v2")

    try:
        load_alpaca_credentials(broker)
    except AlpacaConfigError as exc:
        assert "paper endpoint" in str(exc)
    else:
        raise AssertionError("expected live Alpaca endpoint to be rejected")


def test_load_alpaca_credentials_reports_missing_secret_without_revealing_key(monkeypatch):
    broker = BrokerConfig(name="alpaca", endpoint_env="alpaca_endpoint", key_env="alpaca_key", secret_env="alpaca_secret")
    monkeypatch.setenv("alpaca_endpoint", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("alpaca_key", "paper-key")
    monkeypatch.delenv("alpaca_secret", raising=False)

    try:
        load_alpaca_credentials(broker)
    except AlpacaConfigError as exc:
        message = str(exc)
        assert "alpaca_secret" in message
        assert "paper-key" not in message
    else:
        raise AssertionError("expected missing secret to be rejected")


def test_submit_market_order_posts_sanitized_paper_order_to_alpaca(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "order-123",
                    "symbol": "AAPL",
                    "side": "buy",
                    "status": "filled",
                    "filled_qty": "4.2",
                    "filled_avg_price": "125.50",
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["key_header"] = request.headers["Apca-api-key-id"]
        captured["secret_header"] = request.headers["Apca-api-secret-key"]
        return FakeResponse()

    monkeypatch.setattr(alpaca_module, "urlopen", fake_urlopen)

    fill = AlpacaPaperClient(credentials).submit_market_order("AAPL", side="buy", notional=527.10)

    assert captured["url"] == "https://paper-api.alpaca.markets/v2/orders"
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "symbol": "AAPL",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "notional": "527.10",
    }
    assert captured["key_header"] == "paper-key"
    assert captured["secret_header"] == "paper-secret"
    assert fill.order_id == "order-123"
    assert fill.status == "filled"
    assert fill.quantity == 4.2
    assert fill.price == 125.50
    assert fill.notional == 527.10


def test_submit_market_order_cancels_unfilled_order_and_raises_without_fill(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), request.full_url))
        if request.get_method() == "POST":
            return FakeResponse({"id": "order-pending", "symbol": "AAPL", "side": "buy", "status": "accepted"})
        if request.get_method() == "GET":
            return FakeResponse({"id": "order-pending", "symbol": "AAPL", "side": "buy", "status": "new"})
        if request.get_method() == "DELETE":
            return FakeResponse({"id": "order-pending", "status": "canceled"})
        raise AssertionError("unexpected request")

    monkeypatch.setattr(alpaca_module, "urlopen", fake_urlopen)

    try:
        AlpacaPaperClient(credentials, poll_attempts=1, poll_delay_seconds=0).submit_market_order("AAPL", side="buy", notional=100.0)
    except AlpacaConfigError as exc:
        message = str(exc)
        assert "not filled" in message
        assert "local ledger was not updated" in message
    else:
        raise AssertionError("expected unfilled order to be rejected")

    assert calls == [
        ("POST", "https://paper-api.alpaca.markets/v2/orders"),
        ("GET", "https://paper-api.alpaca.markets/v2/orders/order-pending"),
        ("DELETE", "https://paper-api.alpaca.markets/v2/orders/order-pending"),
    ]


def test_get_positions_reads_alpaca_positions_endpoint(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    client = AlpacaPaperClient(credentials)
    requested_paths = []

    def fake_request(path, *, method="GET", payload=None):
        requested_paths.append((path, method, payload))
        return [{"symbol": "AAPL", "qty": "2", "avg_entry_price": "150.25"}]

    monkeypatch.setattr(client, "_request_json", fake_request)

    positions = client.get_positions()

    assert requested_paths == [("/positions", "GET", None)]
    assert positions == [{"symbol": "AAPL", "qty": "2", "avg_entry_price": "150.25"}]


def test_get_options_contracts_queries_paper_contract_endpoint(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    client = AlpacaPaperClient(credentials)
    requested_paths = []

    def fake_request(path, *, method="GET", payload=None):
        requested_paths.append(path)
        return {
            "option_contracts": [
                {
                    "symbol": "AAPL260620C00105000",
                    "underlying_symbol": "AAPL",
                    "type": "call",
                    "expiration_date": "2026-06-20",
                    "strike_price": "105",
                }
            ]
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    contracts = client.get_options_contracts(
        ["AAPL"],
        "call",
        "2026-06-10",
        "2026-07-10",
        strike_price_gte=85.0,
        strike_price_lte=115.0,
        limit=500,
    )

    assert len(contracts) == 1
    assert contracts[0]["symbol"] == "AAPL260620C00105000"
    assert requested_paths == [
        "/options/contracts?underlying_symbols=AAPL&type=call&status=active&expiration_date_gte=2026-06-10&expiration_date_lte=2026-07-10&limit=500&strike_price_gte=85.0&strike_price_lte=115.0"
    ]


def test_get_latest_option_quotes_uses_alpaca_data_endpoint(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    client = AlpacaPaperClient(credentials, data_endpoint="https://data.alpaca.markets/v1beta1")
    requested = []

    def fake_request_url(url, label, *, method="GET", payload=None):
        requested.append((url, label, method, payload))
        return {"quotes": {"AAPL260620C00105000": {"bp": 1.2, "ap": 1.3}}}

    monkeypatch.setattr(client, "_request_json_url", fake_request_url)

    quotes = client.get_latest_option_quotes(["AAPL260620C00105000"], feed="indicative")

    assert quotes == {"AAPL260620C00105000": {"bp": 1.2, "ap": 1.3}}
    assert requested == [
        (
            "https://data.alpaca.markets/v1beta1/options/quotes/latest?symbols=AAPL260620C00105000&feed=indicative",
            "/options/quotes/latest",
            "GET",
            None,
        )
    ]


def test_submit_option_limit_order_posts_single_leg_alpaca_paper_order(monkeypatch):
    credentials = alpaca_module.AlpacaCredentials(
        endpoint="https://paper-api.alpaca.markets/v2",
        key="paper-key",
        secret="paper-secret",
    )
    client = AlpacaPaperClient(credentials)
    captured = []

    def fake_request(path, *, method="GET", payload=None):
        captured.append((path, method, payload))
        return {
            "id": "option-order-123",
            "symbol": "AAPL260620C00105000",
            "side": "buy",
            "status": "filled",
            "filled_qty": "1",
            "filled_avg_price": "1.25",
        }

    monkeypatch.setattr(client, "_request_json", fake_request)

    fill = client.submit_option_buy_to_open("aapl260620c00105000", contracts=1, limit_price=1.25)

    assert captured == [
        (
            "/orders",
            "POST",
            {
                "symbol": "AAPL260620C00105000",
                "qty": "1",
                "side": "buy",
                "type": "limit",
                "limit_price": "1.25",
                "time_in_force": "day",
            },
        )
    ]
    assert fill.order_id == "option-order-123"
    assert fill.symbol == "AAPL260620C00105000"
    assert fill.quantity == 1
    assert fill.price == 1.25
    assert fill.notional == 125.0
