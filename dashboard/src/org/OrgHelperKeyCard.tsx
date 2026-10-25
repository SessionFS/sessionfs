/**
 * Migration 084 — org-wide LLM key for the session helper.
 *
 * Rendered on the org Policies tab for owner/admin (the role gate lives in
 * OrgPage). The server never returns the key, so the password input shows a
 * "key ending in ••••1234" hint from `key_hint` instead; saving with the
 * field blank keeps the stored key. When the org's plan no longer includes
 * the helper, the card drops to a "remove or disable" state — the stored key
 * has stopped resolving and can only be deleted or switched off.
 */

import { useEffect, useState } from 'react';

import { useToast } from '../hooks/useToast';
import {
  useOrgLlmSettings,
  useSaveOrgLlmSettings,
  useDeleteOrgLlmSettings,
} from './useOrgLlmSettings';
import { Button, Input, Select, Switch } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

const PROVIDERS = [
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'google', label: 'Google' },
] as const;

interface OrgHelperKeyCardProps {
  orgId: string;
}

export default function OrgHelperKeyCard({ orgId }: OrgHelperKeyCardProps) {
  const { addToast } = useToast();
  const { data, isLoading } = useOrgLlmSettings(orgId);
  const save = useSaveOrgLlmSettings(orgId);
  const remove = useDeleteOrgLlmSettings(orgId);

  const [provider, setProvider] = useState('openrouter');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [enabled, setEnabled] = useState(true);

  useEffect(() => {
    if (!data) return;
    if (data.provider) setProvider(data.provider);
    if (data.model) setModel(data.model);
    if (data.base_url) setBaseUrl(data.base_url);
    setEnabled(data.enabled);
  }, [data]);

  const hasKey = data?.has_api_key === true;
  const planIncludesHelper = data?.plan_includes_helper !== false;
  const keyHint = data?.key_hint;
  const keyPlaceholder = hasKey && keyHint ? `key ending in ••••${keyHint}` : 'sk-…';

  function handleSave() {
    save.mutate(
      {
        provider,
        model,
        apiKey: apiKey || undefined,
        baseUrl: baseUrl || null,
        enabled,
      },
      {
        onSuccess: () => {
          setApiKey('');
          addToast('success', 'Helper key saved');
        },
        onError: (err) => addToast('error', `Save failed: ${errorMessage(err)}`),
      },
    );
  }

  function handleDisable() {
    save.mutate(
      {
        provider: data?.provider ?? '',
        model: data?.model ?? '',
        apiKey: undefined,
        baseUrl: data?.base_url ?? null,
        enabled: false,
      },
      {
        onSuccess: () => addToast('success', 'Helper key disabled'),
        onError: (err) => addToast('error', `Disable failed: ${errorMessage(err)}`),
      },
    );
  }

  function handleDelete() {
    remove.mutate(undefined, {
      onSuccess: () => {
        setApiKey('');
        setBaseUrl('');
        addToast('success', 'Helper key removed');
      },
      onError: (err) => addToast('error', `Delete failed: ${errorMessage(err)}`),
    });
  }

  const heading = (
    <h3 id="org-helper-key-heading">AI key for the session helper</h3>
  );

  if (isLoading) {
    return (
      <section aria-labelledby="org-helper-key-heading">
        {heading}
        <p className="text-sm text-text-tertiary py-2">Loading settings…</p>
      </section>
    );
  }

  if (!planIncludesHelper) {
    if (!hasKey) {
      return (
        <section aria-labelledby="org-helper-key-heading">
          {heading}
          <p className="text-sm text-text-secondary">
            The session helper requires an Enterprise plan. Upgrade to configure
            an org-wide key.
          </p>
        </section>
      );
    }
    return (
      <section aria-labelledby="org-helper-key-heading">
        {heading}
        <p role="status" className="text-sm text-text-secondary">
          Your plan no longer includes the session helper. Remove or disable the
          stored key.
        </p>
        <div className="flex items-center gap-3 mt-3">
          {enabled && (
            <Button
              type="button"
              variant="primary"
              onClick={handleDisable}
              disabled={save.isPending}
              loading={save.isPending}
            >
              Disable
            </Button>
          )}
          <Button
            type="button"
            variant="danger"
            onClick={handleDelete}
            disabled={remove.isPending}
            loading={remove.isPending}
          >
            Delete key
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section aria-labelledby="org-helper-key-heading">
      {heading}
      <p className="text-sm text-text-secondary">
        An org-wide LLM key the session helper falls back to when a member has
        no personal judge key. The key is stored server-side and never shown
        again; only its last four characters are surfaced.
      </p>

      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <Switch
            checked={enabled}
            onChange={setEnabled}
            label={enabled ? 'Enabled' : 'Disabled'}
            aria-label="Enable org helper key"
          />
        </div>

        <Select
          value={provider}
          onValueChange={setProvider}
          options={[...PROVIDERS]}
          title="Provider"
          aria-label="Provider"
        />

        <Input
          type="text"
          value={model}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setModel(e.target.value)}
          placeholder="claude-sonnet-5-5"
          title="Model"
          className="font-mono"
        />

        <Input
          type="password"
          value={apiKey}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setApiKey(e.target.value)}
          placeholder={keyPlaceholder}
          title="API Key"
          className="font-mono"
        />
        {hasKey && (
          <span className="text-xs text-text-tertiary -mt-2">
            A key is stored. Leave the field blank to keep it.
          </span>
        )}

        <Input
          type="text"
          value={baseUrl}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setBaseUrl(e.target.value)}
          placeholder="https://litellm.company.internal/v1"
          title="Base URL (optional)"
          className="font-mono"
        />

        <div className="flex items-center gap-3">
          <Button
            type="button"
            variant="primary"
            onClick={handleSave}
            disabled={!model || save.isPending}
            loading={save.isPending}
          >
            Save
          </Button>
          {hasKey && (
            <Button
              type="button"
              variant="danger"
              onClick={handleDelete}
              disabled={remove.isPending}
              loading={remove.isPending}
            >
              Delete key
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}
