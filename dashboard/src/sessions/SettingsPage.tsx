import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
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

export default function SettingsPage() {
  const { auth, logout } = useAuth();
  const { data: profile } = useQuery({
    queryKey: ['me'],
    queryFn: () => auth!.client.getMe(),
    enabled: !!auth,
  });
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
    if (!judgeModel || (!judgeApiKey && !judgeBaseUrl)) return;
    saveJudge.mutate(
      { provider: judgeProvider, model: judgeModel, apiKey: judgeApiKey, baseUrl: judgeBaseUrl || undefined },
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
        setJudgeBaseUrl('');
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

      {profile && (
        <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
          <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">Account</h2>
          <div className="mb-2">
            <label className="text-sm text-text-muted block mb-1">Email</label>
            <div className="text-sm text-text-primary">{profile.email}</div>
          </div>
          <div className="flex gap-4 text-sm text-text-secondary">
            <span>Tier: <span className="text-text-primary">{profile.tier}</span></span>
            <span>Verified: {profile.email_verified
              ? <span className="text-green-400">yes</span>
              : <span className="text-yellow-400">no</span>
            }</span>
          </div>
        </div>
      )}

      <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
        <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">Connection</h2>

        <div className="mb-3">
          <label className="text-sm text-text-muted block mb-1">Server URL</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1">
              {auth.baseUrl}
            </code>
            <CopyButton text={auth.baseUrl} />
          </div>
        </div>

        <div className="mb-3">
          <label className="text-sm text-text-muted block mb-1">API Key</label>
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
        <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">Judge Configuration</h2>
        <p className="text-sm text-text-muted mb-3">
          Configure the LLM used for session audits. Your key is stored server-side and used only for audit requests.
        </p>

        {judgeLoading ? (
          <div className="text-sm text-text-muted py-2">Loading settings...</div>
        ) : (
          <>
            <div className="mb-3">
              <label className="text-sm text-text-muted block mb-1">Provider</label>
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
              <label className="text-sm text-text-muted block mb-1">
                Model
                {discovering && <span className="text-text-muted/50 ml-2">discovering...</span>}
              </label>
              {judgeBaseUrl && discoveredModels.length > 0 ? (
                <select
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary focus:outline-none focus:border-accent font-mono"
                >
                  <option value="">Select a model...</option>
                  {discoveredModels.map((m) => (
                    <option key={m.id} value={m.id}>{m.id}{m.owned_by ? ` (${m.owned_by})` : ''}</option>
                  ))}
                </select>
              ) : judgeBaseUrl ? (
                <input
                  type="text"
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder="model-name"
                  className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
                />
              ) : isOpenRouter ? (
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
              <label className="text-sm text-text-muted block mb-1">API Key</label>
              <input
                type="password"
                value={judgeApiKey}
                onChange={(e) => { setJudgeApiKey(e.target.value); setJudgeSaved(false); }}
                placeholder={keyPlaceholder}
                className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
            </div>

            <div className="mb-3">
              <label className="text-sm text-text-muted block mb-1">Base URL <span className="text-text-muted/50">(optional)</span></label>
              <input
                type="text"
                value={judgeBaseUrl}
                onChange={(e) => { setJudgeBaseUrl(e.target.value); setJudgeSaved(false); }}
                placeholder="https://litellm.company.internal/v1"
                className="w-full px-2 py-1.5 bg-bg-primary border border-border rounded text-sm text-text-secondary placeholder:text-text-muted/50 focus:outline-none focus:border-accent font-mono"
              />
              <p className="text-xs text-text-muted/60 mt-1">
                Leave blank for provider default. Set for LiteLLM, vLLM, Ollama, or any OpenAI-compatible gateway.
              </p>
            </div>

            <div className="flex items-center gap-2 mb-2">
              <button
                onClick={handleSaveJudge}
                disabled={!judgeModel || (!judgeApiKey && !judgeBaseUrl) || saveJudge.isPending}
                className="px-3 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors disabled:opacity-50"
              >
                {saveJudge.isPending ? 'Saving...' : 'Save'}
              </button>
              {keySet && (
                <button
                  onClick={handleClearJudge}
                  disabled={clearJudge.isPending}
                  className="px-3 py-1.5 text-sm border border-red-500/30 text-red-400 rounded hover:bg-red-500/10 transition-colors disabled:opacity-50"
                >
                  {clearJudge.isPending ? 'Clearing...' : 'Clear Key'}
                </button>
              )}
            </div>

            <div className="text-sm">
              {judgeSaved && <span className="text-green-400">Key saved</span>}
              {!judgeSaved && keySet && <span className="text-green-400">Key saved</span>}
              {!judgeSaved && !keySet && <span className="text-text-muted">No key configured</span>}
            </div>

            {saveJudge.isError && (
              <div className="text-sm text-red-400 mt-2">
                {saveJudge.error instanceof Error ? saveJudge.error.message : 'Failed to save settings.'}
              </div>
            )}
          </>
        )}
      </div>

      {/* Auto-audit */}
      <AutoAuditSection />

      {/* Autosync */}
      <AutosyncSection />

      {/* GitHub Integration */}
      <GitHubIntegrationSection />

      <button
        onClick={logout}
        className="px-4 py-2 text-sm border border-red-500/30 text-red-400 rounded hover:bg-red-500/10 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}

function AutoAuditSection() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['auditTrigger'],
    queryFn: () => auth!.client.getAuditTrigger(),
    enabled: !!auth,
    staleTime: 30_000,
  });

  const update = useMutation({
    mutationFn: (trigger: string) => auth!.client.updateAuditTrigger(trigger),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['auditTrigger'] }),
  });

  const current = data?.trigger || 'manual';

  return (
    <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
      <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">Auto-audit</h2>
      <div className="space-y-2">
        {[
          { value: 'manual', label: 'Manual only — run audits when you choose' },
          { value: 'on_sync', label: 'On sync — automatically audit after each push' },
          { value: 'on_pr', label: 'On PR/MR — audit when a pull/merge request is opened' },
        ].map(({ value, label }) => (
          <label key={value} className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
            <input type="radio" name="audit-trigger" checked={current === value}
              onChange={() => update.mutate(value)} disabled={update.isPending}
              className="text-accent focus:ring-accent" />
            {label}
          </label>
        ))}
      </div>
    </div>
  );
}


