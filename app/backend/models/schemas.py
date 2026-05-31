from datetime import datetime, timedelta
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
from src.llm.models import ModelProvider
from enum import Enum
from app.backend.services.graph import extract_base_agent_key


class FlowRunStatus(str, Enum):
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"


class AgentModelConfig(BaseModel):
    agent_id: str
    model_name: Optional[str] = None
    model_provider: Optional[ModelProvider] = None


class PortfolioPosition(BaseModel):
    ticker: str
    quantity: float
    trade_price: float

    @field_validator('trade_price')
    @classmethod
    def price_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError('Trade price must be positive!')
        return v


class GraphNode(BaseModel):
    id: str
    type: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    position: Optional[Dict[str, Any]] = None


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


class HedgeFundResponse(BaseModel):
    decisions: dict
    analyst_signals: dict


class ErrorResponse(BaseModel):
    message: str
    error: str | None = None


# Base class for shared fields between HedgeFundRequest and BacktestRequest
class BaseHedgeFundRequest(BaseModel):
    tickers: List[str]
    graph_nodes: List[GraphNode]
    graph_edges: List[GraphEdge]
    agent_models: Optional[List[AgentModelConfig]] = None
    model_name: Optional[str] = "gpt-4.1"
    model_provider: Optional[ModelProvider] = ModelProvider.OPENAI
    margin_requirement: float = 0.0
    portfolio_positions: Optional[List[PortfolioPosition]] = None
    api_keys: Optional[Dict[str, str]] = None

    def get_agent_ids(self) -> List[str]:
        """Extract agent IDs from graph structure"""
        return [node.id for node in self.graph_nodes]

    def get_agent_model_config(self, agent_id: str) -> tuple[str, ModelProvider]:
        """Get model configuration for a specific agent"""
        if self.agent_models:
            # Extract base agent key from unique node ID for matching
            base_agent_key = extract_base_agent_key(agent_id)
            
            for config in self.agent_models:
                # Check both unique node ID and base agent key for matches
                config_base_key = extract_base_agent_key(config.agent_id)
                if config.agent_id == agent_id or config_base_key == base_agent_key:
                    return (
                        config.model_name or self.model_name,
                        config.model_provider or self.model_provider
                    )
        # Fallback to global model settings
        return self.model_name, self.model_provider


class RiskManagerConfig(BaseModel):
    """Configurable knobs for the volatility/correlation-based position limits.

    Auto-spawned with these defaults when no Risk Manager node is on the canvas,
    so existing flows behave identically. Drop a node in only to override.
    """
    limit_multiplier: float = 1.0
    disable_correlation_penalty: bool = False
    disabled: bool = False


class StrategyConfig(BaseModel):
    """Trading strategy declared by a Strategy node — read by the PM, partially
    enforced by the Trading Account when placing paper orders."""
    style: Optional[str] = None
    sizing_rule: Optional[str] = None
    max_position_pct: Optional[float] = None
    max_sector_pct: Optional[float] = None
    holding_period: Optional[str] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    allow_stocks: bool = True
    allow_options: bool = False
    allow_etfs: bool = False
    note: Optional[str] = None


class BacktestRequest(BaseHedgeFundRequest):
    start_date: str
    end_date: str
    initial_capital: float = 100000.0
    # Scope wiki writes per day to this flow's memory so the track-record view
    # picks up the simulated decisions. Without this, backtest runs are invisible
    # to the learning loop. Unset → no per-flow wiki write.
    flow_id: Optional[int] = None
    # Strategy/Risk Manager configs ride through the same way as /run.
    strategy: Optional[StrategyConfig] = None
    risk_manager: Optional[RiskManagerConfig] = None


class BacktestDayResult(BaseModel):
    date: str
    portfolio_value: float
    cash: float
    decisions: Dict[str, Any]
    executed_trades: Dict[str, int]
    analyst_signals: Dict[str, Any]
    current_prices: Dict[str, float]
    long_exposure: float
    short_exposure: float
    gross_exposure: float
    net_exposure: float
    long_short_ratio: Optional[float] = None


class BacktestPerformanceMetrics(BaseModel):
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    max_drawdown: Optional[float] = None
    max_drawdown_date: Optional[str] = None
    long_short_ratio: Optional[float] = None
    gross_exposure: Optional[float] = None
    net_exposure: Optional[float] = None


class BacktestResponse(BaseModel):
    results: List[BacktestDayResult]
    performance_metrics: BacktestPerformanceMetrics
    final_portfolio: Dict[str, Any]


