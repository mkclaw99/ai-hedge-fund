import { MessageItem } from '@/contexts/node-context';
import type { BuiltInNode, Node } from '@xyflow/react';

export type NodeMessage = MessageItem;

export type AgentNode = Node<{ name: string, description: string, status: string }, 'agent-node'>;
export type ForecasterNode = Node<{ name: string, description: string, status: string }, 'forecaster-node'>;
export type JimSimonsNode = Node<{ name: string, description: string, status: string }, 'jim-simons-node'>;
export type InvestmentReportNode = Node<{ name: string, description: string, status: string }, 'investment-report-node'>;
export type JsonOutputNode = Node<{ name: string, description: string, status: string }, 'json-output-node'>;
export type PortfolioStartNode = Node<{ name: string, description: string, status: string }, 'portfolio-start-node'>;
export type PortfolioManagerNode = Node<{ name: string, description: string, status: string }, 'portfolio-manager-node'>;
export type StockAnalyzerNode = Node<{ name: string, description: string, status: string }, 'stock-analyzer-node'>;
export type MemoryNode = Node<{ name: string, description: string, status: string }, 'memory-node'>;
export type ResearchAreaNode = Node<{ name: string, description: string, status: string }, 'research-area-node'>;
export type ResearchCompaniesNode = Node<{ name: string, description: string, status: string }, 'research-companies-node'>;
export type RiskManagerNode = Node<{ name: string, description: string, status: string }, 'risk-manager-node'>;
export type StrategyNode = Node<{ name: string, description: string, status: string }, 'strategy-node'>;
export type TradingAccountNode = Node<{ name: string, description: string, status: string }, 'trading-account-node'>;
export type AppNode = BuiltInNode | AgentNode | ForecasterNode | JimSimonsNode | InvestmentReportNode | JsonOutputNode | PortfolioStartNode | PortfolioManagerNode | StockAnalyzerNode | MemoryNode | ResearchAreaNode | ResearchCompaniesNode | RiskManagerNode | StrategyNode | TradingAccountNode;
