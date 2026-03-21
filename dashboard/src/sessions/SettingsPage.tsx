import { useAuth } from '../auth/AuthContext';
import CopyButton from '../components/CopyButton';

export default function SettingsPage() {
  const { auth, logout } = useAuth();

  if (!auth) return null;

  const maskedKey = auth.apiKey.slice(0, 12) + '\u2026' + auth.apiKey.slice(-4);

  return (
    <div className="max-w-lg mx-auto px-4 py-8">
      <h1 className="text-lg font-medium text-text-primary mb-6">Settings</h1>

      <div className="bg-bg-secondary border border-border rounded-lg p-4 mb-4">
        <h2 className="text-xs uppercase tracking-wider text-text-muted mb-3">Connection</h2>

        <div className="mb-3">
          <label className="text-xs text-text-muted block mb-1">Server URL</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1">
              {auth.baseUrl}
            </code>
            <CopyButton text={auth.baseUrl} />
          </div>
        </div>

        <div className="mb-3">
          <label className="text-xs text-text-muted block mb-1">API Key</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-bg-primary px-2 py-1 rounded flex-1 font-mono">
              {maskedKey}
            </code>
            <CopyButton text={auth.apiKey} />
          </div>
        </div>
      </div>

      <button
        onClick={logout}
        className="px-4 py-2 text-sm border border-red-500/30 text-red-400 rounded hover:bg-red-500/10 transition-colors"
      >
        Logout
      </button>
    </div>
  );
}
