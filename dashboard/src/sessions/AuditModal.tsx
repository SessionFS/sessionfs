import { useState, useMemo } from 'react';
import { useRunAudit } from '../hooks/useAudit';

const MODELS = [
  { value: 'claude-sonnet-4-20250514', label: 'Claude Sonnet 4', provider: 'anthropic' },
  { value: 'gpt-4o', label: 'GPT-4o', provider: 'openai' },
  { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro', provider: 'google' },
];

interface Props {
  sessionId: string;
  messageCount: number;
  onClose: () => void;
  onComplete: () => void;
}

export default function AuditModal({ sessionId, messageCount, onClose, onComplete }: Props) {
  const [model, setModel] = useState(MODELS[0].value);
  const [apiKey, setApiKey] = useState('');
  const runAudit = useRunAudit();

  const selectedModel = MODELS.find((m) => m.value === model)!;
  const estimatedCost = (messageCount * 0.01).toFixed(2);

  const keyPlaceholder = useMemo(() => {
    if (selectedModel.provider === 'anthropic') return 'sk-ant-...';
    if (selectedModel.provider === 'openai') return 'sk-...';
    return 'AIza...';
  }, [selectedModel.provider]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    runAudit.mutate(
      {
        sessionId,
        model,
        llmApiKey: apiKey,
        provider: selectedModel.provider,
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

            <label className="block text-xs text-text-muted mb-1">Model</label>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
            >
              {MODELS.map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>

            <label className="block text-xs text-text-muted mb-1">API Key</label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={keyPlaceholder}
              required
              className="w-full mb-3 px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
            />

            <div className="text-xs text-text-muted mb-3">
              Estimated cost: ~${estimatedCost} ({messageCount} messages)
            </div>

            <div className="text-xs text-text-muted/70 bg-bg-primary border border-border rounded px-3 py-2 mb-3">
              Your API key is used for this request only and is never stored.
            </div>

            {runAudit.isError && (
              <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded px-3 py-2 mb-3">
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
              className="px-3 py-1.5 text-xs text-text-secondary border border-border rounded hover:bg-bg-tertiary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!apiKey || runAudit.isPending}
              className="px-3 py-1.5 text-xs bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
            >
              {runAudit.isPending ? 'Running...' : 'Run Audit'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
