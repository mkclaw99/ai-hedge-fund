"""Read-only derivatives data — currently options summaries via Alpaca.

Surface for the Strategy node's "Options" toggle: the frontend can probe
``GET /derivatives/{ticker}`` to display chain info, and during a run with
``strategy.allow_options=True`` the graph layer pulls one per ticker and feeds
it into the PM's prompt.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.services import derivatives
from app.backend.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/derivatives")


@router.get("/{ticker}")
async def get_options(ticker: str, db: Session = Depends(get_db)):
    """Compact options-chain summary for one ticker (Alpaca paper data).

    Returns ``{optionable: bool, ...}``; never raises (fail-open).
    """
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return derivatives.get_options_summary(ticker, api_keys)
