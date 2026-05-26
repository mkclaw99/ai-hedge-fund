"""Alpaca Trading API client — read-only account / positions / orders.

This is the *trading* side of Alpaca (``paper-api.alpaca.markets`` by default),
distinct from the market-data side (:class:`v2.data.alpaca_client.AlpacaClient`).
It reads live/paper portfolio state so the v2 pipeline can reconcile target
weights against real holdings.

    from v2.broker import AlpacaBroker

    with AlpacaBroker() as broker:
        account = broker.get_account()
        positions = broker.get_positions()

Read-only by design
-------------------
Order *submission* is intentionally **not** implemented. The project is for
research/education and "does not actually make any trades" (see README). Add
a ``submit_order`` method here only if you deliberately want live execution.

Credentials (env, override via constructor):
    - ``ALPACA_KEY``         (or ``APCA_API_KEY_ID``)      — API key id
    - ``ALPACA_SECRET_KEY``  (or ``APCA_API_SECRET_KEY``)  — API secret
    - ``ALPACA_ENDPOINT``    (or ``ENDPOINT``)             — trading-API base,
      e.g. ``https://paper-api.alpaca.markets/v2`` (paper) — defaults to paper.
"""

from __future__ import annotations

import logging
import os
import time

import requests

from v2.broker.models import AlpacaAccount, AlpacaOrder, AlpacaPosition

logger = logging.getLogger(__name__)


class AlpacaBroker:
    """Read-only Alpaca Trading API client."""

    DEFAULT_ENDPOINT = "https://paper-api.alpaca.markets/v2"
    _RETRY_DELAYS = (5, 15, 30)

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        endpoint: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key or os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID", "")
        self._api_secret = api_secret or os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("APCA_API_SECRET_KEY", "")
        # Normalize the trading-API base: accept it with or without the ``/v2``
        # version segment (paper URLs often include it, live URLs often don't).
        endpoint = (
            endpoint or os.environ.get("ALPACA_ENDPOINT") or os.environ.get("ENDPOINT") or self.DEFAULT_ENDPOINT
        ).rstrip("/")
        if not endpoint.endswith("/v2"):
            endpoint += "/v2"
        self._endpoint = endpoint
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "accept": "application/json",
        })

    @property
    def is_configured(self) -> bool:
        """True only when both a key id and a secret are present."""
        return bool(self._api_key and self._api_secret)

    @property
    def is_paper(self) -> bool:
        """True if pointed at a paper-trading endpoint."""
        return "paper-api" in self._endpoint

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> AlpacaBroker:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_account(self) -> AlpacaAccount | None:
        """Fetch the account snapshot. Returns ``None`` on failure."""
        resp = self._request("GET", "/account")
        if resp is None:
            return None
        try:
            return AlpacaAccount(**resp.json())
        except (ValueError, TypeError):
            return None

    def get_positions(self) -> list[AlpacaPosition]:
        """Fetch all open positions. Returns ``[]`` on failure."""
        resp = self._request("GET", "/positions")
        if resp is None:
            return []
        out: list[AlpacaPosition] = []
        for row in resp.json() or []:
            try:
                out.append(AlpacaPosition(**row))
            except (ValueError, TypeError):
                continue
        return out

    def get_orders(
        self,
        status: str = "open",
        limit: int = 100,
        direction: str = "desc",
    ) -> list[AlpacaOrder]:
        """Fetch orders (``status`` = open | closed | all). Returns ``[]`` on failure."""
        resp = self._request("GET", "/orders", params={
            "status": status,
            "limit": limit,
            "direction": direction,
        })
        if resp is None:
            return []
        out: list[AlpacaOrder] = []
        for row in resp.json() or []:
            try:
                out.append(AlpacaOrder(**row))
            except (ValueError, TypeError):
                continue
        return out

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> requests.Response | None:
        """HTTP request with retry on 429. Never raises."""
        url = self._endpoint + path
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None)):
            try:
                resp = self._session.request(method, url, timeout=self._timeout, **kwargs)
            except requests.RequestException as exc:
                logger.warning("Request error on %s %s: %s", method, path, exc)
                return None

            if resp.status_code == 429 and delay is not None:
                logger.info(
                    "Rate limited (429), retrying in %ds (attempt %d/%d)",
                    delay, attempt + 1, len(self._RETRY_DELAYS),
                )
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                logger.warning("%s %s returned %d", method, path, resp.status_code)
                return None

            return resp

        logger.warning(
            "Rate limit exhausted after %d retries on %s %s",
            len(self._RETRY_DELAYS), method, path,
        )
        return None
