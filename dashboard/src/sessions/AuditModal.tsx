import { useState, useMemo, useEffect } from 'react';
import { useAuth } from '../auth/AuthContext';
import { useJudgeSettings } from '../hooks/useJudgeSettings';
import { useLlmProviders } from '../hooks/useLlmProviders';
import { useBackgroundTasks } from '../components/BackgroundTasks';
import { Dialog, DialogHeader, DialogFooter } from '../components/ui/Dialog';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Select } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

interface Props {
  open: boolean;
  sessionId: string;
  sessionTitle?: string;
  messageCount: number;
  onClose: () => void;
  onComplete: () => void;
}

export default function AuditModal({ open, sessionId, sessionTitle, messageCount, onClose, onComplete }: Props) {
  const { auth } = useAuth();
  const { data: savedSettings } = useJudgeSettings();
  const { data: providerData } = useLlmProviders();
  const [provider, setProvider] = useState('anthropic');
  const [model, setModel] = useState('claude-sonnet-4-6');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [useSavedKey, setUseSavedKey] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discoveredModels, setDiscoveredModels] = useState<{ id: string; owned_by: string }[]>([]);
  const [fetchedModels, setFetchedModels] = useState<{ id: string; owned_by: string }[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'ok' | 'fail'>('idle');
  const { startAudit } = useBackgroundTasks();

  const providers = providerData?.providers ?? [];
  const currentProvider = providers.find((p) => p.id === provider);
  const isDiscovery = currentProvider?.discovery === true;

  // Pre-fill from saved settings
  useEffect(() => {
    if (savedSettings) {
      if (savedSettings.provider) setProvider(savedSettings.provider);
      if (savedSettings.model) setModel(savedSettings.model);
      if (savedSettings.key_set) setUseSavedKey(true);
      if (savedSettings.base_url) setBaseUrl(savedSettings.base_url);
    }
  }, [savedSettings]);

  // Discover models when base URL is set
  useEffect(() => {
    if (!baseUrl || !auth) {
      setDiscoveredModels([]);
      return;
    }
    const timer = setTimeout(async () => {
      setDiscovering(true);
      try {
        const result = await auth.client.discoverModels(baseUrl, apiKey || undefined);
        setDiscoveredModels(result.models || []);
      } catch {
        setDiscoveredModels([]);
      } finally {
        setDiscovering(false);
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [baseUrl, apiKey, auth]);

  const isOpenRouter = provider === 'openrouter';
  const models = fetchedModels.length > 0
    ? fetchedModels.map((m) => ({ value: m.id, label: m.id + (m.owned_by ? ` (${m.owned_by})` : '') }))
    : (currentProvider?.suggested_models ?? []).map((m) => ({ value: m, label: m }));
  const estimatedCost = (messageCount * 0.01).toFixed(2);
  const hasSavedKey = savedSettings?.key_set === true;
  const hasBaseUrl = !!baseUrl;

  const keyPlaceholder = useMemo(() => {
    if (isOpenRouter) return 'sk-or-…';
    if (provider === 'anthropic') return 'sk-ant-…';
    if (provider === 'google') return 'AIza…';
    return 'sk-…';
  }, [provider, isOpenRouter]);

  function handleProviderChange(newProvider: string) {
    setProvider(newProvider);
    setFetchedModels([]);
    const next = providers.find((p) => p.id === newProvider);
    if (next?.suggested_models.length && !hasBaseUrl) setModel(next.suggested_models[0]);
  }

  async function handleFetchModels() {
    if (!auth || !isDiscovery) return;
    setFetching(true);
    try {
      const result = await auth.client.discoverModels('', apiKey || undefined, provider);
      setFetchedModels(result.models || []);
    } catch {
      setFetchedModels([]);
    } finally {
      setFetching(false);
    }
  }

  async function handleTestConnection() {
    if (!baseUrl || !auth) return;
    setTestStatus('testing');
    try {
      const result = await auth.client.discoverModels(baseUrl, apiKey || undefined);
      setTestStatus(result.models && result.models.length > 0 ? 'ok' : 'fail');
    } catch {
      setTestStatus('fail');
    }
  }

  const canSubmit = model && (hasSavedKey || apiKey || hasBaseUrl) && !submitting;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    const key = useSavedKey && !apiKey ? '' : apiKey;

    try {
      startAudit(
        sessionId,
        sessionTitle || sessionId.slice(0, 12),
        model,
        key,
        provider,
        baseUrl || undefined,
      );
      onComplete();
    } catch (err) {
      setError(errorMessage(err, 'Failed to start audit'));
      setSubmitting(false);
    }
  }

  const providerOptions = providers.map((p) => ({ value: p.id, label: p.label }));

  const modelSelectOptions = useMemo(() => {
    if (hasBaseUrl && discoveredModels.length > 0) {
      return discoveredModels.map((m) => ({
        value: m.id,
        label: m.id + (m.owned_by ? ` (${m.owned_by})` : ''),
      }));
    }
    return models.map((m) => ({ value: m.value, label: m.label }));
  }, [hasBaseUrl, discoveredModels, models]);

  return (
    <Dialog open={open} onClose={onClose} titleId="audit-modal-title">
      <DialogHeader titleId="audit-modal-title">Run Audit</DialogHeader>

      <form onSubmit={handleSubmit} className="space-y-3">
        <Select
          title="Provider"
          value={provider}
          onValueChange={(v) => handleProviderChange(v)}
          options={providerOptions}
        />

        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Model
            {discovering && <span className="text-text-tertiary ml-2">discovering…</span>}
          </label>
          {hasBaseUrl && discoveredModels.length > 0 ? (
            <Select
              value={model}
              onValueChange={(v) => setModel(v)}
              options={[{ value: '', label: 'Select a model…' }, ...modelSelectOptions]}
            />
          ) : hasBaseUrl ? (
            <Input
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="model-name"
            />
          ) : (
            <>
              <Select
                value={models.some((m) => m.value === model) ? model : '__custom__'}
                onValueChange={(v) => {
                  if (v === '__custom__') setModel('');
                  else setModel(v);
                }}
                options={[
                  ...models.map((m) => ({ value: m.value, label: m.label })),
                  { value: '__custom__', label: 'Other model…' },
                ]}
              />
              {!models.some((m) => m.value === model) && (
                <Input
                  type="text"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder={isOpenRouter ? 'vendor/model-name' : 'model-name'}
                  className="mt-1.5"
                />
              )}
            </>
          )}
          {isDiscovery && !hasBaseUrl && (
            <Button
              type="button"
              variant="secondary"
              onClick={handleFetchModels}
              disabled={fetching}
              loading={fetching}
              className="mt-1.5"
            >
              Fetch models
            </Button>
          )}
        </div>

        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">API Key</label>
          {hasSavedKey && (
            <div className="flex items-center gap-2 mb-2">
              <label className="flex items-center gap-1.5 text-sm text-text-secondary cursor-pointer">
                <input
                  type="checkbox"
                  checked={useSavedKey}
                  onChange={(e) => setUseSavedKey(e.target.checked)}
                  className="rounded border-border"
                />
                Use saved API key
              </label>
              <span className="text-sm text-success">Key saved</span>
            </div>
          )}
          {(!hasSavedKey || !useSavedKey) && (
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={hasBaseUrl ? 'Optional for local endpoints' : keyPlaceholder}
              required={!hasSavedKey && !hasBaseUrl}
            />
          )}
          {hasSavedKey && useSavedKey && (
            <Input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Override with one-time key (optional)"
              className="mt-2"
            />
          )}
        </div>

        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">
            Base URL <span className="text-text-tertiary">(optional)</span>
          </label>
          <div className="flex gap-2 mb-1">
            <Input
              type="text"
              value={baseUrl}
              onChange={(e) => { setBaseUrl(e.target.value); setTestStatus('idle'); }}
              placeholder="https://litellm.company.internal/v1"
              className="flex-1"
            />
            {baseUrl && (
              <button
                type="button"
                onClick={handleTestConnection}
                disabled={testStatus === 'testing'}
                className="px-2 py-1.5 text-xs border border-border rounded hover:bg-bg-tertiary transition-colors whitespace-nowrap text-text-secondary"
              >
                {testStatus === 'testing' ? 'Testing…' :
                 testStatus === 'ok' ? 'Connected' :
                 testStatus === 'fail' ? 'Failed' : 'Test'}
              </button>
            )}
          </div>
          {testStatus === 'ok' && <p className="text-xs text-success mb-3">Connection successful ({discoveredModels.length} models found)</p>}
          {testStatus === 'fail' && <p className="text-xs text-danger mb-3">Connection failed — check URL and API key</p>}
          {!baseUrl && <p className="text-xs text-text-tertiary mb-1">Leave blank for provider default. Set for LiteLLM, vLLM, Ollama.</p>}
        </div>

        <div className="text-sm text-text-tertiary">
          Estimated cost: ~${estimatedCost} ({messageCount} messages)
        </div>

        <div className="text-sm text-text-tertiary bg-bg-sunken border border-border rounded-lg px-3 py-2">
          {hasSavedKey && useSavedKey && !apiKey
            ? 'Using your saved API key from Settings.'
            : hasBaseUrl && !apiKey
            ? 'Using custom endpoint (no API key required for local endpoints).'
            : 'Your API key is used for this request only and is never stored.'}
        </div>

        {!!error && (
          <div className="text-sm text-danger bg-danger/10 border border-danger/30 rounded-lg px-3 py-2">
            {error}
          </div>
        )}
      </form>

      <DialogFooter>
        <Button variant="secondary" onClick={onClose} disabled={submitting}>
          Cancel
        </Button>
        <Button
          type="submit"
          onClick={handleSubmit}
          disabled={!canSubmit || submitting}
          loading={submitting}
        >
          {submitting ? 'Running…' : 'Run Audit'}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}
