import { Link } from 'react-router-dom';
import { Card, Button, Badge } from '../components/ui';
import type { RepresentedPersona } from '../api/client';
import { displayTier } from '../utils/tier';

export default function AccountSection({ profile, logout }: { profile: any; logout: () => void }) {
  return (
    <div className="space-y-5">
      {profile && (
        <Card level="elevated" className="p-5">
          <h2 className="text-lg font-semibold text-text-primary mb-4">Account</h2>
          <div className="space-y-3">
            <div>
              <label className="text-sm text-text-tertiary block mb-1">Email</label>
              <div className="text-sm text-text-primary">{profile.email}</div>
            </div>
            <div className="flex gap-4 text-sm text-text-secondary">
              <span>Tier: <span className="text-text-primary font-medium capitalize" data-testid="account-tier">{displayTier(profile)}</span></span>
              <span>Verified: {profile.email_verified
                ? <span className="text-success">yes</span>
                : <span className="text-warning">no</span>
              }</span>
            </div>
          </div>
        </Card>
      )}

      {/* GitHub #92 — the org personas that represent this user */}
      {profile?.represented_personas?.length > 0 && (
        <Card level="elevated" className="p-5">
          <h2 className="text-lg font-semibold text-text-primary mb-3">Represented personas</h2>
          <p className="text-sm text-text-tertiary mb-3">
            You represent these org personas. Edit them on the organization page.
          </p>
          <div className="flex flex-wrap gap-2">
            {profile.represented_personas.map((p: RepresentedPersona) => (
              <Link
                key={`${p.org_id}:${p.persona_id}`}
                to={`/settings/organization?tab=personas&org=${p.org_id}`}
                title={`${p.role} · ${p.org_name}`}
                className="rounded-full outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
              >
                <Badge variant="info" label={p.name} size="md" />
              </Link>
            ))}
          </div>
        </Card>
      )}

      {/* Capture Health card — only visible when at least one watcher is degraded */}
      <CaptureHealthCard captureHealth={profile?.capture_health} />

      {/* Last Sync card */}
      {profile?.last_client_version && (
        <Card level="elevated" className="p-5">
          <div className="flex items-center gap-2 mb-4">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="23 4 23 10 17 10" />
              <polyline points="1 20 1 14 7 14" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
            <h2 className="text-lg font-semibold text-text-primary">Last Sync</h2>
            {profile.latest_version && profile.last_client_version === profile.latest_version && (
              <span className="ml-auto px-2.5 py-0.5 bg-success/10 text-success rounded-full text-xs font-medium">
                Up to date
              </span>
            )}
            {profile.latest_version && profile.last_client_version !== profile.latest_version && (
              <span className="ml-auto px-2.5 py-0.5 bg-warning/10 text-warning rounded-full text-xs font-medium">
                Update available
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-sm text-text-tertiary block mb-0.5">Package version</span>
              <span className="text-text-primary font-mono">{profile.last_client_version}</span>
            </div>
            <div>
              <span className="text-sm text-text-tertiary block mb-0.5">Platform</span>
              <span className="text-text-primary">{profile.last_client_platform || '-'}</span>
            </div>
            <div>
              <span className="text-sm text-text-tertiary block mb-0.5">Device</span>
              <span className="text-text-primary font-mono">{profile.last_client_device || '-'}</span>
            </div>
            <div>
              <span className="text-sm text-text-tertiary block mb-0.5">Last synced</span>
              <span className="text-text-primary">
                {profile.last_sync_at ? new Date(profile.last_sync_at).toLocaleString() : '-'}
              </span>
            </div>
          </div>
          {profile.latest_version && profile.last_client_version !== profile.latest_version && (
            <div className="mt-4 p-3 bg-warning/5 border border-warning/20 rounded-lg text-sm">
              <p className="text-warning font-medium">New version available</p>
              <p className="text-text-tertiary mt-1">
                Run <code className="bg-surface px-1.5 py-0.5 rounded text-text-secondary">pip install --upgrade sessionfs</code> to update from {profile.last_client_version} to {profile.latest_version}.
              </p>
            </div>
          )}
        </Card>
      )}

      <Button variant="danger" onClick={logout}>
        Logout
      </Button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Capture Health Card — only visible when a watcher is degraded     */
/* ------------------------------------------------------------------ */

interface WatcherHealth {
  name: string;
  health: string;
  degraded_since: string | null;
  last_error: string | null;
  sessions_tracked: number;
}

function CaptureHealthCard({ captureHealth }: { captureHealth: WatcherHealth[] | null }) {
  if (!captureHealth || captureHealth.length === 0) return null;

  const degraded = captureHealth.filter((w) => w.health === 'degraded');
  if (degraded.length === 0) return null; // healthy = quiet

  const TOOL_LABELS: Record<string, string> = {
    'claude-code': 'Claude Code',
    'codex': 'Codex',
    'gemini-cli': 'Gemini CLI',
    'cursor': 'Cursor',
    'cline': 'Cline',
    'copilot-cli': 'Copilot CLI',
    'kilo-code': 'Kilo Code',
    'amp': 'Amp',
    'roo-code': 'Roo Code',
    'antigravity': 'Antigravity',
  };

  return (
    <Card level="elevated" className="p-5 border-warning/30">
      <div className="flex items-center gap-2 mb-3">
        <span className="inline-block w-2.5 h-2.5 rounded-full bg-warning" />
        <h2 className="text-lg font-semibold text-text-primary">
          Capture Degraded
        </h2>
        <span className="px-2 py-0.5 bg-warning/10 text-warning rounded-full text-xs font-medium">
          {degraded.length} tool{degraded.length > 1 ? 's' : ''}
        </span>
      </div>

      <div className="space-y-3">
        {degraded.map((w) => (
          <div
            key={w.name}
            className="p-3 bg-surface border border-border rounded-lg"
          >
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm font-medium text-text-primary">
                {TOOL_LABELS[w.name] || w.name}
              </span>
              <span className="text-xs text-text-tertiary font-mono">
                ({w.sessions_tracked} sessions)
              </span>
            </div>
            {w.degraded_since && (
              <div className="text-xs text-text-tertiary mb-1">
                Failing since{' '}
                {new Date(w.degraded_since).toLocaleString()}
              </div>
            )}
            {w.last_error && (
              <div className="text-xs text-danger font-mono bg-danger/5 border border-danger/10 rounded px-2 py-1 mt-1 max-h-10 overflow-hidden">
                {w.last_error.length > 200
                  ? w.last_error.slice(0, 200) + '…'
                  : w.last_error}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
