import {
  BadgeDollarSign,
  Bot,
  Brain,
  Calculator,
  ChartLine,
  ChartPie,
  Database,
  FlaskConical,
  ListChecks,
  LucideIcon,
  Network,
  Play,
  Wallet,
  Zap
} from 'lucide-react';
import { Agent, getAgents } from './agents';

// Define component items by group
export interface ComponentItem {
  name: string;
  icon: LucideIcon;
  description?: string;
}

export interface ComponentGroup {
  name: string;
  icon: LucideIcon;
  iconColor: string;
  description?: string;
  items: ComponentItem[];
}

/**
 * Get all component groups, including agents fetched from the backend
 */
export const getComponentGroups = async (): Promise<ComponentGroup[]> => {
  const agents = await getAgents();

  return [
    {
      name: "Start Nodes",
      icon: Play,
      iconColor: "text-blue-500",
      description: "Entry points that feed data into a flow. Every flow begins with one of these.",
      items: [
        { name: "Portfolio Input", icon: ChartPie, description: "Defines the starting portfolio: initial cash, tickers, and the position limits agents trade within." },
        { name: "Stock Input", icon: ChartLine, description: "Supplies the stock ticker(s) and date range to analyze." },
      ]
    },
    {
      name: "Research",
      icon: FlaskConical,
      iconColor: "text-amber-500",
      description: "Two chained researcher roles that turn an investment theme into a company universe for the Analysts.",
      items: [
        { name: "Fundamental Research", icon: FlaskConical, description: "Topic researcher: under your mandate, reads your materials + analyst's data and writes a fundamental research note. Connect it to a Fundamental Companies node." },
        { name: "Fundamental Companies", icon: ListChecks, description: "Company researcher: extracts the relevant, tradable companies from the Fundamental Research note and hands them to the Analysts." },
      ]
    },
    {
      name: "Analysts",
      icon: Bot,
      iconColor: "text-red-500",
      description: "Individual investor-style agents. Each studies the inputs and outputs a bullish or bearish signal with its reasoning.",
      items: agents.map((agent: Agent) => ({
        name: agent.display_name,
        icon: Bot,
        description: agent.description || agent.investing_style,
      }))
    },
    {
      name: "Swarms",
      icon: Network,
      iconColor: "text-yellow-500",
      description: "Preset bundles of several analyst agents you can drop in with a single click.",
      items: [
        { name: "Data Wizards", icon: Calculator, description: "A swarm of data- and fundamentals-driven analyst agents." },
        { name: "Market Mavericks", icon: Zap, description: "A swarm of momentum- and market-driven analyst agents." },
        { name: "Value Investors", icon: BadgeDollarSign, description: "A swarm of value-investing agents that hunt for undervalued companies." },
      ]
    },
    {
      name: "End Nodes",
      icon: Brain,
      iconColor: "text-green-500",
      description: "Terminal nodes that consume the analysts' signals to produce a decision.",
      items: [
        { name: "Strategy", icon: Brain, description: "Declares the trading rules (style, sizing, caps, holding period, instrument universe, free-text mandate). Read by the PM; the Trading Account enforces the position cap." },
        { name: "Portfolio Manager", icon: Brain, description: "Aggregates every analyst's signal and makes the final buy / sell / hold decisions and position sizes." },
      ]
    },
    {
      name: "Resources",
      icon: Database,
      iconColor: "text-purple-500",
      description: "Flow-scoped facilities the agents draw on. Drop one in to see and manage it.",
      items: [
        { name: "Memory", icon: Database, description: "This flow's research memory. It accumulates each run; analysts read back their own prior calls and the Portfolio Manager reads everything." },
        { name: "Trading Account", icon: Wallet, description: "Your trading account — paper-trading only for now (read-only Alpaca Paper). Set the starting budget and see cash / equity / buying power." },
        { name: "Risk Manager", icon: Database, description: "Tunes the volatility/correlation-based position caps that gate the Portfolio Manager. Auto-spawned with defaults — drop one in only to override." },
      ]
    },
  ];
};
