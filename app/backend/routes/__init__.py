from fastapi import APIRouter

from app.backend.routes.hedge_fund import router as hedge_fund_router
from app.backend.routes.health import router as health_router
from app.backend.routes.storage import router as storage_router
from app.backend.routes.flows import router as flows_router
from app.backend.routes.flow_runs import router as flow_runs_router
from app.backend.routes.ollama import router as ollama_router
from app.backend.routes.language_models import router as language_models_router
from app.backend.routes.api_keys import router as api_keys_router
from app.backend.routes.memory import router as memory_router
from app.backend.routes.research import router as research_router
from app.backend.routes.tickers import router as tickers_router
from app.backend.routes.trading import router as trading_router
from app.backend.routes.derivatives import router as derivatives_router
from app.backend.routes.forecaster import router as forecaster_router
from app.backend.routes.simons import router as simons_router
from app.backend.routes.usage import router as usage_router

# Main API router
api_router = APIRouter()

# Include sub-routers
api_router.include_router(health_router, tags=["health"])
api_router.include_router(hedge_fund_router, tags=["hedge-fund"])
api_router.include_router(storage_router, tags=["storage"])
api_router.include_router(flows_router, tags=["flows"])
api_router.include_router(flow_runs_router, tags=["flow-runs"])
api_router.include_router(ollama_router, tags=["ollama"])
api_router.include_router(language_models_router, tags=["language-models"])
api_router.include_router(api_keys_router, tags=["api-keys"])
api_router.include_router(memory_router, tags=["memory"])
api_router.include_router(research_router, tags=["research"])
api_router.include_router(usage_router, tags=["usage"])
api_router.include_router(tickers_router, tags=["tickers"])
api_router.include_router(trading_router, tags=["trading"])
api_router.include_router(derivatives_router, tags=["derivatives"])
api_router.include_router(forecaster_router, tags=["forecaster"])
api_router.include_router(simons_router, tags=["simons"])