class HedgeFundRequest(BaseHedgeFundRequest):
    end_date: Optional[str] = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    start_date: Optional[str] = None
    initial_cash: float = 100000.0
    flow_id: Optional[int] = None  # Scopes this run's research memory to its flow
    # Research areas: a theme (analyst slug) is discovered into a tradable universe
    # at run time; materials are user-provided grounding injected into the analysts.
    research_theme: Optional[str] = None
    research_materials: Optional[str] = None
    research_mandate: Optional[str] = None  # Fundamental Research node: the topic researcher's lens
    research_company_mandate: Optional[str] = None  # Fundamental Companies node: the extraction lens
    research_max_companies: Optional[int] = 10
    research_schedule: Optional[str] = "off"  # off | hourly | daily | weekly — auto-refresh cadence
    # Chronos-2 per-node config (Time Series Forecaster). None → module defaults
    # (CONTEXT_LEN=256, PRED_LEN=10). Clamped to the model's published limits
    # in the agent itself, so a bad input never crashes the run.
    forecaster_context_len: Optional[int] = None
    forecaster_prediction_len: Optional[int] = None
    # Bar frequency: 'day' (default, via provider chain) | 'hour' | '5min' |
    # '1min' (intraday via yfinance). Context/prediction lengths apply as
    # *bar counts* at the chosen frequency, not always trading days.
    forecaster_bar_frequency: Optional[str] = None
    # When True (and a Trading Account node is in the flow with Auto-trade on), the
    # PM's per-ticker decisions are submitted as market orders on the user's Alpaca
    # PAPER account. Default off — even paper orders are a real action.
    place_paper_orders: Optional[bool] = False
    # Trading Account node's "Starting Budget" — total capital this account should deploy.
    # BUY/SHORT orders are sized to (min(starting_budget, buying_power) / N_open_actions) ×
    # (confidence/100) ÷ price. If unset, the run uses the account's buying_power directly.
    starting_budget: Optional[float] = None
    # Strategy node config — declares trading rules (style, sizing, caps, instruments,
    # free-text mandate). Read by the PM; the Trading Account enforces max_position_pct.
    strategy: Optional[StrategyConfig] = None
    # Risk Manager node config — overrides the hardcoded position-cap calculation.
    # None = use defaults (= auto-spawned behaviour, identical to pre-node history).
    risk_manager: Optional[RiskManagerConfig] = None
    # Replay-strategy mode: skip the analyst layer and pre-populate analyst_signals
    # from the flow's wiki, so the PM re-decides on cached signals with the new
    # Strategy params. The frontend sends a slimmed `graph_nodes` (just the PM) and
    # the explicit ticker universe — no theme/research re-run, no LLM analyst calls.
    skip_analysts: Optional[bool] = False

    def get_start_date(self) -> str:
        """Calculate start date if not provided"""
        if self.start_date:
            return self.start_date
        return (datetime.strptime(self.end_date, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")


# Flow-related schemas
class FlowCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    viewport: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None
    is_template: bool = False
    tags: Optional[List[str]] = None


class FlowUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    viewport: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None
    is_template: Optional[bool] = None
    tags: Optional[List[str]] = None


class FlowResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    viewport: Optional[Dict[str, Any]]
    data: Optional[Dict[str, Any]]
    is_template: bool
    tags: Optional[List[str]]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class FlowSummaryResponse(BaseModel):
    """Lightweight flow response without nodes/edges for listing"""
    id: int
    name: str
    description: Optional[str]
    is_template: bool
    tags: Optional[List[str]]
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


# Flow Run schemas
class FlowRunCreateRequest(BaseModel):
    """Request to create a new flow run"""
    request_data: Optional[Dict[str, Any]] = None


class FlowRunUpdateRequest(BaseModel):
    """Request to update an existing flow run"""
    status: Optional[FlowRunStatus] = None
    results: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class FlowRunResponse(BaseModel):
    """Complete flow run response"""
    id: int
    flow_id: int
    status: FlowRunStatus
    run_number: int
    created_at: datetime
    updated_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    request_data: Optional[Dict[str, Any]]
    results: Optional[Dict[str, Any]]
    error_message: Optional[str]

    class Config:
        from_attributes = True


class FlowRunSummaryResponse(BaseModel):
    """Lightweight flow run response for listing"""
    id: int
    flow_id: int
    status: FlowRunStatus
    run_number: int
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_message: Optional[str]

    class Config:
        from_attributes = True


# API Key schemas
class ApiKeyCreateRequest(BaseModel):
    """Request to create or update an API key"""
    provider: str = Field(..., min_length=1, max_length=100)
    key_value: str = Field(..., min_length=1)
    description: Optional[str] = None
    is_active: bool = True


class ApiKeyUpdateRequest(BaseModel):
    """Request to update an existing API key"""
    key_value: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ApiKeyResponse(BaseModel):
    """Complete API key response"""
    id: int
    provider: str
    key_value: str
    is_active: bool
    description: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    last_used: Optional[datetime]

    class Config:
        from_attributes = True


class ApiKeySummaryResponse(BaseModel):
    """API key response without the actual key value"""
    id: int
    provider: str
    is_active: bool
    description: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]
    last_used: Optional[datetime]
    has_key: bool = True  # Indicates if a key is set

    class Config:
        from_attributes = True


class ApiKeyBulkUpdateRequest(BaseModel):
    """Request to update multiple API keys at once"""
    api_keys: List[ApiKeyCreateRequest]
