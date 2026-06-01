import { api } from '@/services/api';

export interface LanguageModel {
  display_name: string;
  model_name: string;
  provider: "Anthropic" | "DeepSeek" | "Google" | "Groq" | "OpenAI" | "LM Studio";
}

// Cache for models to avoid repeated API calls
let languageModels: LanguageModel[] | null = null;

// Cache for the user-pinned default. Stored on the backend (see
// /language-models/default), but we also memoise the current resolved
// LanguageModel here so getDefaultModel() during a render burst doesn't
// fire one fetch per agent node mount. Cleared by setDefaultModel /
// clearDefaultModel so the next read picks up the change.
let cachedDefault: LanguageModel | null | undefined = undefined; // undefined = not yet loaded

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

/**
 * Get the list of models from the backend API
 * Uses caching to avoid repeated API calls
 */
export const getModels = async (): Promise<LanguageModel[]> => {
  if (languageModels) {
    return languageModels;
  }

  try {
    languageModels = await api.getLanguageModels();
    return languageModels;
  } catch (error) {
    console.error('Failed to fetch models:', error);
    throw error; // Let the calling component handle the error
  }
};

/**
 * Resolve the default model new agent nodes adopt when nothing is selected.
 *
 * Priority:
 *   1. The user-pinned default from `GET /language-models/default` (if any).
 *   2. ``gemini-3.1-pro-preview`` — historical hardcoded preference.
 *   3. The first Google model on the list.
 *   4. The first available model (any provider).
 *   5. ``null`` if no models are reachable at all.
 *
 * The pin from (1) is set via Settings → Models → "Set default" and stored
 * server-side, so it survives browser reloads and works across browsers.
 */
export const getDefaultModel = async (): Promise<LanguageModel | null> => {
  if (cachedDefault !== undefined) return cachedDefault;
  try {
    const models = await getModels();
    let pinned: { provider: string | null; model_name: string | null } | null = null;
    try {
      const r = await fetch(`${API_BASE_URL}/language-models/default`);
      if (r.ok) pinned = await r.json();
    } catch { /* fall through to hardcoded chain */ }

    const fromPin = pinned?.model_name
      ? models.find((m) => m.model_name === pinned!.model_name && m.provider === pinned!.provider) || null
      : null;

    cachedDefault =
      fromPin ||
      models.find((m) => m.model_name === 'gemini-3.1-pro-preview') ||
      models.find((m) => m.provider === 'Google') ||
      models[0] ||
      null;
    return cachedDefault;
  } catch (error) {
    console.error('Failed to get default model:', error);
    return null;
  }
};

/**
 * Pin a model as the global default. Persisted server-side; next
 * ``getDefaultModel()`` returns this without going through the fallback chain.
 */
export const setDefaultModel = async (model: LanguageModel): Promise<void> => {
  const r = await fetch(`${API_BASE_URL}/language-models/default`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider: model.provider, model_name: model.model_name }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  cachedDefault = undefined; // invalidate so the next read picks up the new pin
};

/**
 * Unpin — new agent nodes fall back to the hardcoded chain (Gemini 3.1 Pro
 * preview → any Google → first available).
 */
export const clearDefaultModel = async (): Promise<void> => {
  const r = await fetch(`${API_BASE_URL}/language-models/default`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  cachedDefault = undefined;
};

/**
 * Returns the currently pinned ``{provider, model_name}`` from the backend,
 * or ``{provider: null, model_name: null}`` when nothing is pinned. Used by
 * Settings → Models to mark which row is the current default — distinct
 * from ``getDefaultModel`` which also runs the hardcoded fallback chain.
 */
export const getPinnedDefault = async (): Promise<{ provider: string | null; model_name: string | null }> => {
  try {
    const r = await fetch(`${API_BASE_URL}/language-models/default`);
    if (!r.ok) return { provider: null, model_name: null };
    return await r.json();
  } catch {
    return { provider: null, model_name: null };
  }
};
