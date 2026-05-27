// Client for research-area endpoints (analyst-backed investment themes).
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

export interface ResearchTheme {
  slug: string;
  name: string;
  company_count?: number;
  status?: string;
}

/** Fetch analyst's investment themes for the Research Area dropdown (fail-soft to []). */
export async function getThemes(): Promise<ResearchTheme[]> {
  try {
    const res = await fetch(`${API_BASE_URL}/research/themes`);
    if (!res.ok) return [];
    const data = await res.json();
    return (data?.themes ?? []) as ResearchTheme[];
  } catch {
    return [];
  }
}
