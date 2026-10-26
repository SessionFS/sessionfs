/**
 * State and helpers for "Test connection" on LLM key forms (personal judge
 * settings and the org helper key). The rendering lives in
 * LlmConnectionTest.tsx.
 *
 * The test sends the form's current values: the typed key when the field is
 * filled, otherwise only provider/model/base_url so the server uses the saved
 * key — which it does only for the exact provider/endpoint the key was saved
 * with (400 `key_required` otherwise).
 */

import { useCallback, useState } from 'react';

import {
  ApiError,
  type LlmConnectionTestBody,
  type LlmConnectionTestResult,
  type LlmProvider,
} from '../api/client';
import { errorMessage } from '../utils/errorMessage';

export type ConnectionTestState =
  | { kind: 'idle' }
  | { kind: 'testing' }
  | { kind: 'result'; result: LlmConnectionTestResult }
  | { kind: 'key_required'; message: string }
  | { kind: 'error'; message: string };

export function useLlmConnectionTest(
  run: (body: LlmConnectionTestBody) => Promise<LlmConnectionTestResult>,
) {
  const [state, setState] = useState<ConnectionTestState>({ kind: 'idle' });

  const test = useCallback(
    async (body: LlmConnectionTestBody) => {
      setState({ kind: 'testing' });
      try {
        const result = await run(body);
        setState({ kind: 'result', result });
      } catch (err) {
        if (err instanceof ApiError && err.code === 'key_required') {
          setState({
            kind: 'key_required',
            message: 'Enter the API key to test a different provider or endpoint.',
          });
        } else if (err instanceof ApiError && err.code === 'test_rate_limited') {
          setState({ kind: 'error', message: 'Too many connection tests. Wait a minute and try again.' });
        } else {
          setState({ kind: 'error', message: errorMessage(err, 'The connection test failed.') });
        }
      }
    },
    [run],
  );

  const reset = useCallback(() => setState({ kind: 'idle' }), []);

  return { state, test, reset, testing: state.kind === 'testing' };
}

export interface SaveFieldError {
  field: 'baseUrl' | 'apiKey' | null;
  message: string;
}

/** Map a refused save (422 `{code, message}`) to the field it belongs to. */
export function saveFieldError(err: unknown): SaveFieldError | null {
  if (!(err instanceof ApiError)) return null;
  const message = errorMessage(err);
  if (err.code === 'endpoint_not_api' || err.code === 'endpoint_blocked') {
    return { field: 'baseUrl', message };
  }
  if (err.code === 'invalid_key' || err.code === 'api_key_required') {
    return { field: 'apiKey', message };
  }
  return null;
}

const GENERIC_BASE_URL_PLACEHOLDER = 'https://litellm.company.internal/v1';

/** Base URL placeholder: the provider's own API address when it has one. */
export function baseUrlPlaceholder(provider: LlmProvider | undefined): string {
  return provider?.default_base_url || GENERIC_BASE_URL_PLACEHOLDER;
}

export function baseUrlHint(provider: LlmProvider | undefined): string {
  const gateways = 'Set it only for LiteLLM, vLLM, Ollama, or another OpenAI-compatible gateway.';
  if (!provider) return `Leave empty to use the provider's API. ${gateways}`;
  return `Leave empty to use ${provider.label}'s API. ${gateways}`;
}
