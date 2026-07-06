import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { resolveApiBaseUrl } from './apiBase';
import Wordmark from '../components/Wordmark';
import { Card, Button } from '../components/ui';

/**
 * SSO login completion. The API's /callback redirects the browser here with a
 * one-time code in the URL fragment (`#code=...`). We trade it at
 * POST /auth/sso/exchange for the api_key, then log in.
 *
 * Security:
 * - The fragment is cleared from the URL/history IMMEDIATELY on read, before any
 *   async work — so no analytics/RUM/Referer path can capture the one-time code
 *   (F2). The exchange runs once (a ref guards StrictMode double-invoke).
 * - The exchange POSTs with credentials so the HttpOnly browser-binding cookie
 *   is sent; only the browser that authenticated can redeem the code (F1). All
 *   failure modes surface the same generic message.
 */
export default function SsoCallback() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    // Read the fragment, then WIPE it from the URL/history before anything async.
    const rawHash = window.location.hash.startsWith('#')
      ? window.location.hash.slice(1)
      : '';
    const params = new URLSearchParams(rawHash);
    const code = params.get('code');
    const errCode = params.get('error');
    window.history.replaceState(null, '', window.location.pathname);

    if (errCode === 'pending_confirmation') {
      setError(
        'This account needs explicit confirmation before it can sign in with ' +
          'SSO. Please contact your organization admin.',
      );
      return;
    }
    if (!code) {
      setError('Missing sign-in code. Please start the SSO login again.');
      return;
    }

    // Prefer the API base the /start flow used (a custom Server URL survives the
    // IdP round-trip via sessionStorage); fall back to the environment default.
    let base = resolveApiBaseUrl();
    try {
      const saved = sessionStorage.getItem('sfs_sso_api_base');
      if (saved) base = saved;
      sessionStorage.removeItem('sfs_sso_api_base');
    } catch {
      /* sessionStorage unavailable — use the resolved default */
    }
    (async () => {
      try {
        const resp = await fetch(`${base}/api/v1/auth/sso/exchange`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include', // send the browser-binding cookie (F1)
          body: JSON.stringify({ code }),
        });
        if (!resp.ok) {
          setError(
            'Sign-in could not be completed. The link may have expired — ' +
              'please try signing in again.',
          );
          return;
        }
        const data = (await resp.json()) as { api_key?: string };
        if (!data.api_key) {
          setError('Sign-in could not be completed. Please try again.');
          return;
        }
        await login(base, data.api_key);
        navigate('/', { replace: true });
      } catch {
        setError('Sign-in could not be completed. Please try again.');
      }
    })();
  }, [login, navigate]);

  return (
    <div className="flex items-center justify-center min-h-screen bg-bg px-4">
      <Card className="w-full max-w-sm p-8 text-center space-y-5">
        <div className="flex justify-center">
          <Wordmark />
        </div>
        {error ? (
          <>
            <p className="text-red-500 text-sm" role="alert">
              {error}
            </p>
            <Button onClick={() => navigate('/login', { replace: true })}>
              Back to sign in
            </Button>
          </>
        ) : (
          <p className="text-text-secondary text-sm" aria-live="polite">
            Completing sign-in…
          </p>
        )}
      </Card>
    </div>
  );
}
