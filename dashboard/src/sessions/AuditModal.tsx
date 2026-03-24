import { useState, useMemo, useEffect } from 'react';
import { useRunAudit } from '../hooks/useAudit';
import { useJudgeSettings } from '../hooks/useJudgeSettings';

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

interface Props {
  sessionId: string;
  messageCount: number;
  onClose: () => void;
  onComplete: () => void;
}

export default function AuditModal({ sessionId, messageCount, onClose, onComplete }: Props) {
  const { data: savedSettings } = useJudgeSettings();
  const [provider, setProvider] = useState('anthropic');
  const [model, setModel] = useState('claude-sonnet-4-20250514');
  const [apiKey, setApiKey] = useState('');
  const [useSavedKey, setUseSavedKey] = useState(false);
  const runAudit = useRunAudit();

  // Pre-fill from saved settings when they load
  useEffect(() => {
    if (savedSettings) {
      if (savedSettings.provider) setProvider(savedSettings.provider);
      if (savedSettings.model) setModel(savedSettings.model);
      if (savedSettings.key_set) setUseSavedKey(true);
    }
  }, [savedSettings]);

  const isOpenRouter = provider === 'openrouter';
  const models = PROVIDER_MODELS[provider] || [];
  const estimatedCost = (messageCount * 0.01).toFixed(2);
  const hasSavedKey = savedSettings?.key_set === true;

  const keyPlaceholder = useMemo(() => {
    if (isOpenRouter) return 'sk-or-...';
    if (provider === 'anthropic') return 'sk-ant-...';
    if (provider === 'openai') return 'sk-...';
    return 'AIza...';
  }, [provider, isOpenRouter]);

  // When provider changes, reset model to first available or empty for openrouter
  function handleProviderChange(newProvider: string) {
    setProvider(newProvider);
    const providerModels = PROVIDER_MODELS[newProvider];
    if (providerModels?.length) setModel(providerModels[0].value);
  }

  const canSubmit = model && (hasSavedKey || apiKey);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    runAudit.mutate(
      {
        sessionId,
        model,
        llmApiKey: useSavedKey && !apiKey ? '__saved__' : apiKey,
        provider,
      },
      {
        onSuccess: () => onComplete(),
      },
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bg-secondary border border-border rounded-lg w-full max-w-md mx-4 shadow-xl">
        <form onSubmit={handleSubmit}>
          <div className="p-5">
            <h2 className="text-base font-medium text-text-primary mb-4">Run Audit</h2>

            <label className="block text-sm text-text-muted mb-1">Provider</label>
            <select
              value={provider}
              onChange={(e) => handleProviderChange(e.target.value)}
              className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>

            <label className="block text-sm text-text-muted mb-1">Model</label>
            <select
              value={models.some(m => m.value === model) ? model : '__custom__'}
              onChange={(e) => {
                if (e.target.value === '__custom__') {
                  setModel('');
                } else {
                  setModel(e.target.value);
                }
              }}
              className="w-full mb-1.5 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
            >
              {models.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
              {isOpenRouter && <option value="__custom__">Custom model ID...</option>}
            </select>
            {isOpenRouter && !models.some(m => m.value === model) && (
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="provider/model-name (e.g. meta-llama/llama-3.3-70b)"
                className="w-full mb-1.5 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
            )}
            <div className="mb-3" />

            <label className="block text-sm text-text-muted mb-1">API Key</label>
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
                <span className="text-sm text-green-400">Key saved</span>
              </div>
            )}
            {(!hasSavedKey || !useSavedKey) ? (
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={keyPlaceholder}
                required={!hasSavedKey}
                className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
            ) : (
              <div className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-muted">
                Using saved API key (enter key below to override)
              </div>
            )}
            {hasSavedKey && useSavedKey && (
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Override with one-time key (optional)"
                className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
            )}

            <div className="text-sm text-text-muted mb-3">
              Estimated cost: ~${estimatedCost} ({messageCount} messages)
            </div>

            <div className="text-sm text-text-muted/70 bg-bg-primary border border-border rounded px-3 py-2 mb-3">
              {hasSavedKey && useSavedKey && !apiKey
                ? 'Using your saved API key from Settings.'
                : 'Your API key is used for this request only and is never stored.'}
            </div>

            {runAudit.isError && (
              <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/30 rounded px-3 py-2 mb-3">
                {runAudit.error instanceof Error
                  ? runAudit.error.message
                  : 'Audit failed. Check your API key and try again.'}
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 px-5 py-3 border-t border-border">
            <button
              type="button"
              onClick={onClose}
              disabled={runAudit.isPending}
              className="px-3 py-1.5 text-sm text-text-secondary border border-border rounded hover:bg-bg-tertiary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit || runAudit.isPending}
              className="px-3 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
            >
              {runAudit.isPending ? 'Running...' : 'Run Audit'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
