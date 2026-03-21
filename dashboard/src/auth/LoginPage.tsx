import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { signup, ApiError } from '../api/client';

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [baseUrl, setBaseUrl] = useState(
    window.location.hostname === 'localhost'
      ? `${window.location.protocol}//${window.location.host}`
      : 'https://api.sessionfs.dev',
  );
  const [apiKey, setApiKey] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [signupKey, setSignupKey] = useState('');

  async function handleLogin(e: FormEvent) {
    e.preventDefault();
    setError('');
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
    setLoading(true);
    try {
      const result = await signup(baseUrl, email);
      setSignupKey(result.raw_key);
      setApiKey(result.raw_key);
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
    <div className="flex items-center justify-center min-h-screen">
      <div className="w-full max-w-sm p-8 bg-bg-secondary rounded-lg border border-border">
        <h1 className="text-xl font-semibold text-text-primary mb-1">SessionFS</h1>
        <p className="text-text-secondary text-sm mb-6">
          {mode === 'login' ? 'Sign in with your API key' : 'Create a new account'}
        </p>

        {signupKey && (
          <div className="mb-4 p-3 bg-bg-tertiary rounded border border-role-assistant/30 text-sm">
            <p className="text-role-assistant font-medium mb-1">Account created</p>
            <p className="text-text-secondary mb-2">Save your API key — it won't be shown again:</p>
            <code className="block p-2 bg-bg-primary rounded text-xs break-all select-all text-text-primary">
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
              <span className="text-text-secondary text-xs uppercase tracking-wide">Server URL</span>
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
              />
            </label>
            <label className="block mb-6">
              <span className="text-text-secondary text-xs uppercase tracking-wide">API Key</span>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk_sfs_..."
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
                autoFocus
              />
            </label>
            <button
              type="submit"
              disabled={loading || !apiKey}
              className="w-full py-2 bg-accent text-bg-primary font-medium rounded text-sm hover:bg-accent-hover disabled:opacity-50 transition-colors"
            >
              {loading ? 'Connecting...' : 'Sign In'}
            </button>
            <p className="mt-4 text-center text-text-muted text-xs">
              No account?{' '}
              <button type="button" onClick={() => setMode('signup')} className="text-accent hover:underline">
                Sign up
              </button>
            </p>
          </form>
        ) : (
          <form onSubmit={handleSignup}>
            <label className="block mb-4">
              <span className="text-text-secondary text-xs uppercase tracking-wide">Server URL</span>
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
              />
            </label>
            <label className="block mb-6">
              <span className="text-text-secondary text-xs uppercase tracking-wide">Email</span>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className="mt-1 block w-full px-3 py-2 bg-bg-primary border border-border rounded text-sm text-text-primary focus:outline-none focus:border-accent"
                autoFocus
              />
            </label>
            <button
              type="submit"
              disabled={loading || !email}
              className="w-full py-2 bg-accent text-bg-primary font-medium rounded text-sm hover:bg-accent-hover disabled:opacity-50 transition-colors"
            >
              {loading ? 'Creating...' : 'Create Account'}
            </button>
            <p className="mt-4 text-center text-text-muted text-xs">
              Have an API key?{' '}
              <button type="button" onClick={() => setMode('login')} className="text-accent hover:underline">
                Sign in
              </button>
            </p>
          </form>
        )}
      </div>
    </div>
  );
}
