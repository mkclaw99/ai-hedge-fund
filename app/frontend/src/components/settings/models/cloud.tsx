import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { clearDefaultModel, getPinnedDefault, LanguageModel, setDefaultModel } from '@/data/models';
import { cn } from '@/lib/utils';
import { Check, Cloud, RefreshCw, Star, X } from 'lucide-react';
import { useEffect, useState } from 'react';

interface CloudModelsProps {
  className?: string;
}

interface CloudModel {
  display_name: string;
  model_name: string;
  provider: string;
}

interface ModelProvider {
  name: string;
  models: Array<{
    display_name: string;
    model_name: string;
  }>;
}

export function CloudModels({ className }: CloudModelsProps) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Currently pinned default — `{provider, model_name}` or both null when
  // unset. Read on mount and after every set/clear so the "Default" badge
  // tracks the backend without a full refresh of the providers list.
  const [pinned, setPinned] = useState<{ provider: string | null; model_name: string | null }>({ provider: null, model_name: null });
  const [pendingDefault, setPendingDefault] = useState<string | null>(null); // `${provider}-${model_name}` while a PUT/DELETE is in flight

  const fetchProviders = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('http://localhost:8001/language-models/providers');
      if (response.ok) {
        const data = await response.json();
        setProviders(data.providers);
      } else {
        const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
        setError(`Failed to fetch providers: ${errorData.detail}`);
      }
    } catch (error) {
      console.error('Failed to fetch cloud model providers:', error);
      setError('Failed to connect to backend service');
    }
    setLoading(false);
  };

  const fetchPinned = async () => setPinned(await getPinnedDefault());

  useEffect(() => {
    fetchProviders();
    fetchPinned();
  }, []);

  const handleSetDefault = async (model: CloudModel) => {
    const key = `${model.provider}-${model.model_name}`;
    setPendingDefault(key);
    try {
      await setDefaultModel(model as LanguageModel);
      await fetchPinned();
    } catch (e) {
      console.error('Failed to set default model:', e);
    } finally {
      setPendingDefault(null);
    }
  };

  const handleClearDefault = async () => {
    setPendingDefault('__clear__');
    try {
      await clearDefaultModel();
      await fetchPinned();
    } catch (e) {
      console.error('Failed to clear default model:', e);
    } finally {
      setPendingDefault(null);
    }
  };

  // Flatten all models from all providers into a single array
  const allModels: CloudModel[] = providers.flatMap(provider =>
    provider.models.map(model => ({
      ...model,
      provider: provider.name
    }))
  ).sort((a, b) => a.provider.localeCompare(b.provider));

  return (
    <div className={cn("space-y-6", className)}>

      {error && (
        <div className="bg-red-900/20 border border-red-600/30 rounded-lg p-4">
          <div className="flex items-start gap-3">
            <Cloud className="h-5 w-5 text-red-500 mt-0.5" />
            <div>
              <h4 className="font-medium text-red-300">Error</h4>
              <p className="text-sm text-red-500 mt-1">{error}</p>
            </div>
          </div>
        </div>
      )}

      <div className="space-y-2">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-primary
          ">Available Models</h3>
          <span className="text-xs text-muted-foreground">
            {allModels.length} models from {providers.length} providers
          </span>
        </div>

        {loading ? (
          <div className="text-center py-8">
            <RefreshCw className="h-8 w-8 mx-auto mb-2 animate-spin text-muted-foreground" />
            <p className="text-sm text-muted-foreground">Loading cloud models...</p>
          </div>
        ) : allModels.length > 0 ? (
          <TooltipProvider>
          <div className="space-y-1">
            {allModels.map((model) => {
              const isDefault = pinned.provider === model.provider && pinned.model_name === model.model_name;
              const key = `${model.provider}-${model.model_name}`;
              const isPending = pendingDefault === key;
              return (
              <div
                key={key}
                className="group flex items-center justify-between bg-muted hover-bg rounded-md px-3 py-2.5 transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    {isDefault && (
                      // Star icon next to the display name so the current default
                      // is also spotted on a quick scan — not just inferred from
                      // the badge column. Tooltip says how to clear it.
                      <Tooltip delayDuration={200}>
                        <TooltipTrigger asChild>
                          <Star className="h-3.5 w-3.5 text-amber-500 fill-amber-500 shrink-0" aria-label="Default model" />
                        </TooltipTrigger>
                        <TooltipContent side="top">Currently pinned as the default for new agent nodes.</TooltipContent>
                      </Tooltip>
                    )}
                    <span className="font-medium text-sm truncate text-primary">{model.display_name}</span>
                    {model.model_name !== model.display_name && (
                      <span className="font-mono text-xs text-muted-foreground">
                        {model.model_name}
                      </span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  {isDefault ? (
                    // Default-active: badge stays visible, X button clears the pin
                    // (group-hover, so it doesn't add clutter when scanning).
                    <>
                      <Badge className="text-xs bg-amber-500/10 text-amber-500 border-amber-500/40">
                        <Check className="h-3 w-3 mr-1" /> Default
                      </Badge>
                      <Tooltip delayDuration={200}>
                        <TooltipTrigger asChild>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={isPending || pendingDefault === '__clear__'}
                            onClick={handleClearDefault}
                            className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 transition-opacity"
                            aria-label="Clear default model"
                          >
                            <X className="h-3.5 w-3.5" />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="max-w-xs">
                          Unpin. New agent nodes fall back to the hardcoded chain
                          (Gemini 3.1 Pro Preview → any Google → first available).
                        </TooltipContent>
                      </Tooltip>
                    </>
                  ) : (
                    // Not default: "Set as default" button on hover.
                    <Tooltip delayDuration={200}>
                      <TooltipTrigger asChild>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={isPending}
                          onClick={() => handleSetDefault(model)}
                          className="h-6 text-xs opacity-0 group-hover:opacity-100 transition-opacity"
                          aria-label={`Set ${model.display_name} as the default model`}
                        >
                          {isPending ? '…' : 'Set as default'}
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="max-w-xs">
                        Pin this model so newly-added agent nodes adopt it by default.
                        Per-node selections still override it.
                      </TooltipContent>
                    </Tooltip>
                  )}
                  <Badge className="text-xs text-primary bg-primary/10 border-primary/30 hover:bg-primary/20 hover:border-primary/50">
                    {model.provider}
                  </Badge>
                </div>
              </div>
              );
            })}
          </div>
          </TooltipProvider>
        ) : (
          !loading && (
            <div className="text-center py-8 text-muted-foreground">
              <Cloud className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">No models available</p>
            </div>
          )
        )}
      </div>
    </div>
  );
} 