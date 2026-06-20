import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { Button, Input } from '../components/ui';

/**
 * Guided self-service license activation (v0.11.0).
 *
 * Drives the three backend endpoints (all live in prod, edge rate-limited):
 *   GET  /api/v1/org/activate/info?key=…   → non-oracular preview {valid, org_name?, tier?}
 *   POST /api/v1/org/activate              → Phase A: {status:'verification_sent'} OR (exact-match
 *                                            shortcut) the full {org_id, name, slug, tier, …} result
 *   POST /api/v1/org/activate/verify       → Phase B: {org_id, name, slug, tier, seats_limit, …}
 *
 * Email verification is REQUIRED: the standard path emails a single-use, time-limited
 * code that the admin types in the "code" step. The only exception is when the caller's
 * verified account email already matches the license contact email, in which case Phase A
 * returns the activated org directly and we skip straight to the success state.
 */

type Step = 'key' | 'review' | 'code' | 'done';

interface ActivationInfo {
  valid: boolean;
  org_name?: string;
  tier?: string;
}

interface ActivationResult {
  org_id: string;
  name: string;
  slug: string;
  tier: string;
  seats_limit?: number;
  verification_method?: string;
}

function errorMessage(body: unknown, fallback: string): string {
  const b = body as { error?: { message?: string }; detail?: string } | null;
  return b?.error?.message || b?.detail || fallback;
}

export default function ActivateLicensePanel() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  const jsonHeaders = { ...headers, 'Content-Type': 'application/json' };
  const apiBase = auth?.baseUrl || (window as any).__SFS_API_URL__ || '';

  const [step, setStep] = useState<Step>('key');
  const [key, setKey] = useState('');
  const [orgNameOverride, setOrgNameOverride] = useState('');
  const [slugOverride, setSlugOverride] = useState('');
  const [code, setCode] = useState('');
  const [info, setInfo] = useState<ActivationInfo | null>(null);
  const [result, setResult] = useState<ActivationResult | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function lookUpKey() {
    setError('');
    setBusy(true);
    try {
      const res = await fetch(
        `${apiBase}/api/v1/org/activate/info?key=${encodeURIComponent(key.trim())}`,
        { headers },
      );
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(errorMessage(body, 'Could not look up that license.'));
      }
      if (!body?.valid) {
        // Non-oracular: the server never says WHY, so neither do we.
        throw new Error('That license key is invalid or not available for activation.');
      }
      setInfo(body as ActivationInfo);
      setOrgNameOverride((body.org_name as string) || '');
      setStep('review');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    setError('');
    setBusy(true);
    try {
      const payload: Record<string, string> = { key: key.trim() };
      if (orgNameOverride.trim()) payload.org_name = orgNameOverride.trim();
      if (slugOverride.trim()) payload.slug = slugOverride.trim();
      const res = await fetch(`${apiBase}/api/v1/org/activate`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(errorMessage(body, 'Activation could not be started.'));
      }
      if (body?.org_id) {
        // Exact-match email shortcut — Phase A completed activation directly.
        finish(body as ActivationResult);
        return;
      }
      // Standard path: a verification code was emailed to the license contact.
      setStep('code');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    setError('');
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/api/v1/org/activate/verify`, {
        method: 'POST',
        headers: jsonHeaders,
        body: JSON.stringify({ token: code.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(
          errorMessage(
            body,
            'That code is invalid, expired, or already used. Start activation again.',
          ),
        );
      }
      finish(body as ActivationResult);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function finish(res: ActivationResult) {
    setResult(res);
    setStep('done');
  }

  function goToOrg() {
    // Surface the newly-created org: /me now carries org_id, OrgPage re-fetches.
    queryClient.invalidateQueries({ queryKey: ['me'] });
    queryClient.invalidateQueries({ queryKey: ['org-info'] });
  }

  return (
    <div className="bg-bg-elevated border border-border rounded-xl p-6 text-left">
      <h3 className="text-lg font-semibold text-text-primary mb-1">Activate a license</h3>
      <p className="text-sm text-text-tertiary mb-5">
        Have an enterprise or team license key? Activate it here to create your organization —
        you'll become its owner.
      </p>

      {error && (
        <div
          role="alert"
          className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-400"
        >
          {error}
        </div>
      )}

      {step === 'key' && (
        <div className="space-y-4">
          <Input
            title="License key"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="SFS-XXXX-XXXX-XXXX"
            autoComplete="off"
            spellCheck={false}
          />
          <Button onClick={lookUpKey} disabled={!key.trim() || busy}>
            {busy ? 'Checking…' : 'Look up license'}
          </Button>
        </div>
      )}

      {step === 'review' && info && (
        <div className="space-y-4">
          <div className="rounded-lg border border-border bg-surface px-4 py-3">
            <div className="text-sm text-text-tertiary">This license will create</div>
            <div className="text-base font-semibold text-text-primary">
              {info.org_name || 'your organization'}
            </div>
            {info.tier && (
              <span className="mt-1 inline-block rounded-full bg-brand/15 px-2 py-0.5 text-xs font-medium text-brand">
                {info.tier} tier
              </span>
            )}
          </div>
          <Input
            title="Organization name (optional override)"
            value={orgNameOverride}
            onChange={(e) => setOrgNameOverride(e.target.value)}
            placeholder={info.org_name || 'My Organization'}
          />
          <Input
            title="Slug (optional override)"
            value={slugOverride}
            onChange={(e) => setSlugOverride(e.target.value)}
            placeholder="my-organization"
          />
          <p className="text-xs text-text-tertiary">
            A single-use verification code will be emailed to the license contact to confirm
            ownership before activation completes.
          </p>
          <div className="flex gap-2">
            <Button onClick={activate} disabled={busy}>
              {busy ? 'Starting…' : 'Activate'}
            </Button>
            <Button variant="ghost" onClick={() => setStep('key')} disabled={busy}>
              Back
            </Button>
          </div>
        </div>
      )}

      {step === 'code' && (
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            We emailed a verification code to the license contact. Enter it below to finish
            activating <strong className="text-text-primary">{orgNameOverride || info?.org_name}</strong>.
          </p>
          <Input
            title="Verification code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="Paste the emailed code"
            autoComplete="one-time-code"
            spellCheck={false}
          />
          <div className="flex gap-2">
            <Button onClick={verify} disabled={!code.trim() || busy}>
              {busy ? 'Verifying…' : 'Verify & activate'}
            </Button>
            <Button variant="ghost" onClick={() => setStep('review')} disabled={busy}>
              Back
            </Button>
          </div>
        </div>
      )}

      {step === 'done' && result && (
        <div className="space-y-4">
          <div className="rounded-lg border border-green-500/30 bg-green-500/10 px-4 py-3">
            <div className="text-base font-semibold text-text-primary">
              {result.name} is activated
            </div>
            <div className="text-sm text-text-secondary">
              You're the <strong>owner</strong> · {result.tier} tier
              {typeof result.seats_limit === 'number' ? ` · ${result.seats_limit} seats` : ''}
            </div>
          </div>
          <Button onClick={goToOrg}>Go to your organization</Button>
        </div>
      )}
    </div>
  );
}
