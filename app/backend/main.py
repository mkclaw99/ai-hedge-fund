from dotenv import load_dotenv

# Load .env so keys like FINANCIAL_DATASETS_API_KEY and the LLM provider keys
# are available to the agents/tools via their os.getenv() fallbacks. Does not
# override anything already set in the environment or sent per-request.
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import asyncio

from app.backend.routes import api_router
from app.backend.database.connection import engine
from app.backend.database.models import Base
from app.backend.services.ollama_service import ollama_service
from app.backend.services import research_scheduler, trade_scheduler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifecycle: check Ollama, then run the research-area refresh scheduler."""
    # Install the persistent LLM response cache as early as possible — every
    # LangChain LLM call from here on (analysts, PM, news sentiment, …) goes
    # through it. Disable with HEDGE_LLM_CACHE=disabled. Fail-open: any setup
    # error logs and the app continues uncached.
    from src.utils.llm_cache import init_llm_cache
    init_llm_cache()
    await _check_ollama()
    scheduler_tasks: list[asyncio.Task] = []
    if research_scheduler.is_enabled():
        scheduler_tasks.append(asyncio.create_task(research_scheduler.scheduler_loop()))
    # Decoupled trade-tick scheduler — fires PM-only ticks on the cadence
    # set by each flow's Trading Account node. Independent of the analyst
    # refresh loop above; see app/backend/services/trade_scheduler.py.
    if trade_scheduler.is_enabled():
        scheduler_tasks.append(asyncio.create_task(trade_scheduler.scheduler_loop()))
    yield
    for t in scheduler_tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AI Hedge Fund API",
    description="Backend API for AI Hedge Fund",
    version="0.1.0",
    lifespan=lifespan,
)

# Initialize database tables (this is safe to run multiple times)
Base.metadata.create_all(bind=engine)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"],  # Frontend URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include all routes
app.include_router(api_router)

async def _check_ollama():
    """Check Ollama availability (logged at startup)."""
    try:
        logger.info("Checking Ollama availability...")
        status = await ollama_service.check_ollama_status()
        
        if status["installed"]:
            if status["running"]:
                logger.info(f"✓ Ollama is installed and running at {status['server_url']}")
                if status["available_models"]:
                    logger.info(f"✓ Available models: {', '.join(status['available_models'])}")
                else:
                    logger.info("ℹ No models are currently downloaded")
            else:
                logger.info("ℹ Ollama is installed but not running")
                logger.info("ℹ You can start it from the Settings page or manually with 'ollama serve'")
        else:
            logger.info("ℹ Ollama is not installed. Install it to use local models.")
            logger.info("ℹ Visit https://ollama.com to download and install Ollama")
            
    except Exception as e:
        logger.warning(f"Could not check Ollama status: {e}")
        logger.info("ℹ Ollama integration is available if you install it later")
