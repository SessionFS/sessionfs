import { useAuth } from '../auth/AuthContext';
import CopyButton from '../components/CopyButton';
import { Card } from '../components/ui';
import { GitHubIntegrationSection } from './GitHubIntegrationSection';

/** Integrations: the API connection details plus the GitHub App link. */
export default function IntegrationsSection() {
  const { auth } = useAuth();
  if (!auth) return null;

  const maskedKey = auth.apiKey.slice(0, 12) + '…' + auth.apiKey.slice(-4);

  return (
    <div className="space-y-5">
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-4">Connection</h2>

        <div className="mb-4">
          <label className="text-sm text-text-tertiary block mb-1">Server URL</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-surface border border-border px-3 py-2 rounded-lg flex-1">
              {auth.baseUrl}
            </code>
            <CopyButton text={auth.baseUrl} />
          </div>
        </div>

        <div>
          <label className="text-sm text-text-tertiary block mb-1">API Key</label>
          <div className="flex items-center gap-2">
            <code className="text-sm text-text-secondary bg-surface border border-border px-3 py-2 rounded-lg flex-1 font-mono">
              {maskedKey}
            </code>
            <CopyButton text={auth.apiKey} />
          </div>
        </div>
      </Card>

      <GitHubIntegrationSection />
    </div>
  );
}
