"""Read-only Alpaca **paper-trading** endpoints, backing the Trading Account node."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.services import alpaca_paper
from app.backend.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/trading")


@router.get("/paper/account")
async def paper_account(db: Session = Depends(get_db)):
    """Paper account summary (cash, equity, buying power, status)."""
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return alpaca_paper.get_account(api_keys)


@router.get("/paper/positions")
async def paper_positions(db: Session = Depends(get_db)):
    """Current paper-account positions."""
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return {"positions": alpaca_paper.get_positions(api_keys)}


@router.get("/paper/orders")
async def paper_orders(db: Session = Depends(get_db), status: str = "all", limit: int = 50):
    """Recent paper-account orders for the Details view."""
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return {"orders": alpaca_paper.get_orders(api_keys, status=status, limit=limit)}


@router.get("/paper/portfolio-history")
async def paper_portfolio_history(
    db: Session = Depends(get_db),
    period: str = "1M",
    timeframe: str | None = None,
):
    """Equity time-series powering the Performance chart in the Details dialog.

    ``period`` ∈ {1D, 5D, 7D, 1M, 3M, 6M, 1A, 5A, all}. ``timeframe`` is
    auto-picked when omitted (1H for sub-week, 1D otherwise) so the sample
    count stays under a few hundred and the inline SVG chart is cheap."""
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return alpaca_paper.get_portfolio_history(api_keys, period=period, timeframe=timeframe)


@router.post("/paper/reset")
async def paper_reset(db: Session = Depends(get_db)):
    """Reset the paper account back to its $100,000 starting balance.

    Destructive: wipes positions, cancels open orders, restores cash and
    equity. Same as clicking Reset on Alpaca's dashboard. Gated by the
    paper credentials being set; nothing here can hit a live account
    (alpaca_paper hard-codes the paper host). Returns ``{ok, reason?}``."""
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return alpaca_paper.reset_account(api_keys)
