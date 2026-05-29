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
  research_schedule?: string; // off | daily | weekly
  place_paper_orders?: boolean; // submit PM decisions to Alpaca PAPER (opt-in via Trading Account node)
  starting_budget?: number;     // Trading Account node's Starting Budget (drives budget-aware sizing)
  strategy?: StrategyConfig;    // Strategy node config — style, sizing, caps, instruments, mandate
  skip_analysts?: boolean;      // Replay strategy on cached signals (no analyst re-run, no theme resolve)
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
}

export interface BacktestRequest extends BaseHedgeFundRequest {
  start_date: string;
  end_date: string;
  initial_capital?: number;
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