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
