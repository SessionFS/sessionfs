import { useState, useEffect } from 'react';
import { useAuth } from '../auth/AuthContext';
import { useJudgeSettings, useSaveJudgeSettings, useClearJudgeSettings } from '../hooks/useJudgeSettings';
import { useToast } from '../hooks/useToast';
import { judgeSettingsSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import FieldError from '../components/FieldError';
import { Card, Button, Input, Select } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

const PROVIDERS = [
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'google', label: 'Google' },
] as const;

const PROVIDER_MODELS: Record<string, { value: string; label: string }[]> = {
  anthropic: [
    { value: 'claude-opus-4-6', label: 'Claude Opus 4.6' },
    { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
    { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5' },
  ],
  openai: [
    { value: 'gpt-5.2', label: 'GPT-5.2' },
    { value: 'gpt-5.2-pro', label: 'GPT-5.2 Pro' },
    { value: 'gpt-4o', label: 'GPT-4o' },
    { value: 'o3', label: 'o3 (reasoning)' },
    { value: 'o4-mini', label: 'o4 Mini (reasoning)' },
  ],
  google: [
    { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
    { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
  ],
  openrouter: [
    { value: 'anthropic/claude-opus-4-6', label: 'Claude Opus 4.6' },
    { value: 'anthropic/claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
    { value: 'openai/gpt-5.2', label: 'GPT-5.2' },
    { value: 'openai/gpt-4o', label: 'GPT-4o' },
    { value: 'openai/o3', label: 'o3 (reasoning)' },
    { value: 'google/gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
    { value: 'deepseek/deepseek-v3.2', label: 'DeepSeek V3.2' },
    { value: 'deepseek/deepseek-r1', label: 'DeepSeek R1' },
  ],
};

export default function AuditJudgeSettings() {
  const { auth } = useAuth();
  const { addToast } = useToast();
  const { data: judgeSettings, isLoading: judgeLoading } = useJudgeSettings();
  const saveJudge = useSaveJudgeSettings();
  const clearJudge = useClearJudgeSettings();

  const [judgeProvider, setJudgeProvider] = useState('openrouter');
  const [judgeModel, setJudgeModel] = useState('');
  const [judgeApiKey, setJudgeApiKey] = useState('');
  const [judgeBaseUrl, setJudgeBaseUrl] = useState('');
  const [judgeSaved, setJudgeSaved] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<{ id: string; owned_by: string }[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [judgeErrors, setJudgeErrors] = useState<FieldErrors>({});

  // Populate from saved settings
  useEffect(() => {
    if (judgeSettings) {
      if (judgeSettings.provider) setJudgeProvider(judgeSettings.provider);
      if (judgeSettings.model) setJudgeModel(judgeSettings.model);
      if (judgeSettings.base_url) setJudgeBaseUrl(judgeSettings.base_url);
    }
  }, [judgeSettings]);

  // Discover models when base URL changes
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

  const isOpenRouter = judgeProvider === 'openrouter';
  const providerModels = PROVIDER_MODELS[judgeProvider] || [];
  const keySet = judgeSettings?.key_set === true;

  function handleProviderChange(newProvider: string) {
    setJudgeProvider(newProvider);
    setJudgeSaved(false);
    if (newProvider === 'openrouter') {
      setJudgeModel('');
    } else {
      const models = PROVIDER_MODELS[newProvider];
      if (models?.length) setJudgeModel(models[0].value);
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

  const keyPlaceholder =
    isOpenRouter ? 'sk-or-…' :
    judgeProvider === 'anthropic' ? 'sk-ant-…' :
    judgeProvider === 'openai' ? 'sk-…' :
    'AIza…';

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
                options={[...PROVIDERS]}
              />
            </div>

            <div className="mb-4">
              <label className="text-sm text-text-tertiary block mb-1">
                Model
                {discovering && <span className="text-text-tertiary opacity-50 ml-2">discovering…</span>}
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
              ) : isOpenRouter ? (
                <Input
                  type="text"
                  value={judgeModel}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder="anthropic/claude-sonnet-4"
                  title="Model"
                  className="font-mono"
                />
              ) : (
                <Select
                  value={judgeModel}
                  onValueChange={(v) => { setJudgeModel(v); setJudgeSaved(false); }}
                  options={providerModels}
                />
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
