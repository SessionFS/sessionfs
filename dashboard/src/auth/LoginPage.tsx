import { useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { signup, ApiError } from '../api/client';
import { loginSchema, signupSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import Wordmark from '../components/Wordmark';
import { Card, Button, Input } from '../components/ui';

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [mode, setMode] = useState<'login' | 'signup'>(
    searchParams.get('mode') === 'signup' ? 'signup' : 'login',
  );
  const [baseUrl, setBaseUrl] = useState(() => {
    // 1. Build-time env var wins
    if (import.meta.env.VITE_API_URL) return import.meta.env.VITE_API_URL;
    // 2. Local dev
    if (window.location.hostname === 'localhost') return 'http://localhost:8000';
    // 3. Production: app.<domain> → api.<domain> so signup never POSTs
    //    to the static dashboard host (which returns 405)
    const host = window.location.hostname;
    if (host.startsWith('app.')) {
      return `${window.location.protocol}//api.${host.slice(4)}`;
    }
    // 4. Last resort — same origin (kept for self-hosted single-host deploys)
    return window.location.origin;
  });
  const [apiKey, setApiKey] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [signupKey, setSignupKey] = useState('');
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});

  function clearFieldError(field: string) {
    setFieldErrors((prev) => ({ ...prev, [field]: undefined }));
  }

  function validateLoginField(field: 'apiKey' | 'baseUrl') {
    const data = { apiKey, baseUrl };
    const result = loginSchema.safeParse(data);
    if (!result.success) {
      const errs = fieldErrorsFromZod(result.error);
      setFieldErrors((prev) => ({ ...prev, [field]: errs[field] }));
    } else {
      setFieldErrors((prev) => ({ ...prev, [field]: undefined }));
    }
  }

  function validateSignupField(field: 'email' | 'baseUrl') {
    const data = { email, baseUrl };
    const result = signupSchema.safeParse(data);
    if (!result.success) {
      const errs = fieldErrorsFromZod(result.error);
      setFieldErrors((prev) => ({ ...prev, [field]: errs[field] }));
    } else {
      setFieldErrors((prev) => ({ ...prev, [field]: undefined }));
    }
  }

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setError('');
    const result = loginSchema.safeParse({ apiKey, baseUrl });
    if (!result.success) {
      setFieldErrors(fieldErrorsFromZod(result.error));
      return;
    }
    setFieldErrors({});
    setLoading(true);
    try {
      await login(baseUrl, apiKey);
      navigate('/');
    } catch (err) {
      setError(err instanceof ApiError ? `Auth failed (${err.status})` : String(err));
    } finally {
      setLoading(false);
    }
  }

  async function handleSignup(e: FormEvent) {
    e.preventDefault();
    setError('');
    const result = signupSchema.safeParse({ email, baseUrl });
    if (!result.success) {
      setFieldErrors(fieldErrorsFromZod(result.error));
      return;
    }
    setFieldErrors({});
    setLoading(true);
    try {
      const signupResult = await signup(baseUrl, email);
      setSignupKey(signupResult.raw_key);
      // Auto-login with the new key and navigate to onboarding.
      // Pass the raw key via router state so the getting-started page
      // can display it in a copy-able banner.
      await login(baseUrl, signupResult.raw_key);
      navigate('/getting-started', { state: { apiKey: signupResult.raw_key } });
      return;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError('Email already registered. Use Login instead.');
      } else {
        setError(err instanceof ApiError ? `Signup failed (${err.status})` : String(err));
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative flex items-center justify-center min-h-screen px-4">
      <div
        className="pointer-events-none absolute inset-x-0 top-0 h-72 opacity-80"
        style={{
          background: 'radial-gradient(48rem 20rem at 50% -10%, color-mix(in srgb, var(--brand) 16%, transparent), transparent 72%)',
        }}
      />
      <Card level="elevated" className="w-full max-w-sm p-8 relative">
        <div className="flex flex-col items-center mb-6 text-center">
          <div className="brand-badge rounded-2xl px-4 py-3">
            <Wordmark size="lg" />
          </div>
          <p className="mt-4 max-w-xs text-sm text-text-secondary">
            Resume work, build project memory, and hand off AI sessions without losing context.
          </p>
        </div>
        <h1 className="sr-only">SessionFS</h1>
        <p className="text-text-secondary text-sm mb-6">
          {mode === 'login' ? 'Sign in with your API key' : 'Create a new account'}
        </p>

        {signupKey && (
          <div className="mb-4 p-3 bg-bg-tertiary rounded border border-[var(--accent)]/30 text-sm">
            <p className="text-[var(--accent)] font-medium mb-1">Account created</p>
            <p className="text-text-secondary mb-2">Save your API key — it won't be shown again:</p>
            <code className="block p-2 bg-bg-primary rounded text-sm break-all select-all text-text-primary">
              {signupKey}
            </code>
          </div>
        )}

        {error && (
          <div className="mb-4 p-3 bg-[var(--danger)]/10 border border-[var(--danger)]/30 rounded text-danger text-sm">
            {error}
          </div>
        )}

        {mode === 'login' ? (
          <form onSubmit={handleLogin}>
            <div className="mb-4">
              <Input
                type="url"
                value={baseUrl}
                onChange={(e) => { setBaseUrl(e.target.value); clearFieldError('baseUrl'); }}
                onBlur={() => validateLoginField('baseUrl')}
                title="Server URL"
                error={fieldErrors.baseUrl}
              />
            </div>
            <div className="mb-6">
              <Input
                type="password"
                value={apiKey}
                onChange={(e) => { setApiKey(e.target.value); clearFieldError('apiKey'); }}
                onBlur={() => { if (apiKey) validateLoginField('apiKey'); }}
                placeholder="sk_sfs_…"
                title="API Key"
                error={fieldErrors.apiKey}
                autoFocus
              />
            </div>
            <Button
              type="submit"
              variant="primary"
              size="md"
              loading={loading}
              disabled={!apiKey}
              className="w-full"
            >
              {loading ? 'Connecting…' : 'Sign In'}
            </Button>
            <p className="mt-4 text-center text-text-tertiary text-sm">
              No account?{' '}
              <button type="button" onClick={() => { setMode('signup'); setFieldErrors({}); }} className="text-[var(--accent)] hover:underline">
                Sign up
              </button>
            </p>
          </form>
        ) : (
          <form onSubmit={handleSignup}>
            <div className="mb-4">
              <Input
                type="url"
                value={baseUrl}
                onChange={(e) => { setBaseUrl(e.target.value); clearFieldError('baseUrl'); }}
                onBlur={() => validateSignupField('baseUrl')}
                title="Server URL"
                error={fieldErrors.baseUrl}
              />
            </div>
            <div className="mb-6">
              <Input
                type="email"
                value={email}
                onChange={(e) => { setEmail(e.target.value); clearFieldError('email'); }}
                onBlur={() => { if (email) validateSignupField('email'); }}
                placeholder="you@example.com"
                title="Email"
                error={fieldErrors.email}
                autoFocus
              />
            </div>
            <Button
              type="submit"
              variant="primary"
              size="md"
              loading={loading}
              disabled={!email}
              className="w-full"
            >
              {loading ? 'Creating…' : 'Create Account'}
            </Button>
            <p className="mt-4 text-center text-text-tertiary text-sm">
              Have an API key?{' '}
              <button type="button" onClick={() => { setMode('login'); setFieldErrors({}); }} className="text-[var(--accent)] hover:underline">
                Sign in
              </button>
            </p>
          </form>
        )}
      </Card>
    </div>
  );
}
