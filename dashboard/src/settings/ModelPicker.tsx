/**
 * Model field for LLM key forms (personal judge settings and the org helper
 * key). Shows the models a provider/endpoint offers as a dropdown:
 *
 * - a custom Base URL is queried automatically (debounced) as it is typed;
 * - a registry provider with a list-models API gets a "Fetch models" button;
 * - otherwise the provider's suggested models are offered.
 *
 * Any list can be bypassed with "Model id not in list — enter manually", and a
 * saved model id the list doesn't contain opens in manual mode so it is never
 * silently replaced.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';

import type { DiscoveredModels, LlmProvider } from '../api/client';
import { Button, Input, Select } from '../components/ui';

export type DiscoverModelsFn = (
  baseUrl: string,
  apiKey: string | undefined,
  provider: string | undefined,
) => Promise<DiscoveredModels>;

interface ModelPickerProps {
  providerId: string;
  provider: LlmProvider | undefined;
  model: string;
  onModelChange: (model: string) => void;
  baseUrl: string;
  apiKey: string;
  /** Bumped by the form when the API key field is committed (blur): a
   *  custom endpoint is looked up again then, never per keystroke. */
  keyCommit?: number;
  discover: DiscoverModelsFn;
  error?: string;
  /** Debounce for Base URL discovery (tests pass 0). */
  debounceMs?: number;
}

type Model = DiscoveredModels['models'][number];

function lookupErrorMessage(err: unknown): string {
  return err instanceof ApiError && err.status === 429
    ? 'Too many model lookups. Wait a minute and try again.'
    : 'Could not fetch models. Enter a model id manually.';
}

function optionLabel(m: Model): string {
  return m.id + (m.owned_by ? ` (${m.owned_by})` : '');
}

export default function ModelPicker({
  providerId,
  provider,
  model,
  onModelChange,
  baseUrl,
  apiKey,
  keyCommit = 0,
  discover,
  error,
  debounceMs = 500,
}: ModelPickerProps) {
  // Models listed at a custom Base URL, tagged with the URL they came from.
  const [endpoint, setEndpoint] = useState<{ base: string; models: Model[] }>({ base: '', models: [] });
  // A failed lookup is not cached as "no models"; only its message is kept,
  // for the URL it was made for.
  const [endpointError, setEndpointError] = useState<{ base: string; message: string } | null>(null);
  // "Fetch models" results, tagged with the provider they were fetched for.
  const [fetched, setFetched] = useState<{ provider: string; models: Model[]; error: string | null }>({
    provider: '',
    models: [],
    error: null,
  });
  const [discovering, setDiscovering] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [manual, setManual] = useState(false);
  // Only the latest "Fetch models" request may write its result.
  const fetchSeq = useRef(0);
  // A provider switch starts from its own list, not a manual entry left over
  // from the previous provider (reset during render).
  const [listProvider, setListProvider] = useState(providerId);
  if (listProvider !== providerId) {
    setListProvider(providerId);
    setManual(false);
  }

  // Custom endpoints: list models when the Base URL changes (debounced), when
  // the key field is committed, or on Fetch models. Typing the key does not
  // look anything up: each lookup counts against a per-minute limit.
  const trimmedBase = baseUrl.trim();
  const keyRef = useRef(apiKey);
  useEffect(() => {
    keyRef.current = apiKey;
  }, [apiKey]);
  const endpointSeq = useRef(0);
  const lookupEndpoint = useCallback(
    async (base: string) => {
      const seq = ++endpointSeq.current;
      setDiscovering(true);
      try {
        const result = await discover(base, keyRef.current || undefined, undefined);
        if (seq !== endpointSeq.current) return;
        setEndpoint({ base, models: result.models || [] });
        setEndpointError(null);
      } catch (err) {
        if (seq === endpointSeq.current) setEndpointError({ base, message: lookupErrorMessage(err) });
      } finally {
        if (seq === endpointSeq.current) setDiscovering(false);
      }
    },
    [discover],
  );
  useEffect(() => {
    if (!trimmedBase) return;
    const timer = setTimeout(() => void lookupEndpoint(trimmedBase), debounceMs);
    return () => clearTimeout(timer);
  }, [trimmedBase, keyCommit, debounceMs, lookupEndpoint]);
  const endpointModels = endpoint.base === trimmedBase ? endpoint.models : [];

  const canFetch = trimmedBase ? true : provider?.discovery === true;

  async function handleFetch() {
    if (trimmedBase) {
      void lookupEndpoint(trimmedBase);
      return;
    }
    // Capture what this request is for: a provider switch (or a newer
    // request) while it is in flight must not apply its answer.
    const seq = ++fetchSeq.current;
    const forProvider = providerId;
    setFetching(true);
    try {
      const result = await discover('', apiKey || undefined, forProvider);
      if (seq !== fetchSeq.current) return;
      setFetched({
        provider: forProvider,
        models: result.models || [],
        error: result.error ? `${result.error}. Showing suggested models.` : null,
      });
      setManual(false);
    } catch (err) {
      if (seq !== fetchSeq.current) return;
      setFetched({
        provider: forProvider,
        models: [],
        error: lookupErrorMessage(err),
      });
    } finally {
      if (seq === fetchSeq.current) setFetching(false);
    }
  }
  const providerModels = fetched.provider === providerId ? fetched.models : [];
  const fetchError = trimmedBase
    ? endpointError?.base === trimmedBase
      ? endpointError.message
      : null
    : fetched.provider === providerId
      ? fetched.error
      : null;

  const suggested: Model[] = (provider?.suggested_models ?? []).map((id) => ({ id, owned_by: '' }));
  const list: Model[] = trimmedBase
    ? endpointModels
    : providerModels.length > 0
      ? providerModels
      : providerId === 'openrouter'
        ? [] // hundreds of vendor/model ids: typed, or fetched on demand
        : suggested;

  const inList = model === '' || list.some((m) => m.id === model);
  const showSelect = list.length > 0 && !manual && inList;

  const lookingUp = discovering && !!trimmedBase;
  const status = lookingUp
    ? 'Looking up models at this address…'
    : !trimmedBase && fetching
      ? 'Fetching models…'
      : undefined;

  return (
    <div className="flex flex-col gap-1.5" data-testid="model-picker">
      {showSelect ? (
        <Select
          value={model}
          onValueChange={onModelChange}
          title="Model"
          options={[{ value: '', label: 'Select a model…' }, ...list.map((m) => ({ value: m.id, label: optionLabel(m) }))]}
          error={error}
          hint={status}
        />
      ) : (
        <Input
          type="text"
          value={model}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => onModelChange(e.target.value)}
          placeholder={providerId === 'openrouter' ? 'vendor/model-name' : 'model-name'}
          title="Model"
          className="font-mono"
          error={error}
          hint={status}
        />
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {canFetch && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => void handleFetch()}
            disabled={trimmedBase ? lookingUp : fetching}
            loading={trimmedBase ? lookingUp : fetching}
          >
            Fetch models
          </Button>
        )}
        {showSelect && (
          <button
            type="button"
            className="text-xs text-brand hover:underline"
            onClick={() => setManual(true)}
          >
            Model id not in list — enter manually
          </button>
        )}
        {!showSelect && list.length > 0 && (
          <button
            type="button"
            className="text-xs text-brand hover:underline"
            onClick={() => {
              setManual(false);
              if (!list.some((m) => m.id === model)) onModelChange('');
            }}
          >
            Choose from the list ({list.length})
          </button>
        )}
      </div>
      {fetchError && <p className="text-xs text-text-tertiary">{fetchError}</p>}
    </div>
  );
}
