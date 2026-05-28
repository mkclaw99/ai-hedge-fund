"""Ticker → company name endpoint, so the UI can show "Coherent (COHR)"."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.services import ticker_names
from app.backend.services.api_key_service import ApiKeyService

router = APIRouter(prefix="/tickers")


@router.get("/names")
async def get_names(tickers: str = "", db: Session = Depends(get_db)):
    """Return ``{ticker: name}`` for the given comma-separated tickers."""
    ticks = [t.strip() for t in tickers.split(",") if t.strip()]
    api_keys = ApiKeyService(db).get_api_keys_dict()
    return {"names": ticker_names.resolve(ticks, api_keys=api_keys)}
