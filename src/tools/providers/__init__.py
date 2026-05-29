"""Multi-vendor market-data providers with transparent auto-fallback.

For historical context: every call in :mod:`src.tools.api` originally went
through Financial Datasets only. When that vendor was unavailable (rate
limited, out of credits, network hiccup) the whole app silently degraded to
empty results — analysts made no-data calls, the track record collapsed,
the backtest's price layer died.

This package introduces a chain of providers per data type. The chain walks
providers in order and returns the first non-empty result. Providers
implement the same interface (the :class:`~base.DataProvider` Protocol)
and return the same Pydantic models from :mod:`src.data.models`, so
callers in :mod:`src.tools.api` are oblivious to which vendor served the
data.

Default chain (per data type):
    prices       FD → Alpaca → yfinance
    metrics      FD → yfinance       (yfinance is best-effort, schema-mapped)
    line_items   FD → yfinance       (yfinance is best-effort, lossy)
    insider      FD → yfinance       (yfinance only has recent ones)
    news         FD → Alpaca → yfinance
    market_cap   FD → yfinance       (compute or read .info["marketCap"])

The order is hard-coded (no UI knob) because most users just want it to
keep working — see commit msg for the design discussion.
"""
from __future__ import annotations

from src.tools.providers.alpaca import AlpacaProvider
from src.tools.providers.base import DataProvider
from src.tools.providers.chain import ProviderChain
from src.tools.providers.financial_datasets import FDProvider
from src.tools.providers.yfinance_provider import YFinanceProvider


def build_default_chain(api_keys: dict | None = None) -> ProviderChain:
    """Construct the default provider chain.

    ``api_keys`` is the credentials dict the routes get from
    :class:`app.backend.services.api_key_service.ApiKeyService` (also accepts
    env vars as a fallback inside each provider). The chain is built fresh
    per call — providers are cheap, and threading per-request credentials
    through globals would be worse than the small constructor cost.
    """
    api_keys = api_keys or {}
    providers: list[DataProvider] = [
        FDProvider(api_key=api_keys.get("FINANCIAL_DATASETS_API_KEY")),
        AlpacaProvider(
            api_key=api_keys.get("ALPACA_PAPER_API_KEY_ID") or api_keys.get("ALPACA_KEY"),
            api_secret=api_keys.get("ALPACA_PAPER_SECRET_KEY") or api_keys.get("ALPACA_SECRET_KEY"),
        ),
        YFinanceProvider(),  # no creds — always available, always last
    ]
    return ProviderChain(providers)


__all__ = [
    "AlpacaProvider",
    "DataProvider",
    "FDProvider",
    "ProviderChain",
    "YFinanceProvider",
    "build_default_chain",
]
