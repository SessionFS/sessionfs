import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import CopyButton from '../components/CopyButton';
import { useJudgeSettings, useSaveJudgeSettings, useClearJudgeSettings } from '../hooks/useJudgeSettings';
import { useToast } from '../hooks/useToast';
import { judgeSettingsSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import FieldError from '../components/FieldError';

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

type SettingsTab = 'account' | 'judge' | 'connection' | 'preferences';

function getInitialTheme(): 'light' | 'dark' | 'system' {
  const stored = localStorage.getItem('sfs-theme');
  if (stored === 'light' || stored === 'dark') return stored;
  return 'system';
}

function applyTheme(choice: 'light' | 'dark' | 'system') {
  let resolved: 'light' | 'dark';
  if (choice === 'system') {
    resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    localStorage.removeItem('sfs-theme');
  } else {
    resolved = choice;
    localStorage.setItem('sfs-theme', choice);
  }
  document.documentElement.setAttribute('data-theme', resolved);
}

export default function SettingsPage() {
  const { auth, logout } = useAuth();
  const { data: profile } = useQuery({
    queryKey: ['me'],
    queryFn: () => auth!.client.getMe(),
    enabled: !!auth,
  });

  const [activeTab, setActiveTab] = useState<SettingsTab>('account');

  if (!auth) return null;

  const tabs: { key: SettingsTab; label: string }[] = [
    { key: 'account', label: 'Account' },
    { key: 'judge', label: 'Judge' },
    { key: 'connection', label: 'Connection' },
    { key: 'preferences', label: 'Preferences' },
  ];

  return (
    <div className="max-w-2xl mx-auto px-4 py-8">
      <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)] mb-6">Settings</h1>

      {/* Tab navigation */}
      <div className="flex gap-0 border-b border-[var(--border)] mb-6">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-4 py-2 text-[14px] font-medium transition-colors ${
              activeTab === t.key
                ? 'text-[var(--text-primary)] border-b-2 border-[var(--brand)] -mb-px'
                : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === 'account' && (
        <AccountTab profile={profile} logout={logout} />
      )}
      {activeTab === 'judge' && <JudgeTab />}
      {activeTab === 'connection' && <ConnectionTab />}
      {activeTab === 'preferences' && <PreferencesTab />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Account Tab                                                        */
/* ------------------------------------------------------------------ */

function AccountTab({ profile, logout }: { profile: any; logout: () => void }) {
  return (
    <div className="space-y-5">
      {profile && (
        <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
          <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-4">Account</h2>
          <div className="space-y-3">
            <div>
              <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">Email</label>
              <div className="text-sm text-[var(--text-primary)]">{profile.email}</div>
            </div>
            <div className="flex gap-4 text-sm text-[var(--text-secondary)]">
              <span>Tier: <span className="text-[var(--text-primary)] font-medium capitalize">{profile.tier}</span></span>
              <span>Verified: {profile.email_verified
                ? <span className="text-green-500">yes</span>
                : <span className="text-yellow-500">no</span>
              }</span>
            </div>
          </div>
        </div>
      )}

      {/* Last Sync card */}
      {profile?.last_client_version && (
        <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10" />
              <polyline points="1 20 1 14 7 14" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
            <h2 className="text-lg font-semibold text-[var(--text-primary)]">Last Sync</h2>
            {profile.latest_version && profile.last_client_version === profile.latest_version && (
              <span className="ml-auto px-2.5 py-0.5 bg-green-500/10 text-green-500 rounded-full text-xs font-medium">
                Up to date
              </span>
            )}
            {profile.latest_version && profile.last_client_version !== profile.latest_version && (
              <span className="ml-auto px-2.5 py-0.5 bg-yellow-500/10 text-yellow-500 rounded-full text-xs font-medium">
                Update available
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Package version</span>
              <span className="text-[var(--text-primary)] font-mono">{profile.last_client_version}</span>
            </div>
            <div>
              <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Platform</span>
              <span className="text-[var(--text-primary)]">{profile.last_client_platform || '-'}</span>
            </div>
            <div>
              <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Device</span>
              <span className="text-[var(--text-primary)] font-mono">{profile.last_client_device || '-'}</span>
            </div>
            <div>
              <span className="text-[13px] text-[var(--text-tertiary)] block mb-0.5">Last synced</span>
              <span className="text-[var(--text-primary)]">
                {profile.last_sync_at ? new Date(profile.last_sync_at).toLocaleString() : '-'}
              </span>
            </div>
          </div>
          {profile.latest_version && profile.last_client_version !== profile.latest_version && (
            <div className="mt-4 p-3 bg-yellow-500/5 border border-yellow-500/20 rounded-lg text-sm">
              <p className="text-yellow-500 font-medium">New version available</p>
              <p className="text-[var(--text-tertiary)] mt-1">
                Run <code className="bg-[var(--surface)] px-1.5 py-0.5 rounded text-[var(--text-secondary)]">pip install --upgrade sessionfs</code> to update from {profile.last_client_version} to {profile.latest_version}.
              </p>
            </div>
          )}
        </div>
      )}

      <button
        onClick={logout}
        className="px-4 py-2 text-sm border border-red-500/30 text-red-500 rounded-lg hover:bg-red-500/10 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Connection Tab                                                     */
/* ------------------------------------------------------------------ */

function ConnectionTab() {
  const { auth } = useAuth();
  if (!auth) return null;

  const maskedKey = auth.apiKey.slice(0, 12) + '\u2026' + auth.apiKey.slice(-4);

  return (
    <div className="space-y-5">
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
        <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-4">Connection</h2>

        <div className="mb-4">
          <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">Server URL</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-[var(--text-secondary)] bg-[var(--surface)] border border-[var(--border)] px-3 py-2 rounded-lg flex-1">
              {auth.baseUrl}
            </code>
            <CopyButton text={auth.baseUrl} />
          </div>
        </div>

        <div>
          <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">API Key</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-[var(--text-secondary)] bg-[var(--surface)] border border-[var(--border)] px-3 py-2 rounded-lg flex-1 font-mono">
              {maskedKey}
            </code>
            <CopyButton text={auth.apiKey} />
          </div>
        </div>
      </div>

      {/* GitHub Integration */}
      <GitHubIntegrationSection />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Judge Tab                                                          */
/* ------------------------------------------------------------------ */

function JudgeTab() {
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
    isOpenRouter ? 'sk-or-...' :
    judgeProvider === 'anthropic' ? 'sk-ant-...' :
    judgeProvider === 'openai' ? 'sk-...' :
    'AIza...';

  return (
    <div className="space-y-5">
      {/* Judge Configuration */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
        <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-2">Judge Configuration</h2>
        <p className="text-sm text-[var(--text-tertiary)] mb-4">
          Configure the LLM used for session audits. Your key is stored server-side and used only for audit requests.
        </p>

        {judgeLoading ? (
          <div className="text-sm text-[var(--text-tertiary)] py-2">Loading settings...</div>
        ) : (
          <>
            <div className="mb-4">
              <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">Provider</label>
              <select
                value={judgeProvider}
                onChange={(e) => handleProviderChange(e.target.value)}
                className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)]"
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>

            <div className="mb-4">
              <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">
                Model
                {discovering && <span className="text-[var(--text-tertiary)] opacity-50 ml-2">discovering...</span>}
              </label>
              {judgeBaseUrl && discoveredModels.length > 0 ? (
                <select
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)] font-mono"
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
                  className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] placeholder:text-[var(--text-tertiary)] placeholder:opacity-50 focus:outline-none focus:border-[var(--brand)] font-mono"
                />
              ) : isOpenRouter ? (
                <input
                  type="text"
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  placeholder="anthropic/claude-sonnet-4"
                  className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] placeholder:text-[var(--text-tertiary)] placeholder:opacity-50 focus:outline-none focus:border-[var(--brand)] font-mono"
                />
              ) : (
                <select
                  value={judgeModel}
                  onChange={(e) => { setJudgeModel(e.target.value); setJudgeSaved(false); }}
                  className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)]"
                >
                  {providerModels.map((m) => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              )}
              <FieldError message={judgeErrors.model} />
            </div>

            <div className="mb-4">
              <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">API Key</label>
              <input
                type="password"
                value={judgeApiKey}
                onChange={(e) => { setJudgeApiKey(e.target.value); setJudgeSaved(false); if (judgeErrors.apiKey) setJudgeErrors((prev) => ({ ...prev, apiKey: undefined })); }}
                placeholder={keyPlaceholder}
                className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] placeholder:text-[var(--text-tertiary)] placeholder:opacity-50 focus:outline-none focus:border-[var(--brand)] font-mono"
              />
              <FieldError message={judgeErrors.apiKey} />
            </div>

            <div className="mb-4">
              <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">Base URL <span className="opacity-50">(optional)</span></label>
              <input
                type="text"
                value={judgeBaseUrl}
                onChange={(e) => { setJudgeBaseUrl(e.target.value); setJudgeSaved(false); if (judgeErrors.baseUrl) setJudgeErrors((prev) => ({ ...prev, baseUrl: undefined })); }}
                placeholder="https://litellm.company.internal/v1"
                className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] placeholder:text-[var(--text-tertiary)] placeholder:opacity-50 focus:outline-none focus:border-[var(--brand)] font-mono"
              />
              <FieldError message={judgeErrors.baseUrl} />
              <p className="text-xs text-[var(--text-tertiary)] opacity-60 mt-1">
                Leave blank for provider default. Set for LiteLLM, vLLM, Ollama, or any OpenAI-compatible gateway.
              </p>
            </div>

            <div className="flex items-center gap-3 mb-3">
              <button
                onClick={handleSaveJudge}
                disabled={!judgeModel || (!judgeApiKey && !judgeBaseUrl) || saveJudge.isPending}
                className="bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
              >
                {saveJudge.isPending ? 'Saving...' : 'Save'}
              </button>
              {keySet && (
                <button
                  onClick={handleClearJudge}
                  disabled={clearJudge.isPending}
                  className="px-4 py-2 text-sm border border-red-500/30 text-red-500 rounded-lg hover:bg-red-500/10 transition-colors disabled:opacity-50"
                >
                  {clearJudge.isPending ? 'Clearing...' : 'Clear Key'}
                </button>
              )}
            </div>

            <div className="text-sm">
              {judgeSaved && <span className="text-green-500">Key saved</span>}
              {!judgeSaved && keySet && <span className="text-green-500">Key saved</span>}
              {!judgeSaved && !keySet && <span className="text-[var(--text-tertiary)]">No key configured</span>}
            </div>

            {saveJudge.isError && (
              <div className="text-sm text-red-500 mt-2">
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
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Preferences Tab                                                    */
/* ------------------------------------------------------------------ */

function PreferencesTab() {
  const [theme, setTheme] = useState<'light' | 'dark' | 'system'>(getInitialTheme);

  function handleThemeChange(value: string) {
    const choice = value as 'light' | 'dark' | 'system';
    setTheme(choice);
    applyTheme(choice);
  }

  return (
    <div className="space-y-5">
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
        <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-4">Appearance</h2>
        <div>
          <label className="text-[13px] text-[var(--text-tertiary)] block mb-1">Theme</label>
          <select
            value={theme}
            onChange={(e) => handleThemeChange(e.target.value)}
            className="w-full max-w-xs bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-[14px] text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)]"
          >
            <option value="light">Light</option>
            <option value="dark">Dark</option>
            <option value="system">System</option>
          </select>
          <p className="text-xs text-[var(--text-tertiary)] opacity-60 mt-1.5">
            Choose how SessionFS looks. System follows your OS preference.
          </p>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Auto-audit Section                                                 */
/* ------------------------------------------------------------------ */

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
    <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
      <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-3">Auto-audit</h2>
      <div className="space-y-2">
        {[
          { value: 'manual', label: 'Manual only -- run audits when you choose' },
          { value: 'on_sync', label: 'On sync -- automatically audit after each push' },
          { value: 'on_pr', label: 'On PR/MR -- audit when a pull/merge request is opened' },
        ].map(({ value, label }) => (
          <label key={value} className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)] cursor-pointer">
            <input type="radio" name="audit-trigger" checked={current === value}
              onChange={() => update.mutate(value)} disabled={update.isPending}
              className="text-[var(--brand)] focus:ring-[var(--brand)]" />
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
    <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
      <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-2">Autosync</h2>
      <p className="text-sm text-[var(--text-tertiary)] mb-3">
        Automatically sync sessions to the cloud. The daemon pushes changes after a short debounce period.
      </p>

      {isLoading ? (
        <div className="text-sm text-[var(--text-tertiary)] py-2">Loading...</div>
      ) : (
        <div className="space-y-2">
          {(['off', 'all', 'selective'] as const).map((mode) => (
            <label key={mode} className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)] cursor-pointer">
              <input
                type="radio"
                name="sync-mode"
                checked={currentMode === mode}
                onChange={() => updateMode.mutate(mode)}
                disabled={updateMode.isPending}
                className="text-[var(--brand)] focus:ring-[var(--brand)]"
              />
              {mode === 'off' && 'Off -- Manual sync only (sfs push)'}
              {mode === 'all' && 'All sessions -- Sync everything automatically'}
              {mode === 'selective' && 'Selective -- Choose which sessions to sync'}
            </label>
          ))}

          {updateMode.isError && (
            <div className="text-sm text-red-500 mt-1">Failed to update sync mode.</div>
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
    <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
      <h2 className="text-lg font-semibold text-[var(--text-primary)] mb-2">GitHub Integration</h2>
      <p className="text-sm text-[var(--text-tertiary)] mb-4">
        Automatically post AI context comments on pull requests showing which sessions contributed to the code.
      </p>

      {isLoading ? (
        <div className="text-sm text-[var(--text-tertiary)] py-2">Loading...</div>
      ) : connected ? (
        <>
          <div className="flex items-center gap-2 mb-4">
            <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
            <span className="text-sm text-[var(--text-primary)]">
              Connected to <span className="font-medium">{ghSettings.account_login}</span>
              <span className="text-[var(--text-tertiary)] ml-1">({ghSettings.account_type})</span>
            </span>
          </div>

          <div className="space-y-2 mb-4">
            <label className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.auto_comment ?? true}
                onChange={(e) => updateSettings.mutate({ auto_comment: e.target.checked })}
                className="rounded border-[var(--border)] bg-[var(--surface)] text-[var(--brand)] focus:ring-[var(--brand)]"
              />
              Auto-comment on PRs
            </label>
            <label className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_trust_score ?? true}
                onChange={(e) => updateSettings.mutate({ include_trust_score: e.target.checked })}
                className="rounded border-[var(--border)] bg-[var(--surface)] text-[var(--brand)] focus:ring-[var(--brand)]"
              />
              Include trust scores
            </label>
            <label className="flex items-center gap-2.5 text-sm text-[var(--text-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={ghSettings.include_session_links ?? true}
                onChange={(e) => updateSettings.mutate({ include_session_links: e.target.checked })}
                className="rounded border-[var(--border)] bg-[var(--surface)] text-[var(--brand)] focus:ring-[var(--brand)]"
              />
              Include session links
            </label>
          </div>

          {updateSettings.isError && (
            <div className="text-sm text-red-500 mb-2">Failed to update settings.</div>
          )}

          <a
            href="https://github.com/apps/sessionfs-ai-context/installations/new"
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-[var(--brand)] hover:underline"
          >
            Manage installation
          </a>
        </>
      ) : (
        <div>
          <p className="text-sm text-[var(--text-tertiary)] mb-3">Not connected</p>
          <a
            href="https://github.com/apps/sessionfs-ai-context/installations/new"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors"
          >
            Connect GitHub
          </a>
        </div>
      )}
    </div>
  );
}