function AutosyncSection() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data: syncSettings, isLoading } = useQuery({
    queryKey: ['syncSettings'],
    queryFn: () => auth!.client.getSyncSettings(),
    enabled: !!auth,
    staleTime: 30_000,
  });

  const updateMode = useMutation({
    mutationFn: (mode: string) => auth!.client.updateSyncSettings(mode),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['syncSettings'] }),
  });

  const currentMode = syncSettings?.mode || 'off';

  return (
    <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
      <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">Autosync</h2>
      <p className="text-sm text-text-muted mb-3">
        Automatically sync sessions to the cloud. The daemon pushes changes after a short debounce period.
      </p>

      {isLoading ? (
        <div className="text-sm text-text-muted py-2">Loading...</div>
      ) : (
        <div className="space-y-2">
          {(['off', 'all', 'selective'] as const).map((mode) => (
            <label key={mode} className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
              <input
                type="radio"
                name="sync-mode"
                checked={currentMode === mode}
                onChange={() => updateMode.mutate(mode)}
                disabled={updateMode.isPending}
                className="text-accent focus:ring-accent"
              />
              {mode === 'off' && 'Off — Manual sync only (sfs push)'}
              {mode === 'all' && 'All sessions — Sync everything automatically'}
              {mode === 'selective' && 'Selective — Choose which sessions to sync'}
            </label>
          ))}

          {updateMode.isError && (
            <div className="text-sm text-red-400 mt-1">Failed to update sync mode.</div>
          )}
        </div>
      )}
    </div>
  );
}


function GitHubIntegrationSection() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  const { data: ghSettings, isLoading } = useQuery({
    queryKey: ['github-installation'],
    queryFn: () => auth!.client.getGitHubInstallation(),
    enabled: !!auth,
  });

  const updateSettings = useMutation({
    mutationFn: (updates: { auto_comment?: boolean; include_trust_score?: boolean; include_session_links?: boolean }) =>
      auth!.client.updateGitHubInstallation(updates),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['github-installation'] }),
  });

  const connected = ghSettings && ghSettings.account_login;

  return (
    <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
      <h2 className="text-sm uppercase tracking-wider text-text-muted mb-3">GitHub Integration</h2>
      <p className="text-sm text-text-muted mb-3">
        Automatically post AI context comments on pull requests showing which sessions contributed to the code.
      </p>

      {isLoading ? (
        <div className="text-sm text-text-muted py-2">Loading...</div>
      ) : connected ? (
        <>
          <div className="flex items-center gap-2 mb-3">
            <span className="inline-block w-2 h-2 rounded-full bg-green-400" />
            <span className="text-sm text-text-primary">
              Connected to <span className="font-medium">{ghSettings.account_login}</span>
              <span className="text-text-muted ml-1">({ghSettings.account_type})</span>
            </span>
          </div>

          <div className="space-y-2 mb-3">
            <label className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.auto_comment ?? true}
                onChange={(e) => updateSettings.mutate({ auto_comment: e.target.checked })}
                className="rounded border-border bg-bg-primary text-accent focus:ring-accent"
              />
              Auto-comment on PRs
            </label>
            <label className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_trust_score ?? true}
                onChange={(e) => updateSettings.mutate({ include_trust_score: e.target.checked })}
                className="rounded border-border bg-bg-primary text-accent focus:ring-accent"
              />
              Include trust scores
            </label>
            <label className="flex items-center gap-2 text-sm text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_session_links ?? true}
                onChange={(e) => updateSettings.mutate({ include_session_links: e.target.checked })}
                className="rounded border-border bg-bg-primary text-accent focus:ring-accent"
              />
              Include session links
            </label>
          </div>

          {updateSettings.isError && (
            <div className="text-sm text-red-400 mb-2">Failed to update settings.</div>
          )}

          <a
            href="https://github.com/apps/sessionfs-ai-context/installations/new"
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-accent hover:underline"
          >
            Manage installation
          </a>
        </>
      ) : (
        <div>
          <p className="text-sm text-text-muted mb-3">Not connected</p>
          <a
            href="https://github.com/apps/sessionfs-ai-context/installations/new"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block px-3 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/90 transition-colors"
          >
            Connect GitHub
          </a>
        </div>
      )}
    </div>
  );
}
