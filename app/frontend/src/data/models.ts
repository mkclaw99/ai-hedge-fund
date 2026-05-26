import { api } from '@/services/api';

export interface LanguageModel {
  display_name: string;
  model_name: string;
  provider: "Anthropic" | "DeepSeek" | "Google" | "Groq" | "OpenAI";
}

// Cache for models to avoid repeated API calls
let languageModels: LanguageModel[] | null = null;

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
 * Get the default model (Gemini 3.1 Pro) from the models list.
 * Falls back to any Google model, then the first available model.
 */
export const getDefaultModel = async (): Promise<LanguageModel | null> => {
  try {
    const models = await getModels();
    return (
      models.find(model => model.model_name === "gemini-3.1-pro-preview") ||
      models.find(model => model.provider === "Google") ||
      models[0] ||
      null
    );
  } catch (error) {
    console.error('Failed to get default model:', error);
    return null;
  }
};
