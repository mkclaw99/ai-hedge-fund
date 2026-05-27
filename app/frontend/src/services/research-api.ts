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

export interface MaterialsStatus {
  has_brief: boolean;
  filename?: string | null;
  brief?: string | null;
}

/** Current PDF-materials status for a flow (whether a distilled brief exists). */
export async function getMaterials(flowId: number): Promise<MaterialsStatus> {
  try {
    const res = await fetch(`${API_BASE_URL}/research/materials?flow_id=${flowId}`);
    if (!res.ok) return { has_brief: false };
    return await res.json();
  } catch {
    return { has_brief: false };
  }
}

/** Upload a PDF information base for a flow; backend extracts + distills a brief. */
export async function uploadMaterials(
  flowId: number,
  file: File,
): Promise<{ filename: string; source_chars: number; brief: string }> {
  const fd = new FormData();
  fd.append('flow_id', String(flowId));
  fd.append('file', file);
  const res = await fetch(`${API_BASE_URL}/research/materials`, { method: 'POST', body: fd });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail?.detail || `Upload failed (${res.status})`);
  }
  return res.json();
}
