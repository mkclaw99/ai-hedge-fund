"""Tests for AlpacaBroker — offline mapping + gated live smoke tests."""

import os

import pytest

from v2.broker import AlpacaBroker


# ---------------------------------------------------------------------------
# Offline fakes
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._responses.pop(0)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Offline unit tests
# ---------------------------------------------------------------------------

def test_endpoint_normalization():
    # live endpoint without /v2 gets it appended
    live = AlpacaBroker(api_key="k", api_secret="s", endpoint="https://api.alpaca.markets")
    assert live._endpoint == "https://api.alpaca.markets/v2"
    assert live.is_paper is False
    # paper endpoint that already has /v2 is preserved (no double /v2)
    paper = AlpacaBroker(api_key="k", api_secret="s", endpoint="https://paper-api.alpaca.markets/v2")
    assert paper._endpoint == "https://paper-api.alpaca.markets/v2"
    assert paper.is_paper is True


def test_get_account_coerces_string_numbers():
    resp = _FakeResp(200, {
        "id": "abc", "account_number": "PA123", "status": "ACTIVE",
        "cash": "1000.50", "portfolio_value": "2500.00", "equity": "2500.00",
        "buying_power": "5000", "currency": "USD",
    })
    broker = AlpacaBroker(api_key="k", api_secret="s")
    broker._session = _FakeSession([resp])

    acct = broker.get_account()
    assert acct is not None
    assert acct.status == "ACTIVE"
    assert acct.cash == 1000.50          # string -> float coercion
    assert acct.buying_power == 5000.0
    assert broker._session.calls[0][1].endswith("/account")


def test_get_positions_maps():
    resp = _FakeResp(200, [
        {"symbol": "AAPL", "qty": "10", "side": "long", "market_value": "1500",
         "avg_entry_price": "140", "unrealized_pl": "100"},
        {"symbol": "MSFT", "qty": "-5", "side": "short", "market_value": "-2000"},
    ])
    broker = AlpacaBroker(api_key="k", api_secret="s")
    broker._session = _FakeSession([resp])

    positions = broker.get_positions()
    assert len(positions) == 2
    assert positions[0].symbol == "AAPL" and positions[0].qty == 10.0
    assert positions[1].qty == -5.0


def test_get_orders_passes_status():
    resp = _FakeResp(200, [{"id": "o1", "symbol": "AAPL", "side": "buy", "qty": "10", "status": "filled"}])
    broker = AlpacaBroker(api_key="k", api_secret="s")
    broker._session = _FakeSession([resp])

    orders = broker.get_orders(status="closed", limit=50)
    assert len(orders) == 1 and orders[0].id == "o1"
    assert broker._session.calls[0][2]["params"]["status"] == "closed"


def test_reads_return_safe_defaults_on_error():
    broker = AlpacaBroker(api_key="k", api_secret="s")
    broker._session = _FakeSession([_FakeResp(403, {"message": "forbidden"})])
    assert broker.get_account() is None

    broker._session = _FakeSession([_FakeResp(500, {})])
    assert broker.get_positions() == []


# ---------------------------------------------------------------------------
# Live smoke tests
# ---------------------------------------------------------------------------

_HAS_CREDS = bool(
    (os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID"))
    and (os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY"))
)

live = pytest.mark.skipif(
    not _HAS_CREDS,
    reason="live Alpaca smoke tests require ALPACA_KEY + ALPACA_SECRET_KEY",
)


@live
def test_live_account():
    with AlpacaBroker() as broker:
        acct = broker.get_account()
        assert acct is not None, "Account fetch failed — check credentials/endpoint"
        print(f"  account status={acct.status}  equity={acct.equity}  cash={acct.cash}  paper={broker.is_paper}")


@live
def test_live_positions():
    with AlpacaBroker() as broker:
        positions = broker.get_positions()      # may legitimately be empty
        print(f"  {len(positions)} open positions")
