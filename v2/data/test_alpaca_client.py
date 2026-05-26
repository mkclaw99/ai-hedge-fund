"""Tests for AlpacaClient — offline mapping/pagination + gated live smoke tests."""

import os

import pytest

from v2.data import AlpacaClient, DataClient


# ---------------------------------------------------------------------------
# Offline fakes (no network, no credentials needed)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Returns queued responses in order; records each call's kwargs."""

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

def test_satisfies_dataclient_protocol():
    client = AlpacaClient(api_key="k", api_secret="s")
    assert isinstance(client, DataClient)


def test_timeframe_mapping():
    assert AlpacaClient._timeframe("day", 1) == "1Day"
    assert AlpacaClient._timeframe("minute", 5) == "5Min"
    assert AlpacaClient._timeframe("week", 1) == "1Week"
    assert AlpacaClient._timeframe("bogus", 1) == "1Day"  # falls back to Day


def test_get_prices_maps_and_paginates():
    page1 = _FakeResp(200, {
        "bars": [{"t": "2024-01-02T05:00:00Z", "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 1000, "n": 10, "vw": 1.4}],
        "next_page_token": "TOK",
    })
    page2 = _FakeResp(200, {
        "bars": [{"t": "2024-01-03T05:00:00Z", "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0, "v": 2000}],
        "next_page_token": None,
    })
    client = AlpacaClient(api_key="k", api_secret="s")
    client._session = _FakeSession([page1, page2])

    prices = client.get_prices("AAPL", "2024-01-01", "2024-01-31")

    assert len(prices) == 2
    assert prices[0].open == 1.0 and prices[0].close == 1.5
    assert prices[0].time[:10] == "2024-01-02"   # how the engines key trading days
    assert prices[1].volume == 2000
    # the second request must carry the page token from page 1
    assert client._session.calls[1][2]["params"]["page_token"] == "TOK"
    # first request hits the single-symbol bars endpoint
    assert client._session.calls[0][1].endswith("/v2/stocks/AAPL/bars")


def test_get_prices_returns_empty_on_http_error():
    client = AlpacaClient(api_key="k", api_secret="s")
    client._session = _FakeSession([_FakeResp(403, {"message": "forbidden"})])
    assert client.get_prices("AAPL", "2024-01-01", "2024-01-31") == []


def test_get_news_maps_and_caps():
    resp = _FakeResp(200, {
        "news": [
            {"headline": "Big news", "source": "benzinga", "created_at": "2024-01-02T10:00:00Z",
             "url": "http://x", "symbols": ["AAPL"]},
            {"headline": "More news", "source": "benzinga", "created_at": "2024-01-01T10:00:00Z",
             "url": "http://y", "symbols": ["AAPL"]},
        ],
        "next_page_token": None,
    })
    client = AlpacaClient(api_key="k", api_secret="s")
    client._session = _FakeSession([resp])

    news = client.get_news("AAPL", "2024-01-31", limit=1)

    assert len(news) == 1                         # capped at limit
    assert news[0].ticker == "AAPL"
    assert news[0].title == "Big news"
    assert news[0].source == "benzinga"


def test_fundamentals_are_unsupported():
    client = AlpacaClient(api_key="k", api_secret="s")
    assert client.get_financial_metrics("AAPL", "2024-01-01") == []
    assert client.get_insider_trades("AAPL", "2024-01-01") == []
    assert client.get_company_facts("AAPL") is None
    assert client.get_earnings("AAPL") is None


def test_is_configured():
    client = AlpacaClient(api_key="k", api_secret="s")
    assert client.is_configured is True
    client._api_secret = ""            # env-independent: exercise the property directly
    assert client.is_configured is False


# ---------------------------------------------------------------------------
# Live smoke tests (require real credentials)
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
@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "NVDA"])
def test_live_prices(ticker):
    with AlpacaClient() as alpaca:
        prices = alpaca.get_prices(ticker, "2024-01-01", "2024-03-01")
        assert len(prices) > 0, f"No prices for {ticker}"
        assert prices[0].close > 0
        print(f"  {ticker}: {len(prices)} bars  [{prices[0].time[:10]} → {prices[-1].time[:10]}]")
