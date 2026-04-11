import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { signup, ApiError } from '../api/client';
import { loginSchema, signupSchema, fieldErrorsFromZod, type FieldErrors } from '../utils/validation';
import FieldError from '../components/FieldError';
import Wordmark from '../components/Wordmark';

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<'login' | 'signup'>('login');
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
      setApiKey(signupResult.raw_key);
      setMode('login');
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
      <div className="w-full max-w-sm p-8 bg-bg-secondary rounded-lg border border-border shadow-[var(--shadow-md)] relative">
        <div className="flex flex-col items-center mb-6 text-center">
          <div className="brand-badge rounded-2xl px-4 py-3">
            <Wordmark size="lg" />
          </div>
          <p className="mt-4 max-w-xs text-sm text-text-secondary">
            Resume work, build project memory, and hand off AI sessions without losing context.
          </p>
        </div>
        <h1 className="text-3xl font-bold tracking-tight text-text-primary mb-1 sr-only">SessionFS</h1>
        <p className="text-text-secondary text-sm mb-6">
          {mode === 'login' ? 'Sign in with your API key' : 'Create a new account'}
        </p>

        {signupKey && (
          <div className="mb-4 p-3 bg-bg-tertiary rounded border border-role-assistant/30 text-sm">
            <p className="text-role-assistant font-medium mb-1">Account created</p>
            <p className="text-text-secondary mb-2">Save your API key — it won't be shown again:</p>
            <code className="block p-2 bg-bg-primary rounded text-sm break-all select-all text-text-primary">
              {signupKey}
            </code>
          </div>
        )}

        {error && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
            {error}
          </div>
        )}

        {mode === 'login' ? (
          <form onSubmit={handleLogin}>
            <label className="block mb-4">
              <span className="text-[var(--text-secondary)] text-[14px] font-medium">Server URL</span>
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => { setBaseUrl(e.target.value); clearFieldError('baseUrl'); }}
                onBlur={() => validateLoginField('baseUrl')}
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
              />
              <FieldError message={fieldErrors.baseUrl} />
            </label>
            <label className="block mb-6">
              <span className="text-[var(--text-secondary)] text-[14px] font-medium">API Key</span>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => { setApiKey(e.target.value); clearFieldError('apiKey'); }}
                onBlur={() => { if (apiKey) validateLoginField('apiKey'); }}
                placeholder="sk_sfs_..."
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
                autoFocus
              />
              <FieldError message={fieldErrors.apiKey} />
            </label>
            <button
              type="submit"
              disabled={loading || !apiKey}
              className="w-full px-5 py-2.5 bg-[var(--brand)] text-white font-semibold rounded-lg text-sm hover:bg-[var(--brand-hover)] disabled:opacity-50 transition-colors"
            >
              {loading ? 'Connecting...' : 'Sign In'}
            </button>
            <p className="mt-4 text-center text-text-muted text-sm">
              No account?{' '}
              <button type="button" onClick={() => { setMode('signup'); setFieldErrors({}); }} className="text-accent hover:underline">
                Sign up
              </button>
            </p>
          </form>
        ) : (
          <form onSubmit={handleSignup}>
            <label className="block mb-4">
              <span className="text-[var(--text-secondary)] text-[14px] font-medium">Server URL</span>
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => { setBaseUrl(e.target.value); clearFieldError('baseUrl'); }}
                onBlur={() => validateSignupField('baseUrl')}
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
              />
              <FieldError message={fieldErrors.baseUrl} />
            </label>
            <label className="block mb-6">
              <span className="text-[var(--text-secondary)] text-[14px] font-medium">Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => { setEmail(e.target.value); clearFieldError('email'); }}
                onBlur={() => { if (email) validateSignupField('email'); }}
                placeholder="you@example.com"
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
                autoFocus
              />
              <FieldError message={fieldErrors.email} />
            </label>
            <button
              type="submit"
              disabled={loading || !email}
              className="w-full px-5 py-2.5 bg-[var(--brand)] text-white font-semibold rounded-lg text-sm hover:bg-[var(--brand-hover)] disabled:opacity-50 transition-colors"
            >
              {loading ? 'Creating...' : 'Create Account'}
            </button>
            <p className="mt-4 text-center text-text-muted text-sm">
              Have an API key?{' '}
              <button type="button" onClick={() => { setMode('login'); setFieldErrors({}); }} className="text-accent hover:underline">
                Sign in
              </button>
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
