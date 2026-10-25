import { useState, useEffect } from 'react';
import { useAuth } from '../auth/AuthContext';
import { useJudgeSettings, useSaveJudgeSettings, useClearJudgeSettings } from '../hooks/useJudgeSettings';
import { useLlmProviders } from '../hooks/useLlmProviders';
import { useToast } from '../hooks/useToast';
import { judgeSettingsSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import FieldError from '../components/FieldError';
import { Card, Button, Input, Select } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

export default function AuditJudgeSettings() {
  const { auth } = useAuth();
  const { addToast } = useToast();
  const { data: judgeSettings, isLoading: judgeLoading } = useJudgeSettings();
  const { data: providerData } = useLlmProviders();
  const saveJudge = useSaveJudgeSettings();
  const clearJudge = useClearJudgeSettings();

  const providers = providerData?.providers ?? [];
  const [judgeProvider, setJudgeProvider] = useState('openrouter');
  const [judgeModel, setJudgeModel] = useState('');
  const [judgeApiKey, setJudgeApiKey] = useState('');
  const [judgeBaseUrl, setJudgeBaseUrl] = useState('');
  const [judgeSaved, setJudgeSaved] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<{ id: string; owned_by: string }[]>([]);
  const [fetchedProviderModels, setFetchedProviderModels] = useState<{ id: string; owned_by: string }[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [judgeErrors, setJudgeErrors] = useState<FieldErrors>({});

  const currentProvider = providers.find((p) => p.id === judgeProvider);
  const isDiscovery = currentProvider?.discovery === true;

  // Populate from saved settings
  useEffect(() => {
    if (judgeSettings) {
      if (judgeSettings.provider) setJudgeProvider(judgeSettings.provider);
      if (judgeSettings.model) setJudgeModel(judgeSettings.model);
      if (judgeSettings.base_url) setJudgeBaseUrl(judgeSettings.base_url);
    }
  }, [judgeSettings]);

  // Discover models when base URL changes (custom endpoints)
  useEffect(() => {
    if (!judgeBaseUrl || !auth) {
      setDiscoveredModels([]);
      return;
    }
    const timer = setTimeout(async () => {
      setDiscovering(true);
      try {
        const result = await auth.client.discoverModels(judgeBaseUrl, judgeApiKey || undefined);
        setDiscoveredModels(result.models || []);
      } catch {
        setDiscoveredModels([]);
      } finally {
        setDiscovering(false);
      }
    }, 500); // debounce
    return () => clearTimeout(timer);
  }, [judgeBaseUrl, judgeApiKey, auth]);

  if (!auth) return null;

  const providerOptions = providers.map((p) => ({ value: p.id, label: p.label }));
  const suggestedOptions = (currentProvider?.suggested_models ?? []).map((m) => ({ value: m, label: m }));
  const keySet = judgeSettings?.key_set === true;

  function handleProviderChange(newProvider: string) {
    setJudgeProvider(newProvider);
    setJudgeSaved(false);
    setFetchedProviderModels([]);
    const next = providers.find((p) => p.id === newProvider);
    setJudgeModel(next?.suggested_models[0] ?? '');
  }

  async function handleFetchModels() {
    if (!auth || !isDiscovery) return;
    setFetchingModels(true);
    try {
      const result = await auth.client.discoverModels('', judgeApiKey || undefined, judgeProvider);
      setFetchedProviderModels(result.models || []);
    } catch {
      setFetchedProviderModels([]);
    } finally {
      setFetchingModels(false);
    }
  }

  function handleSaveJudge() {
    const result = judgeSettingsSchema.safeParse({
      provider: judgeProvider,
      model: judgeModel,
      apiKey: judgeApiKey || undefined,
      baseUrl: judgeBaseUrl || undefined,
    });
    if (!result.success) {
      setJudgeErrors(fieldErrorsFromZod(result.error));
      return;
    }
    setJudgeErrors({});
    saveJudge.mutate(
      { provider: judgeProvider, model: judgeModel, apiKey: judgeApiKey, baseUrl: judgeBaseUrl || undefined },
      {
        onSuccess: () => {
          setJudgeSaved(true);
          setJudgeApiKey('');
          addToast('success', 'Settings saved');
        },
      },
    );
  }

  function handleClearJudge() {
    clearJudge.mutate(undefined, {
      onSuccess: () => {
        setJudgeApiKey('');
        setJudgeBaseUrl('');
        setJudgeSaved(false);
      },
    });
  }

  const isOpenRouter = judgeProvider === 'openrouter';
  const keyPlaceholder =
    isOpenRouter ? 'sk-or-…' :
    judgeProvider === 'anthropic' ? 'sk-ant-…' :
    judgeProvider === 'google' ? 'AIza…' :
    'sk-…';

  const fetchedOptions = fetchedProviderModels.map((m) => ({
    value: m.id,
    label: m.id + (m.owned_by ? ` (${m.owned_by})` : ''),
  }));

  return (
    <div className="space-y-5">
      {/* Audit Judge */}
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-2">Security review model</h2>
        <p className="text-sm text-text-tertiary mb-4">
          Configure the LLM (the "Audit Judge") that reviews sessions for security findings. Your key is stored server-side and used only for audit requests.
        </p>

        {judgeLoading ? (
          <div className="text-sm text-text-tertiary py-2">Loading settings…</div>
        ) : (
          <>
            <div className="mb-4">
              <label className="text-sm text-text-tertiary block mb-1">Provider</label>
              <Select
                value={judgeProvider}
                onValueChange={(v) => handleProviderChange(v)}
                options={providerOptions}
              />
            </div>

            <div className="mb-4">
              <label className="text-sm text-text-tertiary block mb-1">
                Model
                {discovering && <span className="text-text-tertiary opacity-50 ml-2">discovering…</span>}
                {fetchingModels && <span className="text-text-tertiary opacity-50 ml-2">fetching…</span>}
              </label>
              {judgeBaseUrl && discoveredModels.length > 0 ? (
                <Select
                  value={judgeModel}
                  onValueChange={(v) => { setJudgeModel(v); setJudgeSaved(false); }}
                  options={[
                    { value: '', label: 'Select a model…' },
                    ...discoveredModels.map((m) => ({ value: m.id, label: m.id + (m.owned_by ? ` (${m.owned_by})` : '') })),
                  ]}
                />
              ) : judgeBaseUrl ? (
                <Input
                  type="text"
                  value={judgeModel}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder="model-name"
                  title="Model"
                  className="font-mono"
                />
              ) : fetchedOptions.length > 0 ? (
                <Select
                  value={judgeModel}
                  onValueChange={(v) => { setJudgeModel(v); setJudgeSaved(false); }}
                  options={[{ value: '', label: 'Select a model…' }, ...fetchedOptions]}
                />
              ) : isOpenRouter || suggestedOptions.length === 0 ? (
                <Input
                  type="text"
                  value={judgeModel}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder={isOpenRouter ? 'vendor/model-name' : 'model-name'}
                  title="Model"
                  className="font-mono"
                />
              ) : (
                <Select
                  value={judgeModel}
                  onValueChange={(v) => { setJudgeModel(v); setJudgeSaved(false); }}
                  options={suggestedOptions}
                />
              )}
              {isDiscovery && !judgeBaseUrl && (
                <Button
                  type="button"
                  variant="secondary"
                  onClick={handleFetchModels}
                  disabled={fetchingModels}
                  loading={fetchingModels}
                  className="mt-1.5"
                >
                  Fetch models
                </Button>
              )}
              <FieldError message={judgeErrors.model} />
            </div>

            <div className="mb-4">
              <Input
                type="password"
                value={judgeApiKey}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeApiKey(e.target.value); setJudgeSaved(false); if (judgeErrors.apiKey) setJudgeErrors((prev) => ({ ...prev, apiKey: undefined })); }}
                placeholder={keyPlaceholder}
                title="API Key"
                className="font-mono"
              />
              <FieldError message={judgeErrors.apiKey} />
            </div>

            <div className="mb-4">
              <Input
                type="text"
                value={judgeBaseUrl}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeBaseUrl(e.target.value); setJudgeSaved(false); if (judgeErrors.baseUrl) setJudgeErrors((prev) => ({ ...prev, baseUrl: undefined })); }}
                placeholder="https://litellm.company.internal/v1"
                title="Base URL (optional)"
                className="font-mono"
              />
              <FieldError message={judgeErrors.baseUrl} />
              <p className="text-xs text-text-tertiary opacity-60 mt-1">
                Leave blank for provider default. Set for LiteLLM, vLLM, Ollama, or any OpenAI-compatible gateway.
              </p>
            </div>

            <div className="flex items-center gap-3 mb-3">
              <Button
                variant="primary"
                onClick={handleSaveJudge}
                disabled={!judgeModel || (!judgeApiKey && !judgeBaseUrl) || saveJudge.isPending}
                loading={saveJudge.isPending}
              >
                Save
              </Button>
              {keySet && (
                <Button
                  variant="danger"
                  onClick={handleClearJudge}
                  disabled={clearJudge.isPending}
                  loading={clearJudge.isPending}
                >
                  Clear Key
                </Button>
              )}
            </div>

            <div className="text-sm">
              {judgeSaved && <span className="text-success">Key saved</span>}
              {!judgeSaved && keySet && <span className="text-success">Key saved</span>}
              {!judgeSaved && !keySet && <span className="text-text-tertiary">No key configured</span>}
            </div>

            {saveJudge.isError && (
              <div className="text-sm text-danger mt-2">
                {errorMessage(saveJudge.error, 'Failed to save settings.')}
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
