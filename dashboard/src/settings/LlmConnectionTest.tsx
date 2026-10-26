/**
 * "Test connection" result and save-warning notices for LLM key forms. State
 * and helpers are in llmConnection.ts.
 */

import type { SaveWarning } from '../api/client';
import { Alert } from '../components/ui';
import type { ConnectionTestState } from './llmConnection';

const FAILURE_TITLES: Record<string, string> = {
  endpoint_not_api: "This address isn't a chat API",
  invalid_key: 'The key was rejected',
  access_denied: 'Access denied',
  model_not_found: 'Model not found',
  rate_limited: 'Rate limited',
  insufficient_balance: 'No balance left',
  endpoint_blocked: 'Address not allowed',
  timeout: 'No answer in time',
  unreachable: "Couldn't connect",
  provider_error: 'The provider returned an error',
};

/** The result region — the single live region for the test result. It is
 *  always rendered so screen readers hear updates; the Alerts inside carry
 *  no role of their own, so each result is announced once. */
export function ConnectionTestStatus({ state }: { state: ConnectionTestState }) {
  let content = null;
  if (state.kind === 'testing') {
    content = <p className="text-sm text-text-tertiary">Testing connection…</p>;
  } else if (state.kind === 'result' && state.result.ok) {
    const r = state.result;
    content = (
      <Alert variant="success" title="Connection works" announce={false}>
        Connected to {r.endpoint_host || r.provider} · {r.model}
        {typeof r.latency_ms === 'number' ? ` · ${r.latency_ms} ms` : ''}
      </Alert>
    );
  } else if (state.kind === 'result') {
    content = (
      <Alert variant="danger" title={FAILURE_TITLES[state.result.code] ?? 'Connection failed'} announce={false}>
        {state.result.message}
      </Alert>
    );
  } else if (state.kind === 'key_required') {
    content = (
      <Alert variant="warning" title="Enter the API key" announce={false}>
        {state.message}
      </Alert>
    );
  } else if (state.kind === 'error') {
    content = (
      <Alert variant="danger" title="Connection test failed" announce={false}>
        {state.message}
      </Alert>
    );
  }
  return (
    <div aria-live="polite" aria-atomic="true" data-testid="connection-test-status">
      {content}
    </div>
  );
}

/** Non-blocking notice for transient check failures on a successful save. */
export function SaveWarnings({ warnings }: { warnings: SaveWarning[] }) {
  if (warnings.length === 0) return null;
  return (
    <Alert variant="warning" title="Saved, but the connection check didn't pass">
      <ul className="list-disc pl-4">
        {warnings.map((w) => (
          <li key={w.code}>{w.message}</li>
        ))}
      </ul>
    </Alert>
  );
}
