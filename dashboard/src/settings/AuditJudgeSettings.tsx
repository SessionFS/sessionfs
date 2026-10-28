import { useState, useEffect } from 'react';
import { useAuth } from '../auth/AuthContext';
import {
  useJudgeSettings,
  useSaveJudgeSettings,
  useClearJudgeSettings,
  useJudgeConnectionTester,
} from '../hooks/useJudgeSettings';
import { useLlmProviders } from '../hooks/useLlmProviders';
import { useToast } from '../hooks/useToast';
import { judgeSettingsSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import { Card, Button, Input, Select } from '../components/ui';
import ModelPicker from './ModelPicker';
import { useKeyCommit } from './useKeyCommit';
import { errorMessage } from '../utils/errorMessage';
import type { SaveWarning } from '../api/client';
import { ConnectionTestStatus, SaveWarnings } from './LlmConnectionTest';
import {
  baseUrlHint,
  baseUrlPlaceholder,
  saveFieldError,
  useLlmConnectionTest,
} from './llmConnection';

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
  const [keyCommit, onKeyBlur] = useKeyCommit(judgeApiKey);
  const [judgeBaseUrl, setJudgeBaseUrl] = useState('');
  const [judgeSaved, setJudgeSaved] = useState(false);
  const [judgeErrors, setJudgeErrors] = useState<FieldErrors>({});
  const [saveWarnings, setSaveWarnings] = useState<SaveWarning[]>([]);

  const connection = useLlmConnectionTest(useJudgeConnectionTester());
  const resetConnection = connection.reset;

  // A result describes the values it was run with; editing them clears it.
  useEffect(() => {
    resetConnection();
  }, [judgeProvider, judgeModel, judgeApiKey, judgeBaseUrl, resetConnection]);

  const currentProvider = providers.find((p) => p.id === judgeProvider);

  // Populate from saved settings
  useEffect(() => {
    if (judgeSettings) {
      if (judgeSettings.provider) setJudgeProvider(judgeSettings.provider);
      if (judgeSettings.model) setJudgeModel(judgeSettings.model);
      if (judgeSettings.base_url) setJudgeBaseUrl(judgeSettings.base_url);
    }
  }, [judgeSettings]);

  if (!auth) return null;

  const providerOptions = providers.map((p) => ({ value: p.id, label: p.label }));
  const keySet = judgeSettings?.key_set === true;

  function handleProviderChange(newProvider: string) {
    setJudgeProvider(newProvider);
    setJudgeSaved(false);
    const next = providers.find((p) => p.id === newProvider);
    setJudgeModel(next?.suggested_models[0] ?? '');
  }

  function handleTest() {
    void connection.test({
      provider: judgeProvider,
      model: judgeModel || undefined,
      base_url: judgeBaseUrl.trim() || '',
      api_key: judgeApiKey || undefined,
    });
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
    setSaveWarnings([]);
    saveJudge.mutate(
      { provider: judgeProvider, model: judgeModel, apiKey: judgeApiKey, baseUrl: judgeBaseUrl || undefined },
      {
        onSuccess: (data) => {
          setJudgeSaved(true);
          setJudgeApiKey('');
          setSaveWarnings(data?.warnings ?? []);
          addToast('success', 'Settings saved');
        },
        onError: (err) => {
          // A refused save (the address isn't a chat API, the key was
          // rejected) belongs on the field that caused it.
          const fieldError = saveFieldError(err);
          if (fieldError?.field) {
            setJudgeErrors((prev) => ({ ...prev, [fieldError.field as string]: fieldError.message }));
          }
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
              <ModelPicker
                providerId={judgeProvider}
                provider={currentProvider}
                model={judgeModel}
                onModelChange={(v) => { setJudgeModel(v); setJudgeSaved(false); }}
                baseUrl={judgeBaseUrl}
                apiKey={judgeApiKey}
                keyCommit={keyCommit}
                discover={auth.client.discoverModels}
                error={judgeErrors.model}
              />
            </div>

            <div className="mb-4">
              <Input
                type="password"
                onBlur={onKeyBlur}
                value={judgeApiKey}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeApiKey(e.target.value); setJudgeSaved(false); if (judgeErrors.apiKey) setJudgeErrors((prev) => ({ ...prev, apiKey: undefined })); }}
                placeholder={keyPlaceholder}
                title="API Key"
                className="font-mono"
                error={judgeErrors.apiKey}
              />
            </div>

            <div className="mb-4">
              <Input
                type="text"
                value={judgeBaseUrl}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setJudgeBaseUrl(e.target.value); setJudgeSaved(false); if (judgeErrors.baseUrl) setJudgeErrors((prev) => ({ ...prev, baseUrl: undefined })); }}
                placeholder={baseUrlPlaceholder(currentProvider)}
                title="Base URL (optional)"
                className="font-mono"
                error={judgeErrors.baseUrl}
                hint={baseUrlHint(currentProvider)}
              />
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
              <Button
                variant="secondary"
                onClick={handleTest}
                disabled={connection.testing || (!judgeApiKey && !keySet && !judgeBaseUrl)}
                loading={connection.testing}
              >
                Test connection
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

            <div className="mt-3 space-y-2">
              <ConnectionTestStatus state={connection.state} />
              <SaveWarnings warnings={saveWarnings} />
            </div>

            {saveJudge.isError && !saveFieldError(saveJudge.error)?.field && (
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
