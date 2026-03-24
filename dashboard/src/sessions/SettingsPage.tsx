import { useState, useEffect } from 'react';
import { useAuth } from '../auth/AuthContext';
import CopyButton from '../components/CopyButton';
import { useJudgeSettings, useSaveJudgeSettings, useClearJudgeSettings } from '../hooks/useJudgeSettings';

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
    { value: 'gpt-5.4', label: 'GPT-5.4' },
    { value: 'gpt-5.4-mini', label: 'GPT-5.4 Mini' },
    { value: 'gpt-5.3-codex', label: 'GPT-5.3 Codex' },
    { value: 'o3', label: 'o3' },
    { value: 'o4-mini', label: 'o4 Mini' },
  ],
  google: [
    { value: 'gemini-3.1-pro', label: 'Gemini 3.1 Pro' },
    { value: 'gemini-3.1-flash', label: 'Gemini 3.1 Flash' },
  ],
  openrouter: [
    { value: 'anthropic/claude-opus-4.6', label: 'Claude Opus 4.6' },
    { value: 'anthropic/claude-sonnet-4.6', label: 'Claude Sonnet 4.6' },
    { value: 'openai/gpt-5.4', label: 'GPT-5.4' },
    { value: 'openai/gpt-5.3-codex', label: 'GPT-5.3 Codex' },
    { value: 'openai/o3', label: 'o3' },
    { value: 'google/gemini-3.1-pro', label: 'Gemini 3.1 Pro' },
    { value: 'deepseek/deepseek-v3.2', label: 'DeepSeek V3.2' },
    { value: 'deepseek/deepseek-r1', label: 'DeepSeek R1' },
  ],
};

export default function SettingsPage() {
  const { auth, logout } = useAuth();
  const { data: judgeSettings, isLoading: judgeLoading } = useJudgeSettings();
  const saveJudge = useSaveJudgeSettings();
  const clearJudge = useClearJudgeSettings();

  const [judgeProvider, setJudgeProvider] = useState('openrouter');
  const [judgeModel, setJudgeModel] = useState('');
  const [judgeApiKey, setJudgeApiKey] = useState('');
  const [judgeSaved, setJudgeSaved] = useState(false);

  // Populate from saved settings
  useEffect(() => {
    if (judgeSettings) {
      if (judgeSettings.provider) setJudgeProvider(judgeSettings.provider);
      if (judgeSettings.model) setJudgeModel(judgeSettings.model);
    }
  }, [judgeSettings]);

  if (!auth) return null;

  const maskedKey = auth.apiKey.slice(0, 12) + '\u2026' + auth.apiKey.slice(-4);
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
    if (!judgeModel || !judgeApiKey) return;
    saveJudge.mutate(
      { provider: judgeProvider, model: judgeModel, apiKey: judgeApiKey },
      {
        onSuccess: () => {
          setJudgeSaved(true);
          setJudgeApiKey('');
        },
      },
    );
  }

  function handleClearJudge() {
    clearJudge.mutate(undefined, {
      onSuccess: () => {
        setJudgeApiKey('');
        setJudgeSaved(false);
      },
    });
  }

  const keyPlaceholder =
    isOpenRouter ? 'sk-or-...' :
    judgeProvider === 'anthropic' ? 'sk-ant-...' :
    judgeProvider === 'openai' ? 'sk-...' :
    'AIza...';

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <h1 className="text-lg font-medium text-text-primary mb-6">Settings</h1>

      <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Connection</h2>

        <div className="mb-3">
          <label className="text-xs text-text-muted block mb-1">Server URL</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1">
              {auth.baseUrl}
            </code>
            <CopyButton text={auth.baseUrl} />
          </div>
        </div>

        <div className="mb-3">
          <label className="text-xs text-text-muted block mb-1">API Key</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1 font-mono">
              {maskedKey}
            </code>
            <CopyButton text={auth.apiKey} />
          </div>
        </div>
      </div>

      {/* Judge Configuration */}
      <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Judge Configuration</h2>
        <p className="text-xs text-text-muted mb-3">
          Configure the LLM used for session audits. Your key is stored server-side and used only for audit requests.
        </p>

        {judgeLoading ? (
          <div className="text-xs text-text-muted py-2">Loading settings...</div>
        ) : (
          <>
            <div className="mb-3">
              <label className="text-xs text-text-muted block mb-1">Provider</label>
              <select
                value={judgeProvider}
                onChange={(e) => handleProviderChange(e.target.value)}
                className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>

            <div className="mb-3">
              <label className="text-xs text-text-muted block mb-1">Model</label>
              {isOpenRouter ? (
                <input
                  type="text"
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder="anthropic/claude-sonnet-4"
                  className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
                />
              ) : (
                <select
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent"
                >
                  {providerModels.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              )}
            </div>

            <div className="mb-3">
              <label className="text-xs text-text-muted block mb-1">API Key</label>
              <input
                type="password"
                value={judgeApiKey}
                onChange={(e) => { setJudgeApiKey(e.target.value); setJudgeSaved(false); }}
                placeholder={keyPlaceholder}
                className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
            </div>

            <div className="flex items-center gap-2 mb-2">
              <button
                onClick={handleSaveJudge}
                disabled={!judgeModel || !judgeApiKey || saveJudge.isPending}
                className="px-3 py-1.5 text-xs bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
              >
                {saveJudge.isPending ? 'Saving...' : 'Save'}
              </button>
              {keySet && (
                <button
                  onClick={handleClearJudge}
                  disabled={clearJudge.isPending}
                  className="px-3 py-1.5 text-xs border border-red-500/30 text-red-400 rounded hover:bg-red-500/10 transition-colors disabled:opacity-50"
                >
                  {clearJudge.isPending ? 'Clearing...' : 'Clear Key'}
                </button>
              )}
            </div>

            <div className="text-xs">
              {judgeSaved && <span className="text-green-400">Key saved</span>}
              {!judgeSaved && keySet && <span className="text-green-400">Key saved</span>}
              {!judgeSaved && !keySet && <span className="text-text-muted">No key configured</span>}
            </div>

            {saveJudge.isError && (
              <div className="text-xs text-red-400 mt-2">
                {saveJudge.error instanceof Error ? saveJudge.error.message : 'Failed to save settings.'}
              </div>
            )}
          </>
        )}
      </div>

      <button
        onClick={logout}
        className="px-4 py-2 text-sm border border-red-500/30 text-red-400 rounded hover:bg-red-500/10 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}
