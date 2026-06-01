// Shared types for API requests and responses
export enum ModelProvider {
  OPENAI = 'OpenAI',
  ANTHROPIC = 'Anthropic',
  GROQ = 'Groq',
  OLLAMA = 'Ollama',
}

export interface AgentModelConfig {
  agent_id: string;
  model_name?: string;
  model_provider?: ModelProvider;
}

export interface GraphNode {
  id: string;
  type?: string;
  data?: any;
  position?: { x: number; y: number };
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type?: string;
  data?: any;
}

export interface PortfolioPosition {
  ticker: string;
  quantity: number;
  trade_price: number;
}

// Base interface for shared fields between HedgeFundRequest and BacktestRequest
export interface BaseHedgeFundRequest {
  tickers: string[];
  graph_nodes: GraphNode[];
  graph_edges: GraphEdge[];
  agent_models?: AgentModelConfig[];
  model_name?: string;
  model_provider?: ModelProvider;
  margin_requirement?: number;
  portfolio_positions?: PortfolioPosition[];
}

export interface HedgeFundRequest extends BaseHedgeFundRequest {
  end_date?: string;
  start_date?: string;
  initial_cash?: number;
  flow_id?: number; // Scopes this run's research memory to its flow
  // Research areas: a theme (analyst slug) is discovered into a tradable universe
  // at run time; materials are user-provided grounding for the analysts.
  research_theme?: string;
  research_materials?: string;
  research_mandate?: string; // Fundamental Research: topic researcher's lens
  research_company_mandate?: string; // Fundamental Companies: extraction lens
  research_max_companies?: number;
  research_schedule?: string; // off | hourly | daily | weekly
  place_paper_orders?: boolean; // submit PM decisions to Alpaca PAPER (opt-in via Trading Account node)
  starting_budget?: number;     // Trading Account node's Starting Budget (drives budget-aware sizing)
  forecaster_context_len?: number;     // Chronos-2 context window (32-8192). undef → backend default 256
  forecaster_prediction_len?: number;  // Chronos-2 forecast horizon (1-1024). undef → backend default 10
  forecaster_bar_frequency?: 'day' | 'hour' | '5min' | '1min'; // bar resolution; intraday via yfinance
  // Per-agent Gemini thinking-budget enum keyed by agent node id.
  // Ignored on non-Google models.
  agent_thinking_budgets?: Record<string, 'off' | 'low' | 'medium' | 'high' | 'dynamic'>;
  strategy?: StrategyConfig;    // Strategy node config — style, sizing, caps, instruments, mandate
  skip_analysts?: boolean;      // Replay strategy on cached signals (no analyst re-run, no theme resolve)
  risk_manager?: RiskManagerConfig; // Risk Manager node — vol/correlation cap overrides
  // Decoupled trade-tick — owned by the Trading Account node, carried across
  // so the run executor knows it's a slim PM-only replay with fresh prices.
  trade_schedule?: 'off' | '5min' | '15min' | 'hourly';
  refresh_prices?: boolean;
}

export interface RiskManagerConfig {
  limit_multiplier?: number;
  disable_correlation_penalty?: boolean;
  disabled?: boolean;
}

export interface StrategyConfig {
  style?: string;             // value | growth | momentum | mean_reversion | event_driven | income
  sizing_rule?: string;       // equal_weight | conviction_weighted | risk_parity | fixed_dollar
  max_position_pct?: number;  // % of portfolio
  max_sector_pct?: number;    // % of portfolio (LLM-honoured)
  holding_period?: string;    // day | swing | position | long_term
  stop_loss_pct?: number;
  take_profit_pct?: number;
  allow_stocks?: boolean;
  allow_options?: boolean;
  allow_etfs?: boolean;
  note?: string;              // free-text mandate
  // Decoupled trade-tick throttles (read by the PM skip predicate).
  min_decision_interval_minutes?: number;
  price_move_threshold_pct?: number;
  max_signal_age_hours?: number;
}

export interface BacktestRequest extends BaseHedgeFundRequest {
  start_date: string;
  end_date: string;
  initial_capital?: number;
  flow_id?: number;              // Scope wiki writes to this flow → track record sees them
  strategy?: StrategyConfig;
  risk_manager?: RiskManagerConfig;
  // Optional flow-wide configs the Play-trigger nodes also forward in
  // backtest mode. Previously absent from this type — call sites sent
  // them anyway, producing a pre-existing TS2353 (and silently dropping
  // them when properties beyond the type were narrowed elsewhere).
  place_paper_orders?: boolean;
  starting_budget?: number;
  forecaster_context_len?: number;
  forecaster_prediction_len?: number;
  forecaster_bar_frequency?: 'day' | 'hour' | '5min' | '1min';
  agent_thinking_budgets?: Record<string, 'off' | 'low' | 'medium' | 'high' | 'dynamic'>;
}

export interface BacktestDayResult {
  date: string;
  portfolio_value: number;
  cash: number;
  decisions: Record<string, any>;
  executed_trades: Record<string, number>;
  analyst_signals: Record<string, any>;
  current_prices: Record<string, number>;
  long_exposure: number;
  short_exposure: number;
  gross_exposure: number;
  net_exposure: number;
  long_short_ratio: number | null;
}

export interface BacktestPerformanceMetrics {
  sharpe_ratio?: number;
  sortino_ratio?: number;
  max_drawdown?: number;
  max_drawdown_date?: string;
  long_short_ratio?: number;
  gross_exposure?: number;
  net_exposure?: number;
} 